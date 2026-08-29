"""Focused offline tests for Phase 3 acceptance/recovery semantics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.phase3_argument_orchestrator import (
    Phase3ArgumentOrchestrator,
)
from optomind_research.runtime.r3_production_handoff import (
    read_r3_production_handoff,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)


@pytest.fixture
def tmp_path(request):
    root = Path(__file__).resolve().parents[1] / ".pytest-basetemp-phase3-recovery"
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


class _FakeContract:
    def __init__(self, values: dict):
        self.values = values

    def to_dict(self):
        return self.values


def _harness(tmp_path: Path) -> ReviewHarnessOrchestrator:
    return ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=tmp_path / "query.json",
            base_kb_sqlite=tmp_path / "kb.sqlite",
            output_root=tmp_path / "out",
        ),
        run_dir=tmp_path / "run",
    )


def test_context_audit_accepts_bounded_mentor_guidance():
    source = (
        "A very long mentor guidance passage containing many scientific "
        "terms and explicit writing constraints for this section."
    )
    bounded = source[:47]
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": [source]},
        "contract": _FakeContract({"mentor_guidance": [bounded]}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": [bounded]}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is True
    assert audit["payload_audit_status"] == "applicable"
    assert audit["checks"]["mentor_guidance"] is True
    assert audit["normalization_notes"][0]["field"] == "mentor_guidance"


def test_context_audit_rejects_tiny_guidance_substring():
    source = "A very long mentor guidance passage containing many scientific terms."
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": [source]},
        "contract": _FakeContract({"mentor_guidance": ["A very"]}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": ["A very"]}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is False
    assert audit["checks"]["mentor_guidance"] is False


def test_context_audit_continues_after_too_short_candidate():
    source = "A complete scientific mentor guidance sentence for this section."
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": [source]},
        "contract": _FakeContract({"mentor_guidance": ["tiny", source]}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": ["tiny", source]}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is True
    assert audit["checks"]["mentor_guidance"] is True


def test_context_audit_accepts_meaningful_cjk_containment():
    source = "光子晶体中的连续域束缚态具有无限品质因子和零线宽"
    bounded = source[:12]
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": [source]},
        "contract": _FakeContract({"mentor_guidance": [bounded]}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": [bounded]}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is True
    assert audit["checks"]["mentor_guidance"] is True


def test_context_audit_empty_mentor_guidance_requires_empty_contract():
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": []},
        "contract": _FakeContract({"mentor_guidance": []}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": []}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is True
    assert audit["checks"]["mentor_guidance"] is True


def test_context_audit_empty_mentor_guidance_rejects_nonempty_contract():
    state = {
        "claim_status": "real_llm_decomposed",
        "context_source_values": {"mentor_guidance": []},
        "contract": _FakeContract({"mentor_guidance": ["unexpected"]}),
        "m2a_input_payload": {
            "section_contract": {"mentor_guidance": []}
        },
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is False
    assert audit["checks"]["mentor_guidance"] is False


def test_context_audit_reused_claims_is_not_applicable():
    state = {
        "claim_status": "existing_claims_reused",
        "context_source_values": {"mentor_guidance": ["full source guidance"]},
        "contract": _FakeContract({"mentor_guidance": ["full source guidance"]}),
        "m2a_input_payload": {},
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is True
    assert audit["payload_audit_status"] == "not_applicable"
    assert audit["payload_audit_reason"] == (
        "existing_claims_reused_and_m2a_not_called"
    )
    assert audit["checks"]["mentor_guidance"] is True


def test_context_audit_reused_claims_still_requires_authoritative_contract():
    state = {
        "claim_status": "existing_claims_reused",
        "context_source_values": {"mentor_guidance": ["full source guidance"]},
        "contract": _FakeContract({"mentor_guidance": ["mismatched guidance"]}),
        "m2a_input_payload": {},
    }

    audit = Phase3ArgumentOrchestrator._context_handoff_audit(state)

    assert audit["passed"] is False
    assert audit["payload_audit_status"] == "not_applicable"
    assert audit["checks"]["mentor_guidance"] is False


def test_phase3_status_allows_stale_soft_failure(tmp_path: Path):
    harness = _harness(tmp_path)
    report = type(
        "Report",
        (),
        {
            "valid": True,
            "section_readiness": {
                "S01": {"outcome": "ready_with_limits"}
            },
            "global_readiness": {
                "status": "ready_with_limits",
                "ready_section_ids": ["S01"],
                "section_count": 1,
            },
        },
    )()
    acceptance = {
        "status": "failed",
        "r4_handoff_ready": False,
        "claim_quality_passed": True,
        "evidence_permission_passed": True,
        "coverage_request_quality_passed": True,
        "verifier_batch_budget_passed": True,
        "duplicate_bundle_categories_detected": False,
        "engineering_safety": {
            "all_ids_traceable": True,
            "relation_revalidation_passed": True,
            "coverage_atlas_uses_migrated_relation_graph": True,
        },
        "material_quality": {"generic_claims_detected": False},
        "cost": {"runtime_failure_count": 0},
    }

    assert harness._phase3_acceptance_hard_failure(acceptance) is False
    assert harness._phase3_status(report=report, acceptance=acceptance) == (
        "completed_with_limits"
    )


def test_phase3_status_blocks_true_hard_failure(tmp_path: Path):
    harness = _harness(tmp_path)
    report = type(
        "Report",
        (),
        {
            "valid": True,
            "section_readiness": {
                "S01": {"outcome": "ready_with_limits"}
            },
            "global_readiness": {
                "status": "ready_with_limits",
                "ready_section_ids": ["S01"],
                "section_count": 1,
            },
        },
    )()
    acceptance = {
        "status": "failed",
        "claim_quality_passed": True,
        "evidence_permission_passed": False,
        "coverage_request_quality_passed": True,
        "verifier_batch_budget_passed": True,
        "duplicate_bundle_categories_detected": False,
        "engineering_safety": {
            "all_ids_traceable": True,
            "relation_revalidation_passed": True,
            "coverage_atlas_uses_migrated_relation_graph": True,
        },
        "material_quality": {"generic_claims_detected": False},
        "cost": {"runtime_failure_count": 0},
    }

    assert harness._phase3_acceptance_hard_failure(acceptance) is True
    assert harness._phase3_status(report=report, acceptance=acceptance) == "failed"


def _phase3_fixture(tmp_path: Path):
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c1','p1','Paper one','Full mechanism passage.','fulltext','fulltext')"
        )
        conn.commit()
    ledger = tmp_path / "shared.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p1",
                        "title": "Paper one",
                        "canonical_chunk_ids": ["c1"],
                        "literature_role": "mechanism",
                        "scope_fit": "direct",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    overlays = {}
    for sid, chunk in (("S01", "c1"), ("S02", "")):
        overlay = tmp_path / f"{sid}.json"
        overlay.write_text(
            json.dumps(
                {
                    "paper_ids": ["p1"] if chunk else [],
                    "chunk_ids": [chunk] if chunk else [],
                    "paper_overrides": (
                        {"p1": {"scope_fit": "direct", "use_permission": "factual_support"}}
                        if chunk
                        else {}
                    ),
                    "chunk_overrides": (
                        {"c1": {"scope_fit": "direct", "use_permission": "factual_support"}}
                        if chunk
                        else {}
                    ),
                }
            ),
            encoding="utf-8",
        )
        overlays[sid] = overlay
    blueprint = {
        "sections": [
            {
                "section_id": "S01",
                "title": "Mechanism",
                "chapter_argument": "Explain the mechanism.",
                "required_roles": ["mechanism"],
                "claims": [
                    {
                        "claim_id": "S01-C01",
                        "statement": "The mechanism is established.",
                        "supporting_text_chunk_ids": ["c1"],
                        "load_bearing": True,
                    }
                ],
            },
            {
                "section_id": "S02",
                "title": "Comparison",
                "chapter_argument": "Compare regimes.",
                "required_roles": ["comparison"],
                "claims": [],
            },
        ]
    }
    return {
        "blueprint": blueprint,
        "ledger": ledger,
        "kb": kb,
        "overlays": overlays,
    }


def test_coverage_retrieval_is_at_most_one_wave_across_iterations(
    tmp_path: Path,
    monkeypatch,
):
    fixture = _phase3_fixture(tmp_path)
    output = tmp_path / "phase3"
    calls = {"count": 0}
    decomposition_counts: dict[tuple[str, str], int] = {}
    bind_counts: dict[tuple[str, str], int] = {}
    build_counts: dict[tuple[str, str], int] = {}
    original_decompose = Phase3ArgumentOrchestrator._decompose_claims
    original_bind = Phase3ArgumentOrchestrator._bind_section
    original_build = Phase3ArgumentOrchestrator._build_bundle

    def counted_decompose(self, state):
        sid = state["section"]["section_id"]
        decomposition_counts[("decompose", sid)] = (
            decomposition_counts.get(("decompose", sid), 0) + 1
        )
        return original_decompose(self, state)

    def counted_bind(self, state, migrated_edges):
        sid = state["section"]["section_id"]
        bind_counts[("bind", sid)] = bind_counts.get(("bind", sid), 0) + 1
        return original_bind(self, state, migrated_edges)

    def counted_build(self, state, migrated_edges):
        sid = state["section"]["section_id"]
        build_counts[("build", sid)] = build_counts.get(("build", sid), 0) + 1
        return original_build(self, state, migrated_edges)

    monkeypatch.setattr(
        Phase3ArgumentOrchestrator,
        "_decompose_claims",
        counted_decompose,
    )
    monkeypatch.setattr(
        Phase3ArgumentOrchestrator,
        "_bind_section",
        counted_bind,
    )
    monkeypatch.setattr(
        Phase3ArgumentOrchestrator,
        "_build_bundle",
        counted_build,
    )

    def coverage_executor(requests, iteration):
        calls["count"] += 1
        if iteration == 1:
            return {
                "S02": {
                    "claims": [
                        {
                            "claim_id": "S02-C01",
                            "statement": "New comparison evidence is available.",
                            "supporting_text_chunk_ids": [],
                            "load_bearing": False,
                        }
                    ],
                    "candidate_text_chunks": [
                        {
                            "chunk_id": "candidate-chunk",
                            "paper_id": "candidate-paper",
                            "text": "A newly retrieved comparison passage.",
                            "use_permission": "factual_support",
                            "content_depth": "fulltext",
                            "context_complete": True,
                        }
                    ],
                }
            }
        return {}

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map={"user_question": "How does the mechanism work?"},
        coverage_atlas={"sections": []},
        relation_graph={"edges": []},
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert calls["count"] == 1
    assert run["coverage_waves_executed"] == 1
    assert len(run["iterations"]) == 2
    assert run["iterations"][1]["sections_processed"] == ["S02"]
    assert decomposition_counts[("decompose", "S02")] == 2
    assert bind_counts[("bind", "S02")] == 2
    assert build_counts[("build", "S02")] == 2


def test_no_requests_after_processed_iteration_does_not_rebind_again(
    tmp_path: Path,
    monkeypatch,
):
    fixture = _phase3_fixture(tmp_path)
    output = tmp_path / "phase3-no-requests"
    decomposition_counts: dict[tuple[str, str], int] = {}
    original_decompose = Phase3ArgumentOrchestrator._decompose_claims
    original_make_requests = Phase3ArgumentOrchestrator._make_requests

    def counted_decompose(self, state):
        sid = state["section"]["section_id"]
        decomposition_counts[("decompose", sid)] = (
            decomposition_counts.get(("decompose", sid), 0) + 1
        )
        return original_decompose(self, state)

    def limited_requests(self, states, iteration):
        if iteration >= 2:
            return []
        return original_make_requests(self, states, iteration)

    monkeypatch.setattr(
        Phase3ArgumentOrchestrator,
        "_decompose_claims",
        counted_decompose,
    )
    monkeypatch.setattr(
        Phase3ArgumentOrchestrator,
        "_make_requests",
        limited_requests,
    )

    def coverage_executor(requests, iteration):
        if iteration == 1:
            return {
                "S02": {
                    "claims": [
                        {
                            "claim_id": "S02-C01",
                            "statement": "New comparison evidence is available.",
                            "supporting_text_chunk_ids": [],
                            "load_bearing": False,
                        }
                    ],
                    "candidate_text_chunks": [
                        {
                            "chunk_id": "candidate-chunk",
                            "paper_id": "candidate-paper",
                            "text": "A newly retrieved comparison passage.",
                            "use_permission": "factual_support",
                            "content_depth": "fulltext",
                            "context_complete": True,
                        }
                    ],
                }
            }
        return {}

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map={"user_question": "How does the mechanism work?"},
        coverage_atlas={"sections": []},
        relation_graph={"edges": []},
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    assert decomposition_counts[("decompose", "S02")] == 2


def test_existing_bic_r3_handoff_revalidates_without_model_calls(tmp_path: Path):
    project_root = Path(__file__).resolve().parents[1]
    source_root = (
        project_root
        / "outputs"
        / "research_harness_e2e"
        / "bic_autonomous_20260816_v4_localfirst_publication"
        / "phase3_argument_orchestration"
    )
    if not (source_root / "R3_PRODUCTION_HANDOFF.json").is_file():
        pytest.skip("optional BIC artifact is not present in this checkout")
    copied = tmp_path / "phase3"
    copied.mkdir()
    for name in ("R3_PRODUCTION_HANDOFF.json", "PHASE3_ACCEPTANCE.json"):
        shutil.copy2(source_root / name, copied / name)

    handoff, report = read_r3_production_handoff(
        copied / "R3_PRODUCTION_HANDOFF.json"
    )
    assert report.valid is True
    harness = _harness(tmp_path)
    blueprint = {
        "sections": [
            {"section_id": section_id}
            for section_id in handoff.section_ids
        ]
    }
    validated_handoff, validated_report = harness._validate_existing_phase3_handoff(
        handoff_path=copied / "R3_PRODUCTION_HANDOFF.json",
        blueprint=blueprint,
        topic_identity=dict(handoff.topic_identity or {}),
    )

    assert validated_report.valid is True
    assert validated_handoff is not None
    ready_ids = validated_report.global_readiness.get("ready_section_ids", [])
    assert ready_ids
    acceptance = json.loads(
        (copied / "PHASE3_ACCEPTANCE.json").read_text(encoding="utf-8")
    )
    assert harness._phase3_status(
        report=validated_report,
        acceptance=acceptance,
    ) == "completed_with_limits"


def test_phase2_recovery_resolves_project_relative_paths_without_double_prefix(
    tmp_path: Path,
):
    repo_root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "bic_like_run"
    coverage_root = run_dir / "section_coverage"
    section_dir = coverage_root / "sections" / "S02"
    phase3_root = run_dir / "phase3_argument_orchestration"
    for directory in (section_dir, phase3_root, run_dir / "review_lead"):
        directory.mkdir(parents=True, exist_ok=True)

    blueprint_path = run_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
    blueprint_path.write_text(
        json.dumps({"sections": [{"section_id": "S02"}]}),
        encoding="utf-8",
    )
    (run_dir / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.state.v1",
                "run_id": run_dir.name,
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "TOPIC_IDENTITY.json").write_text(
        json.dumps({"fingerprint": "topic-fp", "valid": True}),
        encoding="utf-8",
    )
    query_plan = run_dir / "query_plan.json"
    query_plan.write_text("{}", encoding="utf-8")
    query_hash = hashlib.sha256(query_plan.read_bytes()).hexdigest()
    (phase3_root / "PHASE3_INPUT_FINGERPRINT.json").write_text(
        json.dumps(
            {
                "schema_version": "research_harness.phase3_input_fingerprint.v1",
                "sha256": "recovery-test",
                "files": {"query_plan": query_hash},
            }
        ),
        encoding="utf-8",
    )

    source_kb = run_dir / "source.sqlite"
    scoped_kb = run_dir / "scoped.sqlite"
    supplemental_kb = coverage_root / "supplemental_oa_kb.sqlite"
    for path in (source_kb, scoped_kb, supplemental_kb):
        sqlite3.connect(path).close()

    project_relative_section_dir = (
        section_dir.relative_to(repo_root).as_posix()
    )
    package_path = section_dir / "SECTION_MATERIAL_PACKAGE.json"
    ledger_path = section_dir / "SECTION_SOURCE_LEDGER.json"
    package_path.write_text(
        json.dumps({"section_id": "S02"}),
        encoding="utf-8",
    )
    ledger_path.write_text(
        json.dumps({"section_id": "S02"}),
        encoding="utf-8",
    )
    coverage_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "research_harness.section_coverage_run.v1",
        "run_id": "section_coverage",
        "status": "partial",
        "blueprint_path": str(blueprint_path),
        "base_kb_sqlite": str(scoped_kb),
        "supplemental_kb_sqlite": str(supplemental_kb),
        "sections": [
            {
                "section_id": "S02",
                "status": "needs_more_literature",
                "work_dir": project_relative_section_dir,
            }
        ],
        "material_bundles": {
            "S02": {
                "material_package_path": package_path.relative_to(repo_root).as_posix(),
                "source_ledger_path": ledger_path.relative_to(repo_root).as_posix(),
                "kb_sqlite": str(scoped_kb),
                "staging_kb_sqlite": str(supplemental_kb),
            }
        },
        "total_cost_cny": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }
    (coverage_root / "SECTION_COVERAGE_RUN.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=source_kb,
            output_root=tmp_path / "out",
            rebuild_phase3_handoff=True,
        ),
        run_dir=run_dir,
    )
    result = harness._rehydrate_phase3_recovery_coverage(
        blueprint={"sections": [{"section_id": "S02"}]},
        topic_identity={"fingerprint": "topic-fp", "valid": True},
        scoped_runtime_kb=scoped_kb,
        source_base_kb=source_kb,
    )

    assert result is not None
    assert "S02" in result.material_bundles
    recovered_package = result.material_bundles["S02"].material_package_path
    assert recovered_package == package_path.resolve()
    assert "section_coverage/outputs" not in str(recovered_package)


def test_stable_project_relative_path_is_representation_invariant(
    tmp_path: Path,
):
    project_root = tmp_path / "project"
    portfolio = (
        project_root
        / "outputs"
        / "run"
        / "section_coverage"
        / "ARTICLE_EVIDENCE_PORTFOLIO.json"
    )

    absolute = ReviewHarnessOrchestrator._stable_project_relative_path(
        portfolio,
        project_root,
    )
    relative_input = ReviewHarnessOrchestrator._stable_project_relative_path(
        portfolio.relative_to(Path.cwd()),
        project_root,
    )

    assert absolute == relative_input
    assert absolute == str(portfolio.relative_to(project_root))


def test_stable_project_relative_path_keeps_outside_paths_absolute(
    tmp_path: Path,
):
    outside = tmp_path / "outside" / "portfolio.json"
    project_root = tmp_path / "project"

    value = ReviewHarnessOrchestrator._stable_project_relative_path(
        outside,
        project_root,
    )

    assert Path(value).is_absolute()
    assert value == str(outside.resolve())


def test_adaptive_outcome_bytes_are_same_for_relative_and_absolute_workdir(
    tmp_path: Path,
    monkeypatch,
):
    project_root = Path(__file__).resolve().parents[1]
    run_dir = tmp_path / "run"
    coverage_dir = run_dir / "section_coverage"
    phase3_rel = run_dir / "phase3_rel"
    phase3_abs = run_dir / "phase3_abs"
    for path in (coverage_dir, phase3_rel, phase3_abs, run_dir / "review_lead"):
        path.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "research_harness.section_coverage_run.v1",
        "run_id": "section_coverage",
        "status": "partial",
        "sections": [
            {
                "section_id": "S02",
                "status": "needs_more_literature",
                "work_dir": "sections/S02",
            }
        ],
        "material_bundles": {},
    }
    (coverage_dir / "SECTION_COVERAGE_RUN.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (coverage_dir / "RELATION_GRAPH.json").write_text(
        json.dumps({"edges": []}),
        encoding="utf-8",
    )
    query_plan = run_dir / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    source_kb = run_dir / "source.sqlite"
    scoped_kb = run_dir / "scoped.sqlite"
    sqlite3.connect(source_kb).close()
    sqlite3.connect(scoped_kb).close()

    from optomind_research.runtime import section_asset_overlay
    from optomind_research.runtime import coverage_atlas

    monkeypatch.setattr(
        section_asset_overlay,
        "build_section_asset_overlay",
        lambda **kwargs: (
            Path(kwargs["output_path"]).parent.mkdir(
                parents=True, exist_ok=True
            ),
            Path(kwargs["output_path"]).write_text(
                "{}", encoding="utf-8"
            ),
        ),
    )
    monkeypatch.setattr(
        coverage_atlas,
        "build_coverage_atlas",
        lambda **kwargs: {
            "schema_version": "research_harness.coverage_atlas.v1",
            "sections": [],
            "relation_graph": {},
        },
    )

    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=source_kb,
            output_root=tmp_path / "out",
        ),
        run_dir=run_dir,
    )

    def build_inputs(work_dir: Path, phase3_root: Path):
        coverage = SimpleNamespace(
            material_bundles={},
            work_dir=work_dir,
        )
        return harness._build_phase3_inputs(
            blueprint={"sections": [{"section_id": "S02"}]},
            coverage=coverage,
            scoped_runtime_kb=scoped_kb,
            source_base_kb=source_kb,
            phase3_root=phase3_root,
        )

    rel_work_dir = Path(coverage_dir.relative_to(Path.cwd()))
    rel_inputs = build_inputs(rel_work_dir, phase3_rel)
    abs_inputs = build_inputs(coverage_dir.resolve(), phase3_abs)
    rel_bytes = Path(rel_inputs["adaptive_outcomes_path"]).read_bytes()
    abs_bytes = Path(abs_inputs["adaptive_outcomes_path"]).read_bytes()

    assert rel_bytes == abs_bytes
    assert hashlib.sha256(rel_bytes).hexdigest() == hashlib.sha256(
        abs_bytes
    ).hexdigest()
    if os.name == "nt":
        assert b"\r\n" in rel_bytes


def test_legacy_bic_portfolio_path_uses_platform_native_backslashes():
    project_root = Path(__file__).resolve().parents[1]
    portfolio = (
        project_root
        / "outputs"
        / "research_harness_e2e"
        / "bic_autonomous_20260816_v4_localfirst_publication"
        / "section_coverage"
        / "ARTICLE_EVIDENCE_PORTFOLIO.json"
    )

    normalized = ReviewHarnessOrchestrator._stable_project_relative_path(
        portfolio,
        project_root,
    )

    assert normalized == str(portfolio.relative_to(project_root))
    assert "\\" in normalized
    assert "/" not in normalized
