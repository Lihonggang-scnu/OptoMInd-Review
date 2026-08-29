from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.blueprint_argument_quality import build_claim_evidence_contracts
from optomind_research.review_blueprint_planner import ConceptNode, DynamicReviewBlueprintPlanner


@pytest.fixture
def tmp_path(request):
    """Workspace-local temporary directory (system temp is sandbox-blocked)."""
    base = Path(__file__).resolve().parents[1] / ".pytest-basetemp-blueprint-argument-quality"
    base.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = base / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _unit(chunk_id: str, permission: str, proposition_id: str = "p1") -> dict:
    return {
        "unit_id": f"unit:{chunk_id}",
        "identity": {"chunk_id": chunk_id, "paper_id": f"paper:{chunk_id}"},
        "durable_content": {"content_depth": "fulltext"},
        "durable_content_card": {"content_quality": {"source_kind": "fulltext", "evidence_ceiling": permission}},
        "query_annotations": [{"propositions": [{"proposition_id": proposition_id, "statement": "The method improves inverse design accuracy."}]}],
        "audit": {"source_provenance": {"use_permission": permission}},
    }


def test_factual_claim_gets_permission_aware_contract() -> None:
    sections = [{
        "section_id": "S01",
        "candidate_text_chunks": [
            {"chunk_id": "c1", "paper_id": "paper-1", "title": "Differentiable inverse design", "text_preview": "The method improves inverse design accuracy."},
            {"chunk_id": "c2", "paper_id": "paper-2", "title": "Abstract comparison", "text_preview": "The method is discussed."},
        ],
        "claims": [{"claim_id": "S01-C01", "statement": "Differentiable solvers improve inverse design accuracy.", "claim_kind": "direct_fact", "evidence_requirement": "factual", "load_bearing": True}],
    }]
    units = {"c1": _unit("c1", "factual_support"), "c2": _unit("c2", "contextual_or_qualified_support", "p2")}
    sections, report = build_claim_evidence_contracts(sections, units)
    contract = sections[0]["claims"][0]["evidence_contract"]
    assert contract["status"] == "candidate_ready"
    assert contract["minimum_use_permission"] == "factual_support"
    assert "c1" in contract["candidate_chunk_ids"]
    assert report["factual_candidate_ready_ratio"] == 1.0


def test_abstract_only_material_stays_a_gap_for_factual_claim() -> None:
    sections = [{
        "section_id": "S01",
        "candidate_text_chunks": [{"chunk_id": "c1", "title": "Inverse design", "text_preview": "The method improves accuracy."}],
        "claims": [{"claim_id": "S01-C01", "statement": "The method improves accuracy.", "claim_kind": "direct_fact", "evidence_requirement": "factual"}],
    }]
    sections, _ = build_claim_evidence_contracts(sections, {"c1": _unit("c1", "contextual_or_qualified_support")})
    contract = sections[0]["claims"][0]["evidence_contract"]
    assert contract["status"] == "gap"
    assert any("factual_support" in item for item in contract["missing_components"])


def test_contracts_are_not_final_bindings() -> None:
    sections = [{
        "section_id": "S01",
        "candidate_text_chunks": [{"chunk_id": "c1", "text_preview": "A mechanism is reported."}],
        "claims": [{"claim_id": "S01-C01", "statement": "A mechanism is reported.", "claim_kind": "mechanism_synthesis"}],
    }]
    sections, report = build_claim_evidence_contracts(sections, {"c1": _unit("c1", "factual_support")})
    assert "later_binding_rule" in sections[0]["claims"][0]["evidence_contract"]
    assert "Candidate contracts" in report["interpretation"]


def test_axis_assignment_does_not_reuse_stale_claim_text(tmp_path) -> None:
    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concept.json",
        tmp_path / "out",
        user_question="Compare methods and validation.",
        problem_understanding="Compare methods and validation.",
        scope_definition="Compare methods and validation.",
        enable_mentor=False,
    )
    planner.concept_nodes = [
        ConceptNode("material-axis:F04", "F04", "Axis", "Forward-model fidelity", "", "", {}, raw={}),
        ConceptNode("material-axis:F07", "F07", "Axis", "Benchmarking standards and datasets for comparing simulated and measured responses", "", "", {}, raw={}),
    ]
    sections = [
        {
            "section_id": "S03",
            "title": "Forward-model fidelity and reliability",
            "argument_role": "Analyze simulation error propagation.",
            "key_questions": ["How does the simulated model affect reliability?"],
            "candidate_search_seeds": ["forward model error propagation"],
            "evidence_risks": ["Benchmarking standards are still missing from this section."],
            "concept_map_nodes": [{"node_id": "material-axis:F04"}],
            "candidate_text_chunks": [],
        },
        {
            "section_id": "S04",
            "title": "Simulation-experiment validation and benchmarking",
            "argument_role": "Identify benchmarking deficiencies and measured validation gaps.",
            "key_questions": ["How should simulated and measured responses be compared?"],
            "candidate_search_seeds": ["metasurface benchmarking measured response"],
            "concept_map_nodes": [],
            "candidate_text_chunks": [{
                "chunk_id": "c1",
                "title": "Benchmarking optical responses",
                "text_preview": "Standards compare simulated and measured responses.",
                "material_card_binding": {"propositions": [{"statement": "Standards compare simulated and measured responses."}]},
            }],
        },
    ]
    updated, coverage = planner._ensure_seed_axis_coverage(sections)
    assert coverage["added_axes"] == [{"axis_id": "F07", "section_id": "S04", "basis": "distinctive_axis_term_match"}]
    assert any(node["node_id"] == "material-axis:F07" for node in updated[1]["concept_map_nodes"])
