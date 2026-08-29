"""Adaptive evidence-readiness gate for review claims.

Readiness means "enough to use this carefully worded claim", not "the whole
field has reached consensus". Specific source-grounded findings therefore do
not need the same temporal and methodological breadth as universal claims.
"""

from __future__ import annotations

import re
from typing import Any


def _chunk_text(chunk: dict[str, Any]) -> str:
    return " ".join(
        str(chunk.get(key) or "")
        for key in ("content", "text", "text_preview", "search_text", "section_path")
    ).lower()


def _is_abstract_chunk(chunk: dict[str, Any]) -> bool:
    return str(chunk.get("source_kind") or chunk.get("evidence_level") or "").strip().lower() == "abstract"


def _is_broad_claim(text: str) -> bool:
    lowered = str(text or "").lower()
    patterns = (
        r"\bconsensus\b", r"\buniversal(?:ly)?\b", r"\bgenerally\b",
        r"\bacross (?:all|multiple|diverse)\b", r"\blong[- ]term\b",
        r"\bstate[- ]of[- ]the[- ]art\b", r"\bfield[- ]wide\b",
        r"\balways\b", r"\bdominant\b", r"\bsuperior to all\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def evaluate_evidence_readiness(
    *,
    claim_text: str,
    supporting_chunks: list[dict[str, Any]],
    claim_type: str = "",
    binding_status: str = "",
    binding_confidence: str = "",
    missing_components: list[str] | None = None,
    load_bearing: bool = False,
) -> dict[str, Any]:
    chunk_count = len(supporting_chunks)
    paper_ids = {
        str(c.get("source_paper_id") or c.get("paper_id") or c.get("doi") or "")
        for c in supporting_chunks
        if c.get("source_paper_id") or c.get("paper_id") or c.get("doi")
    }
    unique_papers = len(paper_ids)
    years = [c.get("publication_year") or c.get("year") for c in supporting_chunks]
    years = [int(y) for y in years if isinstance(y, (int, str)) and str(y).isdigit()]
    temporal_span = max(years) - min(years) if len(years) >= 2 else 0
    broad_claim = _is_broad_claim(claim_text)
    missing = [str(x) for x in (missing_components or []) if str(x).strip()]

    chunk_count_score = 0 if chunk_count == 0 else 8 if chunk_count == 1 else 16 if chunk_count == 2 else 20 if chunk_count == 3 else 23 if chunk_count == 4 else 25
    diversity_score = 0 if unique_papers <= 1 else 15 if unique_papers == 2 else 20 if unique_papers == 3 else 25

    if broad_claim:
        temporal_score = 0 if temporal_span < 1 else 10 if temporal_span <= 2 else 20 if temporal_span <= 5 else 25
    else:
        # A timeless mechanism or one published measurement does not become
        # invalid merely because supporting papers are close in publication year.
        temporal_score = 15 if years else 10
        if temporal_span >= 3:
            temporal_score = 20
        if temporal_span >= 6:
            temporal_score = 25

    texts = [_chunk_text(c) for c in supporting_chunks]
    type_keywords = {
        "mechanism": ("mechanism", "theory", "model", "equation", "dependence", "symmetry", "causal", "scaling"),
        "measurement": ("measured", "observed", "experiment", "result", "data", "temperature", "efficiency"),
        "comparison": ("compared", "versus", "higher", "lower", "outperform", "benchmark", "difference"),
        "application": ("application", "device", "deployment", "fabrication", "operation", "sensor", "system"),
    }
    keywords = type_keywords.get(str(claim_type or "").lower(), ())
    matched_chunks = sum(1 for text in texts if any(keyword in text for keyword in keywords)) if keywords else 0
    type_match_score = 15 if not keywords else 10 if matched_chunks == 0 else 20 if matched_chunks == 1 else 25

    readiness_score = chunk_count_score + diversity_score + temporal_score + type_match_score
    abstract_only = bool(supporting_chunks) and all(_is_abstract_chunk(chunk) for chunk in supporting_chunks)
    if abstract_only:
        # Keep abstract evidence usable for targeted completion while preventing
        # a one-paper abstract from reaching the same readiness as full text.
        readiness_score = min(readiness_score, 60)
    scores = {
        "chunk_count": chunk_count_score,
        "diversity": diversity_score,
        "temporal": temporal_score,
        "type_match": type_match_score,
    }
    bottleneck_axis = min(scores, key=scores.get)
    status = str(binding_status or "").lower()
    confidence = str(binding_confidence or "").lower()
    decision_basis = "axis_score"

    if chunk_count == 0 or status in {"insufficient", "contradicted"}:
        action = "block"
        decision_basis = "no_usable_bound_evidence"
    elif missing:
        action = "supplement"
        decision_basis = "essential_claim_component_missing"
    elif status == "synthesized" and unique_papers >= 2 and confidence in {"high", "medium"}:
        action = "proceed"
        decision_basis = "independent_sources_collectively_cover_claim"
    elif status == "direct" and confidence in {"high", "medium"}:
        if broad_claim and unique_papers < 3:
            action = "supplement"
            decision_basis = "broad_claim_needs_wider_source_diversity"
        elif load_bearing and unique_papers < 2:
            action = "supplement"
            decision_basis = "load_bearing_claim_needs_second_source"
        else:
            action = "proceed"
            decision_basis = "direct_source_grounding_sufficient_for_specific_claim"
    elif status == "partial":
        action = "supplement"
        decision_basis = "partial_support_needs_targeted_completion"
    elif readiness_score >= 75:
        action = "proceed"
    elif readiness_score >= 50:
        action = "supplement"
    else:
        action = "block"

    if abstract_only and action == "proceed":
        action = "supplement"
        decision_basis = "abstract_only_evidence_provisional"

    if action == "proceed":
        bottleneck_axis = "none"

    return {
        "readiness_score": readiness_score,
        "chunk_count_score": chunk_count_score,
        "diversity_score": diversity_score,
        "temporal_score": temporal_score,
        "type_match_score": type_match_score,
        "bottleneck_axis": bottleneck_axis,
        "action": action,
        "decision_basis": decision_basis,
        "details": {
            "chunk_count": chunk_count,
            "unique_papers": unique_papers,
            "temporal_span_years": temporal_span,
            "years_range": f"{min(years)}-{max(years)}" if years else "N/A",
            "broad_claim": broad_claim,
            "missing_component_count": len(missing),
            "binding_status": status,
            "binding_confidence": confidence,
            "abstract_only": abstract_only,
        },
    }
