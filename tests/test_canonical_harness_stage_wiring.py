"""Deterministic tests for the canonical harness stage boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.r3_production_handoff import (
    build_r3_production_handoff,
    write_r3_production_handoff,
)
from optomind_research.runtime.review_harness_orchestrator import (
    HarnessArtifactValidationError,
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.topic_identity import (
    build_topic_identity_contract,
)
from optomind_research.runtime.topic_scoped_kb_stage import (
    SCOPE_DECISION_RULE_VERSION,
    _canonical_sha256,
    _reuse_contract,
    derive_topic_scope_contract,
)
from optomind_research.runtime.s2_policy_runtime import load_s2_policy
from optomind_research.runtime.topic_scoped_kb_stage import build_s2_query_telemetry
import run_review_harness
from run_review_harness import build_parser as build_review_harness_parser


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-canonical-r4"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _query_plan() -> dict:
    return {
        "input": {
            "user_query": "How do achromatic metalenses support near eye displays?",
        },
        "output": {
            "problem_understanding": (
                "Review achromatic metalenses for near eye displays and bandwidth."
            ),
            "scope_definition": {
                "main_scope": "Achromatic metalenses for near eye displays",
                "scope_items": ["bandwidth", "dispersion"],
            },
            "keyword_decomposition": {
                "keywords": ["achromatic metalens", "near eye display"],
            },
        },
    }


def _write_plan_and_base(tmp_path: Path) -> tuple[Path, Path]:
    plan_path = tmp_path / "query_plan.json"
    plan_path.write_text(
        json.dumps(_query_plan(), ensure_ascii=False), encoding="utf-8"
    )
    base = tmp_path / "core58.sqlite"
    with sqlite3.connect(base) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT)"
        )
        connection.commit()
    return plan_path, base


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_valid_manifest(
    *,
    work_dir: Path,
    query_plan_path: Path,
    source_base: Path,
    status: str = "partial",
) -> tuple[Path, Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    runtime = work_dir / "review_knowledge_base.s2.sqlite"
    with sqlite3.connect(runtime) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS text_chunks "
            "(chunk_id TEXT, paper_id TEXT, text TEXT)"
        )
        connection.commit()
    contract = derive_topic_scope_contract(
        json.loads(query_plan_path.read_text(encoding="utf-8"))
    )
    query_plan = json.loads(query_plan_path.read_text(encoding="utf-8"))
    policy = load_s2_policy()
    telemetry = build_s2_query_telemetry()
    telemetry_path = work_dir / "S2_QUERY_TELEMETRY.json"
    telemetry_path.write_text(
        json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reuse_contract = _reuse_contract(
        query_plan=query_plan,
        base_kb_sqlite=source_base,
        policy=policy,
        scope_contract=contract,
        papers=(),
        chunks=(),
        graph=None,
        query_telemetry=telemetry,
        extra_manifest={},
    )
    manifest = {
        "schema_version": "optomind.topic_scoped_kb_manifest.v1",
        "created_at": "2026-08-03T00:00:00Z",
        "status": status,
        "source_base_kb_sqlite": str(source_base),
        "source_base_kb_sha256": _file_sha256(source_base),
        "query_plan_path": str(query_plan_path),
        "query_plan_sha256": _file_sha256(query_plan_path),
        "policy_path": policy.config_path,
        "policy_sha256": policy.config_sha256,
        "policy": policy.to_dict(),
        "runtime_kb_sqlite": str(runtime),
        "runtime_kb_sha256": _file_sha256(runtime),
        "scope_decision_rule_version": SCOPE_DECISION_RULE_VERSION,
        "reuse_contract": reuse_contract,
        "scope_contract": contract.to_dict(),
        "selection": {},
        "ingest": {},
        "final_filter": {},
        "provenance_counts": {},
        "evidence": {},
        "evidence_sample": [],
        "table_counts": {},
        "s2_query_telemetry": telemetry,
        "telemetry_sha256": _file_sha256(telemetry_path),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest_path = work_dir / "KB_MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return runtime, manifest_path


def _write_valid_s2_bootstrap(
    *,
    work_dir: Path,
    scoped_base: Path,
    query_plan_path: Path,
    schema_version: str,
    status: str = "needs_more_literature",
) -> dict:
    """Write a fully hash-bound S2 bootstrap report and its sibling artifacts."""

    runtime, manifest_path = _write_valid_manifest(
        work_dir=work_dir,
        query_plan_path=query_plan_path,
        source_base=scoped_base,
        status=status,
    )
    graph_path = work_dir / "S2_LITERATURE_GRAPH.json"
    graph_path.write_text("{}", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = {
        "schema_version": schema_version,
        "status": status,
        "source_base_kb_sqlite": str(scoped_base),
        "source_base_kb_sha256": _file_sha256(scoped_base),
        "runtime_kb_sqlite": str(runtime),
        "runtime_kb_sha256": _file_sha256(runtime),
        "kb_manifest_path": str(manifest_path),
        "kb_manifest_sha256": manifest["manifest_sha256"],
        "graph_path": str(graph_path),
        "graph_sha256": _file_sha256(graph_path),
        "telemetry_sha256": _file_sha256(
            work_dir / "S2_QUERY_TELEMETRY.json"
        ),
        "accepted_s2_body_chunks": 0,
    }
    report["report_sha256"] = _canonical_sha256(report)
    report_path = work_dir / "S2_BOOTSTRAP_REPORT.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _harness(tmp_path: Path) -> tuple[ReviewHarnessOrchestrator, Path, Path]:
    plan_path, base = _write_plan_and_base(tmp_path)
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=plan_path,
            base_kb_sqlite=base,
            output_root=tmp_path,
            global_cost_budget_cny=100.0,
        ),
        run_dir=tmp_path / "run",
    )
    return harness, plan_path, base


@pytest.mark.parametrize(
    "schema_version",
    [
        "review_harness.s2_bootstrap.v2",
        "review_harness.s2_bootstrap.v3",
    ],
)
def test_scoped_kb_precedes_s2_and_broad_base_never_reaches_retrieval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str,
) -> None:
    harness, plan_path, broad_base = _harness(tmp_path)
    calls: list[tuple[str, Path]] = []

    def fake_build_topic_scoped_kb(**kwargs):
        calls.append(("topic_scoped_kb", Path(kwargs["base_kb_sqlite"])))
        runtime, manifest = _write_valid_manifest(
            work_dir=Path(kwargs["work_dir"]),
            query_plan_path=plan_path,
            source_base=broad_base,
        )
        return {
            "status": "partial",
            "runtime_kb_sqlite": str(runtime),
            "manifest_path": str(manifest),
        }

    def fake_prepare_s2(**kwargs):
        scoped_base = Path(kwargs["base_kb_sqlite"])
        calls.append(("s2", scoped_base))
        assert scoped_base != broad_base
        return _write_valid_s2_bootstrap(
            work_dir=Path(kwargs["work_dir"]),
            scoped_base=scoped_base,
            query_plan_path=plan_path,
            schema_version=schema_version,
        )

    import optomind_research.s2_harness_bootstrap as s2_module
    import optomind_research.runtime.topic_scoped_kb_stage as scoped_module

    monkeypatch.setattr(
        scoped_module, "build_topic_scoped_kb", fake_build_topic_scoped_kb
    )
    monkeypatch.setattr(
        s2_module, "prepare_s2_harness_kb", fake_prepare_s2
    )

    scoped = harness._prepare_topic_scoped_kb(broad_base)
    s2 = harness._prepare_s2_kb(
        scoped_runtime_kb=Path(scoped["runtime_kb_sqlite"])
    )

    assert [item[0] for item in calls] == ["topic_scoped_kb", "s2"]
    assert calls[0][1] == broad_base
    assert calls[1][1] == Path(scoped["runtime_kb_sqlite"])
    assert s2["status"] == "needs_more_literature"


def test_s2_bootstrap_unknown_schema_version_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, plan_path, _base = _harness(tmp_path)
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped")

    import optomind_research.s2_harness_bootstrap as s2_module

    def fake_prepare_s2(**kwargs):
        return _write_valid_s2_bootstrap(
            work_dir=Path(kwargs["work_dir"]),
            scoped_base=Path(kwargs["base_kb_sqlite"]),
            query_plan_path=plan_path,
            schema_version="review_harness.s2_bootstrap.v99",
        )

    monkeypatch.setattr(s2_module, "prepare_s2_harness_kb", fake_prepare_s2)
    with pytest.raises(
        HarnessArtifactValidationError,
        match="schema is incompatible",
    ):
        harness._prepare_s2_kb(scoped_runtime_kb=scoped_runtime)


def test_topic_manifest_paths_are_provenance_after_directory_migration(
    tmp_path: Path,
) -> None:
    harness, plan_path, base = _harness(tmp_path)
    work_dir = harness.work_dir / "topic_scoped_kb"
    runtime, manifest_path = _write_valid_manifest(
        work_dir=work_dir,
        query_plan_path=plan_path,
        source_base=base,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "source_base_kb_sqlite": r"C:\Users\old\OptoMind\base.sqlite",
            "query_plan_path": r"C:\Users\old\OptoMind\query.json",
            "runtime_kb_sqlite": r"C:\Users\old\OptoMind\runtime.sqlite",
        }
    )
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validated, resolved_runtime = harness._validate_topic_scoped_manifest(
        {
            "status": "partial",
            "manifest_path": r"C:\Users\old\OptoMind\KB_MANIFEST.json",
            "runtime_kb_sqlite": r"C:\Users\old\OptoMind\runtime.sqlite",
        },
        work_dir=work_dir,
        expected_source_base=base,
        stage="topic_scoped_kb",
    )
    assert validated["manifest_sha256"] == manifest["manifest_sha256"]
    assert resolved_runtime == runtime


def test_topic_manifest_never_falls_back_to_external_runtime_path(
    tmp_path: Path,
) -> None:
    harness, plan_path, base = _harness(tmp_path)
    work_dir = harness.work_dir / "topic_scoped_kb"
    runtime, manifest_path = _write_valid_manifest(
        work_dir=work_dir,
        query_plan_path=plan_path,
        source_base=base,
    )
    external_runtime = tmp_path / "external.sqlite"
    shutil.copy2(runtime, external_runtime)
    missing_runtime_stage = harness.work_dir / "missing-runtime-stage"
    missing_runtime_stage.mkdir(parents=True)
    shutil.copy2(manifest_path, missing_runtime_stage / "KB_MANIFEST.json")
    shutil.copy2(
        work_dir / "S2_QUERY_TELEMETRY.json",
        missing_runtime_stage / "S2_QUERY_TELEMETRY.json",
    )
    with pytest.raises(HarnessArtifactValidationError, match="current stage directory"):
        harness._validate_topic_scoped_manifest(
            {
                "status": "partial",
                "manifest_path": str(manifest_path),
                "runtime_kb_sqlite": str(external_runtime),
            },
            work_dir=missing_runtime_stage,
            expected_source_base=base,
            stage="topic_scoped_kb",
        )


def test_s2_failure_is_terminal_before_review_lead_or_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, _, base = _harness(tmp_path)
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped")
    calls: list[str] = []

    def fake_scoped(source_base: Path) -> dict:
        calls.append("topic_scoped_kb")
        return {
            "status": "partial",
            "runtime_kb_sqlite": str(scoped_runtime),
            "manifest_path": str(tmp_path / "manifest.json"),
        }

    def fake_s2(**kwargs) -> dict:
        calls.append("s2")
        assert Path(kwargs["scoped_runtime_kb"]) == scoped_runtime
        return {"status": "failed", "error": "offline bootstrap failure"}

    monkeypatch.setattr(harness, "_prepare_topic_scoped_kb", fake_scoped)
    monkeypatch.setattr(harness, "_prepare_s2_kb", fake_s2)

    result = harness.run()

    assert calls == ["topic_scoped_kb", "s2"]
    assert result.status == "failed"
    assert result.completed_stage == "s2_literature_intelligence"
    assert not (result.work_dir / "review_lead").exists()
    assert not (result.work_dir / "authoring").exists()
    assert base.exists()


def _valid_handoff(topic_identity: dict, *, unresolved: bool = False):
    claim = {
        "claim_id": "S01:C01",
        "section_id": "S01",
        "statement": "The measured mechanism controls the optical response.",
        "criticality": "load_bearing",
        "claim_state": "open_question" if unresolved else "grounded",
        "evidence_type": "mechanism",
        "unresolved": unresolved,
        "unresolved_reasons": ["independent validation is missing"]
        if unresolved
        else [],
    }
    binding = {
        "section_id": "S01",
        "claims": {
            "S01:C01": {
                "claim_id": "S01:C01",
                "evidence_binding_status": "unbound" if unresolved else "bound",
                "permission_status": "unbound" if unresolved else "bound",
                "write_status": "write_with_declared_gap" if unresolved else "bound",
                "supporting_chunk_ids": [] if unresolved else ["K01"],
                "factual_support_chunk_ids": [] if unresolved else ["K01"],
                "paper_ids": [] if unresolved else ["P01"],
            }
        },
    }
    return build_r3_production_handoff(
        topic_identity=topic_identity,
        sections=[
            {
                "section_id": "S01",
                "title": "Measured mechanism",
                "topic_identity": dict(topic_identity),
            }
        ],
        coverage_atlas={
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": dict(topic_identity),
            "sections": [{"section_id": "S01", "needs_expansion": unresolved}],
            "relation_graph": {"edge_count": 0},
        },
        section_argument_contracts={
            "S01": {
                "schema_version": "research_harness.section_argument_contract.v1",
                "section_id": "S01",
                "status": "contract_ready",
                "argument_tasks": [{
                    "task_id": "S01:T01",
                    "description": "Explain the measured mechanism.",
                }],
            }
        },
        claims_by_criticality={
            "load_bearing": [claim], "supporting": [], "optional": []
        },
        material_inventory={
            "papers": {
                "P01": {
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                }
            },
            "chunks": {
                "K01": {
                    "chunk_id": "K01",
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_kind": "fulltext",
                }
            },
            "visuals": {},
        },
        material_bindings={"S01": binding},
        relation_graph={"schema_version": "r3.relation_graph.v1", "edges": []},
        claim_dag={"schema_version": "research_harness.claim_graph.v1", "edges": []},
        gaps=(
            [{
                "gap_id": "G01",
                "section_id": "S01",
                "kind": "load_bearing_claim_material_gap",
                "claim_ids": ["S01:C01"],
                "blocking": True,
                "priority": "load_bearing",
            }]
            if unresolved
            else []
        ),
        coverage_requests=[],
        synthesis_bundles={
            "S01": {
                "section_id": "S01",
                "status": "needs_more_literature"
                if unresolved
                else "material_ready",
                "readiness_status": "needs_more_literature"
                if unresolved
                else "ready_for_authoring",
                "paper_ids": [] if unresolved else ["P01"],
                "chunk_ids": [] if unresolved else ["K01"],
                "claim_category_assignments": [{
                    "claim_id": "S01:C01",
                    "category": "established_points",
                }],
            }
        },
        visual_bindings={"S01": []},
        visual_needs={"S01": []},
    )


def test_legacy_r3_handoff_without_fingerprint_is_rebuilt_from_paid_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, plan_path, broad_base = _harness(tmp_path)
    topic = build_topic_identity_contract(_query_plan())
    phase3_root = tmp_path / "phase3"
    phase3_root.mkdir()
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _valid_handoff(topic),
        fail_on_invalid=True,
    )
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped-runtime")
    coverage = SimpleNamespace(
        work_dir=tmp_path / "coverage",
        material_bundles={},
    )
    coverage.work_dir.mkdir()
    blueprint = {
        "topic_identity": topic,
        "sections": [{"section_id": "S01", "title": "Measured mechanism"}],
    }
    harness.config.phase3_artifacts_root = phase3_root

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module

    captured: list[dict] = []

    class MustRebuild:
        def __init__(self, *args, **kwargs):
            captured.append(dict(kwargs))
            self.output_dir = Path(kwargs["output_dir"])

        def run(self):
            write_r3_production_handoff(
                self.output_dir / "R3_PRODUCTION_HANDOFF.json",
                _valid_handoff(topic),
                fail_on_invalid=True,
            )
            return {"status": "completed", "r4_handoff_ready": True}

    monkeypatch.setattr(phase3_module, "Phase3ArgumentOrchestrator", MustRebuild)
    result = harness._run_phase3_argument_orchestration(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )

    assert result["status"] == "completed"
    assert result["reused"] is False
    assert result["r3_handoff_path"].endswith("R3_PRODUCTION_HANDOFF.json")
    assert len(captured) == 1
    assert captured[0]["shared_kb_paths"] == [scoped_runtime.resolve()]
    assert (phase3_root / "_invalidated").exists()
    assert plan_path.exists()


def test_phase3_stage_accounting_reads_canonical_usage_and_is_resume_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, _, broad_base = _harness(tmp_path)
    phase3_root = tmp_path / "phase3"
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped-runtime")
    coverage = SimpleNamespace(work_dir=tmp_path / "coverage", material_bundles={})
    coverage.work_dir.mkdir()
    blueprint = {"sections": [{"section_id": "S01", "title": "Mechanism"}]}
    topic = build_topic_identity_contract(_query_plan())

    def fake_phase3(**kwargs):
        phase3_root.mkdir(parents=True, exist_ok=True)
        (phase3_root / "PHASE3_RUN.json").write_text(
            json.dumps({
                "llm": {
                    "calls_observed_or_estimated": 4,
                    "input_tokens_observed": 1200,
                    "output_tokens_observed": 300,
                    "estimated_cost_cny": 1.25,
                    "token_count_source": "provider_reported",
                    "usage_is_provider_reported": True,
                }
            }),
            encoding="utf-8",
        )
        return {
            "status": "completed",
            "work_dir": str(phase3_root),
            "reused": False,
        }

    monkeypatch.setattr(harness, "_run_phase3_argument_orchestration", fake_phase3)
    first = harness._run_phase3_stage_and_record(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert first["status"] == "completed"
    first_stage = dict(harness.stage_costs["phase3_argument_orchestration"])
    assert first_stage["estimated_cost_cny"] == 1.25
    assert first_stage["input_tokens"] == 1200
    assert first_stage["output_tokens"] == 300
    assert first_stage["model_call_count"] == 4
    assert harness._total_cost_cny() >= 1.25

    second = harness._run_phase3_stage_and_record(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert second["status"] == "completed"
    second_stage = harness.stage_costs["phase3_argument_orchestration"]
    assert second_stage["estimated_cost_cny"] == 1.25
    assert second_stage["input_tokens"] == 1200
    assert second_stage["output_tokens"] == 300
    assert second_stage["model_call_count"] == 4


def test_phase3_input_views_exclude_broad_kb_and_keep_section_overlay(
    tmp_path: Path,
) -> None:
    harness, _, broad_base = _harness(tmp_path)
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped-runtime")
    supplemental = tmp_path / "supplemental_oa_kb.sqlite"
    supplemental.write_bytes(b"supplemental")
    ledger = tmp_path / "coverage" / "sections" / "S01" / "SECTION_SOURCE_LEDGER.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "section_id": "S01",
            "sources": [{
                "paper_id": "P01",
                "canonical_chunk_ids": ["K01"],
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
            }],
        }),
        encoding="utf-8",
    )
    coverage = SimpleNamespace(
        work_dir=tmp_path / "coverage",
        material_bundles={
            "S01": SimpleNamespace(
                source_ledger_path=ledger,
                kb_sqlite=broad_base,
                staging_kb_sqlite=supplemental,
            )
        },
    )
    result = harness._build_phase3_inputs(
        blueprint={"sections": [{"section_id": "S01", "title": "Mechanism"}]},
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
        phase3_root=tmp_path / "phase3",
    )

    assert result["shared_kb_paths"] == [scoped_runtime.resolve(), supplemental.resolve()]
    assert broad_base.resolve() not in result["shared_kb_paths"]
    overlay = json.loads(
        result["overlay_paths"]["S01"].read_text(encoding="utf-8")
    )
    assert str(broad_base.resolve()) not in overlay["shared_kb_paths"]
    assert str(scoped_runtime.resolve()) in overlay["shared_kb_paths"]


def test_unresolved_r3_sections_close_authoring_with_needs_more_literature(
    tmp_path: Path,
) -> None:
    harness, _, _ = _harness(tmp_path)
    topic = build_topic_identity_contract(_query_plan())
    phase3_root = tmp_path / "phase3"
    phase3_root.mkdir()
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _valid_handoff(topic, unresolved=True),
        fail_on_invalid=True,
    )
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint={
            "sections": [{"section_id": "S01", "title": "Mechanism"}]
        },
    )
    assert gate["status"] == "needs_more_literature"
    assert gate["blocked_sections"]


def test_phase3_live_policy_is_explicit_and_offline_policy_is_zero_api(
    tmp_path: Path,
):
    harness, _, _ = _harness(tmp_path)
    harness.config.visual_test_mode = False
    live = harness._phase3_runtime_options(section_count=2)
    assert live["real_llm_claims"] is True
    assert live["real_llm_dag"] is False
    assert live["claim_model_tier"] == "b_plus_model"
    assert live["execute_coverage"] is True
    large = harness._phase3_runtime_options(section_count=10)
    assert large["real_llm_dag"] is False

    harness.config.phase3_real_llm_dag = True
    opted_in = harness._phase3_runtime_options(section_count=2)
    assert opted_in["real_llm_dag"] is True

    harness.config.phase3_real_llm_dag = None
    harness.config.visual_test_mode = True
    offline = harness._phase3_runtime_options(section_count=2)
    assert offline["real_llm_claims"] is False
    assert offline["real_llm_dag"] is False
    assert offline["execute_coverage"] is False

    harness.config.phase3_execute_coverage = False
    explicitly_disabled = harness._phase3_runtime_options(section_count=2)
    assert explicitly_disabled["execute_coverage"] is False


def test_phase3_dag_cli_defaults_off_and_requires_explicit_opt_in() -> None:
    parser = build_review_harness_parser()
    default_args = parser.parse_args(["--query-plan", "query_plan.json"])
    assert default_args.phase3_llm_dag is None

    opted_in = parser.parse_args(["--query-plan", "query_plan.json", "--phase3-llm-dag"])
    assert opted_in.phase3_llm_dag is True

    opted_out = parser.parse_args(["--query-plan", "query_plan.json", "--no-phase3-llm-dag"])
    assert opted_out.phase3_llm_dag is False

    with pytest.raises(SystemExit):
        parser.parse_args([
            "--query-plan",
            "query_plan.json",
            "--phase3-llm-dag",
            "--no-phase3-llm-dag",
        ])


def _write_source_base_query_plan(tmp_path: Path) -> Path:
    path = tmp_path / "query_plan.json"
    path.write_text(
        json.dumps(
            {
                "input": {"user_query": "Review achromatic metalenses."},
                "output": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_source_base_snapshot_fresh_and_resume_uses_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_dir = tmp_path / "run"
    (run_dir / "task_material").mkdir(parents=True)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "CURRENT.json").write_text("{}", encoding="utf-8")
    query_path = _write_source_base_query_plan(tmp_path)
    project_calls: list[dict[str, Path]] = []

    def fake_project(*, query_plan_path, output_kb_path, cache_root, report_path, **kwargs):
        project_calls.append(
            {
                "output": Path(output_kb_path),
                "report": Path(report_path),
                "cache_root": Path(cache_root),
            }
        )
        Path(output_kb_path).write_bytes(b"source-projection-v1")
        Path(report_path).write_text(
            json.dumps(
                {
                    "snapshot": str(cache_root / "snapshot-000001"),
                    "input_fingerprint": "fingerprint-v1",
                }
            ),
            encoding="utf-8",
        )
        return {"status": "completed"}

    monkeypatch.setattr(run_review_harness, "project_to_review_kb", fake_project)
    monkeypatch.setattr(
        run_review_harness,
        "resolve_current_snapshot",
        lambda root: root / "snapshot-000001",
    )

    first_path, first_role = run_review_harness._base_kb_for_run(
        None,
        run_dir=run_dir,
        allow_historical_test_assets=False,
        materialize_empty_seed=True,
        query_plan_path=query_path,
        long_term_material_cache_root=cache_root,
    )

    assert first_role == "central_long_term_material_cache_projection"
    assert first_path.is_file()
    assert first_path.read_bytes() == b"source-projection-v1"
    assert len(project_calls) == 1
    assert project_calls[0]["cache_root"] == cache_root

    metadata = json.loads(
        (
            run_dir / "task_material" / "SOURCE_BASE_SNAPSHOT.json"
        ).read_text(encoding="utf-8")
    )
    assert metadata["source_base_kb"] == str(first_path)
    assert metadata["source_base_sha256"]

    # Resume must use the recorded snapshot even when the central pointer advances.
    monkeypatch.setattr(
        run_review_harness,
        "resolve_current_snapshot",
        lambda root: root / "snapshot-000002",
    )
    resumed_path, resumed_role = run_review_harness._base_kb_for_run(
        None,
        run_dir=run_dir,
        allow_historical_test_assets=False,
        materialize_empty_seed=True,
        query_plan_path=query_path,
        long_term_material_cache_root=cache_root,
    )
    assert resumed_path == first_path
    assert resumed_role == first_role
    assert len(project_calls) == 1


def test_source_base_legacy_run_without_snapshot_fails_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_dir = tmp_path / "legacy-run"
    task_material = run_dir / "task_material"
    task_material.mkdir(parents=True)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "CURRENT.json").write_text("{}", encoding="utf-8")
    query_path = _write_source_base_query_plan(tmp_path)

    projection = task_material / "LONG_TERM_MATERIAL_PROJECTION.sqlite"
    projection.write_bytes(b"legacy-projection")
    report = task_material / "LONG_TERM_MATERIAL_PROJECTION.json"
    report.write_text(
        json.dumps(
            {
                "snapshot": str(cache_root / "snapshot-000001"),
                "input_fingerprint": "legacy-fingerprint",
            }
        ),
        encoding="utf-8",
    )

    called = False

    def fake_project(**kwargs):
        nonlocal called
        called = True
        return {"status": "completed"}

    monkeypatch.setattr(run_review_harness, "project_to_review_kb", fake_project)
    monkeypatch.setattr(
        run_review_harness,
        "resolve_current_snapshot",
        lambda root: root / "snapshot-000002",
    )

    with pytest.raises(ValueError, match="legacy run has no source-base snapshot"):
        run_review_harness._base_kb_for_run(
            None,
            run_dir=run_dir,
            allow_historical_test_assets=False,
            materialize_empty_seed=True,
            query_plan_path=query_path,
            long_term_material_cache_root=cache_root,
        )
    assert called is False


def test_source_base_missing_snapshot_fails_closed_without_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    run_dir = tmp_path / "missing-snapshot-run"
    task_material = run_dir / "task_material"
    task_material.mkdir(parents=True)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "CURRENT.json").write_text("{}", encoding="utf-8")
    query_path = _write_source_base_query_plan(tmp_path)

    metadata_path = task_material / "SOURCE_BASE_SNAPSHOT.json"
    missing_snapshot = task_material / "source_base_snapshot" / "missing.sqlite"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "optomind.run_source_base_snapshot.v1",
                "source_base_kb": str(missing_snapshot),
                "source_base_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    projection = task_material / "LONG_TERM_MATERIAL_PROJECTION.sqlite"
    projection.write_bytes(b"legacy-projection")
    report = task_material / "LONG_TERM_MATERIAL_PROJECTION.json"
    report.write_text(
        json.dumps({"snapshot": str(cache_root / "snapshot-000001")}),
        encoding="utf-8",
    )

    called = False

    def fake_project(**kwargs):
        nonlocal called
        called = True
        return {"status": "completed"}

    monkeypatch.setattr(run_review_harness, "project_to_review_kb", fake_project)
    monkeypatch.setattr(
        run_review_harness,
        "resolve_current_snapshot",
        lambda root: root / "snapshot-000002",
    )

    with pytest.raises(ValueError, match="legacy run has no source-base snapshot"):
        run_review_harness._base_kb_for_run(
            None,
            run_dir=run_dir,
            allow_historical_test_assets=False,
            materialize_empty_seed=True,
            query_plan_path=query_path,
            long_term_material_cache_root=cache_root,
        )
    assert called is False


def test_bounded_no_candidate_status_is_not_mislabeled_runtime_failure(
    tmp_path: Path,
):
    harness, plan_path, broad_base = _harness(tmp_path)
    coverage_dir = tmp_path / "coverage"
    coverage_dir.mkdir()
    (coverage_dir / "SECTION_COVERAGE_RUN.json").write_text(
        json.dumps({
            "sections": [{
                "section_id": "S01",
                "status": "failed",
                "worker_status": "deterministic_short_path",
                "stop_reason": "bounded_waves_exhausted_without_candidates",
            }]
        }),
        encoding="utf-8",
    )
    scoped = tmp_path / "scoped.sqlite"
    scoped.write_bytes(b"scoped-run-local-kb")
    result = harness._build_phase3_inputs(
        blueprint={"sections": [{"section_id": "S01", "title": "Mechanism"}]},
        coverage=SimpleNamespace(work_dir=coverage_dir, material_bundles={}),
        scoped_runtime_kb=scoped,
        source_base_kb=broad_base,
        phase3_root=tmp_path / "phase3",
        runtime_options={"real_llm_claims": False},
    )
    assert result["runtime_failures"] == {}


def test_phase3_wiring_passes_live_mode_and_rebuilds_stale_config_from_local_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    harness, _, broad_base = _harness(tmp_path)
    harness.config.visual_test_mode = False
    harness.config.phase3_real_llm_claims = True
    harness.config.phase3_real_llm_dag = True
    topic = build_topic_identity_contract(_query_plan())
    scoped_runtime = tmp_path / "scoped.sqlite"
    scoped_runtime.write_bytes(b"scoped-runtime")
    coverage = SimpleNamespace(work_dir=tmp_path / "coverage", material_bundles={})
    coverage.work_dir.mkdir()
    blueprint = {
        "topic_identity": topic,
        "sections": [{"section_id": "S01", "title": "Mechanism"}],
    }
    phase3_root = tmp_path / "phase3"
    harness.config.phase3_artifacts_root = phase3_root
    captured: list[dict] = []
    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module

    class FakePhase3:
        def __init__(self, **kwargs):
            captured.append(dict(kwargs))
            self.output_dir = Path(kwargs["output_dir"])

        def run(self):
            write_r3_production_handoff(
                self.output_dir / "R3_PRODUCTION_HANDOFF.json",
                _valid_handoff(topic),
                fail_on_invalid=True,
            )
            return {"status": "passed", "r4_handoff_ready": True}

    monkeypatch.setattr(phase3_module, "Phase3ArgumentOrchestrator", FakePhase3)
    first = harness._run_phase3_argument_orchestration(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert first["reused"] is False
    assert captured[0]["real_llm_claims"] is True
    assert captured[0]["real_llm_dag"] is True
    assert captured[0]["shared_kb_paths"] == [scoped_runtime.resolve()]

    # A changed Phase-3 cap invalidates only Phase 3.  It must rebuild from
    # the same run-local KB rather than demand a Phase-2 rerun.
    harness.config.phase3_max_m2a_records = 12
    second = harness._run_phase3_argument_orchestration(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert second["reused"] is False
    assert len(captured) == 2
    assert (phase3_root / "_invalidated").exists()


def _make_phase3_recovery_snapshot(
    tmp_path: Path,
    *,
    section_status: str = "needs_more_literature",
    missing_record: bool = False,
    corrupt_package: bool = False,
    include_handoff: bool = True,
    provided_plan_mode: bool = False,
) -> tuple[ReviewHarnessOrchestrator, dict, dict, Path, Path]:
    """Create a same-run, already-paid Phase-2/Phase-3 recovery fixture."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    harness, plan_path, broad_base = _harness(tmp_path)
    topic = build_topic_identity_contract(_query_plan())
    run_dir = harness.work_dir

    if provided_plan_mode:
        harness.config.query_plan_path = plan_path
    else:
        query_dir = run_dir / "query_planner"
        query_dir.mkdir(parents=True, exist_ok=True)
        cached_plan = query_dir / "query_plan.json"
        cached_plan.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
        harness.config.query_plan_path = cached_plan
        (query_dir / "ORIGINAL_USER_QUESTION.json").write_text(
            json.dumps({"user_question": _query_plan()["input"]["user_query"]}),
            encoding="utf-8",
        )
        (run_dir / "QUERY_PLAN_ENTRY_GATE.json").write_text(
            json.dumps({"status": "passed", "execution_ready": True}),
            encoding="utf-8",
        )
    (run_dir / "TOPIC_IDENTITY.json").write_text(
        json.dumps(topic), encoding="utf-8"
    )

    blueprint = {
        "topic_identity": topic,
        "sections": [{
            "section_id": "S01",
            "title": "Achromatic metalens measured mechanism",
        }],
    }
    blueprint_path = run_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
    blueprint_path.parent.mkdir(parents=True, exist_ok=True)
    blueprint_path.write_text(json.dumps(blueprint), encoding="utf-8")
    (run_dir / "HARNESS_STATE.json").write_text(
        json.dumps({
            "schema_version": "research_harness.state.v1",
            "run_id": run_dir.name,
            "status": "running",
            "current_stage": "section_coverage",
            "stages": {
                "review_lead": {"status": "completed"},
                "phase3_argument_orchestration": {"status": "needs_more_literature"},
            },
        }),
        encoding="utf-8",
    )
    harness._resumed_existing_run = True

    scoped_runtime = run_dir / "s2_literature_intelligence" / "review_knowledge_base.s2.sqlite"
    scoped_runtime.parent.mkdir(parents=True, exist_ok=True)
    scoped_runtime.write_bytes(b"scoped-runtime")

    coverage_root = run_dir / "section_coverage"
    section_dir = coverage_root / "sections" / "S01"
    section_dir.mkdir(parents=True, exist_ok=True)
    package_path = section_dir / "SECTION_MATERIAL_PACKAGE.json"
    ledger_path = section_dir / "SECTION_SOURCE_LEDGER.json"
    if not corrupt_package:
        package_path.write_text(
            json.dumps({
                "schema_version": "2.0",
                "section_id": "S01",
                "section_title": blueprint["sections"][0]["title"],
                "coverage_outcome": (
                    "material_ready"
                    if section_status == "completed"
                    else "needs_more_literature"
                ),
            }),
            encoding="utf-8",
        )
    ledger_path.write_text(
        json.dumps({"schema_version": "2.0", "section_id": "S01", "sources": []}),
        encoding="utf-8",
    )

    records = []
    bundles = {}
    if not missing_record:
        record = {
            "section_id": "S01",
            "status": section_status,
            "worker_status": "deterministic_short_path",
            "work_dir": str(section_dir),
            "input_tokens": 1200,
            "output_tokens": 300,
            "cost_cny": 1.25,
        }
        records.append(record)
        bundles["S01"] = {
            "material_package_path": str(package_path),
            "source_ledger_path": str(ledger_path),
            "kb_sqlite": str(scoped_runtime),
            "staging_kb_sqlite": "",
        }
    manifest = {
        "schema_version": "research_harness.section_coverage_run.v1",
        "run_id": "section_coverage",
        "status": "partial",
        "blueprint_path": str(blueprint_path),
        "base_kb_sqlite": str(scoped_runtime),
        "supplemental_kb_sqlite": "",
        "sections": records,
        "material_bundles": bundles,
        "total_cost_cny": 1.25 if records else 0.0,
        "total_input_tokens": 1200 if records else 0,
        "total_output_tokens": 300 if records else 0,
        "total_cost_basis": "estimated_list_price",
        "cost_is_estimated": True,
    }
    coverage_root.mkdir(parents=True, exist_ok=True)
    (coverage_root / "SECTION_COVERAGE_RUN.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (coverage_root / "ARTICLE_EVIDENCE_PORTFOLIO.json").write_text(
        json.dumps({"schema_version": "phase2.article_evidence_portfolio.v1"}),
        encoding="utf-8",
    )

    phase3_root = run_dir / "phase3_argument_orchestration"
    phase3_root.mkdir(parents=True, exist_ok=True)
    if include_handoff:
        write_r3_production_handoff(
            phase3_root / "R3_PRODUCTION_HANDOFF.json",
            _valid_handoff(topic),
            fail_on_invalid=True,
        )
    (phase3_root / "PHASE3_INPUT_FINGERPRINT.json").write_text(
        json.dumps({
            "schema_version": "research_harness.phase3_input_fingerprint.v1",
            "sha256": "paid-phase3-fingerprint",
            "files": {
                "query_plan": _file_sha256(harness.config.query_plan_path)
            },
        }),
        encoding="utf-8",
    )
    harness.config.phase3_artifacts_root = phase3_root
    # Automatic same-run recovery must not require a rebuild flag.  The
    # Phase-3 input fingerprint decides whether the handoff is reused or
    # archived after the paid Phase-2 snapshot has been rehydrated.
    harness.config.rebuild_phase3_handoff = False
    return harness, blueprint, topic, scoped_runtime, broad_base


def test_phase3_recovery_rehydrates_without_coverage_worker_and_preserves_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path, section_status="needs_more_literature"
    )
    import optomind_research.runtime.review_harness_orchestrator as harness_module

    class ForbiddenCoverageWorker:
        def __init__(self, *args, **kwargs):
            raise AssertionError("coverage worker must not be constructed")

    monkeypatch.setattr(harness_module, "SectionCoverageOrchestrator", ForbiddenCoverageWorker)
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None
    assert coverage.reused_for_phase3_recovery is True
    assert coverage.sections_needing_more_literature == 1
    assert coverage.total_cost_cny == 1.25
    assert coverage.total_input_tokens == 1200
    assert coverage.recovery_telemetry["coverage_worker_called"] is False
    assert coverage.recovery_telemetry["portfolio_retry_allowed"] is False


def test_interrupted_phase3_recovery_needs_rebuild_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path, include_handoff=False
    )
    import optomind_research.runtime.review_harness_orchestrator as harness_module

    class ForbiddenCoverageWorker:
        def __init__(self, *args, **kwargs):
            raise AssertionError("coverage worker must not be constructed")

    monkeypatch.setattr(harness_module, "SectionCoverageOrchestrator", ForbiddenCoverageWorker)
    assert harness.config.rebuild_phase3_handoff is False
    with pytest.raises(HarnessArtifactValidationError, match="both"):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )

    harness.config.rebuild_phase3_handoff = True
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None
    assert coverage.reused_for_phase3_recovery is True
    assert coverage.total_cost_cny == 1.25
    assert coverage.recovery_telemetry["coverage_worker_called"] is False
    assert coverage.recovery_telemetry["portfolio_retry_allowed"] is False


def test_handoff_present_fingerprint_missing_never_rehydrates(
    tmp_path: Path,
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path
    )
    harness.config.rebuild_phase3_handoff = True
    phase3_root = harness.config.phase3_artifacts_root
    (phase3_root / "PHASE3_INPUT_FINGERPRINT.json").unlink()
    with pytest.raises(HarnessArtifactValidationError, match="both"):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )


def test_provided_plan_entry_mode_rehydrates_without_query_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path,
        include_handoff=False,
        provided_plan_mode=True,
    )
    harness.config.rebuild_phase3_handoff = True
    import optomind_research.runtime.review_harness_orchestrator as harness_module

    class ForbiddenCoverageWorker:
        def __init__(self, *args, **kwargs):
            raise AssertionError("coverage worker must not be constructed")

    monkeypatch.setattr(harness_module, "SectionCoverageOrchestrator", ForbiddenCoverageWorker)
    assert not (harness.work_dir / "QUERY_PLAN_ENTRY_GATE.json").exists()
    assert not (harness.work_dir / "query_planner").exists()
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None
    assert coverage.reused_for_phase3_recovery is True
    assert coverage.recovery_telemetry["coverage_worker_called"] is False
    assert coverage.recovery_telemetry["portfolio_retry_allowed"] is False


def test_provided_plan_entry_mode_rejects_mismatched_query_plan_hash(
    tmp_path: Path,
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path,
        include_handoff=False,
        provided_plan_mode=True,
    )
    harness.config.rebuild_phase3_handoff = True
    phase3_root = harness.config.phase3_artifacts_root
    fingerprint_path = phase3_root / "PHASE3_INPUT_FINGERPRINT.json"
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    fingerprint["files"]["query_plan"] = _file_sha256(
        harness.work_dir / "review_lead" / "REVIEW_BLUEPRINT.json"
    )
    fingerprint_path.write_text(json.dumps(fingerprint), encoding="utf-8")
    with pytest.raises(HarnessArtifactValidationError, match="another query plan"):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )


def test_partial_natural_language_confirmation_set_fails_closed(
    tmp_path: Path,
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path,
        include_handoff=False,
    )
    harness.config.rebuild_phase3_handoff = True
    (harness.work_dir / "query_planner" / "query_plan.json").unlink()
    with pytest.raises(HarnessArtifactValidationError, match="incomplete"):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )


def test_malformed_interrupted_phase3_fingerprint_fails_closed(
    tmp_path: Path,
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path, include_handoff=False
    )
    harness.config.rebuild_phase3_handoff = True
    phase3_root = harness.config.phase3_artifacts_root
    (phase3_root / "PHASE3_INPUT_FINGERPRINT.json").write_text(
        json.dumps({"schema_version": "bad", "sha256": "", "files": {}}),
        encoding="utf-8",
    )
    with pytest.raises(HarnessArtifactValidationError, match="fingerprint"):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )


def test_phase3_recovery_preserves_completed_and_failed_truth_and_missing_gap(
    tmp_path: Path,
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path, section_status="completed"
    )
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None
    assert coverage.sections_completed == 1
    assert coverage.material_bundles["S01"].material_package_path.is_file()

    failed_harness, failed_blueprint, failed_topic, failed_scoped, failed_broad = (
        _make_phase3_recovery_snapshot(
            tmp_path / "failed", section_status="failed"
        )
    )
    failed = failed_harness._rehydrate_phase3_recovery_coverage(
        blueprint=failed_blueprint,
        topic_identity=failed_topic,
        scoped_runtime_kb=failed_scoped,
        source_base_kb=failed_broad,
    )
    assert failed is not None
    assert failed.sections_failed == 1
    assert not failed.material_bundles

    missing_harness, missing_blueprint, missing_topic, missing_scoped, missing_broad = (
        _make_phase3_recovery_snapshot(tmp_path / "missing", missing_record=True)
    )
    missing = missing_harness._rehydrate_phase3_recovery_coverage(
        blueprint=missing_blueprint,
        topic_identity=missing_topic,
        scoped_runtime_kb=missing_scoped,
        source_base_kb=missing_broad,
    )
    assert missing is not None
    assert missing.recovery_telemetry["missing_section_ids"] == ["S01"]
    assert missing.sections_needing_more_literature == 1


@pytest.mark.parametrize("corruption", ["missing_manifest", "wrong_section_id", "cross_topic_path"])
def test_phase3_recovery_corrupt_snapshot_fails_before_any_api_path(
    tmp_path: Path, corruption: str
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path
    )
    coverage_root = harness.work_dir / "section_coverage"
    if corruption == "missing_manifest":
        (coverage_root / "SECTION_COVERAGE_RUN.json").unlink()
    elif corruption == "wrong_section_id":
        package = coverage_root / "sections" / "S01" / "SECTION_MATERIAL_PACKAGE.json"
        package.write_text(json.dumps({"section_id": "S99"}), encoding="utf-8")
    else:
        manifest = json.loads((coverage_root / "SECTION_COVERAGE_RUN.json").read_text())
        manifest["material_bundles"]["S01"]["material_package_path"] = str(
            tmp_path / "outside" / "SECTION_MATERIAL_PACKAGE.json"
        )
        (coverage_root / "SECTION_COVERAGE_RUN.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    with pytest.raises(HarnessArtifactValidationError):
        harness._rehydrate_phase3_recovery_coverage(
            blueprint=blueprint,
            topic_identity=topic,
            scoped_runtime_kb=scoped_runtime,
            source_base_kb=broad_base,
        )


def test_phase3_recovery_calls_phase3_and_suppresses_portfolio_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path
    )
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module
    phase3_calls: list[dict] = []

    class FakePhase3:
        def __init__(self, **kwargs):
            phase3_calls.append(dict(kwargs))
            self.output_dir = Path(kwargs["output_dir"])

        def run(self):
            write_r3_production_handoff(
                self.output_dir / "R3_PRODUCTION_HANDOFF.json",
                _valid_handoff(topic),
                fail_on_invalid=True,
            )
            return {"status": "needs_more_literature", "r4_handoff_ready": False}

    monkeypatch.setattr(phase3_module, "Phase3ArgumentOrchestrator", FakePhase3)
    result = harness._run_phase3_argument_orchestration(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert phase3_calls
    assert result["reused"] is False
    assert coverage.recovery_telemetry["portfolio_retry_allowed"] is False
    assert coverage.recovery_telemetry["coverage_worker_called"] is False


def test_interrupted_phase3_rebuild_archives_partial_and_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, blueprint, topic, scoped_runtime, broad_base = _make_phase3_recovery_snapshot(
        tmp_path, include_handoff=False
    )
    harness.config.rebuild_phase3_handoff = True
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None
    phase3_root = Path(harness.config.phase3_artifacts_root)
    assert not (phase3_root / "R3_PRODUCTION_HANDOFF.json").exists()

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module
    phase3_calls: list[dict] = []

    class FakePhase3:
        def __init__(self, **kwargs):
            phase3_calls.append(dict(kwargs))
            self.output_dir = Path(kwargs["output_dir"])

        def run(self):
            write_r3_production_handoff(
                self.output_dir / "R3_PRODUCTION_HANDOFF.json",
                _valid_handoff(topic),
                fail_on_invalid=True,
            )
            (self.output_dir / "PHASE3_ACCEPTANCE.json").write_text(
                json.dumps({"status": "passed", "r4_handoff_ready": True}),
                encoding="utf-8",
            )
            (self.output_dir / "PHASE3_RUN.json").write_text(
                json.dumps({"section_statuses": {"S01": "completed"}, "coverage_runs": []}),
                encoding="utf-8",
            )
            return {"status": "completed", "r4_handoff_ready": True}

    monkeypatch.setattr(phase3_module, "Phase3ArgumentOrchestrator", FakePhase3)
    result = harness._run_phase3_argument_orchestration(
        blueprint=blueprint,
        topic_identity=topic,
        coverage=coverage,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert phase3_calls
    assert result["reused"] is False
    assert (phase3_root / "_invalidated").exists()
    assert (phase3_root / "R3_PRODUCTION_HANDOFF.json").exists()


def test_needs_more_r3_resume_admits_authoring_without_upstream_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal needs_more run with a valid R3 resumes at the authoring gate.

    Phase 2 is never re-entered (its worker is forbidden here), Phase 3 is
    never rebuilt (the existing fingerprint/handoff are reused), and the new
    fail-open-with-limits policy admits the unresolved section instead of
    returning needs_more_literature.
    """

    harness, blueprint, topic, scoped_runtime, broad_base = (
        _make_phase3_recovery_snapshot(
            tmp_path, section_status="needs_more_literature"
        )
    )
    import optomind_research.runtime.review_harness_orchestrator as harness_module

    class ForbiddenCoverageWorker:
        def __init__(self, *args, **kwargs):
            raise AssertionError("coverage worker must not be constructed")

    monkeypatch.setattr(
        harness_module,
        "SectionCoverageOrchestrator",
        ForbiddenCoverageWorker,
    )
    coverage = harness._rehydrate_phase3_recovery_coverage(
        blueprint=blueprint,
        topic_identity=topic,
        scoped_runtime_kb=scoped_runtime,
        source_base_kb=broad_base,
    )
    assert coverage is not None

    phase3_root = Path(harness.config.phase3_artifacts_root)
    fingerprint_path = phase3_root / "PHASE3_INPUT_FINGERPRINT.json"
    fingerprint_before = fingerprint_path.read_bytes()
    # Replace the ready fixture with an unresolved (needs_more) but
    # structurally authorable canonical handoff and give the section its
    # material context.
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _valid_handoff(topic, unresolved=True),
        fail_on_invalid=True,
    )
    ledger = (
        phase3_root
        / "coverage_snapshot"
        / "sections"
        / "S01"
        / "SECTION_SOURCE_LEDGER.json"
    )
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({
            "section_id": "S01",
            "sources": [{
                "paper_id": "P01",
                "title": "Measured mechanism study",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
            }],
        }),
        encoding="utf-8",
    )
    (phase3_root / "shared_kb.sqlite").write_bytes(b"sqlite-placeholder")

    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint=blueprint,
    )
    assert gate["status"] == "passed"
    assert gate["ready_section_ids"] == ["S01"]
    assert gate["admitted_with_limits_section_ids"] == ["S01"]
    assert gate["phase3_advisory_outcomes"]["S01"] == "needs_more_literature"
    admission = json.loads(
        (phase3_root / "R4_AUTHORING_ADMISSION.json").read_text(
            encoding="utf-8"
        )
    )
    assert admission["admitted_section_ids"] == ["S01"]
    assert admission["admitted_with_limits_section_ids"] == ["S01"]
    # Existing paid Phase-3 inputs were never rewritten or rebuilt.
    assert fingerprint_path.read_bytes() == fingerprint_before
