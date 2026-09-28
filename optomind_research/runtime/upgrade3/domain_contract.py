"""Upgrade-3 research-object & question contract (ticket 009).

Converts the original question into one DOMAIN_CONTRACT: object phrases,
target system, task, regime, inclusions/exclusions (with provenance),
required questions, comparison axes and time scope.

Design rules:
- object slots derive from the ORIGINAL QUESTION (deterministic head-phrase
  patterns) and from an explicitly validated model proposal; never from
  high-frequency token ranking.  ``neural``/``network`` alone can never
  confirm an object.
- method-centric questions (e.g. PINN vs differentiable EM solvers) keep a
  joint ``methods + target_system`` object; there is no device-suffix
  whitelist and no ODNN-specific hard-coding.
- exclusions only ever come from explicit user wording, with provenance;
  the pipeline never disguises model-derived exclusions as user requirements.
- time scope is ``unspecified`` when the question says nothing.
- status machine: proposed -> validated -> frozen; an object that cannot be
  determined is ``needs_scope`` and produces NO executable generic-optics
  fallback.
- downstream entries (query, topic scope, coverage, explanatory candidates)
  consume the same ``contract_hash``; a cached artifact without that hash
  cannot be auto-confirmed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_SCHEMA = "optomind.upgrade3.domain_contract.v1"
STATUSES = {"proposed", "validated", "frozen", "needs_scope"}

_HEAD_PATTERNS = [
    r"(?:research progress|progress|advances|recent advances|developments)\s+(?:of|on|in)\s+(?P<head>[^,.;;]+(?:\s+and\s+[^,.;;]+)*)",
    r"(?:comparative\s+)?review\s+(?:of|on|research\s+progress\s+of)\s+(?P<head>[^,.;;]+(?:\s+and\s+[^,.;;]+)*)",
    r"(?:comparative\s+)?review\s+(?:the\s+)?(?P<head>[^,.;;]+)",
    r"^compare\s+(?:the\s+)?(?P<head>[^,.;;]+)",
    r"^(?P<head>[A-Z][^,.;;]+(?:\s+and\s+[^,.;;]+)*)\s*(?:比较|综述)",
]
_COMPARE_SPLIT = re.compile(r",?\s+(?:and|versus|vs\.?|相比|与)\s+", re.I)
_TASK_PATTERNS = [
    (re.compile(r"\bcompar", re.I), "comparison"),
    (re.compile(r"\breview|survey|summar", re.I), "review"),
    (re.compile(r"\bbottleneck|challenge|limitation", re.I), "bottlenecks"),
    (re.compile(r"\bfuture direction|outlook|roadmap", re.I), "future_directions"),
    (re.compile(r"\bapplication", re.I), "applications"),
    (re.compile(r"\btraining", re.I), "training"),
    (re.compile(r"\barchitecture", re.I), "architectures"),
]
_METHOD_HINTS = re.compile(
    r"(?i)\b(method|methods|approach|approaches|solver|solvers|algorithm|algorithms|"
    r"training|paradigm|paradigms|search|strategy|strategies)\b")
_USER_EXCLUSION = re.compile(
    r"(?i)(?:excluding|except|not covering|do not cover|不含|不包括|排除)\s+([^,.;;]+)")
_TIME_SCOPE = re.compile(r"(?i)(20\d{2}\s*(?:-|–|to|since)\s*(20\d{2})?|since\s+20\d{2}|last\s+\d+\s+years)")
_PLURAL = re.compile(r"s$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _tokens(phrase: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", phrase.lower()) if w]


def _grounded(phrase: str, question: str) -> bool:
    """Phrase (or its singular/plural token variant) must appear in the question."""
    q_tokens = _tokens(question)
    p_tokens = [_PLURAL.sub("", t) for t in _tokens(phrase)]
    if not p_tokens:
        return False
    q_norm = [_PLURAL.sub("", t) for t in q_tokens]
    joined = " " + " ".join(q_norm) + " "
    phrase_norm = " ".join(p_tokens)
    if phrase_norm in joined:
        return True
    # every content token present, in order (allows inserted adjectives)
    pos = 0
    hits = 0
    for tok in p_tokens:
        if tok in q_norm[pos:]:
            idx = q_norm.index(tok, pos)
            pos = idx + 1
            hits += 1
    return hits >= max(1, len(p_tokens) - 1)


def _check_grounding(phrase: str, question: str) -> bool:
    if _grounded(phrase, question):
        return True
    # whitespace/plural-insensitive literal check
    q = re.sub(r"\s+", " ", question.lower())
    p = re.sub(r"\s+", " ", phrase.lower().strip())
    return p in q


def extract_slots(question: str) -> Dict[str, Any]:
    q = re.sub(r"\s+", " ", question.strip())
    head = None
    for pattern in _HEAD_PATTERNS:
        m = re.search(pattern, q, re.I)
        if m:
            head = m.group("head").strip()
            break
    task: List[str] = []
    for rx, name in _TASK_PATTERNS:
        if rx.search(q):
            task.append(name)
    required_questions: List[str] = []
    focus = re.search(r"(?:focusing on|focusing|focus on)\s+(?P<focus>[^.]+)", q, re.I)
    if focus:
        required_questions = [p.strip(" .") for p in re.split(r",| and ", focus.group("focus")) if p.strip(" .")]
    comparison_axes: List[str] = []
    cmp = re.search(r"compar(?:ing|e)\s+(?:different\s+)?([^,.;;]+)", q, re.I)
    if cmp:
        comparison_axes = [p.strip() for p in _COMPARE_SPLIT.split(cmp.group(1)) if p.strip()]
    target_system = ""
    m = re.search(r"\bfor\s+((?:[a-z]+\s+){0,3}[a-z]+)\b", q, re.I)
    if m and head and m.group(1).lower() not in head.lower():
        target_system = m.group(1).strip()
    time_scope = "unspecified"
    if _TIME_SCOPE.search(q):
        time_scope = _TIME_SCOPE.search(q).group(0)
    return {"head": head, "task": task, "required_questions": required_questions,
            "comparison_axes": comparison_axes, "target_system": target_system,
            "time_scope": time_scope}


def build_domain_contract(question: str,
                          model_proposal: Optional[Dict[str, Any]] = None,
                          generation_id: str = "") -> Dict[str, Any]:
    q = re.sub(r"\s+", " ", question.strip())
    slots = extract_slots(q)
    provenance = {"object_phrases": "derived_from_original_question",
                  "exclusions": "none_stated"}
    object_phrases: List[str] = []
    if slots["head"]:
        for part in _COMPARE_SPLIT.split(slots["head"]):
            part = part.strip(" .")
            if part and _check_grounding(part, q) and part.lower() not in \
                    [o.lower() for o in object_phrases]:
                object_phrases.append(part)
    # model proposal may ADD phrases only if grounded in the question
    if isinstance(model_proposal, list):
        model_proposal = {"object_phrases": [p for p in model_proposal if isinstance(p, str)]}
    rejected_model_phrases: List[str] = []
    if model_proposal:
        for phrase in (model_proposal.get("object_phrases") or []):
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            if _check_grounding(phrase.strip(), q):
                if phrase.strip().lower() not in [o.lower() for o in object_phrases]:
                    object_phrases.append(phrase.strip())
                    provenance["object_phrases"] = "question_head+validated_model_proposal"
            else:
                rejected_model_phrases.append(phrase.strip())
    exclusions: List[str] = []
    m = _USER_EXCLUSION.search(q)
    if m:
        exclusions = [m.group(1).strip()]
        provenance["exclusions"] = "explicit_user_wording"
    method_centric = bool(slots["head"] and _METHOD_HINTS.search(slots["head"])) or \
        bool(model_proposal and model_proposal.get("object_kind") == "methods+target_system")
    object_kind = "methods+target_system" if method_centric else "research_object"
    status = "validated" if object_phrases else "needs_scope"
    contract = {
        "schema_version": CONTRACT_SCHEMA,
        "envelope": {
            "run_id": "upgrade3_domain_contract",
            "generation_id": generation_id or "g1",
            "attempt_id": "a1",
            "artifact_id": "domain_contract",
            "producer": "upgrade3.domain_contract.build_domain_contract",
            "parent_artifact_hashes": [],
            "created_at": _now(),
            "content_sha256": "",
            "policy_sha256": "not_applicable",
            "prompt_manifest_hash": "not_applicable",
            "model_manifest_hash": "not_applicable",
            "material_snapshot_hash": "not_applicable",
        },
        "original_question": q,
        "original_question_hash": _sha(q),
        "object_kind": object_kind,
        "object_phrases": object_phrases,
        "object_aliases": (model_proposal or {}).get("object_aliases") or [],
        "target_system": slots["target_system"] or (model_proposal or {}).get("target_system", ""),
        "task": slots["task"],
        "regime": (model_proposal or {}).get("regime", ""),
        "inclusions": object_phrases,
        "exclusions": exclusions,
        "provenance": provenance,
        "rejected_model_phrases": rejected_model_phrases,
        "required_questions": slots["required_questions"],
        "comparison_axes": slots["comparison_axes"],
        "time_scope": slots["time_scope"],
        "status": status,
        "version": 1,
    }
    if status == "needs_scope":
        contract["scope_blocker"] = ("object undeterminable from the question; "
                                     "no generic-optics fallback is generated")
    contract["envelope"]["content_sha256"] = hashlib.sha256(_canonical(
        {k: v for k, v in contract.items() if k != "envelope"}).encode("utf-8")).hexdigest()
    return contract


def _DEVICEISH_HEAD(head: str) -> bool:
    # generic physics-engineering device nouns only decide object_kind; there
    # is deliberately no per-domain whitelist here
    return bool(re.search(r"(?i)\b(network|networks|device|devices|material|materials|"
                          r"metasurface|metasurfaces|crystal|crystals|fiber|fibres|fibers|"
                          r"chip|chips|processor|processors)\b", head))


def freeze(contract: Dict[str, Any]) -> Dict[str, Any]:
    if contract["status"] != "validated":
        raise ValueError("only validated contracts can be frozen, got: " + contract["status"])
    frozen = dict(contract, status="frozen")
    frozen["envelope"] = dict(contract["envelope"],
                              content_sha256=hashlib.sha256(_canonical(
                                  {k: v for k, v in frozen.items()
                                   if k != "envelope"}).encode("utf-8")).hexdigest())
    return frozen


def contract_hash(contract: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in contract.items()
                                      if k != "envelope"}).encode("utf-8")).hexdigest()


def check_plan_preserves_question(contract: Dict[str, Any],
                                  plan: Dict[str, Any]) -> Dict[str, Any]:
    """Query Planner proposals are checked locally: object constraints from the
    original question must survive; contradictions stop with needs_scope and
    never produce a generic fallback."""
    problems: List[str] = []
    plan_text = _canonical(plan).lower()
    q = contract.get("original_question", "").lower()
    for phrase in contract["object_phrases"]:
        toks = [_PLURAL.sub("", t) for t in _tokens(phrase)]
        if not all(tok in plan_text for tok in toks):
            problems.append("object_phrase_dropped_by_plan:" + phrase)
    for ex in contract["exclusions"]:
        if _tokens(ex)[0] in plan_text.replace("excluding", "including"):
            problems.append("exclusion_violated_by_plan:" + ex)
    if contract["status"] == "needs_scope":
        problems.append("contract_has_no_validated_object")
    return {"ok": not problems, "problems": problems,
            "verdict": "accepted" if not problems else "needs_scope"}


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
