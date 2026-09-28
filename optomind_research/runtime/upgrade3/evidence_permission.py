"""Upgrade-3 evidence permission & writable-packet state (ticket 012).

One effective-permission function for every adapter (claim pool, R4 author,
enhancer): the final permission is the STRICTEST intersection of

    source_cap (content depth) ∩ scope_cap (adjudicated science domain)
    ∩ claim_role_cap (importance) ∩ model_requested (may only lower)

Key semantics (global contract):
- ``metadata``/DOI alone: discovery_only.  An abstract or structured snippet is
  ``reported_only`` — it may say "the authors report X", never "X is
  experimentally confirmed".
- ``out_of_scope``/``uncertain`` scopes grant NO writable permission (fixes the
  donor bug where out_of_scope content could still surface ``qualified``).
- ``adjacent`` is background-only: it may explain, never support.
- complete fulltext caps at ``qualified_support``: even a full text must wait
  for the claim-level audit (016) before anything becomes ``direct_support``.
- a missing scope/support/integrity field is NOT a default-direct: missing
  scope degrades to ``uncertain``.
- every downgrade carries a reason and the cap that caused it.
- packet states keep their gaps: ``empty`` / ``partial`` (with the missing
  load-bearing slots) / ``conflicting`` (with the contradiction list); a
  contradiction is never resolved by taking the highest permission.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

RECEIPT_SCHEMA = "optomind.upgrade3.evidence_permission_receipt.v1"

PERMISSION_STRENGTH = {
    "discovery_only": 0,
    "background_only": 1,
    "reported_only": 2,
    "qualified_support": 3,
    "direct_support": 4,
}

SOURCE_CAPS = {
    "metadata": "discovery_only",
    "abstract": "reported_only",
    "abstract_claim": "reported_only",
    "structured_snippet": "reported_only",
    "partial_fulltext": "reported_only",
    "fulltext": "qualified_support",
}

SCOPE_CAPS = {
    "direct": "direct_support",
    "adjacent": "background_only",
    "background": "background_only",
    "uncertain": "discovery_only",
    "out_of_scope": "discovery_only",
}

ROLE_CAPS = {
    "load_bearing": "direct_support",
    "supporting": "reported_only",
    "background": "background_only",
}

CLAIM_KINDS_BY_PERMISSION = {
    "direct_support": ["established_fact", "conditioned_comparison", "mechanism",
                       "author_reported"],
    "qualified_support": ["conditioned_comparison", "author_reported"],
    "reported_only": ["author_reported"],
    "background_only": ["background_explanation"],
    "discovery_only": [],
}


class PermissionError(Exception):
    pass


def effective_permission(*, source_depth: Optional[str], scope_verdict: Optional[str],
                         claim_role: str = "supporting",
                         model_requested: Optional[str] = None) -> Dict[str, Any]:
    """The one permission function every adapter must call."""
    caps: List[Dict[str, Any]] = []
    if source_depth not in SOURCE_CAPS:
        caps.append(("discovery_only", f"source_depth_missing_or_unknown:{source_depth!r}"
                   " -> not a default direct"))
    else:
        caps.append((SOURCE_CAPS[source_depth], f"source_cap:{source_depth}"))
    if scope_verdict not in SCOPE_CAPS:
        caps.append(("discovery_only", "scope_missing_or_unknown -> uncertain treatment,"
                   " never default direct"))
    else:
        caps.append((SCOPE_CAPS[scope_verdict], f"scope_cap:{scope_verdict}"))
    if claim_role not in ROLE_CAPS:
        caps.append(("reported_only", f"claim_role_unknown:{claim_role!r}"))
    else:
        caps.append((ROLE_CAPS[claim_role], f"claim_role_cap:{claim_role}"))
    if model_requested is not None:
        if model_requested not in PERMISSION_STRENGTH:
            caps.append(("discovery_only", f"model_requested_unknown:{model_requested!r}"))
        else:
            caps.append((model_requested, "model_requested (may only lower)"))

    effective = "direct_support"
    reasons: List[str] = []
    for cap, reason in caps:
        if PERMISSION_STRENGTH[cap] < PERMISSION_STRENGTH[effective]:
            effective = cap
    for cap, reason in caps:
        if cap == effective:
            reasons.append(reason)
    allowed_kinds = CLAIM_KINDS_BY_PERMISSION[effective]
    not_usable_for: List[str] = []
    if PERMISSION_STRENGTH[effective] < PERMISSION_STRENGTH["direct_support"]:
        not_usable_for.append("established_fact")
    if effective in ("background_only", "discovery_only"):
        not_usable_for += ["conditioned_comparison", "author_reported"]
    if scope_verdict in ("out_of_scope", "uncertain"):
        not_usable_for += ["writable_evidence", "background_explanation_in_packet"
                           if scope_verdict == "out_of_scope" else "writable_evidence"]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "source_cap": caps[0][0],
        "scope_cap": caps[1][0],
        "claim_role_cap": caps[2][0],
        "model_requested": model_requested,
        "effective_permission": effective,
        "allowed_claim_kinds": allowed_kinds,
        "not_usable_for": sorted(set(not_usable_for)),
        "downgrade_reasons": [r for cap, r in caps
                              if PERMISSION_STRENGTH[cap] < PERMISSION_STRENGTH["direct_support"]],
        "writable": PERMISSION_STRENGTH[effective] >= PERMISSION_STRENGTH["qualified_support"],
    }


def apply_caps_to_packet(evidence_rows: Iterable[Dict[str, Any]],
                         get_source_depth, get_scope_verdict,
                         claim_role: str = "supporting") -> List[Dict[str, Any]]:
    """Adapter-side normalisation for claim pool / R4 / enhancer packets:
    every row's permission is recomputed; a model-supplied permission may only
    request an equal or lower value.  Downgrades are recorded per row."""
    out: List[Dict[str, Any]] = []
    for row in evidence_rows:
        requested = row.get("writing_permission") or row.get("use_permission")
        if requested not in PERMISSION_STRENGTH:
            requested = None  # legacy/unknown request strings are ignored, not punished
        receipt = effective_permission(
            source_depth=get_source_depth(row),
            scope_verdict=get_scope_verdict(row),
            claim_role=claim_role,
            model_requested=requested,
        )
        new_row = dict(row)
        new_row["effective_permission"] = receipt["effective_permission"]
        new_row["permission_receipt"] = receipt
        if requested and PERMISSION_STRENGTH.get(requested, 9) > \
                PERMISSION_STRENGTH[receipt["effective_permission"]]:
            new_row["permission_downgraded_from"] = requested
        out.append(new_row)
    return out


def compute_packet_state(rows: List[Dict[str, Any]],
                         load_bearing_slots: Iterable[str]) -> Dict[str, Any]:
    """Packet state from the same rules for every producer.

    empty / partial / conflicting / ready keep their gaps and required
    actions; contradictions are listed, never resolved by taking the max."""
    slots = set(load_bearing_slots)
    state = "empty" if not rows else None
    if state is None:
        covered = set()
        conflicts: List[str] = []
        for row in rows:
            if row.get("effective_permission") in ("qualified_support", "direct_support"):
                for s in row.get("satisfies_slots") or []:
                    covered.add(s)
            if row.get("field_status") == "conflicting":
                conflicts.append("conflicting_field_status:" + str(row.get("fact_id") or row.get("chunk_id")))
            if row.get("contradicts") :
                conflicts.append("contradicts:" + str(row.get("contradicts")))
        missing = sorted(slots - covered)
        if missing:
            state = "partial"
        elif conflicts:
            state = "conflicting"
        else:
            state = "ready"
        if conflicts:
            state = "conflicting"
        return {"state": state,
                "missing_load_bearing_slots": sorted(missing),
                "conflicts": conflicts,
                "required_actions": (["run claim-level audit for missing slots"]
                                     if missing else
                                     ["resolve contradictions before writable export"]
                                     if conflicts else []),
                "audited_count": len(rows)}
    return {"state": "empty", "missing_load_bearing_slots": sorted(slots),
            "conflicts": [], "required_actions": ["acquire evidence"],
            "audited_count": 0}
