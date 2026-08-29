"""Adversarial tests for the mandatory R3 production handoff boundary."""

from __future__ import annotations

import copy
import re
import shutil
import sys
import types
import uuid
from pathlib import Path


# The focused handoff tests do not need the optional AgentScope runtime.  Load
# the runtime package as a namespace so the tests exercise only the two
# handoff modules in scope.
_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "optomind_research" / "runtime"
if "optomind_research.runtime" not in sys.modules:
    _runtime = types.ModuleType("optomind_research.runtime")
    _runtime.__path__ = [str(_RUNTIME_PATH)]
    _runtime.__package__ = "optomind_research.runtime"
    sys.modules["optomind_research.runtime"] = _runtime

import pytest

from optomind_research.runtime.r3_production_handoff import (
    build_r3_production_handoff,
    validate_r3_production_handoff,
    write_r3_production_handoff,
)
from optomind_research.runtime.r4_phase3_artifacts import R4Phase3ArtifactStore


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory."""

    root = (
        Path(__file__).resolve().parents[1]
        / ".pytest-basetemp-r3-handoff"
    )
    root.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = root / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _components() -> dict:
    return {
        "topic_identity": {"topic_id": "topic-optics-v1"},
        "sections": [{"section_id": "S01", "title": "Measured mechanism", "topic_id": "topic-optics-v1"}],
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
                "argument_tasks": [{"task_id": "S01:T01", "description": "Explain the measured mechanism."}],
            }
        },
        "claims_by_criticality": {
            "load_bearing": [{
                "claim_id": "S01:C01",
                "section_id": "S01",
                "statement": "The measured mechanism controls the optical response.",
                "criticality": "load_bearing",
                "claim_state": "grounded",
                "evidence_type": "mechanism",
            }],
            "supporting": [],
            "optional": [],
        },
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
        "material_bindings": {
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
                    }
                },
            }
        },
        "relation_graph": {"schema_version": "r3.relation_graph.v1", "edges": []},
        "claim_dag": {"schema_version": "research_harness.claim_graph.v1", "edges": []},
        "gaps": [],
        "coverage_requests": [],
        "synthesis_bundles": {
            "S01": {
                "section_id": "S01",
                "status": "material_ready",
                "readiness_status": "ready_for_authoring",
                "paper_ids": ["P01"],
                "chunk_ids": ["K01"],
                "claim_category_assignments": [{"claim_id": "S01:C01", "category": "established_points"}],
            }
        },
        "visual_bindings": {"S01": []},
        "visual_needs": {"S01": []},
    }


def _build(**overrides):
    payload = _components()
    payload.update(overrides)
    return build_r3_production_handoff(**payload)


def test_successful_handoff_is_typed_and_authoring_ready(tmp_path: Path):
    handoff = _build()
    report = handoff.validate()

    assert report.valid is True
    assert report.global_readiness["status"] == "ready_for_authoring"
    assert report.section_readiness["S01"]["ready_for_authoring"] is True
    assert handoff.claims_by_criticality["load_bearing"][0].claim_id == "S01:C01"

    path = tmp_path / "R3_PRODUCTION_HANDOFF.json"
    write_r3_production_handoff(path, handoff, fail_on_invalid=True)
    store = R4Phase3ArtifactStore(tmp_path)
    artifacts = store.section("S01")
    assert store.production_handoff_source == "canonical"
    assert store.require_canonical_handoff().schema_version.endswith(".v1")
    assert artifacts.production_handoff_valid is True
    assert artifacts.ready_for_authoring is True
    assert artifacts.claims[0]["claim_id"] == "S01:C01"


def test_missing_claim_inventory_is_rejected():
    payload = _components()
    payload["claims_by_criticality"] = {"load_bearing": [], "supporting": [], "optional": []}
    report = validate_r3_production_handoff(_build(**payload))
    assert report.valid is False
    assert any("missing_claim_inventory" in error or "invented_claim_id" in error for error in report.errors)


def test_discovery_only_material_cannot_support_a_fact():
    payload = copy.deepcopy(_components())
    payload["material_inventory"]["chunks"]["K01"].update({
        "use_permission": "discovery_only",
        "content_depth": "metadata",
        "context_complete": False,
    })
    payload["material_bindings"]["S01"]["claims"]["S01:C01"]["permission_status"] = "bound"
    report = validate_r3_production_handoff(_build(**payload))
    assert report.valid is False
    assert any("discovery_only_cannot_support_claim" in error for error in report.errors)


def test_factual_permission_may_be_written_with_qualified_support():
    payload = copy.deepcopy(_components())
    claim = payload["claims_by_criticality"]["load_bearing"][0]
    claim.update({
        "claim_classification": "qualified",
        "support_classification": "qualified",
        "supported_rewrite": (
            "Available measurements indicate that the mechanism controls "
            "the optical response under the reported conditions."
        ),
    })
    binding = payload["material_bindings"]["S01"]["claims"]["S01:C01"]
    binding.update({
        "claim_classification": "qualified",
        "support_classification": "qualified",
        "permission_status": "bound",
        "write_status": "write_with_qualified_support",
    })

    report = validate_r3_production_handoff(_build(**payload))

    assert report.valid is True
    assert not any("write_status_permission_mismatch" in error for error in report.errors)


def test_qualified_permission_cannot_be_promoted_to_bound_writing():
    payload = copy.deepcopy(_components())
    payload["material_inventory"]["chunks"]["K01"].update({
        "use_permission": "method_transfer",
        "content_depth": "fulltext",
        "context_complete": True,
    })
    claim = payload["claims_by_criticality"]["load_bearing"][0]
    claim.update({
        "claim_classification": "qualified",
        "support_classification": "qualified",
    })
    binding = payload["material_bindings"]["S01"]["claims"]["S01:C01"]
    binding.update({
        "claim_classification": "qualified",
        "support_classification": "qualified",
        "permission_status": "qualified_only",
        "write_status": "bound",
        "factual_support_chunk_ids": [],
        "contextual_support_chunk_ids": ["K01"],
    })

    report = validate_r3_production_handoff(_build(**payload))

    assert report.valid is False
    assert any("write_status_permission_mismatch" in error for error in report.errors)


def test_stale_topic_artifact_is_rejected():
    payload = copy.deepcopy(_components())
    payload["coverage_atlas"]["topic_identity"] = {"topic_id": "old-topic"}
    report = validate_r3_production_handoff(_build(**payload))
    assert report.valid is False
    assert any("stale_topic_artifact" in error for error in report.errors)


def test_invented_chunk_id_is_rejected():
    payload = copy.deepcopy(_components())
    payload["material_bindings"]["S01"]["claims"]["S01:C01"]["supporting_chunk_ids"] = ["K-INVENTED"]
    report = validate_r3_production_handoff(_build(**payload))
    assert report.valid is False
    assert any("invented_chunk_id" in error for error in report.errors)


def test_unresolved_load_bearing_claim_requires_explicit_status():
    payload = copy.deepcopy(_components())
    binding = payload["material_bindings"]["S01"]["claims"]["S01:C01"]
    binding.update({
        "evidence_binding_status": "unbound",
        "permission_status": "unbound",
        "write_status": "write_with_declared_gap",
        "supporting_chunk_ids": [],
        "factual_support_chunk_ids": [],
        "paper_ids": [],
    })
    report = validate_r3_production_handoff(_build(**payload))
    assert report.valid is False
    assert any("load_bearing_claim_without_support_or_unresolved_status" in error for error in report.errors)

    payload["claims_by_criticality"]["load_bearing"][0].update({
        "claim_state": "open_question",
        "unresolved": True,
        "unresolved_reasons": ["independent validation is missing"],
    })
    payload["gaps"] = [{
        "gap_id": "G01",
        "section_id": "S01",
        "kind": "load_bearing_claim_material_gap",
        "claim_ids": ["S01:C01"],
        "blocking": True,
        "priority": "load_bearing",
    }]
    resolved_report = validate_r3_production_handoff(_build(**payload))
    assert resolved_report.valid is True
    assert resolved_report.global_readiness["status"] == "needs_more_literature"


def test_load_bearing_gap_does_not_block_section_with_other_authorable_claims():
    payload = copy.deepcopy(_components())
    payload["claims_by_criticality"]["supporting"].append({
        "claim_id": "S01:C02",
        "section_id": "S01",
        "statement": "The measured mechanism is reproducible under the reported conditions.",
        "criticality": "supporting",
        "claim_state": "grounded",
        "evidence_type": "measurement",
    })
    payload["material_bindings"]["S01"]["claims"]["S01:C01"].update({
        "evidence_binding_status": "unbound",
        "permission_status": "unbound",
        "write_status": "write_with_declared_gap",
        "supporting_chunk_ids": [],
        "factual_support_chunk_ids": [],
        "paper_ids": [],
    })
    payload["claims_by_criticality"]["load_bearing"][0].update({
        "claim_state": "open_question",
        "unresolved": True,
        "unresolved_reasons": ["independent validation is missing"],
    })
    payload["material_bindings"]["S01"]["claims"]["S01:C02"] = {
        "claim_id": "S01:C02",
        "evidence_binding_status": "bound",
        "permission_status": "bound",
        "write_status": "bound",
        "supporting_chunk_ids": ["K01"],
        "factual_support_chunk_ids": ["K01"],
        "paper_ids": ["P01"],
    }
    payload["synthesis_bundles"]["S01"]["claim_category_assignments"].append({
        "claim_id": "S01:C02",
        "category": "established_points",
    })

    report = validate_r3_production_handoff(_build(**payload))

    assert report.valid is True
    readiness = report.section_readiness["S01"]
    assert readiness["outcome"] == "ready_with_limits"
    assert readiness["ready_for_authoring"] is True
    assert "open_question:S01:C01" in readiness["declared_limits"]
    assert "unresolved_load_bearing_claim:S01:C01" in readiness["declared_limits"]


def test_doi_visual_parent_alias_maps_and_unmapped_visual_is_discovery_lead():
    payload = copy.deepcopy(_components())
    payload["material_inventory"]["papers"]["P01"]["doi"] = "10.5555/Optics.P01"
    payload["material_inventory"]["visuals"] = {
        "V-DOI": {
            "visual_id": "V-DOI",
            "paper_id": "https://doi.org/10.5555/optics.p01",
            "status": "accepted",
        },
        "V-EXTERNAL": {
            "visual_id": "V-EXTERNAL",
            "paper_id": "doi:10.9999/not-active",
            "status": "accepted",
        },
    }
    payload["visual_bindings"]["S01"] = [
        {"visual_binding_id": "VB-1", "visual_id": "V-DOI"},
        {"visual_binding_id": "VB-2", "visual_id": "V-EXTERNAL"},
    ]

    handoff = _build(**payload)
    report = handoff.validate()

    assert report.valid is True
    assert handoff.material_inventory["visuals"]["V-DOI"]["paper_id"] == "P01"
    assert "V-EXTERNAL" not in handoff.material_inventory["visuals"]
    assert [row["visual_id"] for row in handoff.visual_bindings["S01"]] == ["V-DOI"]
    assert handoff.identity_resolution["visuals"]["excluded_discovery_lead_count"] == 1


def test_corpus_id_relation_aliases_are_canonical_and_basis_gated():
    payload = copy.deepcopy(_components())
    payload["material_inventory"]["papers"]["P01"]["corpus_id"] = 101
    payload["material_inventory"]["papers"]["P02"] = {
        "paper_id": "P02",
        "external_ids": {"CorpusId": 202},
        "scope_fit": "direct",
        "use_permission": "factual_support",
        "content_depth": "fulltext",
        "context_complete": True,
    }
    payload["material_inventory"]["chunks"]["K02"] = {
        "chunk_id": "K02",
        "paper_id": "P02",
        "scope_fit": "direct",
        "use_permission": "factual_support",
        "content_depth": "fulltext",
        "context_complete": True,
        "source_kind": "fulltext",
    }
    payload["relation_graph"] = {
        "schema_version": "r3.relation_graph.v1",
        "edges": [
            {
                "edge_id": "E-ALIAS",
                "source_paper_id": "CorpusId:101",
                "target_paper_id": "s2:202",
                "edge_type": "supports",
                "source_chunk_id": "K01",
            },
            {
                "edge_id": "E-EXTERNAL",
                "source_paper_id": "CorpusId:101",
                "target_paper_id": "CorpusId:999",
                "edge_type": "cited_by",
                "relation_basis_chunk_ids": ["K01"],
            },
            {
                "edge_id": "E-NO-BASIS",
                "source_paper_id": "CorpusId:101",
                "target_paper_id": "CorpusId:202",
                "edge_type": "supports",
            },
        ],
    }

    handoff = _build(**payload)
    report = handoff.validate()

    assert report.valid is True
    assert handoff.identity_resolution["alias_map"]["s2:corpus:101"] == "P01"
    assert handoff.identity_resolution["alias_map"]["s2:corpus:202"] == "P02"
    authoring_edges = handoff.relation_graph["edges"]
    assert len(authoring_edges) == 1
    assert authoring_edges[0]["source_paper_id"] == "P01"
    assert authoring_edges[0]["target_paper_id"] == "P02"
    assert authoring_edges[0]["relation_basis_chunk_ids"] == ["K01"]
    assert len(handoff.relation_graph["discovery_edges"]) == 2
    assert all(not row.get("authoring_eligible") for row in handoff.relation_graph["discovery_edges"])


def test_validation_errors_aggregate_repeated_invented_ids():
    payload = copy.deepcopy(_components())
    payload["material_bindings"]["S01"]["claims"]["S01:C01"]["paper_ids"] = [
        f"P-INVENTED-{index:02d}" for index in range(12)
    ]

    report = _build(**payload).validate()

    assert report.valid is False
    assert len(report.errors) < 12
    assert any(
        error.startswith("invented_paper_id:material_binding:")
        and ":count=12:" in error
        for error in report.errors
    )


def test_unobserved_relation_with_unknown_endpoint_remains_strict():
    payload = copy.deepcopy(_components())
    payload["relation_graph"] = {
        "schema_version": "r3.relation_graph.v1",
        "edges": [{
            "edge_id": "E-INVENTED",
            "source_paper_id": "P01",
            "target_paper_id": "P-NOT-ACTIVE",
            "edge_type": "supports",
            "relation_basis_chunk_ids": ["K01"],
        }],
    }

    report = _build(**payload).validate()

    assert report.valid is False
    assert any("invented_paper_id:relation_target" in error for error in report.errors)


def test_missing_canonical_handoff_closes_r4_even_when_old_files_exist(tmp_path: Path):
    # Old plural artifacts alone must not open authoring.  The explicit
    # migration constructor remains available to callers that accept it.
    (tmp_path / "COVERAGE_ATLAS.json").write_text("{}", encoding="utf-8")
    store = R4Phase3ArtifactStore(tmp_path)
    artifacts = store.section("S01")
    assert artifacts.production_handoff_valid is False
    assert artifacts.ready_for_authoring is False
    assert artifacts.claims == []
    assert artifacts.bundle["production_handoff_required"] is True
    assert "missing_R3_PRODUCTION_HANDOFF.json" in store.diagnostics
