"""Deterministic quality ceilings shared by Phase 3 argument components.

The language model may propose an interpretation, but it cannot increase the
permission of an asset.  This module is deliberately small and domain-neutral
so the same ceiling is applied by portfolio selection, evidence verification,
and section binding.
"""

from __future__ import annotations

from typing import Any


FACTUAL = "factual_support"
QUALIFIED = "contextual_or_qualified_support"
BACKGROUND = "background_and_candidate_only"
DISCOVERY = "discovery_only"

_FULL_CONTENT_DEPTHS = {
    "fulltext",
    "structured_snippet",
    "s2_snippet",
    "s2_body",
    "publisher_html",
    "pdf",
    "html_markdown",
}
_ABSTRACT_DEPTHS = {
    "abstract",
    "abstract_claim",
    "tldr",
    "metadata",
    "title",
    "snippet",
}
_TRANSFER_ROLES = {
    "method_transfer",
    "cross_domain_analogy",
    "analogy",
    "transfer",
}


def _lower(value: Any, default: str = "") -> str:
    return str(value or default).strip().casefold()


def permission_record(record: dict[str, Any] | None) -> dict[str, Any]:
    """Return normalized provenance fields without inventing missing facts."""

    raw = dict(record or {})
    provenance = raw.get("provenance")
    if not isinstance(provenance, dict):
        provenance = raw.get("route_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    allowed_claim_kinds = raw.get("allowed_claim_kinds") or []
    if isinstance(allowed_claim_kinds, str):
        allowed_claim_kinds = [allowed_claim_kinds]
    return {
        "use_permission": _lower(raw.get("use_permission"), DISCOVERY),
        "scope_fit": _lower(raw.get("scope_fit"), "unreviewed"),
        "source_kind": _lower(raw.get("source_kind") or raw.get("evidence_level"), "unknown"),
        "content_depth": _lower(raw.get("content_depth") or raw.get("evidence_level"), "metadata"),
        "retrieval_role": _lower(raw.get("retrieval_role"), ""),
        "provenance": provenance,
        "allowed_claim_kinds": [str(item).strip().casefold() for item in allowed_claim_kinds if str(item).strip()],
        "context_complete": bool(raw.get("context_complete", False)),
    }


def evidence_ceiling(record: dict[str, Any] | None) -> tuple[str, str]:
    """Return the maximum evidence permission and a deterministic reason.

    ``factual_support`` is intentionally narrow.  Abstracts, metadata,
    transfer/analogy material, adjacent scope, and incomplete snippets can
    still be useful, but only as qualified/contextual material.
    """

    info = permission_record(record)
    permission = info["use_permission"]
    scope = info["scope_fit"]
    depth = info["content_depth"]
    role = info["retrieval_role"]
    provenance_text = " ".join(
        str(value).casefold() for value in info["provenance"].values()
    )
    transfer = (
        scope in {"cross_domain_analogy", "method_transfer"}
        or role in _TRANSFER_ROLES
        or any(marker in provenance_text for marker in _TRANSFER_ROLES)
        or "method_transfer" in info["allowed_claim_kinds"]
        or "cross_domain_analogy" in info["allowed_claim_kinds"]
    )
    if permission in {DISCOVERY, "unknown", ""}:
        return DISCOVERY, "raw_permission_discovery_or_unknown"
    if permission == BACKGROUND:
        return BACKGROUND, "raw_permission_background_only"
    if scope in {"out_of_scope", "off_scope"}:
        return DISCOVERY, "out_of_scope"
    if transfer:
        return QUALIFIED, "transfer_or_cross_domain_ceiling"
    if depth in _ABSTRACT_DEPTHS or depth not in _FULL_CONTENT_DEPTHS:
        return QUALIFIED, "abstract_metadata_or_incomplete_content_ceiling"
    if scope in {"adjacent", "contextual", "unreviewed"}:
        return QUALIFIED, "non_direct_scope_ceiling"
    if permission == FACTUAL and scope == "direct":
        if info["context_complete"] is False and depth in {"structured_snippet", "s2_snippet", "s2_body"}:
            return QUALIFIED, "snippet_context_not_confirmed"
        return FACTUAL, "direct_full_content_permission"
    if permission == QUALIFIED:
        return QUALIFIED, "raw_qualified_permission"
    return BACKGROUND, "conservative_unknown_combination"


def normalize_importance(claim: dict[str, Any]) -> str:
    """Normalize claim importance and enforce the boundary/off-scope rule."""

    raw = str(
        claim.get("importance")
        or claim.get("importance_level")
        or claim.get("priority")
        or ""
    ).strip().casefold()
    if raw not in {"load_bearing", "supporting", "optional"}:
        raw = "load_bearing" if bool(claim.get("load_bearing")) else "supporting"
    fit = _lower(claim.get("section_fit"))
    if fit in {"boundary", "off_scope"} and raw == "load_bearing":
        return "supporting"
    return raw


def is_directly_usable(record: dict[str, Any] | None) -> bool:
    return evidence_ceiling(record)[0] == FACTUAL


def is_qualified_usable(record: dict[str, Any] | None) -> bool:
    return evidence_ceiling(record)[0] in {FACTUAL, QUALIFIED}


__all__ = [
    "FACTUAL",
    "QUALIFIED",
    "BACKGROUND",
    "DISCOVERY",
    "permission_record",
    "evidence_ceiling",
    "normalize_importance",
    "is_directly_usable",
    "is_qualified_usable",
]
