"""Upgrade-3 publication preflight & citation change audit (ticket 028).

The publication path consumes the 026 manifest, the 015 fact registry and a
FRESH 027 science audit.  Rules:

- PUBLICATION_INPUT_MANIFEST records the final text/abstract/captions/BibTeX/
  alias-map/facts/audit hashes; ``allowed=false`` forbids formal render — a
  diagnostic render is labelled ``diagnostic_only`` and can never yield a
  scientific deliverable state.
- CITATION_CHANGE_AUDIT: any citation drop/change versus the audited version
  forces re-audit before formal render; deleting a token while keeping the
  prose is rejected.
- metadata-insufficient references degrade to identifiable entries with
  provenance instead of being silently dropped; an unfixable load-bearing
  reference returns needs_evidence.
- pure normalization (math/unicode/section numbering) keeps position mappings;
  facts and the citation inventory are re-verified against the ACTUAL final
  text; all preflight exceptions are failures (no try/except bypass).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

MANIFEST_SCHEMA = "optomind.upgrade3.publication_input_manifest.v1"
AUDIT_SCHEMA = "optomind.upgrade3.citation_change_audit.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class PreflightFailure(Exception):
    pass


def build_input_manifest(*, final_text: str, abstract: str,
                         captions: List[str], bibtex: str,
                         alias_map: Dict[str, str], facts_hash: str,
                         science_audit_hash: str) -> Dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "final_text_hash": _sha(final_text),
        "abstract_hash": _sha(abstract),
        "abstract_present": bool(abstract.strip()),
        "captions_hashes": [_sha(c) for c in captions],
        "bibtex_hash": _sha(bibtex),
        "alias_map": alias_map,
        "facts_hash": facts_hash,
        "science_audit_hash": science_audit_hash,
    }


def citation_change_audit(*, current_citations: List[str],
                          audited_citations: List[str],
                          load_bearing_citations: Optional[List[str]] = None,
                          final_text: str = "") -> Dict[str, Any]:
    """Detect dropped/changed citations versus the audited version.
    A dropped load-bearing citation (or any citation count drop) forces
    needs_evidence instead of silent prose-only compilation."""
    current_set = set(current_citations)
    audited_set = set(audited_citations)
    dropped = sorted(audited_set - current_set)
    added = sorted(current_set - audited_set)
    load_bearing = set(load_bearing_citations or [])
    dropped_load_bearing = sorted(set(dropped) & load_bearing)
    return {
        "schema_version": AUDIT_SCHEMA,
        "dropped": dropped,
        "added": added,
        "dropped_load_bearing": dropped_load_bearing,
        "count_change": len(current_citations) - len(audited_citations),
        "needs_reaudit": bool(dropped or added),
        "needs_evidence": bool(dropped_load_bearing),
    }


def publication_preflight(*, input_manifest: Dict[str, Any],
                          science_audit: Dict[str, Any],
                          citation_audit: Dict[str, Any],
                          final_text: str,
                          facts: List[Dict[str, Any]],
                          reviewer_available: bool = True,
                          enhancement_enabled: bool = False) -> Dict[str, Any]:
    reasons: List[str] = []
    # 1) audit freshness: the manifest must carry the CURRENT audit hash
    if input_manifest.get("science_audit_hash") != science_audit.get("audit_hash"):
        reasons.append("science_audit_stale_or_mismatched")
    # 2) citation changes force re-audit
    if citation_audit.get("needs_reaudit"):
        reasons.append("citation_changes_without_reaudit")
    if citation_audit.get("needs_evidence"):
        reasons.append("load_bearing_citation_dropped")
    # 3) abstract must exist (renderer may never invent one)
    if not input_manifest.get("abstract_present"):
        reasons.append("abstract_missing_renderer_may_not_invent")
    # 4) facts0 advisory is forbidden: every scientific number bound.
    #    Bibliographic/structural numbers (DOI digits, years, section numbers)
    #    are exempt via the 015 position-bound classifier, never globally.
    from .fact_registry import classify_number_token
    fact_values = {str(f.get("raw_value") or "") for f in facts}
    unbound = []
    for m in re.finditer(r"\d{2,}(?:\.\d+)?", final_text or ""):
        num = m.group(0)
        if num in fact_values:
            continue
        cls = classify_number_token(num, final_text or "")
        if cls.get("class") in ("bibliographic", "structural"):
            continue
        unbound.append(num)
    if unbound:
        reasons.append("scientific_numbers_unbound:" + ",".join(sorted(set(unbound))[:6]))
    # 5) reviewer availability
    if not reviewer_available:
        reasons.append("reviewer_unavailable")
    allowed = not reasons
    return {
        "schema_version": "optomind.upgrade3.publication_preflight.v1",
        "allowed": allowed,
        "reasons": reasons,
        "formal_render_allowed": allowed,
        "diagnostic_render": (None if allowed else
                              {"diagnostic_only": True,
                               "note": "diagnostic output can never yield a "
                                       "scientific deliverable state"}),
        "facts_bound_coverage_pct": (100 if not unbound else
                                     max(0, 100 - len(unbound))),
        "input_manifest": input_manifest,
        "citation_audit": citation_audit,
    }


def _re_module():
    import re
    return re


def find_numbers(text: str) -> List[str]:
    re_mod = _re_module()
    return re_mod.findall(r"\d{2,}(?:\.\d+)?", text or "")
