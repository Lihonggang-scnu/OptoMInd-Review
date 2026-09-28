# Bounded pre-reading routing: send the candidates that matter to a model, once,
# with the material we actually hold, and record exactly what it cost.
#
# This is a ROUTING judgement, not a scientific verdict.  It answers four things
# per paper: which user sub-question it relates to, why the material in hand
# supports that, which of four classes it falls in, and what is still unknown.
# It never says whether a paper proves anything: that needs the full text and
# belongs to the deep-analysis stage.

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LABELS = ("directly_relevant", "background_or_method", "needs_verification",
          "clearly_irrelevant")

#: Papers whose only material is a title are NOT sent to the model: a title is not
#: enough to judge on, and asking anyway invites a confident guess.
MIN_MATERIAL_SOURCES = 2

SYSTEM_PROMPT = """You are a pre-reading router for a literature review.

You receive one review question, its sub-questions, and a batch of candidate
papers. For each candidate you get the material we hold: its title, its abstract
if we have one, a passage of its own text if a search returned one, and a sentence
in which another paper cites it. The material is marked per source. The abstract
and the passage are the paper speaking about itself; the citing sentence is
someone else talking about it and is weaker evidence.

For EACH candidate decide exactly one routing label:
  directly_relevant   -- the material shows this paper studies the question or one
                         of its sub-questions
  background_or_method -- it is about the same setting, or it supplies a method, a
                         measurement or a tool, but it does not study the question
  needs_verification  -- the material in hand is not enough to place it; say what
                         would settle it
  clearly_irrelevant  -- the material shows it studies something else ENTIRELY:
                         a different object, a different question, a different field
                         of application. Use this label only for that.

  Clearly_irrelevant is deliberately narrow, because it removes the paper from the
  reading queue. These all belong in background_or_method, NOT here:
    - the paper argues AGAINST the phenomenon the question is about: that is
      counter-evidence, and counter-evidence is material for the review;
    - it studies the same phenomenon from another side (its rate, its detection, its
      age profile, its measurement) without addressing the question's relation;
    - it is a measurement or methodology paper that the question's quantities would
      be measured with;
    - it is an adjacent technology route or a neighbouring population: a sibling
      route is background by default, and becomes clearly_irrelevant only when the
      question itself excludes it.

Rules:
- Judge only from the material given. Do not use outside knowledge about the paper
  and do not assume content that is not in the material.
- An abstract existing is not the same as the abstract being about the question.
- If the material names a different population, region, material system or task
  than the question, say so; that is a scope difference, not relevance.
- Being cited widely, or being a review, is a USAGE observation, not relevance.
- When the material is thin, prefer needs_verification over a confident label.

Return exactly one JSON object, no prose and no markdown fence:
{"papers": [{"paper_id": "...", "label": "one of the four",
             "sub_question": "which sub-question it relates to, or empty",
             "evidence": "the words in the material that support the label, quoted",
             "uncertain": "what is still unknown, or empty"}]}
One entry per candidate, using the exact paper_id given."""


def _usage_cost(usage: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    inp = float(usage.get("input_tokens") or 0)
    out = float(usage.get("output_tokens") or 0)
    return (inp / 1000.0) * float(prices.get("input_per_1k", 0.0)) + (
        out / 1000.0) * float(prices.get("output_per_1k", 0.0))


def candidate_order(domain: Mapping[str, Any], pools: Mapping[str, Any],
                    portfolio: Mapping[str, Any], *, limit: int = 300) -> list[dict[str, Any]]:
    """Who gets read first, and why.  Order matters more than the cap."""

    priority = list(portfolio.get("priority_portfolio") or [])
    held = [row.get("paper_id") for row in portfolio.get("pending_verification_items") or []]
    deep = list(portfolio.get("deep_analysis_corpus") or [])
    eligible = list(pools.get("eligible_pool") or [])
    core = set(portfolio.get("core_ids") or [])
    seen: list[dict[str, Any]] = []
    used = set()

    def add(paper: str, reason: str) -> None:
        paper = str(paper or "")
        if not paper or paper in used or len(seen) >= limit:
            return
        used.add(paper)
        seen.append({"paper_id": paper, "reason": reason})

    for paper in priority:
        add(paper, "offered_for_priority")
    for paper in held:
        add(paper, "held_for_verification")
    for paper in deep:
        add(paper, "in_deep_corpus")
    for paper in eligible:
        add(paper, "boundary_with_potential" if paper not in core else "investigation_pool")
    return seen


def build_packet(paper: str, meta: Mapping[str, Any], own: Mapping[str, str],
                 mentions: Mapping[str, str], reason: str,
                 caps: Mapping[str, int]) -> dict[str, Any]:
    info = meta.get(paper) or {}
    material = {
        "title": str(info.get("title") or "").strip(),
        "abstract": str(info.get("abstract") or "").strip()[:int(caps.get("abstract", 1600))],
        "own_passage": str(own.get(paper) or "").strip()[:int(caps.get("passage", 900))],
        "citing_sentence": str(mentions.get(paper) or "").strip()[:int(caps.get("citing", 400))],
    }
    present = [name for name, value in material.items() if value]
    return {"paper_id": paper, "why_selected": reason,
            "year": info.get("year"), "venue": info.get("venue") or "",
            "material": {k: v for k, v in material.items() if v},
            "material_sources": present}


def _extract_json(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text).strip().rstrip("`").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reading response")
    return json.loads(text[start:end + 1])


def read_candidates(question: str, facets: Sequence[Mapping[str, Any]], packets: Sequence[Mapping],
                    config: Mapping[str, Any], *, call=None, budget: Mapping[str, Any] | None = None,
                    progress=None) -> dict[str, Any]:
    """Send the packets in batches, with a hard cost and count ceiling."""

    cfg = dict((config.get("reading") or {}))
    prices = {"input_per_1k": float(cfg.get("price_cny_per_1k_input", 0.0008)),
              "output_per_1k": float(cfg.get("price_cny_per_1k_output", 0.002))}
    cap_cny = float((budget or {}).get("cost_cap_cny", cfg.get("cost_cap_cny", 5.0)))
    max_papers = int((budget or {}).get("max_papers", cfg.get("max_papers", 300)))
    batch_default = int(cfg.get("batch_default", 10))
    batch_max = int(cfg.get("batch_max", 20))

    if call is None:
        os.environ.setdefault("QWEN_BYPASS_PROXY", "1")
        from llm.qwen_chat_client import call_qwen_chat

        def call(messages, model_tier):
            return call_qwen_chat("ReadingRouter", messages, model_tier=model_tier,
                                  max_retries=1, temperature=0.0, force_mock=False,
                                  max_tokens=int(cfg.get("max_tokens", 4000)),
                                  response_format={"type": "json_object"})

    sub_questions = [{"id": str(f.get("id") or ""), "ask": str(f.get("ask") or "")}
                     for f in facets or ()]
    judgements: dict[str, dict[str, Any]] = {}
    accounting = {
        "model_tier": str(cfg.get("model_tier", "c_model")),
        "model_names": [], "calls": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0,
        "estimated_cost_cny": 0.0, "cost_cap_cny": cap_cny, "max_papers": max_papers,
        "papers_read": 0, "papers_skipped_for_material": 0, "cost_is_estimate": True,
        "stop_reason": "",
    }
    queue: list[Mapping] = []
    for packet in packets:
        if len(packet.get("material_sources") or []) < MIN_MATERIAL_SOURCES:
            accounting["papers_skipped_for_material"] += 1
            continue
        if accounting["papers_read"] + len(queue) >= max_papers:
            break
        queue.append(packet)

    index = 0
    while index < len(queue):
        if accounting["estimated_cost_cny"] >= cap_cny:
            accounting["stop_reason"] = "cost_cap_reached"
            break
        remaining_cap = max_papers - accounting["papers_read"]
        size = min(batch_default, batch_max, remaining_cap)
        batch = list(queue[index:index + size])
        if not batch:
            break
        payload = {"review_question": question, "sub_questions": sub_questions,
                   "candidates": batch}
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        started = time.time()
        result = call(messages, accounting["model_tier"])
        usage = dict((result or {}).get("_llm_usage") or {})
        accounting["calls"] += 1
        accounting["retries"] += int(usage.get("retry_count") or 0)
        accounting["input_tokens"] += int(usage.get("input_tokens") or 0)
        accounting["output_tokens"] += int(usage.get("output_tokens") or 0)
        name = str(usage.get("model_name") or "")
        if name and name not in accounting["model_names"]:
            accounting["model_names"].append(name)
        accounting["estimated_cost_cny"] = round(
            accounting["estimated_cost_cny"] + _usage_cost(usage, prices), 6)
        if (result or {}).get("fallback_used"):
            accounting["stop_reason"] = "transport_failure"
            break
        try:
            parsed = _extract_json((result or {}).get("content") or "")
        except ValueError:
            accounting["stop_reason"] = "unparseable_response"
            index += len(batch)
            continue
        for row in parsed.get("papers") or ():
            pid = str((row or {}).get("paper_id") or "")
            if not pid:
                continue
            label = str((row or {}).get("label") or "").strip()
            judgements[pid] = {
                "label": label if label in LABELS else "needs_verification",
                "label_as_returned": label,
                "sub_question": str((row or {}).get("sub_question") or "")[:300],
                "evidence": str((row or {}).get("evidence") or "")[:600],
                "uncertain": str((row or {}).get("uncertain") or "")[:400],
                "use": str((row or {}).get("use") or "")[:200],
            }
        accounting["papers_read"] = len(judgements)
        index += len(batch)
        if progress:
            progress(index, len(queue), accounting)
    if not accounting["stop_reason"]:
        accounting["stop_reason"] = ("read_all_candidates" if index >= len(queue)
                                     else "count_cap_reached")
    return {"judgements": judgements, "accounting": accounting,
            "queued": len(queue), "elapsed_seconds": round(time.time() - started, 1)
            if queue else 0.0}


def main() -> int:
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True, help="a CROSSDOMAIN_REWORK run directory")
    parser.add_argument("--max-papers", type=int, default=300)
    parser.add_argument("--cost-cap-cny", type=float, default=5.0)
    args = parser.parse_args()

    from optomind_research.runtime.upgrade3 import scholarly_skeleton as SS

    root = Path(args.run)
    corpus = json.loads((root / "CANDIDATE_CORPUS.json").read_text(encoding="utf-8"))
    sk = json.loads((root / "SCHOLARLY_SKELETON.json").read_text(encoding="utf-8"))
    port = json.loads((root / "INVESTIGATION_PORTFOLIO.json").read_text(encoding="utf-8"))
    meta = json.loads((root / "NODE_META.json").read_text(encoding="utf-8"))
    SS._merge_abstract_fill(root, meta)
    config = SS.load_config()
    edge_set = SS.EdgeSet()
    own, mentions = SS._material_text_by_paper(root, corpus, edge_set)
    # Top-up runs are idempotent: a paper already judged keeps its judgement, and
    # the budget goes to the papers the re-selection brought in.
    previous = {}
    previous_path = root / "READING.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8")).get("judgements") or {}
        except ValueError:
            previous = {}
    order = [row for row in candidate_order(sk.get("domain_relevance") or {},
                                            sk.get("pools") or {}, port,
                                            limit=args.max_papers + len(previous))
             if row["paper_id"] not in previous]
    packets = [build_packet(row["paper_id"], meta, own, mentions, row["reason"],
                            {"abstract": 1600, "passage": 900, "citing": 400})
               for row in order]
    plan = json.loads((root / "PLAN.json").read_text(encoding="utf-8"))
    question = str((plan.get("plan") or {}).get("question_en") or "")
    facets = (plan.get("plan") or {}).get("facets") or []
    outcome = read_candidates(question, facets, packets, config,
                              budget={"max_papers": args.max_papers,
                                      "cost_cap_cny": args.cost_cap_cny},
                              progress=lambda i, n, a: print(
                                  "  %d/%d read, %.4f CNY (estimate)" % (
                                      i, n, a["estimated_cost_cny"]), flush=True))
    merged = dict(previous)
    merged.update(outcome["judgements"])
    outcome["judgements"] = merged
    # Cumulative bookkeeping: a top-up run reports what THIS run spent and what the
    # topic has spent in total, so the ceiling is auditable across runs.
    prior_calls = prior_cost = 0
    if previous_path.exists():
        try:
            prior = json.loads(previous_path.read_text(encoding="utf-8")).get("accounting") or {}
            prior_calls = int(prior.get("cumulative_calls", prior.get("calls", 0)) or 0)
            prior_cost = float(prior.get("cumulative_cost_cny",
                                         prior.get("estimated_cost_cny", 0.0)) or 0.0)
        except ValueError:
            prior_calls = prior_cost = 0
    outcome["accounting"]["previously_judged"] = len(previous)
    outcome["accounting"]["papers_judged_total"] = len(merged)
    outcome["accounting"]["cumulative_calls"] = prior_calls + outcome["accounting"]["calls"]
    outcome["accounting"]["cumulative_cost_cny"] = round(
        prior_cost + outcome["accounting"]["estimated_cost_cny"], 6)
    (root / "READING.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print(json.dumps(outcome["accounting"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
