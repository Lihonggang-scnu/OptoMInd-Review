"""U1: Claim Scope Checker — 文献综述版 Scoop-Check for M2b integration.

Scoop-Check Axes (literature review adapted):
- scope_axis: in-scope / boundary / out-of-scope
- evidence_demand: high / medium / low
- retrieval_urgency: urgent / defer / skip
- kb_coverage_verdict: sufficient / supplement / retrieve

Design Constraints:
- Pure Python (no LLM)
- Domain-agnostic keyword matching + heuristics
- Fast: <50ms per claim
- Conservative: when uncertain, default to 'in-scope + supplement'
"""

from __future__ import annotations

from typing import Any


def check_claim_scope(
    *,
    claim_text: str,
    supporting_chunk_ids: list[str],
    scope_definition: str = "",
    kb_total_chunks: int = 0,
    claim_type: str = "",
) -> dict[str, Any]:
    """Evaluate claim scope and evidence readiness for M2b filtering.

    Args:
        claim_text: The claim statement
        supporting_chunk_ids: Current supporting chunk IDs
        scope_definition: User-defined scope (from blueprint)
        kb_total_chunks: Total chunks in KB (for coverage estimation)
        claim_type: mechanism / application / comparison / review (optional hint)

    Returns:
        {
            "scope_axis": "in-scope" | "boundary" | "out-of-scope",
            "evidence_demand": "high" | "medium" | "low",
            "retrieval_urgency": "urgent" | "defer" | "skip",
            "kb_coverage_verdict": "sufficient" | "supplement" | "retrieve",
            "action": "proceed_to_dag" | "trigger_m3" | "reject",
            "reasoning": "brief explanation",
        }
    """
    claim_lower = claim_text.lower()
    scope_lower = scope_definition.lower()
    chunk_count = len(supporting_chunk_ids)

    # ── Axis 1: Scope ──
    # Check if claim keywords overlap with scope_definition
    scope_axis = "in-scope"
    if scope_definition:
        # Extract keywords from scope (simplified: split on common delimiters)
        scope_keywords = set(w for w in scope_lower.split() if len(w) > 3)
        claim_keywords = set(w for w in claim_lower.split() if len(w) > 3)
        overlap = scope_keywords & claim_keywords
        if len(overlap) >= 2:
            scope_axis = "in-scope"
        elif len(overlap) == 1:
            scope_axis = "boundary"
        else:
            # Check for out-of-scope markers
            oos_markers = ["not related", "beyond scope", "excluded", "outside", "unrelated"]
            if any(marker in claim_lower for marker in oos_markers):
                scope_axis = "out-of-scope"
            else:
                scope_axis = "boundary"  # uncertain, default to boundary
    else:
        # No scope definition → assume in-scope
        scope_axis = "in-scope"

    # ── Axis 2: Evidence Demand ──
    # High: mechanism, theory, quantitative claims
    # Medium: comparison, synthesis
    # Low: definition, review-level meta-claims
    evidence_demand = "medium"
    high_markers = ["mechanism", "theory", "formula", "equation", "measure", "quantitative", "experiment"]
    low_markers = ["definition", "overview", "background", "introduction", "survey", "review framework"]

    if any(marker in claim_lower for marker in high_markers):
        evidence_demand = "high"
    elif any(marker in claim_lower for marker in low_markers):
        evidence_demand = "low"
    else:
        evidence_demand = "medium"

    # ── Axis 3: Retrieval Urgency ──
    # Urgent: high-demand claims with < 3 chunks
    # Defer: medium-demand or sufficient chunks (>= 3)
    # Skip: low-demand or out-of-scope
    retrieval_urgency = "defer"
    if scope_axis == "out-of-scope":
        retrieval_urgency = "skip"
    elif evidence_demand == "high" and chunk_count < 3:
        retrieval_urgency = "urgent"
    elif evidence_demand == "low" or chunk_count >= 5:
        retrieval_urgency = "defer"
    else:
        retrieval_urgency = "defer"

    # ── Axis 4: KB Coverage Verdict ──
    # Sufficient: >= 3 chunks for medium/low demand, >= 5 for high demand
    # Supplement: 1-2 chunks, need more
    # Retrieve: 0 chunks
    kb_coverage_verdict = "supplement"
    if chunk_count == 0:
        kb_coverage_verdict = "retrieve"
    elif evidence_demand == "high" and chunk_count >= 5:
        kb_coverage_verdict = "sufficient"
    elif evidence_demand in {"medium", "low"} and chunk_count >= 3:
        kb_coverage_verdict = "sufficient"
    elif chunk_count >= 1:
        kb_coverage_verdict = "supplement"
    else:
        kb_coverage_verdict = "retrieve"

    # ── Final Action ──
    action = "proceed_to_dag"
    if scope_axis == "out-of-scope":
        action = "reject"
    elif retrieval_urgency == "urgent" or kb_coverage_verdict == "retrieve":
        action = "trigger_m3"
    elif kb_coverage_verdict == "supplement":
        action = "trigger_m3"  # supplement = need M3
    else:
        action = "proceed_to_dag"

    # ── Reasoning ──
    reasoning = f"Scope: {scope_axis}, Evidence: {evidence_demand}, Chunks: {chunk_count}, Coverage: {kb_coverage_verdict}"

    return {
        "scope_axis": scope_axis,
        "evidence_demand": evidence_demand,
        "retrieval_urgency": retrieval_urgency,
        "kb_coverage_verdict": kb_coverage_verdict,
        "action": action,
        "reasoning": reasoning,
    }
