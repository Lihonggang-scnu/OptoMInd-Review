from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.phase3_argument_orchestrator import (
    Phase3ArgumentOrchestrator,
    _llm_audit_summary,
    _partition_claim_lanes,
)
from optomind_research.runtime.section_authoring_tool_registry import (
    _build_asset_graph,
    _build_authoring_evidence_portfolio,
)
from optomind_research.runtime.tool_provider import SectionAuthoringContext


def _strong_pool_fixture(tmp_path: Path, *, chunk_count: int = 200) -> dict:
    kb = tmp_path / "shared.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, "
            "text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?)",
            [
                (
                    f"c{index:03d}",
                    f"p{index % 40:03d}",
                    f"Study {index % 40:03d}",
                    (
                        f"Evidence passage {index:03d} reports a distinct condition, "
                        "measured outcome, limitation, and comparison boundary."
                    ),
                    "fulltext",
                    "fulltext",
                )
                for index in range(chunk_count)
            ],
        )
        conn.commit()

    sources = []
    for paper_index in range(40):
        chunk_ids = [
            f"c{index:03d}"
            for index in range(chunk_count)
            if index % 40 == paper_index
        ]
        if not chunk_ids:
            continue
        sources.append(
            {
                "paper_id": f"p{paper_index:03d}",
                "title": f"Study {paper_index:03d}",
                "canonical_chunk_ids": chunk_ids,
                "literature_role": "comparison",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "context_complete": True,
                "acquisition_status": "fulltext",
                "discovery_route": "semantic_scholar",
                "materialization_route": "parsed_fulltext",
            }
        )
    ledger = tmp_path / "shared_ledger.json"
    ledger.write_text(json.dumps({"sources": sources}), encoding="utf-8")

    overlay = tmp_path / "S07_overlay.json"
    overlay.write_text(
        json.dumps(
            {
                "paper_ids": ["p000"],
                "chunk_ids": ["c000"],
                "paper_overrides": {
                    "p039": {"use_permission": "contextual_or_qualified_support"}
                },
                "chunk_overrides": {
                    "c199": {"use_permission": "contextual_or_qualified_support"}
                },
            }
        ),
        encoding="utf-8",
    )
    section = {
        "section_id": "S07",
        "title": "Decision framework for choosing among alternative methods",
        "argument_role": "Compare alternatives and support conditional selection.",
        "core_question": "Which method should be selected under different constraints?",
        "central_judgment": "The appropriate choice depends on conditions and trade-offs.",
        "argument_tasks": [
            {
                "description": (
                    "Compare alternatives by applicability, cost, performance boundary, "
                    "and evidence strength."
                )
            }
        ],
        "claims": [],
    }
    return {
        "section": section,
        "blueprint": {"sections": [section]},
        "ledger": ledger,
        "kb": kb,
        "overlay": overlay,
    }


def _fake_claim_pool_chat(calls: list[dict]):
    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        batch = payload["batch"]
        slot = batch["claim_proposal_ids"][0]
        return {
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "claim_proposal_id": slot,
                            "statement": (
                                f"Evidence batch {batch['batch_id']} establishes a distinct "
                                "conditional comparison with a measured performance boundary."
                            ),
                            "evidence_type": "comparison",
                            "supporting_text_refs": ["T01"],
                            "relation_roles": ["support", "boundary_condition"],
                            "importance": "load_bearing",
                            "saturation_score": 1.5,
                        }
                    ]
                }
            ),
            "_llm_usage": {
                "success": True,
                "input_tokens": 20,
                "output_tokens": 10,
                "model_name": "qwen3.7-flash",
                "mock_llm": False,
            },
        }

    return fake_chat


def test_strong_pool_reads_200_chunks_in_17_batches_without_inventory_in_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = _strong_pool_fixture(tmp_path)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths={"S07": fixture["overlay"]},
        output_dir=tmp_path / "phase3",
        real_llm_claims=True,
        claim_pool_enabled=True,
        execute_coverage=False,
    )
    state = orchestrator._prepare_section(
        fixture["section"], 0, [fixture["section"]]
    )

    assert len(state["claim_pool_records"]) == 200
    assert len(state["candidate_evidence_digest"]["batches"]) == 17
    assert len(state["graph"].chunks) == 200
    assert state["claim_pool_global_expansion"]["added_chunk_count"] == 199
    assert state["graph"].chunks["c199"].use_permission == (
        "contextual_or_qualified_support"
    )

    model_pool = state["m2a_input_payload"]["section_contract"][
        "candidate_material_pool"
    ]
    assert model_pool["inventory_chunk_count"] == 200
    assert "chunk_ids" not in model_pool
    assert "paper_ids" not in model_pool
    assert "served_claim_pool_chunk_ids" not in model_pool
    decision = state["m2a_input_payload"]["section_contract"][
        "argument_structure"
    ]["decision_framework"]
    assert decision["comparison_dimensions"] == [
        "applicability_conditions",
        "cost_and_resource_demands",
        "performance_boundaries",
        "evidence_type_and_strength",
    ]
    assert orchestrator._context_handoff_audit(state)["passed"] is True

    import optomind_research.claim_decomposer as decomposer_module

    calls: list[dict] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_claim_pool_chat(calls)
    )
    monkeypatch.setattr(
        decomposer_module.ClaimDecomposer,
        "_verify_and_arbitrate",
        lambda self, section, claims, text_chunks, repair=None: claims,
    )
    orchestrator._decompose_claims(state)

    assert len(calls) == 17
    assert all(len(call["batch"]["chunk_index"]) <= 12 for call in calls)
    assert all(
        "chunk_ids"
        not in call["section_contract"]["candidate_material_pool"]
        for call in calls
    )
    audit = state["claim_pool_runtime_audit"]
    assert audit["inventory_chunk_count"] == 200
    assert audit["served_claim_pool_chunk_count"] == 200
    assert audit["claim_pool_batch_count"] == 17
    assert audit["completed_batch_count"] == 17
    assert audit["successful_call_batch_count"] == 17
    assert audit["parsed_batch_count"] == 17
    assert audit["productive_batch_count"] == 17
    assert audit["chunks_submitted_to_successful_calls_count"] == 200
    assert audit["chunks_in_parsed_batches_count"] == 200
    assert audit["chunks_cited_by_candidate_claims_count"] == 17
    assert audit["actual_model_read_chunk_count"] == 200
    assert audit["candidate_claim_count"] > 0
    assert audit["selected_claim_count"] > 0
    assert audit["legacy_single_call_used"] is False
    assert audit["integrity_passed"] is True
    assert state["llm_audit"]["formal_verification_deferred"] is True
    llm_summary = _llm_audit_summary([state])
    assert llm_summary["calls_observed_or_estimated"] == 17
    assert llm_summary["input_tokens"] == 340
    assert llm_summary["output_tokens"] == 170
    assert llm_summary["estimated_cost_cny"] > 0
    orchestrator._bind_section(state, [])
    rebound_audit = state["claim_pool_runtime_audit"]
    assert (
        rebound_audit["authorable_claim_count"]
        + rebound_audit["evidence_gap_claim_count"]
        == len(state["claims"])
    )
    assert set(state["section"]["claim_lanes"]) >= {
        "authorable_claim_ids",
        "evidence_gap_claim_ids",
    }


def test_cost_aware_high_coverage_profile_supports_250_chunks_and_18_core(
    tmp_path: Path, monkeypatch
) -> None:
    """The opt-in larger profile expands upstream reading without requiring it."""

    fixture = _strong_pool_fixture(tmp_path, chunk_count=250)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths={"S07": fixture["overlay"]},
        output_dir=tmp_path / "phase3_high_coverage",
        real_llm_claims=True,
        claim_pool_enabled=True,
        claim_pool_served_limit=250,
        claim_pool_target_range=[110, 150],
        claim_pool_shortlist_limit=50,
        authoring_core_chunk_limit=18,
        execute_coverage=False,
    )
    state = orchestrator._prepare_section(
        fixture["section"], 0, [fixture["section"]]
    )

    assert len(state["claim_pool_records"]) == 250
    assert len(state["candidate_evidence_digest"]["batches"]) == 21
    assert orchestrator.claim_pool_target_range == [110, 150]
    assert orchestrator.claim_pool_shortlist_limit == 50

    import optomind_research.claim_decomposer as decomposer_module

    calls: list[dict] = []
    monkeypatch.setattr(
        decomposer_module, "call_qwen_chat", _fake_claim_pool_chat(calls)
    )
    monkeypatch.setattr(
        decomposer_module.ClaimDecomposer,
        "_verify_and_arbitrate",
        lambda self, section, claims, text_chunks, repair=None: claims,
    )
    orchestrator._decompose_claims(state)

    assert len(calls) == 21
    assert state["claim_pool_runtime_audit"]["served_claim_pool_chunk_count"] == 250
    assert state["claim_pool_runtime_audit"]["claim_pool_batch_count"] == 21
    assert state["section"]["candidate_claim_pool_shortlist_audit"][
        "selection_limit"
    ] == 50

    orchestrator._bind_section(state, [])
    assert len(state["portfolio"].core_chunk_ids) == 18

    authoring_ctx = SectionAuthoringContext(
        section_id="S07",
        section_data={
            **fixture["section"],
            "authoring_core_chunk_limit": 18,
        },
        kb_sqlite=fixture["kb"],
        temp_kb_sqlite=None,
        work_dir=tmp_path / "authoring",
        source_ledger_path=fixture["ledger"],
    )
    authoring_graph = _build_asset_graph(authoring_ctx)
    authoring_portfolio = _build_authoring_evidence_portfolio(
        authoring_ctx, authoring_graph
    )
    assert len(authoring_portfolio["recommended_batch_chunk_ids"]) == 18


def test_global_dag_projection_is_parameterized_per_section_and_total(
    tmp_path: Path,
) -> None:
    fixture = _strong_pool_fixture(tmp_path, chunk_count=40)
    section_a = dict(fixture["section"], section_id="S01")
    section_b = dict(fixture["section"], section_id="S02")
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint={"sections": [section_a, section_b]},
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        output_dir=tmp_path / "phase3_dag_projection",
        real_llm_claims=False,
        claim_pool_enabled=False,
        dag_claims_per_section=12,
        dag_total_claims=16,
    )
    states = [
        {
            "section": section_a,
            "claims": [
                {
                    "claim_id": f"S01-C{i}",
                    "section_id": "S01",
                    "statement": "A sufficiently detailed claim statement.",
                    "load_bearing": True,
                    "support_classification": "supported",
                    "saturation_score": 1.0,
                }
                for i in range(20)
            ],
        },
        {
            "section": section_b,
            "claims": [
                {
                    "claim_id": f"S02-C{i}",
                    "section_id": "S02",
                    "statement": "A sufficiently detailed claim statement.",
                    "load_bearing": True,
                    "support_classification": "supported",
                    "saturation_score": 1.0,
                }
                for i in range(20)
            ],
        },
    ]
    projected, omitted, audit = orchestrator._project_claims_for_global_dag(states)
    assert len(projected) == 16
    assert len(omitted) == 24
    assert audit["claims_per_section"] == 12
    assert audit["total_claims"] == 16


def test_claim_lanes_keep_open_questions_out_of_authorable_claims() -> None:
    authorable, gaps = _partition_claim_lanes(
        [
            {"claim_id": "C1", "support_classification": "supported"},
            {"claim_id": "C2", "support_classification": "qualified"},
            {"claim_id": "C3", "support_classification": "open_question"},
            {"claim_id": "C4", "support_classification": "unsupported"},
        ]
    )
    assert [item["claim_id"] for item in authorable] == ["C1", "C2"]
    assert [item["claim_id"] for item in gaps] == ["C3", "C4"]


def test_direct_real_llm_fixture_can_explicitly_keep_legacy_single_call(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = _strong_pool_fixture(tmp_path, chunk_count=5)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths={"S07": fixture["overlay"]},
        output_dir=tmp_path / "legacy",
        real_llm_claims=True,
        claim_pool_enabled=False,
    )
    state = orchestrator._prepare_section(
        fixture["section"], 0, [fixture["section"]]
    )

    def fake_decompose(self, section):
        self.last_audit = {"legacy_single_call_used": True}
        return [
            {
                "claim_id": "S07-C01",
                "statement": "A bounded comparison supports a conditional choice rule.",
                "importance": "supporting",
            }
        ]

    monkeypatch.setattr(
        "optomind_research.runtime.phase3_argument_orchestrator.ClaimDecomposer.decompose_section",
        fake_decompose,
    )
    orchestrator._decompose_claims(state)
    assert state["claim_status"] == "real_llm_decomposed"
    assert state["claim_pool_runtime_audit"]["claim_pool_enabled"] is False
    assert state["claim_pool_runtime_audit"]["legacy_single_call_used"] is True
    assert state["claim_pool_runtime_audit"]["integrity_passed"] is True


def test_production_strong_pool_marks_silent_legacy_fallback_as_failure(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = _strong_pool_fixture(tmp_path, chunk_count=5)

    def fake_decompose(self, section):
        self.last_audit = {"legacy_single_call_used": True}
        return [
            {
                "claim_id": "S07-C01",
                "statement": (
                    "A measured comparison supports one bounded conditional "
                    "choice under the stated operating constraints."
                ),
                "importance": "load_bearing",
                "supporting_text_chunk_ids": ["c000"],
                "citation_paper_ids": ["p000"],
            }
        ]

    monkeypatch.setattr(
        "optomind_research.runtime.phase3_argument_orchestrator.ClaimDecomposer.decompose_section",
        fake_decompose,
    )
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths={"S07": fixture["overlay"]},
        output_dir=tmp_path / "strong_fallback",
        real_llm_claims=True,
        claim_pool_enabled=True,
        execute_coverage=False,
    ).run()

    assert result["claim_pool_integrity_passed"] is False
    audit = result["claim_pool_audit"]["S07"]
    assert audit["legacy_single_call_used"] is True
    assert "strong_pool_fell_back_to_legacy_single_call" in audit[
        "integrity_violations"
    ]
