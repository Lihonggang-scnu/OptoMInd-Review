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
             "uncertain": "what is still unknown, or empty",
             "evidence_source": "abstract|own_passage|citing_sentence",
             "use": "explicit intended use, required for background_or_method"}]}
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


def validate_routing_rows(packets, rows, facets=()):
    """One input/one output. Routing never constitutes read-confirmed evidence."""
    allowed = {str(p["paper_id"]): p for p in packets}
    grouped = {pid: [] for pid in allowed}
    rejected = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or str(row.get("paper_id")) not in allowed:
            rejected.append("unknown_paper_id")
            continue
        grouped[str(row["paper_id"])].append(row)
    result = {}
    for pid, packet in allowed.items():
        candidates = grouped[pid]
        row = candidates[0] if len(candidates) == 1 else {}
        errors = [] if len(candidates) == 1 else ["missing_or_duplicate_output"]
        label = str(row.get("label") or "")
        if label not in LABELS:
            errors.append("invalid_label")
        quote = str(row.get("evidence") or "").strip().strip('"“”')
        source = str(row.get("evidence_source") or "")
        material = packet.get("material") or {}
        # A unique exact source may be recovered for older router responses.
        matches = [k for k, v in material.items() if k != "title" and quote and quote in v]
        if not source and len(matches) == 1:
            source = matches[0]
        if not quote or source not in matches:
            errors.append("unverified_quote")
        sub_question = str(row.get("sub_question") or "")
        facet_values = {str(f.get(k) or "") for f in facets for k in ("id", "ask")} - {""}
        if facets and label != "clearly_irrelevant" and sub_question not in facet_values:
            errors.append("unknown_or_missing_sub_question")
        use = str(row.get("use") or "").strip()
        if label == "background_or_method" and not use:
            errors.append("method_use_required")
        if source == "citing_sentence" and label == "directly_relevant":
            errors.append("citation_is_not_own_evidence")
        result[pid] = {"label": "needs_verification" if errors else label,
            "label_as_returned": label, "sub_question": str(row.get("sub_question") or ""),
            "evidence": quote if not errors else "", "evidence_source": source,
            "uncertain": str(row.get("uncertain") or ""), "use": use,
            "validation_errors": errors, "read_confirmed": False,
            "routing_validated": not errors}
    return result, rejected


def read_candidates(question: str, facets: Sequence[Mapping[str, Any]], packets: Sequence[Mapping],
                    config: Mapping[str, Any], *, call=None, budget: Mapping[str, Any] | None = None,
                    progress=None) -> dict[str, Any]:
    """Bounded routing with a shared, pre-dispatch model-price reservation ledger.

    Injected calls are single-attempt transports; unknown usage freezes their
    reservation. The production client reserves at each actual HTTP attempt.
    """
    from .module4.runtime import (GlobalBudgetLedger, QwenDirectClient, invoke_client,
        estimated_cost_cny, QwenTransportError)
    cfg = dict(config.get("reading") or {})
    limits = dict(budget or {})
    cap = float(limits.get("cost_cap_cny", cfg.get("cost_cap_cny", 5.0)))
    count = int(limits.get("max_papers", cfg.get("max_papers", 300)))
    model = str(cfg.get("model", "qwen3.7-flash"))
    maximum = int(cfg.get("max_tokens", 4000))
    batch_size = max(1, min(int(cfg.get("batch_default", 10)), int(cfg.get("batch_max", 20))))
    started = time.time()
    ledger = None
    if cap > 0:
        ledger = limits.get("ledger") or GlobalBudgetLedger(limit_cny=cap, path=limits.get("ledger_path"))
    injected = call is not None
    if not injected and ledger is not None:
        client = QwenDirectClient(model=model, key_file=cfg.get("key_file"), max_retries=0,
            max_output_tokens=maximum, thinking=False, json_mode=True, budget_ledger=ledger)
        def call(messages, model_tier):
            return invoke_client(client, messages, model=model, max_output_tokens=maximum,
                                 thinking=False, call_id="reading-router")
    accounting = {"model_tier": model, "model_names": [], "calls": 0, "retries": 0,
        "input_tokens": 0, "output_tokens": 0, "estimated_cost_cny": 0.0,
        "cost_cap_cny": cap, "max_papers": count, "papers_read": 0,
        "papers_skipped_for_material": 0, "cost_is_estimate": True, "stop_reason": "",
        "reservation_enforced": True, "rejected_outputs": [], "read_confirmed": 0}
    queue, judgements, seen = [], {}, set()
    for p in packets:
        pid = str(p.get("paper_id") or "")
        if not pid or pid in seen:
            raise ValueError("routing_input_missing_or_duplicate_paper_id")
        seen.add(pid)
        judgements[pid] = {"label": "needs_verification", "label_as_returned": "",
            "sub_question": "", "evidence": "", "uncertain": "not_routed", "use": "",
            "routing_validated": False, "read_confirmed": False, "validation_errors": ["not_routed"]}
        if len([v for v in (p.get("material") or {}).values() if v]) < MIN_MATERIAL_SOURCES:
            accounting["papers_skipped_for_material"] += 1
            continue
        if len(queue) < max(0, count):
            queue.append(p)
    for index in range(0, len(queue), batch_size):
        batch = queue[index:index + batch_size]
        if ledger is None:
            accounting["stop_reason"] = "cost_cap_reached"
            break
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content":
            json.dumps({"review_question": question, "sub_questions": list(facets), "candidates": batch}, ensure_ascii=False)}]
        reservation = None
        try:
            # Honor the run's own cap even when the shared ledger has a larger
            # project ceiling. The transport also reserves its exact wire bound.
            bound = estimated_cost_cny({"input_tokens": len(json.dumps(messages).encode()) + 512,
                "output_tokens": maximum}, model=model, conservative=True)
            if accounting["estimated_cost_cny"] + bound > cap:
                accounting["stop_reason"] = "cost_cap_reached"
                break
            if injected:
                reservation = ledger.reserve(bound, "reading-router:%d" % index)
            accounting["calls"] += 1
            raw = call(messages, model)
            usage = dict(raw.get("usage") or raw.get("_llm_usage") or {})
            returned_model = str(raw.get("returned_model") or usage.get("model_name") or model)
            cost = estimated_cost_cny(usage, model=returned_model)
            if reservation:
                ledger.settle(reservation["reservation_id"], cost, telemetry={"usage": usage, "returned_model": returned_model})
                reservation = None
            accounting["estimated_cost_cny"] += cost
            accounting["input_tokens"] += int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
            accounting["output_tokens"] += int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
            if returned_model not in accounting["model_names"]:
                accounting["model_names"].append(returned_model)
            content = raw.get("content")
            parsed = content if isinstance(content, Mapping) else _extract_json(content)
            validated, rejected = validate_routing_rows(batch, parsed.get("papers"), facets)
            judgements.update(validated)
            accounting["rejected_outputs"].extend(rejected)
            accounting["papers_read"] += len(batch)
        except (OSError, ValueError, QwenTransportError) as exc:
            if reservation:
                ledger.settle(reservation["reservation_id"], None, uncertain=True)
            accounting["stop_reason"] = "cost_cap_reached" if "budget" in str(exc) else "unverified_response_or_transport"
            accounting["error"] = type(exc).__name__
            break
        if progress:
            progress(index + len(batch), len(queue), accounting)
    if not accounting["stop_reason"]:
        accounting["stop_reason"] = "count_cap_reached" if len(seen) > count else "read_all_candidates"
    return {"judgements": judgements, "accounting": accounting, "queued": len(queue),
            "elapsed_seconds": round(time.time() - started, 1)}


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
    corpus["_bundle_base"] = str(root.resolve())
    edge_path = SS._artifact_path(corpus, root, "edges", "GRAPH_EDGES.jsonl")
    raw_edges = SS._read_bound_edges(corpus, edge_path, root)
    corpus, raw_edges, _ = SS.canonical_view(corpus, raw_edges, meta)
    edge_set = SS.build_layers(raw_edges, corpus, SS.facet_features(corpus, config), config, identity_meta=meta)
    own, mentions = SS._material_text_by_paper(root, corpus, edge_set)
    # Top-up runs are idempotent: a paper already judged keeps its judgement, and
    # the budget goes to the papers the re-selection brought in.
    previous = {}
    previous_path = root / "READING.json"
    if previous_path.exists():
        try:
            previous = {pid: row for pid, row in (json.loads(previous_path.read_text(encoding="utf-8")).get("judgements") or {}).items()
                        if row.get("routing_validated") is True}
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
                                      "cost_cap_cny": args.cost_cap_cny,
                                      "ledger_path": root / "READING_BUDGET.sqlite"},
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
