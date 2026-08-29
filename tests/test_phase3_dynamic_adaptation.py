"""Deterministic adversarial coverage for bounded Phase-3 adaptation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.phase3_argument_orchestrator import (
    Phase3ArgumentOrchestrator,
    adapt_claim_for_partial_coverage,
    classify_claim_support,
)
from optomind_research.runtime.r3_production_handoff import (
    R3ProductionHandoff,
    build_r3_production_handoff,
    migrate_r3_handoff_schema,
    write_r3_production_handoff,
)
from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore


def _record(
    *,
    permission: str = "factual_support",
    depth: str = "fulltext",
    scope: str = "direct",
    text: str = "A bounded full-text passage supports the measured mechanism under tested conditions.",
) -> dict:
    return {
        "chunk_id": "K01",
        "paper_id": "P01",
        "use_permission": permission,
        "content_depth": depth,
        "scope_fit": scope,
        "context_complete": True,
        "source_kind": "fulltext" if depth == "fulltext" else "metadata",
        "normalized_text": text,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _phase3_fixture(tmp_path: Path, *, merge: bool = False) -> dict:
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?, ?, ?, ?, ?, ?)",
            (
                "K01",
                "P01",
                "Mechanism study",
                "The measured mechanism establishes the governing optical response under tested conditions.",
                "fulltext",
                "fulltext",
            ),
        )
        conn.commit()

    ledger = tmp_path / "shared_ledger.json"
    _write_json(ledger, {
        "sources": [{
            "paper_id": "P01",
            "title": "Mechanism study",
            "canonical_chunk_ids": ["K01"],
            "literature_role": "mechanism",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }],
    })
    overlays: dict[str, Path] = {}
    for section_id, allowed in (("S01", ["P01"]), ("S02", [])):
        overlay = tmp_path / f"{section_id}.json"
        _write_json(overlay, {
            "section_id": section_id,
            "paper_ids": allowed,
            "chunk_ids": ["K01"] if allowed else [],
            "paper_overrides": {
                "P01": {
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                }
            } if allowed else {},
            "chunk_overrides": {
                "K01": {
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                }
            } if allowed else {},
        })
        overlays[section_id] = overlay

    second = {
        "section_id": "S02",
        "title": "Uncovered comparison",
        "chapter_argument": "Compare the competing regimes.",
        "required_roles": ["comparison"],
        "claims": [],
    }
    if merge:
        second["merge_with_section_ids"] = ["S01"]
    return {
        "blueprint": {
            "sections": [
                {
                    "section_id": "S01",
                    "title": "Governing mechanism",
                    "chapter_argument": "Explain the governing mechanism and its measurable consequence.",
                    "required_roles": ["mechanism"],
                    "claims": [{
                        "claim_id": "S01:C01",
                        "statement": "The measured mechanism establishes the governing response in this platform.",
                        "evidence_type": "mechanism",
                        "supporting_text_chunk_ids": ["K01"],
                        "citation_paper_ids": ["P01"],
                        "importance": "load_bearing",
                        "load_bearing": True,
                    }],
                },
                second,
            ],
            "review_mode": "focused_perspective",
        },
        "scope_map": {
            "user_question": "How does the mechanism work?",
            "search_anchors": ["optical mechanism"],
        },
        "coverage_atlas": {"sections": []},
        "relation_graph": {"edges": []},
        "ledger": ledger,
        "kb": kb,
        "overlays": overlays,
    }


def _run_phase3(tmp_path: Path, *, merge: bool = False):
    fixture = _phase3_fixture(tmp_path, merge=merge)
    output = tmp_path / "phase3_output"
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        real_llm_claims=False,
        real_llm_dag=False,
        execute_coverage=False,
    ).run()
    return result, output


def test_real_m2a_failure_is_runtime_failure_and_never_a_coverage_request(
    tmp_path: Path, monkeypatch
):
    fixture = _phase3_fixture(tmp_path)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "m2a_failure",
        real_llm_claims=True,
        real_llm_dag=False,
    )
    state = orchestrator._prepare_section(
        fixture["blueprint"]["sections"][1],
        1,
        fixture["blueprint"]["sections"],
    )

    def fail_decompose(self, section):
        raise RuntimeError("synthetic M2a API failure")

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module

    monkeypatch.setattr(
        phase3_module.ClaimDecomposer,
        "decompose_section",
        fail_decompose,
    )
    orchestrator._decompose_claims(state)

    assert state["claims"] == []
    assert state["claim_status"] == "real_llm_runtime_failure"
    assert state["runtime_failure"]["component"] == "M2a"
    assert state["runtime_failure"]["scientific_gap"] is False
    assert orchestrator._make_requests([state], 1) == []


def test_real_m2a_empty_response_is_parse_failure_not_open_question(
    tmp_path: Path, monkeypatch
):
    fixture = _phase3_fixture(tmp_path)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "m2a_parse_failure",
        real_llm_claims=True,
        real_llm_dag=False,
    )
    state = orchestrator._prepare_section(
        fixture["blueprint"]["sections"][1],
        1,
        fixture["blueprint"]["sections"],
    )

    def empty_decompose(self, section):
        return []

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module

    monkeypatch.setattr(
        phase3_module.ClaimDecomposer,
        "decompose_section",
        empty_decompose,
    )
    orchestrator._decompose_claims(state)

    assert state["claims"] == []
    assert state["claim_status"] == "real_llm_parse_failure"
    assert state["runtime_failure"]["error_type"] == "parse_failure"
    assert orchestrator._make_requests([state], 1) == []


def test_real_m2b_failure_is_runtime_failure_and_closes_sections(
    tmp_path: Path, monkeypatch
):
    fixture = _phase3_fixture(tmp_path)
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "m2b_failure",
        real_llm_claims=False,
        real_llm_dag=True,
    )
    states = [
        {
            "section": {"section_id": "S01", "title": "Mechanism"},
            "claims": [{"claim_id": "S01:C01", "statement": "Mechanism claim."}],
            "runtime_failure": {},
            "declared_limits": [],
            "bindings": {},
            "bundle": {"r4_handoff_allowed": True, "status": "material_ready"},
            "status": "material_ready",
            "section_outcome": "ready",
        },
        {
            "section": {"section_id": "S02", "title": "Comparison"},
            "claims": [{"claim_id": "S02:C01", "statement": "Comparison claim."}],
            "runtime_failure": {},
            "declared_limits": [],
            "bindings": {},
            "bundle": {"r4_handoff_allowed": True, "status": "material_ready"},
            "status": "material_ready",
            "section_outcome": "ready",
        },
    ]

    def fail_build(self, *args, **kwargs):
        raise ValueError("synthetic M2b parse failure")

    import optomind_research.runtime.phase3_argument_orchestrator as phase3_module

    monkeypatch.setattr(phase3_module.ArgumentDAGBuilder, "build", fail_build)
    graph = orchestrator._build_claim_graph(states)

    assert graph["status"] == "failed_closed"
    for state in states:
        assert state["runtime_failure"]["component"] == "M2b"
        assert state["runtime_failure"]["scientific_gap"] is False
        assert state["section_outcome"] == "needs_more_literature"
        assert state["bundle"]["r4_handoff_allowed"] is False


def _r3_components(*, optional_gap: bool = False, visual_needs: list[dict] | None = None) -> dict:
    load_claim = {
        "claim_id": "S01:C01",
        "section_id": "S01",
        "statement": "The measured mechanism controls the optical response.",
        "original_statement": "The measured mechanism controls the optical response.",
        "effective_statement": "The measured mechanism controls the optical response.",
        "criticality": "load_bearing",
        "importance": "load_bearing",
        "claim_state": "grounded",
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
            "source_permissions": {"K01": {"evidence_ceiling": "factual_support"}},
        },
    }
    claims_by = {"load_bearing": [load_claim], "supporting": [], "optional": []}
    bindings = {
        "S01": {
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
                    "relation_basis_chunk_ids": [],
                }
            },
        }
    }
    assignments = [{
        "claim_id": "S01:C01",
        "category": "established_points",
        "classification": "supported",
        "effective_statement": load_claim["effective_statement"],
    }]
    gaps: list[dict] = []
    if optional_gap:
        optional_claim = {
            "claim_id": "S01:C02",
            "section_id": "S01",
            "statement": "An optional deployment effect remains unresolved.",
            "original_statement": "An optional deployment effect remains unresolved.",
            "effective_statement": "Open question: an optional deployment effect remains unresolved.",
            "criticality": "optional",
            "importance": "optional",
            "claim_state": "open_question",
            "claim_classification": "open_question",
            "support_classification": "open_question",
            "evidence_binding_status": "open_question",
            "permission_status": "unbound",
            "supported_rewrite": "Unsupported deployment rewrite.",
        }
        claims_by["optional"].append(optional_claim)
        bindings["S01"]["claims"]["S01:C02"] = {
            "claim_id": "S01:C02",
            "evidence_binding_status": "open_question",
            "permission_status": "unbound",
            "write_status": "write_with_declared_gap",
            "supporting_chunk_ids": [],
            "factual_support_chunk_ids": [],
            "paper_ids": [],
            "classification": "open_question",
            "support_classification": "open_question",
            "adaptation_action": "declare_optional_gap",
        }
        assignments.append({
            "claim_id": "S01:C02",
            "category": "conditional_points",
            "classification": "open_question",
            "effective_statement": optional_claim["effective_statement"],
        })
        gaps.append({
            "gap_id": "gap:S01:S01:C02",
            "section_id": "S01",
            "kind": "optional_claim_material_gap",
            "claim_ids": ["S01:C02"],
            "classification": "open_question",
            "blocking": False,
            "priority": "optional",
        })

    return {
        "topic_identity": {"topic_id": "topic-optics-v1"},
        "sections": [{
            "section_id": "S01",
            "title": "Measured mechanism",
            "topic_id": "topic-optics-v1",
        }],
        "coverage_atlas": {
            "schema_version": "research_harness.coverage_atlas.v1",
            "topic_identity": {"topic_id": "topic-optics-v1"},
            "sections": [{"section_id": "S01", "needs_expansion": False}],
            "relation_graph": {"edge_count": 0},
        },
        "section_argument_contracts": {
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
        "claims_by_criticality": claims_by,
        "material_inventory": {
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
        "material_bindings": bindings,
        "relation_graph": {"schema_version": "r3.relation_graph.v1", "edges": []},
        "claim_dag": {"schema_version": "research_harness.claim_graph.v1", "edges": []},
        "gaps": gaps,
        "coverage_requests": [],
        "synthesis_bundles": {
            "S01": {
                "section_id": "S01",
                "status": "ready_with_limits" if optional_gap else "material_ready",
                "section_outcome": "ready_with_limits" if optional_gap else "ready",
                "readiness_status": "ready_for_authoring",
                "paper_ids": ["P01"],
                "chunk_ids": ["K01"],
                "claim_category_assignments": assignments,
            }
        },
        "visual_bindings": {"S01": []},
        "visual_needs": {"S01": list(visual_needs or [])},
    }


def _build_handoff(**kwargs):
    return build_r3_production_handoff(**_r3_components(**kwargs))


def test_partial_support_classification_rejects_discovery_and_preserves_provenance():
    records = {
        "K01": _record(),
        "K02": _record(
            permission="contextual_or_qualified_support",
            text="An adjacent study provides contextual comparison only.",
        ) | {"chunk_id": "K02"},
        "K03": _record(
            permission="discovery_only",
            depth="metadata",
            scope="direct",
            text="Title-level discovery lead.",
        ) | {"chunk_id": "K03"},
    }
    claim = {
        "claim_id": "C01",
        "statement": "The mechanism transfers to deployment conditions.",
        "supported_rewrite": "The mechanism transfers to deployment conditions.",
        "supporting_text_chunk_ids": ["K01", "K02", "K03"],
        "importance": "load_bearing",
    }
    audit = classify_claim_support(claim, records)
    assert audit["classification"] == "qualified"
    assert audit["eligible_support_chunk_ids"] == ["K01", "K02"]
    assert audit["rejected_support_chunk_ids"] == ["K03"]
    discovery_only_claim = dict(claim)
    discovery_only_claim["supporting_text_chunk_ids"] = ["K03"]
    adapted = adapt_claim_for_partial_coverage(
        discovery_only_claim,
        {"K03": records["K03"]},
    )
    assert adapted["support_classification"] == "open_question"
    assert adapted["supporting_text_chunk_ids"] == []
    assert adapted["rejected_support_chunk_ids"] == ["K03"]
    assert adapted["adaptation_action"] == "targeted_coverage_request"
    assert adapted["effective_statement"].startswith("Open question:")
    assert adapted["superseded_supported_rewrite"] == claim["supported_rewrite"]
    assert adapted["supported_rewrite_eligible"] is False
    assert adapted["claim_provenance"]["source_permissions"]["K03"]["evidence_ceiling"] == "discovery_only"


def test_optional_gap_and_load_bearing_request_are_distinct_and_explicit():
    optional = adapt_claim_for_partial_coverage(
        {
            "claim_id": "optional",
            "statement": "An optional effect may improve deployment.",
            "importance": "optional",
        },
        {},
    )
    load_bearing = adapt_claim_for_partial_coverage(
        {
            "claim_id": "load",
            "statement": "The missing mechanism explains the response.",
            "importance": "load_bearing",
        },
        {},
    )
    assert optional["adaptation_action"] == "declare_optional_gap"
    assert optional["effective_statement"].startswith("Open question:")
    assert load_bearing["adaptation_action"] == "targeted_coverage_request"
    assert load_bearing["adaptation_recommendation"]["action"] == "targeted_coverage_request"
    assert "permission-eligible support" in load_bearing["missing_evidence_components"][0]


def test_mixed_ready_and_weak_sections_allow_partial_r3_handoff(tmp_path: Path):
    result, output = _run_phase3(tmp_path)
    assert result["status"] == "passed"
    assert result["r4_handoff_ready"] is True
    assert result["r4_ready_section_ids"] == ["S01"]
    assert result["section_outcomes"] == {
        "S01": "ready",
        "S02": "needs_more_literature",
    }

    handoff = R3ProductionHandoff.from_dict(
        json.loads((output / "R3_PRODUCTION_HANDOFF.json").read_text(encoding="utf-8"))
    )
    report = handoff.validate()
    assert report.valid is True
    assert report.global_readiness["status"] == "ready_with_limits"
    assert report.section_readiness["S01"]["ready_for_authoring"] is True
    assert report.section_readiness["S02"]["ready_for_authoring"] is False

    store = R4Phase3ArtifactStore(output)
    assert store.section("S01").ready_for_authoring is True
    assert store.section("S02").ready_for_authoring is False


def test_merge_path_is_explicit_and_does_not_promote_the_open_claim(tmp_path: Path):
    result, output = _run_phase3(tmp_path, merge=True)
    assert result["status"] == "passed"
    assert result["section_outcomes"]["S02"] == "merge_required"
    assert result["r4_ready_section_ids"] == ["S01"]

    bundles = json.loads((output / "SYNTHESIS_BUNDLES.json").read_text(encoding="utf-8"))
    weak_bundle = next(item for item in bundles["bundles"] if item["section_id"] == "S02")
    assert weak_bundle["section_outcome"] == "merge_required"
    assert weak_bundle["merge_recommendation"]["target_section_ids"] == ["S01"]
    assert weak_bundle["r4_handoff_allowed"] is False

    bindings = json.loads((output / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))
    weak_claim = next(iter(bindings["sections"]["S02"]["claims"].values()))
    assert weak_claim["support_classification"] == "open_question"
    assert weak_claim["supporting_chunk_ids"] == []
    assert weak_claim["adaptation_action"] == "merge_recommendation"


def test_r3_optional_gap_is_nonblocking_but_discovery_permission_violation_fails():
    optional_handoff = _build_handoff(optional_gap=True)
    optional_report = optional_handoff.validate()
    assert optional_report.valid is True
    assert optional_report.section_readiness["S01"]["outcome"] == "ready_with_limits"
    optional_gap = next(item for item in optional_handoff.gaps if item["kind"] == "optional_claim_material_gap")
    assert optional_gap["blocking"] is False

    components = _r3_components()
    components["material_inventory"]["chunks"]["K01"].update({
        "use_permission": "discovery_only",
        "content_depth": "metadata",
        "context_complete": False,
    })
    components["claims_by_criticality"]["load_bearing"][0]["support_classification"] = "supported"
    violation = build_r3_production_handoff(**components).validate()
    assert violation.valid is False
    assert any("discovery_only_cannot_support_claim" in error for error in violation.errors)


def test_visual_needs_and_relation_basis_are_preserved_across_canonical_r3_and_r4(tmp_path: Path):
    need = {
        "need_id": "S01:visual_need:01",
        "purpose": "show the measured mechanism",
        "required": True,
        "claim_id": "S01:C01",
        "satisfied": False,
    }
    handoff = _build_handoff(visual_needs=[need])
    assert handoff.visual_needs["S01"] == [need]
    report = handoff.validate()
    assert report.valid is True
    assert report.section_readiness["S01"]["outcome"] == "ready_with_limits"

    path = tmp_path / "R3_PRODUCTION_HANDOFF.json"
    write_r3_production_handoff(path, handoff, fail_on_invalid=True)
    artifacts = R4Phase3ArtifactStore(tmp_path).section("S01")
    assert artifacts.visual_needs == [need]
    assert artifacts.to_context_payload()["visual_needs"] == [need]

    components = _r3_components()
    components["relation_graph"] = {
        "schema_version": "r3.relation_graph.v1",
        "edges": [{
            "edge_id": "E01",
            "source_paper_id": "P01",
            "target_paper_id": "P01",
            "relation_type": "supports",
            "status": "observed",
            "relation_basis_chunk_ids": ["K01"],
        }],
    }
    relation_handoff = build_r3_production_handoff(**components)
    assert relation_handoff.to_dict()["relation_graph"]["edges"][0]["relation_basis_chunk_ids"] == ["K01"]


def test_canonical_schema_requires_explicit_migration_and_is_deterministic():
    handoff = _build_handoff()
    first = json.dumps(handoff.to_dict(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(handoff.to_dict(), sort_keys=True, separators=(",", ":"))
    assert first == second
    assert handoff.schema_version == "research_harness.r3_production_handoff.v1"

    legacy_payload = handoff.to_dict()
    legacy_payload["schema_version"] = "r3.production_handoff.v1"
    parsed_without_migration = R3ProductionHandoff.from_dict(legacy_payload)
    assert parsed_without_migration.validate().valid is False
    assert any("incompatible_schema_version" in error for error in parsed_without_migration.validate().errors)

    migrated = migrate_r3_handoff_schema(legacy_payload)
    assert migrated.schema_version == "research_harness.r3_production_handoff.v1"
    assert migrated.legacy_migration["explicit"] is True
    assert migrated.legacy_migration["source_schema_version"] == "r3.production_handoff.v1"
    assert migrated.validate().valid is True


def test_live_m2a_mode_receives_bounded_candidate_portfolio_without_network(
    tmp_path: Path, monkeypatch
):
    """The live wiring is exercised with a deterministic model double only."""

    fixture = _phase3_fixture(tmp_path)
    fixture["blueprint"]["sections"][0]["claims"] = []
    seen: list[dict] = []
    import optomind_research.runtime.phase3_argument_orchestrator as module

    def fake_decompose(self, section):
        seen.append(dict(section))
        sid = str(section.get("section_id"))
        if sid == "S01":
            return [{
                "claim_id": "S01:C01",
                "statement": "The measured mechanism establishes the governing response.",
                "evidence_type": "mechanism",
                "importance": "load_bearing",
                "supporting_text_chunk_ids": ["K01"],
            }]
        return [{
            "claim_id": f"{sid}:C01",
            "statement": "This optional section remains an unresolved comparison.",
            "evidence_type": "comparison",
            "importance": "supporting",
            "supporting_text_chunk_ids": ["invented-chunk"],
        }]

    monkeypatch.setattr(module.ClaimDecomposer, "decompose_section", fake_decompose)
    result, output = _run_phase3_with_options(
        fixture,
        tmp_path / "live_m2a",
        real_llm_claims=True,
        real_llm_dag=False,
        max_m2a_input_tokens=8000,
        max_m2a_records=1,
    )

    assert seen and all("candidate_text_chunks" in item for item in seen)
    s01_view = next(item for item in seen if item.get("section_id") == "S01")
    assert s01_view["candidate_text_chunks"]
    assert s01_view["candidate_text_chunks"][0]["chunk_id"] == "K01"
    assert len(s01_view["candidate_text_chunks"]) <= 1
    phase_run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert phase_run["runtime_options"]["real_llm_claims"] is True
    assert phase_run["m2a_budget"]["S01"]["estimated_input_tokens"] <= 8000
    assert result["r4_handoff_ready"] is True
    assert result["section_outcomes"]["S01"] == "ready"
    bindings = json.loads((output / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))
    weak = bindings["sections"]["S02"]["claims"]
    assert all("invented-chunk" not in item.get("supporting_chunk_ids", []) for item in weak.values())


def test_m2a_extreme_cap_uses_minimal_context_without_exceeding_budget(
    tmp_path: Path, monkeypatch
):
    fixture = _phase3_fixture(tmp_path)
    fixture["blueprint"]["sections"][0]["claims"] = []
    long_context = "oversight context " * 120
    fixture["scope_map"]["m1_architecture_guidance"] = [long_context] * 8
    fixture["blueprint"]["sections"][0]["scope_guardrails"] = [long_context] * 8
    fixture["blueprint"]["sections"][0]["argument_tasks"] = [
        {"description": long_context} for _ in range(8)
    ]
    seen: list[dict] = []
    import optomind_research.runtime.phase3_argument_orchestrator as module

    def fake_decompose(self, section):
        seen.append(dict(section))
        return []

    monkeypatch.setattr(module.ClaimDecomposer, "decompose_section", fake_decompose)
    _, output = _run_phase3_with_options(
        fixture,
        tmp_path / "extreme_cap",
        real_llm_claims=True,
        real_llm_dag=False,
        max_m2a_input_tokens=1000,
        max_m2a_records=24,
    )
    phase_run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    audit = phase_run["m2a_budget"]["S01"]
    assert audit["estimated_input_tokens"] <= 1000
    assert audit["minimal_context"] is True
    s01_view = next(item for item in seen if item.get("section_id") == "S01")
    assert len(s01_view.get("candidate_text_chunks") or []) <= 1


def _run_phase3_with_options(fixture: dict, output: Path, **options):
    return (
        Phase3ArgumentOrchestrator(
            blueprint=fixture["blueprint"],
            scope_map=fixture["scope_map"],
            coverage_atlas=fixture["coverage_atlas"],
            relation_graph=fixture["relation_graph"],
            shared_ledger_path=fixture["ledger"],
            shared_kb_paths=[fixture["kb"]],
            overlay_paths=fixture["overlays"],
            output_dir=output,
            real_llm_claims=bool(options.get("real_llm_claims", False)),
            real_llm_dag=bool(options.get("real_llm_dag", False)),
            max_m2a_input_tokens=int(options.get("max_m2a_input_tokens", 8000)),
            max_m2a_records=int(options.get("max_m2a_records", 24)),
            max_dag_candidates=80,
            runtime_failures=options.get("runtime_failures"),
            execute_coverage=False,
        ).run(),
        output,
    )


def test_offline_phase3_never_calls_qwen_and_all_weak_sections_block(
    tmp_path: Path, monkeypatch
):
    fixture = _phase3_fixture(tmp_path)
    # Remove the only section-owned source.  Mock M2a still emits auditable
    # claims, but none can be bound to a real chunk.
    _write_json(fixture["overlays"]["S01"], {
        "section_id": "S01",
        "paper_ids": [],
        "chunk_ids": [],
        "paper_overrides": {},
        "chunk_overrides": {},
    })

    import optomind_research.runtime.phase3_argument_orchestrator as module

    def must_not_call(*args, **kwargs):
        raise AssertionError("offline Phase 3 must not call Qwen")

    monkeypatch.setattr(module, "QwenFreshEvidenceSemanticJudge", must_not_call)
    result, output = _run_phase3_with_options(fixture, tmp_path / "offline")

    phase_run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert phase_run["runtime_options"]["real_llm_claims"] is False
    assert result["r4_handoff_ready"] is False
    assert all(value == "needs_more_literature" for value in result["section_outcomes"].values())


def test_runtime_failure_is_preserved_separately_from_scientific_gap(tmp_path: Path):
    fixture = _phase3_fixture(tmp_path)
    result, output = _run_phase3_with_options(
        fixture,
        tmp_path / "runtime_failure",
        runtime_failures={
            "S02": {
                "section_id": "S02",
                "kind": "runtime_failure",
                "reason": "coverage worker crashed before materialization",
            }
        },
    )
    phase_run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert phase_run["runtime_failures"]["S02"]["kind"] == "runtime_failure"
    assert not any(
        item.get("section_id") == "S02"
        for item in json.loads((output / "COVERAGE_REQUESTS.json").read_text(encoding="utf-8")).get("requests", [])
    )
    handoff = json.loads((output / "R3_PRODUCTION_HANDOFF.json").read_text(encoding="utf-8"))
    assert any(
        gap.get("kind") == "runtime_failure" and gap.get("section_id") == "S02"
        for gap in handoff.get("gaps", [])
    )
    assert result["r4_handoff_ready"] is True
