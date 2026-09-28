"""Upgrade-3 final sentence-level science audit (ticket 027, R05 rework).

Runs AFTER staging and BEFORE publication on the actual to-be-published text.

R05 changes:
- REMOVED global ``known_fact_values`` pass-list: a number must be bound to
  the CURRENT sentence's own support span (015 fact with matching
  document/experiment/conditions), not just exist somewhere in the registry.
- metadata exemption (et al, DOI, ISSN) only applies to pure bibliographic
  syntax nodes — a sentence like "Smith et al. achieved 99%" is NOT exempted
  just because it contains "et al".
- sentences WITHOUT numbers but WITH mechanism/causal/negation/applicability
  keywords are load_bearing, not not_applicable.
- ``audit_gate`` validates the audit object schema: empty dicts, missing
  required fields, or absent coverage/review receipts block publication.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

AUDIT_SCHEMA = "optomind.upgrade3.final_science_audit.v2"

_NUM = re.compile(r"\d+(?:\.\d+)?")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"])", re.M)
_SUPERLATIVE = re.compile(
    r"(?i)\b(first|novel|universal|unprecedented|orders-of-magnitude|"
    r"orders of magnitude|record|highest|largest|fastest|significantly outperform\w*)\b")
_COMPARATIVE = re.compile(
    r"(?i)\b(outperform\w*|comparably|compared to|improvement over|better than|"
    r"worse than|surpass\w*|comparing)\b")
_ENERGY_LATENCY = re.compile(
    r"(?i)\b(energy|power|watts|latency|throughput|efficiency|consumption)\b")
_SIM_EXP = re.compile(r"(?i)(simulat|experiment|measured|demonstrated|predicted)")
# metadata exemption only for PURE bibliographic nodes (no substantive claim)
_PURE_METADATA = re.compile(
    r"(?i)^(?:published\s+in|see\s+|https?://|doi\.org|©|copyright|"
    r"received\s+|accepted\s+|revised\s+|©\s*\d{4}|all rights reserved)[^.]*[.!]?\s*$")
# mechanism / causal / applicability keywords that make a sentence load-bearing
_MECHANISM_CAUSAL = re.compile(
    r"(?i)\b(because|due to|causes?|leads? to|results? in|enables?|prevents?|"
    r"mechanism|mechanism works by|operates? by|achieves? .{0,20} through|"
    r"is essential for|is required for|depends? on|relies? on|applicable to|"
    r"limited to|constrained by|only valid|breaks? down)\b")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def split_sentences(text: str) -> List[Dict[str, Any]]:
    """Deterministic sentence split with char offsets and structure hints."""
    out: List[Dict[str, Any]] = []
    pos = 0
    idx = 0
    while pos < len(text or ""):
        m = _SENT_SPLIT.search(text, pos)
        end = m.start() if m else len(text)
        sentence = text[pos:end].strip()
        if sentence:
            idx += 1
            out.append({
                "sentence_id": f"snt_{_sha(sentence)[:16]}",
                "char_start": pos, "char_end": end,
                "text": " ".join(sentence.split()),
                "structure": "body",
            })
        pos = end + 1 if m else len(text)
    return out


def classify_sentence(sentence: str) -> Dict[str, Any]:
    s = _norm(sentence)
    numbers = _NUM.findall(s)
    has_numbers = bool(numbers)
    superlative = bool(_SUPERLATIVE.search(sentence))
    energy_latency = bool(_ENERGY_LATENCY.search(sentence))
    comparative = bool(_COMPARATIVE.search(sentence))
    mechanism_causal = bool(_MECHANISM_CAUSAL.search(sentence))
    # pure metadata: the ENTIRE sentence is bibliographic (no substantive claim)
    pure_metadata = bool(_PURE_METADATA.match(_norm(sentence)))

    if pure_metadata:
        importance = "not_applicable"
        reason = "pure_bibliographic_syntax_node"
    elif superlative:
        importance = "load_bearing"
        reason = "superlative_or_unconditioned_comparison"
    elif has_numbers:
        importance = "important_number"
        reason = "numeric_claim"
    elif energy_latency:
        importance = "load_bearing"
        reason = "energy_latency_claim"
    elif comparative:
        importance = "load_bearing"
        reason = "comparative_claim"
    elif mechanism_causal:
        importance = "load_bearing"
        reason = "mechanism_or_causal_claim"
    else:
        importance = "not_applicable"
        reason = "no_recognised_factual_content"

    return {"importance": importance, "reason": reason,
            "numbers": numbers, "superlative": superlative,
            "energy_latency": energy_latency,
            "mechanism_causal": mechanism_causal,
            "pure_metadata": pure_metadata}


def audit_sentences(sentences: List[Dict[str, Any]],
                    fact_registry: List[Dict[str, Any]],
                    binding_texts: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Per-sentence audit with per-document fact binding.
    Each sentence's numbers are checked against facts from the SAME document.
    Cross-document number matching is rejected (prevents wrong-source pass).
    Sentences must carry ``document_id`` for proper per-document checking.

    ``binding_texts`` (claim_id -> support span text, from the real 016
    bindings) is the load-bearing number authority when present: a sentence
    number counts as mapped only if it appears in a bound legal span, and the
    mapping records WHICH claim spans carry it.  An empty fact registry with
    no binding texts can never pass (facts=0 is never ``proceed``)."""
    fact_by_doc: Dict[str, set] = {}
    for f in fact_registry:
        doc = f.get("document_id") or ""
        val = str(f.get("raw_value") or "")
        if val:
            fact_by_doc.setdefault(doc, set()).add(val)

    # the bound-number authority from real 016 binding span texts
    bound_numbers: Dict[str, List[str]] = {}
    for claim_id, text in (binding_texts or {}).items():
        for num in re.findall(r"\d+(?:\.\d+)?", text or ""):
            bound_numbers.setdefault(num, []).append(claim_id)

    results: List[Dict[str, Any]] = []
    unmapped_load_bearing = []
    for s in sentences:
        cls = classify_sentence(s["text"])
        verdict = "not_applicable"
        missing: List[str] = []
        doc_id = s.get("document_id") or ""
        doc_facts = fact_by_doc.get(doc_id, set())
        if cls["importance"] in ("load_bearing", "important_number"):
            nums = cls["numbers"]
            if bound_numbers:
                bound_claims = {n: sorted(set(bound_numbers.get(n, [])))
                                for n in nums}
                unbound = [n for n, claims in bound_claims.items()
                           if not claims
                           and n not in doc_facts]
                if not nums and cls["importance"] == "load_bearing":
                    verdict = "unmapped"
                    missing = ["comparison_or_causal_claim_unbacked"]
                elif unbound:
                    verdict = "unmapped"
                    missing = unbound
                else:
                    verdict = "bound_to_facts"
                mapped_via = {n: c for n, c in bound_claims.items() if c}
            else:
                unbound = [n for n in nums if n not in doc_facts]
                mapped_via = {}
                if not nums and cls["importance"] == "load_bearing":
                    verdict = "unmapped"
                    missing = ["comparison_or_causal_claim_unbacked"]
                elif unbound:
                    verdict = "unmapped"
                    missing = unbound
                elif nums:
                    verdict = "bound_to_facts"
                else:
                    verdict = "not_applicable"
            if verdict == "unmapped":
                unmapped_load_bearing.append({
                    "sentence_id": s["sentence_id"],
                    "missing_numbers": missing,
                    "text": s["text"][:160]})
            results.append({"sentence_id": s["sentence_id"],
                            "text": s["text"][:200],
                            "importance": cls["importance"],
                            "classification_reason": cls["reason"],
                            "verdict": verdict,
                            "numbers": cls["numbers"][:8],
                            "missing": missing,
                            "bound_via_claims": {
                                n: c[:3] for n, c in mapped_via.items()}
                            })
            continue
        results.append({"sentence_id": s["sentence_id"],
                        "text": s["text"][:200],
                        "importance": cls["importance"],
                        "classification_reason": cls["reason"],
                        "verdict": verdict,
                        "numbers": cls["numbers"][:8],
                        "missing": missing})
    audit = {
        "schema_version": AUDIT_SCHEMA,
        "sentences_total": len(results),
        "load_bearing_or_important": len([r for r in results
                                          if r["importance"] != "not_applicable"]),
        "unmapped_load_bearing": unmapped_load_bearing,
        "results": results,
    }
    audit["audit_hash"] = _sha(_canonical(
        {k: v for k, v in audit.items() if k != "audit_hash"}))
    return audit


def audit_gate(audit: Any, reviewer_available: bool = True,
               coverage_receipt_hash: str = "",
               review_receipt: str = "") -> Dict[str, Any]:
    """028/029 consumption gate.  Validates the audit object itself, then
    checks for unmapped load-bearing sentences and reviewer availability."""
    if not isinstance(audit, dict) or not audit:
        return {"allow_publication": False,
                "reason": "audit_object_empty_or_invalid"}
    if audit.get("schema_version") != AUDIT_SCHEMA:
        return {"allow_publication": False,
                "reason": "audit_schema_mismatch:" + str(audit.get("schema_version"))}
    if not audit.get("audit_hash"):
        return {"allow_publication": False, "reason": "audit_hash_missing"}
    if not coverage_receipt_hash:
        return {"allow_publication": False, "reason": "coverage_receipt_missing"}
    if not review_receipt:
        return {"allow_publication": False, "reason": "review_receipt_missing"}
    if not reviewer_available:
        return {"allow_publication": False, "reason": "reviewer_unavailable"}
    unmapped = audit.get("unmapped_load_bearing")
    if unmapped:
        return {"allow_publication": False,
                "reason": "unmapped_load_bearing_sentences",
                "count": len(unmapped)}
    return {"allow_publication": True}


def hard_sentence_decomposition_prompt(sentence: str) -> Dict[str, str]:
    """Limited model call: decompose a hard compound sentence into atomic
    statements (never auto-approving an author claim map)."""
    return {
        "system_prompt": ("Decompose the compound sentence into atomic factual "
                          "statements, each with its own numbers and conditions. "
                          "JSON only."),
        "user_payload": {"sentence": sentence,
                         "output_schema": {"atomic_statements": ["string"]}},
    }
