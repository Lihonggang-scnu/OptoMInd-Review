#!/usr/bin/env python3
"""Phase 2 → Phase 3 closed-loop acceptance helpers.

These functions are used by the closed-loop acceptance tests to aggregate
metrics across multiple retrieval waves and a Phase-3 evidence-binding run.
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Qwen LLM usage aggregation
# ---------------------------------------------------------------------------

def _aggregate_qwen_metrics(
    waves: list[dict],
    phase3_run: Optional[dict] = None,
) -> dict[str, Any]:
    """Aggregate Qwen token usage across retrieval waves and an optional Phase-3 run.

    When phase3_run's semantic judge is flagged ``included_once_in_llm_aggregate``,
    the LLM block is counted once (judge already merged in); otherwise the judge
    is tallied separately.
    """
    calls = 0
    input_tokens = 0
    output_tokens = 0
    cost = 0.0

    for wave in waves:
        q = wave.get("qwen") or {}
        t = wave.get("phase2_totals") or {}
        calls += q.get("qwen_calls", 0)
        input_tokens += t.get("input_tokens", 0)
        output_tokens += t.get("output_tokens", 0)
        cost += t.get("estimated_cost_cny", 0.0)

    phase3_inclusive = False
    judge_separate = False

    if phase3_run:
        llm = phase3_run.get("llm") or {}
        judge = phase3_run.get("fresh_evidence_semantic_judge") or {}
        if judge.get("included_once_in_llm_aggregate"):
            # Judge tokens already merged into llm block — count llm once
            calls += llm.get("calls_observed_or_estimated", 0)
            input_tokens += llm.get("input_tokens", 0)
            output_tokens += llm.get("output_tokens", 0)
            cost += llm.get("estimated_cost_cny", 0.0)
            phase3_inclusive = True
            judge_separate = False
        else:
            calls += (
                llm.get("calls_observed_or_estimated", 0)
                + judge.get("api_call_count", 0)
            )
            input_tokens += llm.get("input_tokens", 0) + judge.get("input_tokens", 0)
            output_tokens += llm.get("output_tokens", 0) + judge.get("output_tokens", 0)
            cost += llm.get("estimated_cost_cny", 0.0) + judge.get("estimated_cost_cny", 0.0)
            phase3_inclusive = True
            judge_separate = True

    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_cny": round(cost, 10),
        "phase3_llm_is_inclusive": phase3_inclusive,
        "fresh_evidence_judge_counted_separately": judge_separate,
    }


# ---------------------------------------------------------------------------
# Backend materialization metrics
# ---------------------------------------------------------------------------

def _aggregate_backend_metrics(
    waves: list[dict],
    authoritative: Optional[dict] = None,
) -> dict[str, Any]:
    """Aggregate backend materialization counts across waves.

    For newly_inserted_papers and newly_inserted_chunks, the authoritative
    dict (from the KB post-insertion snapshot) is used when provided.  Without
    it the maximum per-wave value is used as a deduplication-aware fallback
    (waves may report cumulative totals so taking the max avoids double-counting).
    """
    agg: dict[str, int] = {
        "oa_resolution_probes": 0,
        "candidate_attempts": 0,
        "newly_inserted_papers": 0,
        "newly_inserted_chunks": 0,
    }
    max_papers = 0
    max_chunks = 0

    for wave in waves:
        b = wave.get("backends") or {}
        for k in ("oa_resolution_probes", "candidate_attempts"):
            agg[k] += b.get(k, 0)
        max_papers = max(max_papers, b.get("newly_inserted_papers", 0))
        max_chunks = max(max_chunks, b.get("newly_inserted_chunks", 0))

    if authoritative:
        agg["newly_inserted_papers"] = authoritative.get(
            "paper_count", agg["newly_inserted_papers"]
        )
        agg["newly_inserted_chunks"] = authoritative.get(
            "chunk_count", agg["newly_inserted_chunks"]
        )
    else:
        agg["newly_inserted_papers"] = max_papers
        agg["newly_inserted_chunks"] = max_chunks

    return agg


# ---------------------------------------------------------------------------
# Paper receipt summary
# ---------------------------------------------------------------------------

def _material_paper_receipt_summary(rows: list[dict]) -> dict[str, Any]:
    """Summarise per-attempt materialization receipts.

    A paper is "successful" only when it has new_paper=True AND new_chunks > 0;
    duplicate attempts for the same paper_id are collapsed.  Attempts with
    acquisition_status="failed" are catalogued separately.  All other attempts
    (reused chunks, zero new chunks, etc.) are "reused".
    """
    seen_success: dict[str, dict] = {}  # paper_id → first successful row
    failed: list[dict] = []
    reused: list[dict] = []

    for row in rows:
        pid = row.get("paper_id", "")
        is_new = bool(row.get("new_paper"))
        new_chunks = int(row.get("new_chunks") or 0)
        status = row.get("acquisition_status", "")

        if status == "failed":
            failed.append(row)
        elif is_new and new_chunks > 0:
            # Only count a paper as successful once (deduplicate by paper_id).
            # Duplicate attempts for the same already-successful paper are
            # silently dropped — they are neither counted as new successes nor
            # as reused attempts, because the paper is already accounted for.
            if pid not in seen_success:
                seen_success[pid] = row
        else:
            reused.append(row)

    successful = list(seen_success.values())

    return {
        "paper_count": len(successful),
        "successful_papers": successful,
        "failed_attempts": failed,
        "reused_attempts": reused,
        "attempt_receipt_count": len(rows),
        "failed_attempt_count": len(failed),
        "reused_attempt_count": len(reused),
    }


# ---------------------------------------------------------------------------
# Coverage change summary
# ---------------------------------------------------------------------------

def _coverage_change_summary(
    before: dict,
    after: dict,
    claim_graph: Optional[dict] = None,
) -> dict[str, Any]:
    """Summarise which missing evidence components were resolved by new material.

    - fully_closed_components: were in before, absent from after, status="supported"
    - narrowed_components: were in before, still partially present, status="partially_supported"
    - unresolved_components: remain in after's missing_components list
    """
    before_missing: list[str] = list(before.get("missing_components") or [])
    after_missing: list[str] = list(after.get("missing_components") or [])
    after_missing_set = set(after_missing)

    # Build audit lookup from claim_graph
    audit_by_component: dict[str, dict] = {}
    if claim_graph:
        for claim in claim_graph.get("claims") or []:
            for entry in claim.get("fresh_component_audit") or []:
                comp = entry.get("requested_component", "")
                if comp:
                    audit_by_component[comp] = entry

    fully_closed: list[str] = []
    narrowed: list[dict] = []

    for comp in before_missing:
        if comp in after_missing_set:
            continue  # still unresolved
        audit = audit_by_component.get(comp, {})
        support = audit.get("status", "")
        residual = audit.get("residual_components") or []
        if support == "supported" and not residual:
            fully_closed.append(comp)
        elif support in ("partially_supported", "narrowed") or residual:
            narrowed.append(
                {
                    "original_component": comp,
                    "support_state": support or "partially_supported",
                    "residual_components": residual,
                    "supporting_chunk_ids": audit.get("chunk_ids") or [],
                }
            )

    return {
        "fully_closed_components": fully_closed,
        "missing_components_closed": fully_closed,
        "narrowed_components": narrowed,
        "unresolved_components": after_missing,
    }


# ---------------------------------------------------------------------------
# Offline Qwen metric reconciliation
# ---------------------------------------------------------------------------

def _merge_offline_qwen_metrics(source: dict, offline: dict) -> dict[str, Any]:
    """Merge two Qwen metric records, never reducing any recorded value.

    Prefers the higher value for each numeric field; uses source's provenance
    when it differs.
    """
    numeric_keys = ("calls", "input_tokens", "output_tokens", "estimated_cost_cny")
    merged: dict[str, Any] = {}
    for k in numeric_keys:
        sv = source.get(k, 0) or 0
        ov = offline.get(k, 0) or 0
        merged[k] = sv if sv >= ov else ov
    merged["token_provenance"] = (
        source.get("token_provenance") or offline.get("token_provenance") or "unknown"
    )
    return merged
