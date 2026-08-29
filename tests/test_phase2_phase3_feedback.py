from __future__ import annotations

from optomind_research.runtime.phase2_phase3_feedback import (
    BoundedWaveBudget,
    build_query_component_map,
    deduplicate_material_records,
    deduplicate_route_records,
    select_next_wave_request,
)


def test_bounded_wave_budget_never_exceeds_total() -> None:
    budget = BoundedWaveBudget(per_wave=3, total=5, max_waves=2)
    assert budget.valid()
    assert budget.budget_for_wave(1, 0) == 3
    assert budget.budget_for_wave(2, 3) == 2
    assert budget.budget_for_wave(3, 5) == 0
    assert budget.budget_for_wave(2, 5) == 0


def test_query_component_map_preserves_scientific_intent() -> None:
    request = {
        "queries": [
            "jordan canonical form non-hermitian exceptional points",
            "branch point resolvent riemann surface exceptional points",
        ],
        "missing_claim_ids": ["S01-C02", "S01-C04"],
    }
    claims = [
        {"claim_id": "S01-C02", "missing_evidence_components": ["Jordan canonical form block size"]},
        {"claim_id": "S01-C04", "missing_evidence_components": ["Branch-point singularity of the resolvent operator"]},
    ]
    mapping = build_query_component_map(request, claims)
    assert mapping[request["queries"][0]]
    assert mapping[request["queries"][1]]
    assert any("Jordan" in item for item in mapping[request["queries"][0]])
    assert any("Branch" in item for item in mapping[request["queries"][1]])
    assert all(
        "Branch" not in item
        for item in mapping[request["queries"][0]]
    )


def test_duplicate_candidate_across_waves_is_one_material_identity() -> None:
    rows = [
        {
            "wave": 1,
            "candidate_id": "cand_old",
            "doi": "10.1103/pf6y-lxzp",
            "paper_id": "doi:10.1103/pf6y-lxzp",
            "chunk_ids": ["doi:10.1103/pf6y-lxzp:0001", "doi:10.1103/pf6y-lxzp:0002"],
            "new_chunk_ids": ["doi:10.1103/pf6y-lxzp:0001", "doi:10.1103/pf6y-lxzp:0002"],
            "new_paper": True,
            "new_chunks": 2,
            "paper_row_inserted": True,
        },
        {
            "wave": 2,
            "candidate_id": "cand_old",
            "doi": "doi:10.1103/pf6y-lxzp",
            "paper_id": "doi:10.1103/pf6y-lxzp",
            "chunk_ids": ["doi:10.1103/pf6y-lxzp:0001", "doi:10.1103/pf6y-lxzp:0002"],
            "new_chunk_ids": [],
            "new_paper": True,
            "new_chunks": 0,
        },
    ]
    result = deduplicate_material_records(rows)
    assert result["input_count"] == 2
    assert result["unique_count"] == 1
    merged = result["records"][0]
    assert merged["new_paper"] is True
    assert merged["new_chunks"] == 2
    assert merged["waves"] == [1, 2]


def test_route_audit_deduplicates_two_wave_paper_and_chunk_receipts() -> None:
    rows = []
    for wave in (1, 2):
        rows.append({
            "wave": wave,
            "candidate_id": "cand_same",
            "doi": "10.1103/pf6y-lxzp",
            "paper_id": "doi:10.1103/pf6y-lxzp",
            "discovery_route": "semantic_scholar",
            "materialization_route": "legal_oa_fulltext",
            "acquisition_status": "fulltext",
        })
        for index in range(47):
            rows.append({
                "wave": wave,
                "candidate_id": "cand_same",
                "doi": "10.1103/pf6y-lxzp",
                "paper_id": "doi:10.1103/pf6y-lxzp",
                "chunk_id": f"chunk-{index:04d}",
                "discovery_route": "semantic_scholar",
                "materialization_route": "legal_oa_fulltext",
                "acquisition_status": "fulltext",
            })
    result = deduplicate_route_records(rows)
    assert result["input_count"] == 96
    assert result["unique_count"] == 48
    assert result["duplicates_removed"] == 48
    assert len(result["records"]) == len({
        (row.get("paper_id"), row.get("chunk_id")) for row in result["records"]
    })


def test_no_progress_escalates_to_another_unresolved_component() -> None:
    request = {
        "queries": ["jordan block exceptional point", "riemann sheets exceptional point"],
        "query_targets": [
            {"query": "jordan block exceptional point", "missing_components": ["Jordan block"]},
            {"query": "riemann sheets exceptional point", "missing_components": ["Riemann sheets"]},
        ],
    }
    next_request = select_next_wave_request(request, blocked_components=["Jordan block"])
    assert next_request["queries"] == ["riemann sheets exceptional point"]
    assert next_request["wave_target_components"] == ["Riemann sheets"]
    exhausted = select_next_wave_request(request, blocked_components=["Jordan block", "Riemann sheets"])
    assert exhausted["queries"] == []
    assert exhausted["stop_reason"] == "bounded_novel_components_exhausted"


def test_closed_loop_qwen_cost_counts_inclusive_phase3_judge_once() -> None:
    from scripts.run_phase2_phase3_closed_loop_acceptance import (
        _aggregate_qwen_metrics,
    )

    waves = [{
        "qwen": {"qwen_calls": 1},
        "phase2_totals": {
            "input_tokens": 100,
            "output_tokens": 20,
            "estimated_cost_cny": 0.01,
        },
    }]
    phase3_run = {
        "llm": {
            "calls_observed_or_estimated": 1,
            "input_tokens": 200,
            "output_tokens": 40,
            "estimated_cost_cny": 0.02,
        },
        "fresh_evidence_semantic_judge": {
            "api_call_count": 1,
            "input_tokens": 200,
            "output_tokens": 40,
            "estimated_cost_cny": 0.02,
            "included_once_in_llm_aggregate": True,
        },
    }
    metrics = _aggregate_qwen_metrics(waves, phase3_run)
    assert metrics["calls"] == 2
    assert metrics["input_tokens"] == 300
    assert metrics["output_tokens"] == 60
    assert metrics["estimated_cost_cny"] == 0.03
    assert metrics["phase3_llm_is_inclusive"] is True
    assert metrics["fresh_evidence_judge_counted_separately"] is False


def test_cumulative_insertion_telemetry_uses_authoritative_counts() -> None:
    from scripts.run_phase2_phase3_closed_loop_acceptance import (
        _aggregate_backend_metrics,
    )

    waves = [
        {"backends": {
            "oa_resolution_probes": 1,
            "candidate_attempts": 1,
            "newly_inserted_papers": 1,
            "newly_inserted_chunks": 43,
        }},
        {"backends": {
            "oa_resolution_probes": 1,
            "candidate_attempts": 1,
            "newly_inserted_papers": 2,
            "newly_inserted_chunks": 90,
        }},
    ]
    aggregate = _aggregate_backend_metrics(
        waves,
        {"paper_count": 2, "chunk_count": 90},
    )
    assert aggregate["oa_resolution_probes"] == 2
    assert aggregate["candidate_attempts"] == 2
    assert aggregate["newly_inserted_papers"] == 2
    assert aggregate["newly_inserted_chunks"] == 90
    fallback = _aggregate_backend_metrics(waves)
    assert fallback["newly_inserted_papers"] == 2
    assert fallback["newly_inserted_chunks"] == 90


def test_new_material_reporting_separates_success_failure_and_reuse() -> None:
    from scripts.run_phase2_phase3_closed_loop_acceptance import (
        _material_paper_receipt_summary,
    )

    rows = [
        {
            "candidate_id": "success-a",
            "paper_id": "paper-a",
            "doi": "10.1000/a",
            "new_paper": True,
            "new_chunks": 4,
            "acquisition_status": "fulltext",
        },
        {
            "candidate_id": "success-a-wave-2",
            "paper_id": "paper-a",
            "doi": "10.1000/a",
            "new_paper": True,
            "new_chunks": 2,
            "acquisition_status": "fulltext",
        },
        {
            "candidate_id": "failed-b",
            "paper_id": "paper-b",
            "doi": "10.1000/b",
            "new_paper": False,
            "new_chunks": 0,
            "acquisition_status": "failed",
        },
        {
            "candidate_id": "reused-c",
            "paper_id": "paper-c",
            "doi": "10.1000/c",
            "new_paper": False,
            "new_chunks": 0,
            "reused_chunks": 3,
            "acquisition_status": "fulltext",
        },
        {
            "candidate_id": "zero-chunk-d",
            "paper_id": "paper-d",
            "doi": "10.1000/d",
            "new_paper": True,
            "new_chunks": 0,
            "acquisition_status": "fulltext",
        },
    ]
    summary = _material_paper_receipt_summary(rows)
    assert summary["paper_count"] == 1
    assert len(summary["successful_papers"]) == 1
    assert [item["paper_id"] for item in summary["failed_attempts"]] == [
        "paper-b"
    ]
    assert {item["paper_id"] for item in summary["reused_attempts"]} == {
        "paper-c", "paper-d"
    }
    assert summary["attempt_receipt_count"] == 5
    assert summary["failed_attempt_count"] == 1
    assert summary["reused_attempt_count"] == 2


def test_coverage_reporting_separates_full_closure_from_narrowing() -> None:
    from scripts.run_phase2_phase3_closed_loop_acceptance import (
        _coverage_change_summary,
    )

    before = {
        "missing_components": [
            "Fully established proposition",
            "Bandwidth is at least 10 nm",
            "Still unresolved proposition",
        ]
    }
    after = {
        "missing_components": [
            "Unverified numeric constraint: 10 nm",
            "Still unresolved proposition",
        ]
    }
    claim_graph = {"claims": [{
        "fresh_component_audit": [
            {
                "requested_component": "Fully established proposition",
                "status": "supported",
                "residual_components": [],
                "chunk_ids": ["chunk-full"],
            },
            {
                "requested_component": "Bandwidth is at least 10 nm",
                "status": "partially_supported",
                "residual_components": [
                    "Unverified numeric constraint: 10 nm"
                ],
                "chunk_ids": ["chunk-partial"],
            },
        ]
    }]}
    result = _coverage_change_summary(before, after, claim_graph)
    assert result["fully_closed_components"] == [
        "Fully established proposition"
    ]
    assert result["missing_components_closed"] == result[
        "fully_closed_components"
    ]
    assert result["narrowed_components"] == [{
        "original_component": "Bandwidth is at least 10 nm",
        "support_state": "partially_supported",
        "residual_components": ["Unverified numeric constraint: 10 nm"],
        "supporting_chunk_ids": ["chunk-partial"],
    }]
    assert result["unresolved_components"] == [
        "Unverified numeric constraint: 10 nm",
        "Still unresolved proposition",
    ]


def test_offline_reconciliation_never_reduces_recorded_qwen_usage() -> None:
    from scripts.run_phase2_phase3_closed_loop_acceptance import (
        _merge_offline_qwen_metrics,
    )

    merged = _merge_offline_qwen_metrics(
        {
            "calls": 3,
            "input_tokens": 5119,
            "output_tokens": 1071,
            "estimated_cost_cny": 0.002058,
            "token_provenance": "provider_reported_or_clearly_estimated",
        },
        {
            "calls": 2,
            "input_tokens": 2111,
            "output_tokens": 0,
            "estimated_cost_cny": 0.0,
            "token_provenance": "runtime_record",
        },
    )
    assert merged["calls"] == 3
    assert merged["input_tokens"] == 5119
    assert merged["output_tokens"] == 1071
    assert merged["estimated_cost_cny"] == 0.002058
    assert merged["token_provenance"] == (
        "provider_reported_or_clearly_estimated"
    )
