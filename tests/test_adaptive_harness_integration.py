"""Focused, offline integration checks for adaptive harness admission."""

from __future__ import annotations

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
    read_r3_production_handoff,
    write_r3_production_handoff,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.topic_identity import build_topic_identity_contract


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-adaptive-r4"
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
            "user_query": "How do achromatic metalenses support near eye displays?"
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
                "keywords": ["achromatic metalens", "near eye display"]
            },
        },
    }


def _harness(tmp_path: Path) -> ReviewHarnessOrchestrator:
    query_plan = tmp_path / "query_plan.json"
    query_plan.write_text(
        json.dumps(_query_plan(), ensure_ascii=False), encoding="utf-8"
    )
    broad = tmp_path / "broad.sqlite"
    with sqlite3.connect(broad) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT)"
        )
        connection.commit()
    return ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=broad,
            output_root=tmp_path,
            global_cost_budget_cny=100.0,
            produce_research_plan=False,
        ),
        run_dir=tmp_path / "run",
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _r3_handoff(topic: dict, *, all_weak: bool = False):
    sections = [
        {
            "section_id": "S01",
            "title": "Governing mechanism",
            "topic_identity": dict(topic),
        },
        {
            "section_id": "S02",
            "title": "Optional comparison",
            "topic_identity": dict(topic),
        },
    ]
    claim = {
        "claim_id": "S01:C01",
        "section_id": "S01",
        "statement": "The measured mechanism controls the optical response.",
        "criticality": "load_bearing",
        "importance": "load_bearing",
        "claim_state": "grounded",
        "evidence_type": "mechanism",
        "claim_classification": "supported",
        "support_classification": "supported",
        "evidence_binding_status": "bound",
        "permission_status": "bound",
        "supporting_chunk_ids": ["K01"],
        "factual_support_chunk_ids": ["K01"],
        "core_chunk_ids": ["K01"],
        "core_paper_ids": ["P01"],
        "claim_provenance": {
            "declared_support_chunk_ids": ["K01"],
            "eligible_support_chunk_ids": ["K01"],
            "source_permissions": {
                "K01": {"evidence_ceiling": "factual_support"}
            },
        },
    }
    claims_by = {"load_bearing": [] if all_weak else [claim], "supporting": [], "optional": []}
    bindings = {}
    bundles = {}
    gaps = []
    if not all_weak:
        bindings["S01"] = {
            "section_id": "S01",
            "claims": {
                "S01:C01": {
                    "claim_id": "S01:C01",
                    "evidence_binding_status": "bound",
                    "permission_status": "bound",
                    "write_status": "bound",
                    "supporting_chunk_ids": ["K01"],
                    "factual_support_chunk_ids": ["K01"],
                    "paper_ids": ["P01"],
                    "classification": "supported",
                    "support_classification": "supported",
                }
            },
        }
        bundles["S01"] = {
            "section_id": "S01",
            "status": "material_ready",
            "section_outcome": "ready",
            "readiness_status": "ready_for_authoring",
            "paper_ids": ["P01"],
            "chunk_ids": ["K01"],
            "claim_category_assignments": [
                {
                    "claim_id": "S01:C01",
                    "category": "established_points",
                    "classification": "supported",
                }
            ],
        }
    bindings.setdefault("S02", {"section_id": "S02", "claims": {}})
    if all_weak:
        bindings["S01"] = {"section_id": "S01", "claims": {}}
    for section_id in ("S01", "S02"):
        if section_id not in bundles:
            bundles[section_id] = {
                "section_id": section_id,
                "status": "needs_more_literature",
                "section_outcome": "needs_more_literature",
                "readiness_status": "needs_more_literature",
                "paper_ids": [],
                "chunk_ids": [],
                "declared_limits": ["section_has_no_claims"],
            }
        gaps.append(
            {
                "gap_id": f"gap:{section_id}:missing_claims",
                "section_id": section_id,
                "kind": "missing_claims",
                "claim_ids": [],
                "blocking": True,
                "priority": "load_bearing",
            }
        )
    return build_r3_production_handoff(
        topic_identity=topic,
        sections=sections,
        coverage_atlas={
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": dict(topic),
            "sections": [
                {"section_id": "S01", "needs_expansion": all_weak},
                {"section_id": "S02", "needs_expansion": True},
            ],
            "relation_graph": {"edge_count": 0},
        },
        section_argument_contracts={
            section_id: {
                "schema_version": "research_harness.section_argument_contract.v1",
                "section_id": section_id,
                "status": "contract_ready",
                "argument_tasks": [
                    {
                        "task_id": f"{section_id}:T01",
                        "description": "Preserve this planned section explicitly.",
                    }
                ],
            }
            for section_id in ("S01", "S02")
        },
        claims_by_criticality=claims_by,
        material_inventory={
            "papers": {}
            if all_weak
            else {
                "P01": {
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                }
            },
            "chunks": {}
            if all_weak
            else {
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
        material_bindings=bindings,
        relation_graph={"schema_version": "r3.relation_graph.v1", "edges": []},
        claim_dag={"schema_version": "research_harness.claim_graph.v1", "edges": []},
        gaps=gaps,
        coverage_requests=[],
        synthesis_bundles=bundles,
        visual_bindings={"S01": [], "S02": []},
        visual_needs={"S01": [], "S02": []},
    )


def _write_adaptive_manifest(coverage_root: Path) -> None:
    sections = []
    for section_id, outcome in (
        ("S01", "material_ready"),
        ("S02", "needs_more_literature"),
    ):
        section_dir = coverage_root / "sections" / section_id
        package = section_dir / "SECTION_MATERIAL_PACKAGE.json"
        _write_json(package, {"section_id": section_id, "coverage_outcome": outcome})
        _write_json(
            section_dir / "SECTION_SOURCE_LEDGER.json",
            {"section_id": section_id, "sources": []},
        )
        sections.append(
            {
                "section_id": section_id,
                "status": "completed"
                if outcome == "material_ready"
                else "needs_more_literature",
                "work_dir": str(section_dir),
            }
        )
    _write_json(
        coverage_root / "SECTION_COVERAGE_RUN.json",
        {
            "schema_version": "research_harness.section_coverage_run.v1",
            "sections": sections,
            "material_bundles": {
                section_id: {
                    "material_package_path": str(
                        coverage_root
                        / "sections"
                        / section_id
                        / "SECTION_MATERIAL_PACKAGE.json"
                    )
                }
                for section_id in ("S01", "S02")
            },
        },
    )


def test_zero_base_s2_seed_mixed_adaptive_partial_admission_and_budget_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    broad_base = harness.config.base_kb_sqlite
    scoped_base = tmp_path / "scoped_zero_base.sqlite"
    seeded_runtime = tmp_path / "s2_seeded.sqlite"
    with sqlite3.connect(scoped_base) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT)"
        )
        assert connection.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
        connection.commit()
    with sqlite3.connect(seeded_runtime) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT)"
        )
        connection.execute(
            "INSERT INTO text_chunks VALUES ('S2:K01', 'S2:P01', 'seeded')"
        )
        connection.commit()

    s2_inputs: list[Path] = []

    def fake_scoped(source_base: Path) -> dict:
        assert source_base == broad_base
        return {"status": "partial", "runtime_kb_sqlite": str(scoped_base)}

    def fake_s2(*, scoped_runtime_kb: Path) -> dict:
        s2_inputs.append(Path(scoped_runtime_kb))
        return {
            "status": "completed",
            "source_base_kb_sqlite": str(scoped_runtime_kb),
            "runtime_kb_sqlite": str(seeded_runtime),
            "accepted_s2_body_chunks": 1,
        }

    monkeypatch.setattr(harness, "_prepare_topic_scoped_kb", fake_scoped)
    monkeypatch.setattr(harness, "_prepare_s2_kb", fake_s2)
    scoped = harness._prepare_topic_scoped_kb(broad_base)
    s2 = harness._prepare_s2_kb(
        scoped_runtime_kb=Path(scoped["runtime_kb_sqlite"])
    )
    assert s2_inputs == [scoped_base]
    assert Path(s2["runtime_kb_sqlite"]) == seeded_runtime
    assert broad_base not in s2_inputs

    coverage_root = tmp_path / "section_coverage"
    _write_adaptive_manifest(coverage_root)
    coverage = SimpleNamespace(
        work_dir=coverage_root,
        material_bundles={
            "S01": SimpleNamespace(
                source_ledger_path=coverage_root
                / "sections"
                / "S01"
                / "SECTION_SOURCE_LEDGER.json",
                kb_sqlite=seeded_runtime,
                staging_kb_sqlite=None,
            ),
            "S02": SimpleNamespace(
                source_ledger_path=coverage_root
                / "sections"
                / "S02"
                / "SECTION_SOURCE_LEDGER.json",
                kb_sqlite=seeded_runtime,
                staging_kb_sqlite=None,
            ),
        },
    )
    blueprint = {"sections": [{"section_id": "S01"}, {"section_id": "S02"}]}
    phase3_root = tmp_path / "phase3"
    inputs = harness._build_phase3_inputs(
        blueprint=blueprint,
        coverage=coverage,
        scoped_runtime_kb=seeded_runtime,
        source_base_kb=broad_base,
        phase3_root=phase3_root,
    )
    assert inputs["adaptive_outcomes"]["S01"]["coverage_outcome"] == "material_ready"
    assert (
        inputs["adaptive_outcomes"]["S02"]["coverage_outcome"]
        == "needs_more_literature"
    )
    assert broad_base.resolve() not in inputs["shared_kb_paths"]

    topic = build_topic_identity_contract(_query_plan())
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _r3_handoff(topic),
        fail_on_invalid=True,
    )
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint=blueprint,
    )
    assert gate["status"] == "passed"
    assert gate["ready_section_ids"] == ["S01"]
    assert gate["blocked_section_ids"] == ["S02"]
    admission = json.loads(
        (phase3_root / "R4_AUTHORING_ADMISSION.json").read_text(encoding="utf-8")
    )
    assert admission["planned_section_ids"] == ["S01", "S02"]
    assert admission["not_admitted_section_ids"] == ["S02"]

    configs = [
        harness._build_coverage_config(
            blueprint_path=tmp_path / "blueprint.json",
            base_kb_sqlite=seeded_runtime,
            source_base_kb=broad_base,
            output_root=coverage_root,
            stage_cost_budget_cny=10.0,
        ),
        harness._build_coverage_config(
            blueprint_path=tmp_path / "blueprint.json",
            base_kb_sqlite=seeded_runtime,
            source_base_kb=broad_base,
            output_root=coverage_root,
            stage_cost_budget_cny=4.0,
            cost_budget_per_section_cny=1.5,
            bounded_search=True,
            retry_label="portfolio_breadth",
        ),
        harness._build_coverage_config(
            blueprint_path=tmp_path / "blueprint.json",
            base_kb_sqlite=seeded_runtime,
            source_base_kb=broad_base,
            output_root=coverage_root,
            stage_cost_budget_cny=3.0,
            cost_budget_per_section_cny=2.0,
            bounded_search=True,
            retry_label="author_editor_feedback",
        ),
    ]
    assert len({config.article_evidence_portfolio_path for config in configs}) == 1
    assert len({config.cross_wave_state_path for config in configs}) == 1
    assert len({config.global_coverage_ledger_path for config in configs}) == 1
    for config in configs:
        assert config.token_budget_per_section <= 96_000
        assert config.model_context_budget_per_section <= 96_000
        assert config.max_model_calls_per_section <= 6
        assert config.max_coverage_waves <= 2
        assert config.max_audit_calls_per_section <= 2
        assert config.stage_cost_budget_cny <= 10.0


def test_all_weak_canonical_r3_stops_with_needs_more_literature(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    topic = build_topic_identity_contract(_query_plan())
    phase3_root = tmp_path / "phase3"
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _r3_handoff(topic, all_weak=True),
        fail_on_invalid=True,
    )
    _, report = read_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json"
    )
    assert harness._phase3_status(
        report=report,
        acceptance={"status": "failed", "r4_handoff_ready": False},
    ) == "needs_more_literature"
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint={"sections": [{"section_id": "S01"}, {"section_id": "S02"}]},
    )
    assert gate["status"] == "needs_more_literature"
    assert gate["ready_section_ids"] == []
    assert set(gate["blocked_section_ids"]) == {"S01", "S02"}


def test_phase3_input_budget_scales_with_review_size() -> None:
    assert ReviewHarnessOrchestrator._phase3_input_budget_limit(1) == 50_000
    assert ReviewHarnessOrchestrator._phase3_input_budget_limit(2) == 50_000
    assert ReviewHarnessOrchestrator._phase3_input_budget_limit(8) == 200_000


def _r3_handoff_mixed_limits(topic: dict):
    """Four sections with disjoint Phase2/Phase3 readiness.

    S01 is R3 ready-with-limits; S02/S03/S04 are R3 needs_more_literature but
    remain structurally authorable (valid claims plus their own material).
    S04 additionally carries one bibliography-contaminated claim.
    """

    def qualified(claim_id: str, section_id: str, chunk_id: str, paper_id: str):
        return {
            "claim_id": claim_id,
            "section_id": section_id,
            "statement": "The mechanism is reported under the tested operating conditions.",
            "criticality": "supporting",
            "claim_state": "partially_grounded",
            "evidence_type": "mechanism",
            "support_classification": "qualified",
            "evidence_binding_status": "partial",
            "permission_status": "qualified_only",
            "supporting_chunk_ids": [chunk_id],
            "factual_support_chunk_ids": [chunk_id],
            "core_chunk_ids": [chunk_id],
            "core_paper_ids": [paper_id],
        }

    def bound(claim_id: str, section_id: str, chunk_id: str, paper_id: str):
        claim = qualified(claim_id, section_id, chunk_id, paper_id)
        claim.update({
            "support_classification": "supported",
            "evidence_binding_status": "bound",
            "permission_status": "bound",
            "claim_state": "grounded",
        })
        return claim

    def open_question(claim_id: str, section_id: str):
        return {
            "claim_id": claim_id,
            "section_id": section_id,
            "statement": "The transfer to an untested regime remains unresolved.",
            "criticality": "optional",
            "claim_state": "open_question",
            "evidence_type": "mechanism",
            "support_classification": "open_question",
            "evidence_binding_status": "unbound",
            "permission_status": "unbound",
            "supporting_chunk_ids": [],
            "unresolved": True,
            "unresolved_reasons": ["independent validation is missing"],
        }

    sections = ["S01", "S02", "S03", "S04"]
    claims = {
        "S01": [bound("S01:C1", "S01", "K01", "P01"), qualified("S01:C2", "S01", "K01", "P01")],
        "S02": [qualified("S02:C1", "S02", "K02", "P02")],
        "S03": [open_question("S03:C1", "S03")],
        "S04": [
            qualified("S04:C1", "S04", "K04", "P04"),
            {
                "claim_id": "S04:BAD",
                "section_id": "S04",
                "statement": "References, Author A et al., Phys. Rev. Lett. 120, 011 (2018).",
                "criticality": "optional",
                "claim_state": "open_question",
                "evidence_type": "mechanism",
                "support_classification": "open_question",
                "evidence_binding_status": "unbound",
                "permission_status": "unbound",
                "supporting_chunk_ids": [],
            },
        ],
    }
    claims_by_criticality = {
        "load_bearing": [],
        "supporting": [
            claim
            for section_id in sections
            for claim in claims[section_id]
            if claim["criticality"] == "supporting"
        ],
        "optional": [
            claim
            for section_id in sections
            for claim in claims[section_id]
            if claim["criticality"] == "optional"
        ],
    }
    bindings = {
        "S01": {
            "section_id": "S01",
            "claims": {
                "S01:C1": {
                    "claim_id": "S01:C1",
                    "evidence_binding_status": "bound",
                    "permission_status": "bound",
                    "write_status": "bound",
                    "supporting_chunk_ids": ["K01"],
                    "factual_support_chunk_ids": ["K01"],
                    "paper_ids": ["P01"],
                    "support_classification": "supported",
                },
                "S01:C2": {
                    "claim_id": "S01:C2",
                    "evidence_binding_status": "partial",
                    "permission_status": "qualified_only",
                    "write_status": "write_with_qualified_support",
                    "supporting_chunk_ids": ["K01"],
                    "factual_support_chunk_ids": ["K01"],
                    "paper_ids": ["P01"],
                    "support_classification": "qualified",
                },
            },
        },
        "S02": {
            "section_id": "S02",
            "claims": {
                "S02:C1": {
                    "claim_id": "S02:C1",
                    "evidence_binding_status": "partial",
                    "permission_status": "qualified_only",
                    "write_status": "write_with_qualified_support",
                    "supporting_chunk_ids": ["K02"],
                    "factual_support_chunk_ids": ["K02"],
                    "paper_ids": ["P02"],
                    "support_classification": "qualified",
                },
            },
        },
        "S03": {
            "section_id": "S03",
            "claims": {
                "S03:C1": {
                    "claim_id": "S03:C1",
                    "evidence_binding_status": "unbound",
                    "permission_status": "unbound",
                    "write_status": "write_with_declared_gap",
                    "supporting_chunk_ids": [],
                    "paper_ids": [],
                    "support_classification": "open_question",
                },
            },
        },
        "S04": {
            "section_id": "S04",
            "claims": {
                "S04:C1": {
                    "claim_id": "S04:C1",
                    "evidence_binding_status": "partial",
                    "permission_status": "qualified_only",
                    "write_status": "write_with_qualified_support",
                    "supporting_chunk_ids": ["K04"],
                    "factual_support_chunk_ids": ["K04"],
                    "paper_ids": ["P04"],
                    "support_classification": "qualified",
                },
                "S04:BAD": {
                    "claim_id": "S04:BAD",
                    "evidence_binding_status": "unbound",
                    "permission_status": "unbound",
                    "write_status": "write_with_declared_gap",
                    "supporting_chunk_ids": [],
                    "paper_ids": [],
                    "support_classification": "open_question",
                },
            },
        },
    }
    outcomes = {
        "S01": "ready_with_limits",
        "S02": "needs_more_literature",
        "S03": "needs_more_literature",
        "S04": "needs_more_literature",
    }
    bundles = {
        section_id: {
            "section_id": section_id,
            "status": outcomes[section_id],
            "section_outcome": outcomes[section_id],
            "readiness_status": outcomes[section_id],
            "paper_ids": sorted({
                paper
                for claim in claims[section_id]
                for paper in claim.get("core_paper_ids") or []
            }),
            "chunk_ids": sorted({
                chunk
                for claim in claims[section_id]
                for chunk in claim.get("supporting_chunk_ids") or []
            }),
            "claim_category_assignments": [
                {
                    "claim_id": claim["claim_id"],
                    "category": (
                        "open_questions"
                        if claim.get("support_classification") == "open_question"
                        else "established_points"
                    ),
                }
                for claim in claims[section_id]
            ],
        }
        for section_id in sections
    }
    return build_r3_production_handoff(
        topic_identity=topic,
        sections=[
            {
                "section_id": section_id,
                "title": f"Section {section_id}",
                "topic_identity": dict(topic),
            }
            for section_id in sections
        ],
        coverage_atlas={
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": dict(topic),
            "sections": [
                {"section_id": section_id, "needs_expansion": outcomes[section_id] != "ready_with_limits"}
                for section_id in sections
            ],
            "relation_graph": {"edge_count": 0},
        },
        section_argument_contracts={
            section_id: {
                "schema_version": "research_harness.section_argument_contract.v1",
                "section_id": section_id,
                "status": "contract_ready",
                "argument_tasks": [{
                    "task_id": f"{section_id}:T01",
                    "description": f"Explain {section_id}.",
                }],
            }
            for section_id in sections
        },
        claims_by_criticality=claims_by_criticality,
        material_inventory={
            "papers": {
                "P01": {
                    "paper_id": "P01",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                },
                "P02": {
                    "paper_id": "P02",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                },
                "P04": {
                    "paper_id": "P04",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                },
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
                },
                "K02": {
                    "chunk_id": "K02",
                    "paper_id": "P02",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_kind": "fulltext",
                },
                "K04": {
                    "chunk_id": "K04",
                    "paper_id": "P04",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_kind": "fulltext",
                },
            },
            "visuals": {},
        },
        material_bindings=bindings,
        relation_graph={"schema_version": "r3.relation_graph.v1", "edges": []},
        claim_dag={"schema_version": "research_harness.claim_graph.v1", "edges": []},
        gaps=[],
        coverage_requests=[],
        synthesis_bundles=bundles,
        visual_bindings={section_id: [] for section_id in sections},
        visual_needs={section_id: [] for section_id in sections},
    )


def _write_mixed_limits_root(tmp_path: Path, *, drop_s02_ledger: bool = False) -> Path:
    topic = build_topic_identity_contract(_query_plan())
    phase3_root = tmp_path / "phase3"
    write_r3_production_handoff(
        phase3_root / "R3_PRODUCTION_HANDOFF.json",
        _r3_handoff_mixed_limits(topic),
        fail_on_invalid=True,
    )
    for section_id in ("S01", "S02", "S03", "S04"):
        if drop_s02_ledger and section_id == "S02":
            continue
        _write_json(
            phase3_root
            / "coverage_snapshot"
            / "sections"
            / section_id
            / "SECTION_SOURCE_LEDGER.json",
            {
                "section_id": section_id,
                "sources": [{
                    "paper_id": f"P{section_id[1:]}",
                    "title": f"Study {section_id}",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                }],
            },
        )
    (phase3_root / "shared_kb.sqlite").write_bytes(b"sqlite-placeholder")
    _write_json(
        phase3_root / "input" / "ADAPTIVE_COVERAGE_OUTCOMES.json",
        {
            "sections": {
                "S01": {"section_id": "S01", "coverage_outcome": "needs_more_literature"},
                "S02": {"section_id": "S02", "coverage_outcome": "material_ready"},
                "S03": {"section_id": "S03", "coverage_outcome": "needs_more_literature"},
                "S04": {"section_id": "S04", "coverage_outcome": "needs_more_literature"},
            }
        },
    )
    return phase3_root


def test_disjoint_phase2_phase3_readiness_admits_all_structurally_valid_with_limits(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    phase3_root = _write_mixed_limits_root(tmp_path)
    blueprint = {
        "sections": [
            {"section_id": section_id}
            for section_id in ("S01", "S02", "S03", "S04")
        ]
    }
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint=blueprint,
    )
    assert gate["status"] == "passed"
    assert set(gate["ready_section_ids"]) == {"S01", "S02", "S03", "S04"}
    assert set(gate["admitted_with_limits_section_ids"]) == {
        "S02",
        "S03",
        "S04",
    }
    assert gate["excluded_claim_ids_by_section"].get("S04") == ["S04:BAD"]
    assert gate["phase2_advisory_outcomes"]["S02"] == "material_ready"
    assert gate["phase3_advisory_outcomes"]["S02"] == "needs_more_literature"
    admission = json.loads(
        (phase3_root / "R4_AUTHORING_ADMISSION.json").read_text(
            encoding="utf-8"
        )
    )
    assert admission["admitted_section_ids"] == ["S01", "S02", "S03", "S04"]
    assert admission["admitted_with_limits_section_ids"] == ["S02", "S03", "S04"]
    assert admission["excluded_claim_ids_by_section"]["S04"] == ["S04:BAD"]
    assert admission["phase2_advisory_outcomes"]["S01"] == "needs_more_literature"
    assert admission["phase3_advisory_outcomes"]["S01"] == "ready_with_limits"


def test_missing_source_ownership_blocks_only_that_limits_section(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    phase3_root = _write_mixed_limits_root(tmp_path, drop_s02_ledger=True)
    blueprint = {
        "sections": [
            {"section_id": section_id}
            for section_id in ("S01", "S02", "S03", "S04")
        ]
    }
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint=blueprint,
    )
    assert gate["status"] == "passed"
    assert set(gate["ready_section_ids"]) == {"S01", "S03", "S04"}
    assert gate["blocked_section_ids"] == ["S02"]
    assert any(
        "section_source_ledger_not_found" in reason
        for reason in gate["blocked_sections"]["S02"]
    )


def test_invalid_canonical_r3_still_fails_gate(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    phase3_root = tmp_path / "phase3"
    phase3_root.mkdir()
    (phase3_root / "R3_PRODUCTION_HANDOFF.json").write_text(
        "{}", encoding="utf-8"
    )
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint={"sections": [{"section_id": "S01"}]},
    )
    assert gate["status"] == "failed"
    assert "canonical_r3_handoff_required" in gate["reason"]


def test_missing_kb_blocks_limits_sections_but_ready_section_stays_admitted(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    phase3_root = _write_mixed_limits_root(tmp_path)
    (phase3_root / "shared_kb.sqlite").unlink()
    blueprint = {
        "sections": [
            {"section_id": section_id}
            for section_id in ("S01", "S02", "S03", "S04")
        ]
    }
    gate = harness._require_r3_authoring_gate(
        phase3_root=phase3_root,
        blueprint=blueprint,
    )
    assert gate["status"] == "passed"
    assert gate["ready_section_ids"] == ["S01"]
    assert set(gate["blocked_section_ids"]) == {"S02", "S03", "S04"}
    assert all(
        any("phase3_kb_not_found" in reason for reason in reasons)
        for reasons in gate["blocked_sections"].values()
    )


def test_valid_partial_handoff_reconciles_stale_acceptance_without_llm(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    topic = build_topic_identity_contract(_query_plan())
    phase3_root = tmp_path / "phase3"
    handoff_path = phase3_root / "R3_PRODUCTION_HANDOFF.json"
    write_r3_production_handoff(
        handoff_path,
        _r3_handoff(topic, all_weak=False),
        fail_on_invalid=True,
    )
    acceptance_path = phase3_root / "PHASE3_ACCEPTANCE.json"
    acceptance_path.write_text(
        json.dumps(
            {
                "schema_version": "research_harness.phase3_acceptance.v1",
                "status": "failed",
                "r4_handoff_ready": False,
                "partial_handoff_allowed": False,
                "engineering_passed": True,
                "claim_quality_passed": True,
                "evidence_permission_passed": True,
                "coverage_request_quality_passed": True,
                "argument_task_coverage_passed": True,
                "effective_statement_propagation_passed": True,
                "full_blueprint_context_passed": True,
                "context_value_handoff_passed": True,
                "content_depth_aggregation_passed": True,
                "verifier_batch_budget_passed": True,
                "input_budget_passed": False,
                "duplicate_bundle_categories_detected": False,
                "engineering_safety": {"passes": True},
                "material_quality": {"passes": True},
                "cost": {"estimated_input_tokens_total": 45_000},
                "r3_production_handoff": {
                    "validation_status": "failed",
                    "validation_errors": ["obsolete_validator_false_positive"],
                },
            }
        ),
        encoding="utf-8",
    )

    _, report = harness._validate_existing_phase3_handoff(
        handoff_path=handoff_path,
        blueprint={
            "sections": [
                {"section_id": "S01"},
                {"section_id": "S02"},
            ]
        },
        topic_identity=topic,
    )
    refreshed = json.loads(acceptance_path.read_text(encoding="utf-8"))

    assert report.valid is True
    assert refreshed["status"] == "passed"
    assert refreshed["r4_handoff_ready"] is True
    assert refreshed["acceptance_reconciliation"]["model_calls_added"] == 0


def test_status_translation_covers_adaptive_and_r3_vocabularies() -> None:
    for outcome in (
        "material_ready",
        "material_ready_with_limits",
        "merge_required",
        "needs_more_literature",
    ):
        harness_status = ReviewHarnessOrchestrator._coverage_status_from_outcome(
            outcome
        )
        assert harness_status != "failed"
    assert ReviewHarnessOrchestrator._r3_status_from_outcome("ready") == "completed"
    assert (
        ReviewHarnessOrchestrator._r3_status_from_outcome("ready_with_limits")
        == "completed_with_limits"
    )
    assert (
        ReviewHarnessOrchestrator._r3_status_from_outcome("merge_required")
        == "merge_required"
    )
    assert (
        ReviewHarnessOrchestrator._r3_status_from_outcome("needs_more_literature")
        == "needs_more_literature"
    )
