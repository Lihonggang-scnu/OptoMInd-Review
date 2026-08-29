from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from optomind_research.runtime.coverage_decision_contract import (
    assess_explicit_scope_boundary,
    assess_retrieved_paper_scope_boundary,
    build_compact_batched_audit_payload,
    canonical_candidate_decision,
    estimate_json_tokens,
    normalize_pipeline_structure,
    normalize_qwen_usage,
)
from optomind_research.runtime.review_quality_contract import (
    evaluate_adaptive_coverage,
)
from optomind_research.runtime.tool_provider import SectionCoverageContext
from optomind_research.runtime.artifact_schemas import ScopeFit
from optomind_research.s2_kb_bridge import (
    S2KnowledgeBaseBridge,
    validate_foreign_parent_consistency,
)
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk


def _source(
    paper_id: str,
    role: str,
    *,
    claims: list[str],
    permission: str = "factual_support",
    venue: str = "Optics Letters",
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "title": f"{role} source {paper_id}",
        "venue": venue,
        "literature_role": role,
        "scope_fit": "direct",
        "content_depth": "structured_snippet",
        "use_permission": permission,
        "context_complete": True,
        "canonical_chunk_ids": [f"chunk:{paper_id}"],
        "supported_claim_ids": claims,
    }


def test_structured_snippet_upserts_canonical_parent_and_preserves_alias_audit(
    tmp_path: Path,
) -> None:
    db = tmp_path / "supplemental_oa_kb.sqlite"
    paper = S2PaperRecord(
        paper_id="s2-hash-123",
        corpus_id=123,
        title="Optical adaptive device",
        materialization_route="s2_structured_body_snippet",
        content_depth="structured_snippet",
        use_permission="factual_support",
        scope_fit=ScopeFit.direct,
    )
    chunk = UnifiedTextChunk(
        chunk_id="s2chunk:123:100:900:acceptance",
        paper_id="CorpusId:123",
        corpus_id=123,
        title=paper.title,
        text="A complete structured body passage for the optical mechanism. " * 12,
        scope_fit=ScopeFit.direct,
        context_complete=True,
        use_permission="factual_support",
        route_provenance={
            "materialization_route": "s2_structured_body_snippet",
        },
    )

    result = S2KnowledgeBaseBridge(db).ingest(papers=[paper], chunks=[chunk])

    assert result["papers_inserted"] == 1
    assert result["foreign_parent_consistency"]["valid"] is True
    assert result["identity_rebindings"][0]["provider_parent_id"] == "CorpusId:123"
    assert result["identity_rebindings"][0]["canonical_parent_id"] == paper.paper_id
    assert validate_foreign_parent_consistency(db)["valid"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
        assert conn.execute("SELECT paper_id FROM text_chunks").fetchone()[0] == paper.paper_id
        route = json.loads(
            conn.execute("SELECT route_provenance_json FROM text_chunks").fetchone()[0]
        )
        assert route["materialization_route"] == "s2_structured_body_snippet"
        assert route["identity_resolution"]["canonical_parent_id"] == paper.paper_id

    from optomind_research.runtime import section_coverage_tool_registry as registry

    source_route = registry._source_route_fields(
        candidate={},
        chunk_ids=[chunk.chunk_id],
        scope_fit="direct",
    )
    assert source_route["materialization_route"] == "s2_structured_body_snippet"


def test_short_path_structured_transition_writes_canonical_manifest_and_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from optomind_research.runtime import section_coverage_tool_registry as registry
    from optomind_research.runtime.artifact_schemas import OACandidateLedger

    class FakeRetriever:
        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, *args, **kwargs):
            chunk = UnifiedTextChunk(
                chunk_id="s2chunk:123:1:700:transition",
                paper_id="CorpusId:123",
                corpus_id=123,
                title="Canonical transition paper",
                text="A complete structured body passage for the tested mechanism. " * 12,
                scope_fit="direct",
                context_complete=True,
                use_permission="factual_support",
                route_provenance={
                    "materialization_route": "s2_structured_body_snippet"
                },
            )
            return SimpleNamespace(
                accepted_chunks=[chunk],
                rejected_items=[],
                query_runs=[{"query": "mechanism", "status_code": 200}],
            )

    monkeypatch.setattr(
        "optomind_research.s2_text_chunk_retriever.S2TextChunkRetriever",
        FakeRetriever,
    )
    ctx = SectionCoverageContext(
        section_id="S01",
        section_data={
            "section_id": "S01",
            "title": "Mechanism",
            "required_roles": ["foundation"],
            "topic_identity": {"valid": False},
        },
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
        short_path_mode=True,
        min_mode_max_total_papers=1,
    )
    candidate = {
        "candidate_id": "cand-transition",
        "section_id": "S01",
        "role": "foundation",
        "title": "Canonical transition paper",
        "abstract": "A direct abstract for the canonical S2 transition.",
        "scope_fit": "direct",
        "decision": "deferred",
        "is_oa": False,
        "semantic_scholar_id": "s2-hash-transition",
        "corpus_id": 123,
        "backends": ["semantic_scholar"],
        "query_texts": ["mechanism"],
    }
    ctx.register_candidates([candidate])
    registry._append_candidates_to_ledger(ctx.work_dir, ctx.section_id, [candidate])
    result = json.loads(
        registry._make_submit_candidate_audit(ctx)(
            json.dumps(
                [
                    {
                        "candidate_id": "cand-transition",
                        "scope_fit": "direct",
                        "role_fit": ["foundation"],
                        "decision": "approved",
                        "candidate_decision": "materialize_now",
                        "audit_reason": "structured transition fixture",
                    }
                ]
            )
        )
    )
    assert result["status"] == "ok"
    assert result["post_audit_transition"]["status"] == "materialized"

    manifest = json.loads((tmp_path / "MATERIALIZATION_MANIFEST.json").read_text())
    row = manifest["papers"][0]
    assert row["paper_id"] == "s2-hash-transition"
    assert row["paper_row_inserted"] is True
    assert row["acquisition_status"] == "structured_snippet"
    assert row["scope_fit"] == "direct"
    assert row["materialization_route"] == "s2_structured_body_snippet"
    assert row["chunk_count"] >= 1

    with sqlite3.connect(tmp_path / "stage.sqlite") as conn:
        assert conn.execute("SELECT paper_id FROM papers").fetchone()[0] == "s2-hash-transition"
        assert conn.execute("SELECT paper_id FROM text_chunks").fetchone()[0] == "s2-hash-transition"
        route = conn.execute(
            "SELECT materialization_route FROM papers"
        ).fetchone()[0]
        assert route == "s2_structured_body_snippet"
    assert registry._build_source_ledger(ctx) is None
    ledger = json.loads((tmp_path / "SECTION_SOURCE_LEDGER.json").read_text())
    assert ledger["sources"][0]["materialization_route"] == "s2_structured_body_snippet"


def test_discovery_only_row_is_a_limit_when_direct_roles_and_claims_close(
) -> None:
    section = {
        "section_id": "S01",
        "section_role": "mechanism",
        "target_word_count": 900,
        "required_roles": ["foundation", "mechanism"],
        "load_bearing_claims": [
            {"claim_id": "C-foundation", "statement": "foundation"},
            {"claim_id": "C-mechanism", "statement": "mechanism"},
        ],
    }
    readiness = evaluate_adaptive_coverage(
        section,
        [
            _source("p1", "foundation", claims=["C-foundation"]),
            _source("p2", "mechanism", claims=["C-mechanism"], venue="Nano Letters"),
            _source("p3", "foundation", claims=["C-foundation"], venue="ACS Photonics"),
            _source(
                "p4",
                "foundation",
                claims=["C-foundation"],
                permission="discovery_only",
                venue="Nature Photonics",
            ),
        ],
    )

    assert readiness.outcome == "material_ready_with_limits"
    assert readiness.missing_required_roles == []
    assert readiness.unsupported_load_bearing_claims == []
    assert readiness.permission_failures
    assert any("weak_permission_rows_excluded" in item for item in readiness.limitations)


def test_discovery_only_row_cannot_close_a_required_fact() -> None:
    readiness = evaluate_adaptive_coverage(
        {
            "section_id": "S01",
            "section_role": "mechanism",
            "target_word_count": 900,
            "required_roles": ["mechanism"],
            "load_bearing_claims": [
                {"claim_id": "C-mechanism", "statement": "mechanism"}
            ],
        },
        [
            _source(
                "weak-only",
                "mechanism",
                claims=["C-mechanism"],
                permission="discovery_only",
            )
        ],
    )

    assert readiness.outcome == "needs_more_literature"
    assert readiness.factual_permission_sources == 0
    assert "C-mechanism" in readiness.unsupported_load_bearing_claims
    assert "mechanism" in readiness.missing_required_roles


def test_explicit_optical_boundary_rejects_microwave_candidate() -> None:
    section = {
        "scope_guardrails": [
            "Focus only on optical and near-IR implementations; exclude microwave and acoustic regimes."
        ]
    }
    candidate = {
        "title": "Mechanically reprogrammable Pancharatnam-Berry metasurface for microwaves",
        "abstract": "A microwave RF phase-control device with tunable unit cells.",
    }
    boundary = assess_explicit_scope_boundary(section, candidate)
    assert boundary["incompatible"] is True
    assert "microwave_rf" in boundary["incompatible_regimes"]
    compatible_optical = assess_explicit_scope_boundary(
        section,
        {
            "title": "Optical near-IR metasurface mechanism",
            "abstract": "An optical device with near-IR phase control.",
        },
    )
    assert compatible_optical["incompatible"] is False

    from optomind_research.runtime import section_coverage_tool_registry as registry

    ctx = SectionCoverageContext(
        section_id="S01",
        section_data={**section, "topic_identity": {"valid": False}},
        kb_sqlite=None,
        temp_kb_sqlite=Path("stage.sqlite"),
        work_dir=Path("."),
    )
    guarded = registry._candidate_alignment_guard(candidate, ctx)
    assert guarded["hard_reject"] is True
    assert guarded["direct_eligible"] is False


def test_batched_audit_payload_carries_compact_section_constraints_under_budget() -> None:
    section = {
        "section_id": "S01",
        "chapter_argument": "Explain how optical near-IR devices achieve adaptive phase control.",
        "key_questions": ["Which mechanism closes the phase-control loop?"],
        "scope_guardrails": ["Optical/near-IR only; no microwave or acoustic examples."],
        "required_roles": ["foundation", "mechanism"],
        "topic_identity": {"scientific_object": "adaptive optical metasurface"},
    }
    payload = build_compact_batched_audit_payload(
        section=section,
        candidates=[
            {
                "candidate_id": f"candidate-{index}",
                "title": f"Candidate {index}",
                "abstract": "candidate abstract " * 300,
                "role": "mechanism",
            }
            for index in range(6)
        ],
        wave_index=0,
        max_candidates=6,
    )

    assert payload["chapter_argument"] == section["chapter_argument"]
    assert payload["key_questions"] == section["key_questions"]
    assert payload["scope_guardrails"] == section["scope_guardrails"]
    assert payload["required_roles"] == section["required_roles"]
    assert "binding" in payload["audit_protocol"]
    assert estimate_json_tokens(payload) < 20_000


def test_qwen_usage_normalization_and_reconciliation_use_one_accounting_shape(
    tmp_path: Path,
) -> None:
    usage = normalize_qwen_usage(
        {
            "usage": {"prompt_tokens": 1216, "completion_tokens": 528},
            "estimated_cost_cny": 0.0123,
        },
        fallback_input_tokens=1193,
        fallback_output_tokens=99,
    )
    assert usage == {
        "input_tokens": 1216,
        "output_tokens": 528,
        "cost_cny": 0.0123,
        "cost_basis": "estimated_reported",
        "cost_is_estimated": True,
        "input_source": "prompt_tokens",
        "output_source": "completion_tokens",
    }

    from optomind_research.runtime import section_coverage_tool_registry as registry

    ctx = SectionCoverageContext(
        section_id="S01",
        section_data={},
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
    )
    registry._mark_audit_wave(
        ctx,
        wave_index=0,
        candidate_ids=["candidate-1"],
        payload_tokens=1193,
        output_tokens=99,
    )
    registry._reconcile_batched_audit_usage(ctx, usage)
    wave = json.loads((tmp_path / "COVERAGE_WAVE_TELEMETRY.json").read_text())
    phase2 = json.loads((tmp_path / "PHASE2_TELEMETRY.json").read_text())
    assert wave["batched_llm_input_tokens"] == phase2["batched_llm_input_tokens"] == 1216
    assert wave["batched_llm_output_tokens"] == phase2["batched_llm_output_tokens"] == 528
    assert wave["batched_llm_cost_cny"] == phase2["batched_llm_cost_cny"] == 0.0123
    assert wave["cost_basis"] == phase2["cost_basis"] == "estimated_reported"
    assert wave["cost_is_estimated"] is phase2["cost_is_estimated"] is True


def test_retrieved_scope_guard_quarantines_bland_paper_after_one_forbidden_snippet() -> None:
    section = {
        "scope_guardrails": [
            "Focus on optical and near-IR implementations; exclude microwave and acoustic regimes."
        ]
    }
    candidate = {
        "title": "Mechanically reprogrammable Pancharatnam-Berry metasurface",
        "abstract": "A reconfigurable phase-control metasurface.",
    }
    report = assess_retrieved_paper_scope_boundary(
        section,
        candidate,
        [
            {"chunk_id": "optical-1", "text": "Optical near-IR phase control."},
            {
                "chunk_id": "microwave-2",
                "section_path": "Results",
                "text": "The device operates at 6.55 GHz and 6.925 GHz in a wireless microwave system.",
            },
        ],
    )

    assert report["quarantine_all_snippets"] is True
    assert "microwave_rf" in report["incompatible_regimes"]
    assert report["snippet_evidence"][0]["chunk_id"] == "microwave-2"
    assert report["decisive_conflicts"][0]["chunk_id"] == "microwave-2"
    assert report["decisive_conflicts"][0]["reason"] == (
        "quantitative_forbidden_signature_in_core_section"
    )


def test_historical_radiofrequency_analogy_does_not_quarantine_optical_foundation() -> None:
    section = {
        "scope_guardrails": [
            "Focus on optical and near-IR implementations; exclude microwave and acoustic regimes."
        ]
    }
    report = assess_retrieved_paper_scope_boundary(
        section,
        {
            "title": "The road to atomically thin metasurface optics",
            "abstract": "A historical review of optical and near-infrared flat-optics foundations.",
        },
        [
            {
                "chunk_id": "history-1",
                "section_path": "A brief historical perspective of passive metasurfaces",
                "content_kind": "text_chunk",
                "source_locator": {"page": 2, "paragraph": 3},
                "text": (
                    "Optical antenna concepts shaped optical metasurfaces. The optical "
                    "response in visible and near-infrared systems follows, by historical "
                    "analogy, antenna design developed for radiofrequency antennas. Later "
                    "optical and near-IR implementations refined optical phase control, "
                    "optical resonances, and visible-light operation."
                ),
            }
        ],
    )

    assert report["quarantine_all_snippets"] is False
    assert report["decisive_conflicts"] == []
    assert len(report["contextual_mentions"]) == 1
    assert report["contextual_mentions"][0]["chunk_id"] == "history-1"
    assert report["contextual_mentions"][0]["section_class"] == "contextual"
    assert report["contextual_mentions"][0]["chunk_metadata"]["source_locator"]["page"] == 2
    aggregate = report["aggregate_regime_hits"]
    assert sum(aggregate["allowed"].values()) > sum(aggregate["forbidden"].values())


def test_structured_scope_violation_controls_routing_without_reading_audit_prose() -> None:
    hard = canonical_candidate_decision(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "open_access_url": "https://example.org/paper.pdf",
            "audit_reason": "This candidate violates a boundary.",
            "scope_violations": [
                {
                    "code": "forbidden_regime",
                    "severity": "hard",
                    "evidence": "Microwave-only experiment.",
                }
            ],
        }
    )
    soft = canonical_candidate_decision(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "open_access_url": "https://example.org/paper.pdf",
            "audit_reason": "A soft chapter-stage mismatch is noted.",
            "boundary_violations": [
                {"code": "chapter_stage_mismatch", "severity": "soft"}
            ],
        }
    )
    prose_only = canonical_candidate_decision(
        {
            "decision": "approved",
            "scope_fit": "direct",
            "is_oa": True,
            "open_access_url": "https://example.org/paper.pdf",
            "audit_reason": "This candidate violates a boundary.",
        }
    )

    assert hard.action == "reject"
    assert hard.scope_fit == "out_of_scope"
    assert soft.action == "discovery_lead"
    assert prose_only.action == "materialize_now"


def test_duplicate_candidates_retain_all_query_roles_and_provenance() -> None:
    from optomind_research.runtime import section_coverage_tool_registry as registry

    merged = registry._dedup_raw_candidates(
        [
            {
                "doi": "10.1000/shared",
                "title": "Shared optical mechanism",
                "role": "foundation",
                "role_fit": ["foundation"],
                "role_provenance": {"foundation": ["history principles"]},
                "backends": ["semantic_scholar"],
                "query_texts": ["history principles"],
            },
            {
                "doi": "10.1000/shared",
                "title": "Shared optical mechanism",
                "role": "mechanism",
                "role_fit": ["mechanism"],
                "role_provenance": {"mechanism": ["causal mechanism"]},
                "backends": ["openalex"],
                "query_texts": ["causal mechanism"],
            },
        ]
    )

    assert len(merged) == 1
    assert set(merged[0]["role_fit"]) == {"foundation", "mechanism"}
    assert set(merged[0]["role_provenance"]) == {"foundation", "mechanism"}
    assert merged[0]["role_provenance"]["mechanism"] == ["causal mechanism"]


def test_missing_provider_cost_uses_resolved_model_list_price_estimate() -> None:
    usage = normalize_qwen_usage(
        {
            "usage": {"prompt_tokens": 1000, "completion_tokens": 250},
            "model": "",
        },
        fallback_input_tokens=900,
        fallback_output_tokens=200,
        model_tier="standard_model",
    )

    assert usage["model_name"] == "qwen3.6-flash"
    assert usage["cost_basis"] == "estimated_list_price"
    assert usage["cost_provenance"] == "configured_list_price_estimate"
    assert usage["cost_is_estimated"] is True
    assert usage["cost_cny"] > 0
    assert usage["pricing_source"]


def test_pipeline_boundary_repairs_high_confidence_mojibake_but_preserves_unicode() -> None:
    repaired = normalize_pipeline_structure(
        {
            "chapter_argument": "mechanisms\u9225\u6507eometric separation",
            "valid_science": "χ², ϵ, and λ remain valid Unicode.",
        }
    )

    assert repaired["chapter_argument"] == "mechanisms—geometric separation"
    assert repaired["valid_science"] == "χ², ϵ, and λ remain valid Unicode."


def test_adaptive_readiness_exposes_scoped_and_factual_direct_counts_separately() -> None:
    readiness = evaluate_adaptive_coverage(
        {
            "section_id": "S01",
            "section_role": "mechanism",
            "required_roles": ["mechanism"],
            "load_bearing_claims": [],
        },
        [
            _source("factual", "mechanism", claims=[]),
            _source(
                "discovery",
                "mechanism",
                claims=[],
                permission="discovery_only",
            ),
        ],
    )

    assert readiness.scoped_direct_sources == 2
    assert readiness.factual_direct_sources == 1
    assert readiness.direct_sources == readiness.factual_direct_sources
    payload = readiness.to_dict()
    assert payload["scoped_direct_sources"] == 2
    assert payload["factual_direct_sources"] == 1


def test_mocked_short_path_artifacts_agree_on_real_qwen_usage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from optomind_research.runtime.artifact_schemas import OACandidateLedger
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
        SectionCoverageOrchestratorConfig,
    )
    import llm.qwen_chat_client as qwen

    blueprint_path = tmp_path / "blueprint.json"
    blueprint_path.write_text(
        json.dumps(
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "title": "Mock section",
                        "chapter_argument": "Test a bounded audit.",
                        "required_roles": ["foundation"],
                        "optional_roles": [],
                        "load_bearing_claims": [],
                        "topic_identity": {"valid": False},
                        "scope_guardrails": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    section_dir = run_dir / "sections" / "S01"
    section_dir.mkdir(parents=True)
    candidate = {
        "candidate_id": "cand-1",
        "section_id": "S01",
        "role": "foundation",
        "title": "Mock candidate",
        "abstract": "A mock abstract for accounting.",
        "decision": "deferred",
        "scope_fit": "direct",
        "is_oa": False,
        "query_texts": ["mock foundation"],
        "backends": ["openalex"],
    }
    (section_dir / "OA_CANDIDATE_LEDGER.json").write_text(
        json.dumps(
            OACandidateLedger(
                section_id="S01", candidates=[candidate]
            ).model_dump()
        ),
        encoding="utf-8",
    )

    def fake_call(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "candidates": [
                        {
                            "candidate_id": "cand-1",
                            "scope_fit": "direct",
                            "role_fit": ["foundation"],
                            "decision": "rejected",
                            "candidate_decision": "reject",
                            "audit_reason": "mock rejection for accounting",
                        }
                    ]
                }
            ),
            "usage": {"prompt_tokens": 1216, "completion_tokens": 528},
            "estimated_cost_cny": 0.0123,
        }

    monkeypatch.setattr(qwen, "call_qwen_chat", fake_call)
    config = SectionCoverageOrchestratorConfig(
        blueprint_path=blueprint_path,
        base_kb_sqlite=None,
        output_root=tmp_path,
        staging_kb_path=tmp_path / "supplemental.sqlite",
        short_path_mode=True,
        max_coverage_waves=1,
        max_audit_calls_per_section=1,
        max_model_calls_per_section=1,
        max_materialized_papers_per_section=1,
        stage_cost_budget_cny=1.0,
    )
    result = SectionCoverageOrchestrator(config, run_dir=run_dir).run(
        section_ids=["S01"]
    )

    short_path = json.loads((section_dir / "SHORT_PATH_RUN.json").read_text())
    result_artifact = json.loads((section_dir / "RESULT.json").read_text())
    phase2 = json.loads((section_dir / "PHASE2_TELEMETRY.json").read_text())
    wave = json.loads((section_dir / "COVERAGE_WAVE_TELEMETRY.json").read_text())
    run_manifest = json.loads((run_dir / "SECTION_COVERAGE_RUN.json").read_text())
    record = run_manifest["sections"][0]
    receipt = json.loads((section_dir / "USAGE_RECEIPT.json").read_text())

    assert result.total_input_tokens == 1216
    assert result.total_output_tokens == 528
    assert short_path["input_tokens"] == result_artifact["total_input_tokens"] == record["input_tokens"] == 1216
    assert short_path["output_tokens"] == result_artifact["total_output_tokens"] == record["output_tokens"] == 528
    assert phase2["batched_llm_input_tokens"] == wave["batched_llm_input_tokens"] == 1216
    assert phase2["batched_llm_output_tokens"] == wave["batched_llm_output_tokens"] == 528
    assert result.total_cost_cny == short_path["estimated_cost_cny"] == result_artifact["estimated_cost_cny"] == record["cost_cny"] == 0.0123
    assert result.total_cost_basis == short_path["cost_basis"] == record["cost_basis"] == "estimated_reported"
    assert result.cost_is_estimated is True
    assert short_path["cost_is_estimated"] is result_artifact["cost_is_estimated"] is record["cost_is_estimated"] is True
    assert receipt["input_tokens"] == 1216
    assert receipt["output_tokens"] == 528
    assert receipt["cost_cny"] == 0.0123
    assert receipt["cost_basis"] == "estimated_reported"
    assert receipt["model_tier"] == "advanced_model"
    assert receipt["model_name"] == "qwen3.7-flash"
    for artifact in (short_path, result_artifact, record, phase2, wave):
        assert artifact["usage_receipt_id"] == receipt["receipt_id"]
    assert phase2["canonical_usage"]["receipt_id"] == receipt["receipt_id"]
    assert wave["model_input_tokens"] == phase2["model_input_tokens"] == 1216
    assert wave["model_output_tokens"] == phase2["model_output_tokens"] == 528
    assert run_manifest["total_input_tokens"] == 1216
    assert run_manifest["total_output_tokens"] == 528
    assert run_manifest["total_cost_cny"] == 0.0123
    assert run_manifest["total_cost_basis"] == "estimated_reported"
