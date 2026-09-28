"""Upgrade-3 candidate science-domain admission (ticket 010).

Single decision authority replacing the three donor licence-granting sites
(topic-scoped ``evaluate_s2_paper`` direct grants, coverage
``_candidate_alignment_guard`` admissions, and the semantic policy's
DIRECT_GRANTING_STATES).  Rules:

- lexical/vector/current-run provenance only ever yields a *candidate*; a
  writable science verdict requires a judge receipt that quotes the paper's
  research-object span and task span from the supplied text;
- ``uncertain`` and metadata-only candidates are never writable;
- exact reuse only for (document_id, contract_hash, policy_hash) triples
  already reviewed; everything else is judged fresh;
- correct cross-domain method background may only enter the related-background
  lane, never a writable fact lane;
- all decisions are appended to SCOPE_DECISIONS.jsonl with envelope, criterion
  results and the judge receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

VERDICTS = {"reviewed_direct", "adjacent", "background", "rejected", "uncertain"}
WRITABLE = {"reviewed_direct"}
ALLOWED_USES = {
    "reviewed_direct": ["factual_support"],
    "adjacent": ["contextual_support"],
    "background": ["background_only"],
    "rejected": [],
    "uncertain": [],
}
DECISIONS_SCHEMA = "optomind.upgrade3.scope_decision.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ScopeDecisionLog:
    """Append-only SCOPE_DECISIONS.jsonl + (doc, contract, policy) reuse cache."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._index: Dict[str, Dict[str, Any]] = {}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        dec = json.loads(line)
                    except ValueError:
                        continue
                    self._index[dec.get("reuse_key")] = dec
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def reuse_key(self, document_id: str, contract_hash: str, policy_hash: str) -> str:
        return _sha("|".join((document_id, contract_hash, policy_hash)))

    def find_reusable(self, document_id: str, contract_hash: str,
                      policy_hash: str) -> Optional[Dict[str, Any]]:
        return self._index.get(self.reuse_key(document_id, contract_hash, policy_hash))

    def append(self, decision: Dict[str, Any]) -> None:
        with self._lock:
            self._index[decision["reuse_key"]] = decision
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())


def prescreen(paper: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic metadata gate: pass = candidate only, never a licence."""
    title = _norm(paper.get("title"))
    text = _norm(paper.get("text") or "")
    object_tokens: List[List[str]] = []
    for phrase in contract.get("object_phrases") or []:
        toks = [t for t in re.split(r"[^a-z0-9]+", phrase.lower()) if len(t) > 2]
        if toks:
            object_tokens.append(toks)
    hits = 0
    for toks in object_tokens:
        joined = " " + " ".join(toks) + " "
        if joined in f" {title} " or joined in f" {text} ":
            hits += 1
        elif sum(1 for t in toks if f" {t} " in f" {title} {text} ") >= max(1, len(toks) - 1):
            hits += 1
    exclusion_hit = any(
        _norm(ex) and _norm(ex).split()[0] in f" {title} "
        for ex in (contract.get("exclusions") or []))
    criteria = {
        "object_phrase_hits": hits,
        "object_phrases_total": len(object_tokens),
        "exclusion_hit": exclusion_hit,
        "has_text": bool((paper.get("text") or "").strip()),
    }
    if exclusion_hit:
        verdict = "rejected"
    elif hits == 0 and not (paper.get("text") or "").strip():
        verdict = "uncertain"  # metadata-only, nothing to judge
    else:
        verdict = "candidate"
    return {"verdict": verdict, "criteria": criteria}


def _relevant_window(text: str, contract: Dict[str, Any],
                     window: int = 2400) -> Tuple[str, Dict[str, int]]:
    """Deterministic excerpt: start at the first object-phrase hit so the judge
    sees the object-relevant passage, not whatever the first page opens with."""
    n_text = _norm(text)
    starts: List[int] = []
    for phrase in contract.get("object_phrases") or []:
        toks = [t for t in re.split(r"[^a-z0-9]+", phrase.lower()) if len(t) > 2]
        if not toks:
            continue
        m = re.search(r"" + r"[ -]{0,3}".join(map(re.escape, toks)) + r"", n_text)
        if m:
            starts.append(m.start())
    if not starts:
        return n_text[:window], {"window_start": 0, "hit_start": -1}
    hit = min(starts)
    start = max(0, hit - 400)
    return n_text[start:start + window], {"window_start": start, "hit_start": hit}


def build_judge_prompt(contract: Dict[str, Any], paper: Dict[str, Any],
                       max_chars: int = 2600) -> Dict[str, str]:
    text, win = _relevant_window((paper.get("text") or "").strip(), contract,
                                 window=max_chars)
    cut = text
    system = (
        "You are a strict scientific-domain adjudicator. Decide whether the paper "
        "belongs to the review's research object. You must QUOTE the exact short "
        "spans (verbatim substrings of the supplied text) that state the paper's "
        "research object and task. Verdicts: reviewed_direct (direct evidence for "
        "the object), adjacent (same field, different object), background (useful "
        "general context), rejected (wrong domain), uncertain (cannot determine). "
        "Hybrid optical+digital diffractive systems and diffractive optics design "
        "for the object's tasks BELONG to the object. The paper TITLE is "
        "authoritative for the research object: when the title states the object, "
        "an excerpt discussing an implementation detail (e.g. a digital reconstruction "
        "network) does not make the paper merely adjacent. Quote spans verbatim "
        "(copy characters exactly). JSON only.")
    user = {
        "contract_object_phrases": contract.get("object_phrases") or [],
        "contract_target_system": contract.get("target_system", ""),
        "contract_task": contract.get("task", []),
        "paper_title": paper.get("title"),
        "paper_text": cut,
        "output_schema": {"research_object_span": "verbatim substring",
                          "task_span": "verbatim substring",
                          "verdict": list(VERDICTS),
                          "allowed_uses": ["factual_support", "contextual_support",
                                           "background_only", "none"],
                          "reason": "short"},
    }
    return {"system_prompt": system, "user_payload": user}


_UNI_MAP = str.maketrans({chr(0x2010): '-', chr(0x2011): '-', chr(0x2012): '-',
                          chr(0x2013): '-', chr(0x2014): '-', chr(0x2212): '-',
                          chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"',
                          chr(0x201D): '"', chr(0x00A0): ' '})


def _span_in_text(span: str, text: str) -> bool:
    """Verbatim check under unicode normalisation (NFKC + dash/quote folding):
    the model must quote the text, but hyphen/quote codepoint variants must not
    fail an otherwise verbatim quote."""
    import unicodedata
    if not span or not text:
        return False
    n_span = unicodedata.normalize("NFKC", span).translate(_UNI_MAP)
    n_text = unicodedata.normalize("NFKC", text).translate(_UNI_MAP)
    return _norm(n_span) in _norm(n_text)


def _strip_json_fence(content: str) -> str:
    stripped = (content or "").strip()
    if stripped.startswith("```"):
        end = stripped.rfind("```")
        body = stripped[:end]
        body = body[body.find("\n") + 1:] if "\n" in body else body
        return body
    return stripped


def apply_judge_response(response_content: str, paper_text: str) -> Dict[str, Any]:
    """Locally verify the judge's spans and logic; any anomaly -> uncertain."""
    try:
        data = json.loads(_strip_json_fence(response_content))
    except ValueError:
        return {"verdict": "uncertain", "allowed_uses": [],
                "reason": "judge_response_not_json"}
    verdict = str(data.get("verdict") or "uncertain")
    if verdict not in VERDICTS:
        return {"verdict": "uncertain", "allowed_uses": [],
                "reason": "judge_verdict_unknown_enum"}
    ro_span = str(data.get("research_object_span") or "")
    task_span = str(data.get("task_span") or "")
    spans_ok = _span_in_text(ro_span, paper_text) and _span_in_text(task_span, paper_text)
    if not spans_ok:
        return {"verdict": "uncertain", "allowed_uses": [],
                "reason": "judge_spans_not_verbatim_in_text"}
    if verdict == "reviewed_direct" and len(ro_span) < 8:
        return {"verdict": "uncertain", "allowed_uses": [],
                "reason": "direct_without_meaningful_object_span"}
    allowed = data.get("allowed_uses") or []
    if not isinstance(allowed, list) or not allowed:
        allowed = ALLOWED_USES[verdict]
    # never widen: judge list intersected with the verdict's lane
    allowed = [u for u in allowed if u in ALLOWED_USES[verdict]] or ALLOWED_USES[verdict]
    return {"verdict": verdict, "allowed_uses": allowed,
            "research_object_span": ro_span, "task_span": task_span,
            "reason": str(data.get("reason") or "")[:300]}


class ScopeAdmission:
    """The one decision entry every candidate consumer must call."""

    def __init__(self, *, log: ScopeDecisionLog, transport, contract: Dict[str, Any],
                 policy_hash: str, model: str, api_key: str, base_url: str,
                 task_id: str, generation_id: str, judge_system: str,
                 max_input_tokens: int = 4000, max_output_tokens: int = 2500):
        self.log = log
        self.transport = transport
        self.contract = contract
        self.contract_hash = contract.get("envelope", {}).get(
            "content_sha256") or _sha(_canonical(contract.get("object_phrases")))
        self.policy_hash = policy_hash
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.task_id = task_id
        self.generation_id = generation_id
        self.judge_system = judge_system
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens

    def decide(self, paper: Dict[str, Any], logical_call_id: str) -> Dict[str, Any]:
        document_id = paper.get("document_id") or paper.get("doi") or paper.get("paper_id")
        pre = prescreen(paper, self.contract)
        reuse_key = self.log.reuse_key(document_id, self.contract_hash, self.policy_hash)
        reused = self.log.find_reusable(document_id, self.contract_hash, self.policy_hash)
        if reused is not None and reused.get("verdict") not in ("candidate", None):
            out = dict(reused, reused=True)
            return out
        if pre["verdict"] in ("rejected", "uncertain"):
            decision = self._decision(document_id, reuse_key, pre["verdict"], pre,
                                      judge_receipt=None, reused=False)
            self.log.append(decision)
            return decision
        # real judge: must quote spans from the supplied text; one bounded
        # span-repair retry when the model's quotes are not verbatim
        prompt = build_judge_prompt(self.contract, paper)
        verdict_data = None
        response = None
        for repair_round in (0, 1):
            user_payload = prompt["user_payload"]
            if repair_round:
                user_payload = dict(user_payload, span_repair_note=(
                    "Your previous quotes were not verbatim substrings of paper_text. "
                    "Copy the research-object and task spans character-for-character "
                    "from paper_text."))
            response = self.transport.chat_completion(
                task_id=self.task_id, logical_call_id=logical_call_id,
                generation_id=self.generation_id, model=self.model,
                system_prompt=prompt["system_prompt"], user_payload=user_payload,
                api_key=self.api_key, base_url=self.base_url,
                schema=prompt["user_payload"]["output_schema"],
                max_input_tokens=self.max_input_tokens,
                max_output_tokens=self.max_output_tokens, temperature=0.0)
            verdict_data = apply_judge_response(response["content"],
                                                paper.get("text") or "")
            bad_reason = verdict_data.get("reason", "")
            needs_repair = ((verdict_data["verdict"] == "uncertain"
                             and "not_verbatim" in bad_reason)
                            or (verdict_data["verdict"] == "uncertain"
                                and "meaningful_object_span" in bad_reason))
            if not needs_repair:
                break
        decision = self._decision(document_id, reuse_key,
                                  verdict_data["verdict"], pre,
                                  judge_receipt={
                                      "provider_request_id": response["receipt"]["provider_request_id"],
                                      "raw_response_hash": response["receipt"]["raw_response_hash"],
                                      "actual_model": response["receipt"]["actual_model"],
                                      "cost_receipt": response["receipt"]["cost_receipt"],
                                  },
                                  reused=False,
                                  spans={k: verdict_data.get(k) for k in
                                         ("research_object_span", "task_span")},
                                  reason=verdict_data.get("reason", ""),
                                  allowed_uses=verdict_data["allowed_uses"])
        self.log.append(decision)
        return decision

    def _decision(self, document_id, reuse_key, verdict, prescreen_result,
                  judge_receipt, reused, spans=None, reason="", allowed_uses=None):
        return {
            "schema_version": DECISIONS_SCHEMA,
            "reuse_key": reuse_key,
            "document_id": document_id,
            "contract_hash": self.contract_hash,
            "policy_hash": self.policy_hash,
            "criterion_results": prescreen_result["criteria"],
            "verdict": verdict,
            "allowed_uses": allowed_uses if allowed_uses is not None else ALLOWED_USES[verdict],
            "writable": verdict in WRITABLE,
            "research_object_span": (spans or {}).get("research_object_span", ""),
            "task_span": (spans or {}).get("task_span", ""),
            "reason": reason or ("prescreen:" + prescreen_result["verdict"]),
            "judge_receipt": judge_receipt,
            "reused": reused,
        }
