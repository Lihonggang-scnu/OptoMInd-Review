from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from optomind_research.review_blueprint_planner import DynamicReviewBlueprintPlanner


@pytest.fixture()
def tmp_path() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"material-grounding-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _planner(tmp_path: Path) -> DynamicReviewBlueprintPlanner:
    return DynamicReviewBlueprintPlanner(
        tmp_path / "concept-map.json",
        tmp_path / "blueprint",
        user_question="Compare two scientific methods.",
        problem_understanding="Compare two scientific methods.",
        scope_definition="Compare two scientific methods.",
        enable_mentor=False,
        real_llm_plan=False,
        min_sections=4,
        max_sections=4,
    )


def _unit(chunk_id: str, *, source_kind: str = "s2_body_snippet") -> dict:
    permission = (
        "contextual_or_qualified_support"
        if source_kind in {"s2_body_snippet", "abstract"}
        else "factual_support"
    )
    return {
        "unit_id": f"unit:{chunk_id}",
        "work_id": f"work:{chunk_id}",
        "identity": {"chunk_id": chunk_id, "paper_id": f"paper:{chunk_id}"},
        "durable_content": {"content_depth": "abstract" if source_kind == "abstract" else "structured_snippet"},
        "durable_content_card": {
            "content_quality": {
                "source_kind": source_kind,
                "evidence_ceiling": permission,
                "context_complete": False,
            }
        },
        "query_annotations": [
            {
                "query_id": "query:test",
                "question_relevance": "substantial",
                "paper_functions": ["method_or_model"],
                "seed_axis_assignments": [
                    {"axis_id": "F01", "fit": "substantial", "question_function": "comparison_input"}
                ],
                "propositions": [
                    {
                        "proposition_id": f"prop:{chunk_id}",
                        "statement": "The source reports a method comparison.",
                        "proposition_kind": "comparison",
                        "question_function": "comparison_input",
                        "evidence_permissions": {chunk_id: permission},
                    }
                ],
            }
        ],
        "audit": {"source_provenance": {"use_permission": permission}},
    }


def test_material_binding_preserves_permission_and_propositions(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    unit = _unit("chunk-1")
    planner.material_units_by_chunk_id = {"chunk-1": unit}

    row = planner._attach_material_binding(
        {"chunk_id": "chunk-1", "retrieval_source": "material_unit_cache"}
    )

    assert row["material_unit_id"] == "unit:chunk-1"
    assert row["use_permission"] == "contextual_or_qualified_support"
    assert row["factual_support_allowed"] is False
    assert row["material_card_binding"]["bound"] is True
    assert row["material_card_binding"]["propositions"][0]["proposition_id"] == "prop:chunk-1"


def test_sqlite_id_lookup_keeps_retrieval_source_and_material_binding(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    database = tmp_path / "kb.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, section_path TEXT, text TEXT)"
        )
        connection.execute(
            "INSERT INTO text_chunks VALUES (?, ?, ?, ?, ?)",
            ("chunk-1", "paper-1", "A paper", "Results", "The source reports a comparison."),
        )
    planner.db_path = database
    planner.material_units_by_chunk_id = {"chunk-1": _unit("chunk-1")}

    rows = planner._fetch_text_chunks_by_ids(["chunk-1"])

    assert rows[0]["retrieval_source"] == "sqlite_id_lookup"
    assert rows[0]["material_card_binding"]["bound"] is True
    assert rows[0]["use_permission"] == "contextual_or_qualified_support"


def test_validation_rejects_abstract_permission_promotion(tmp_path: Path) -> None:
    planner = _planner(tmp_path)
    sections = []
    for section_index in range(4):
        candidates = []
        for candidate_index in range(3):
            chunk_id = f"chunk-{section_index}-{candidate_index}"
            unit = _unit(chunk_id, source_kind="abstract")
            planner.material_units_by_chunk_id[chunk_id] = unit
            candidates.append(
                planner._attach_material_binding(
                    {"chunk_id": chunk_id, "retrieval_source": "material_unit_cache"},
                    unit,
                )
            )
        sections.append(
            {
                "section_id": f"S{section_index + 1:02d}",
                "title": f"Section {section_index + 1}",
                "concept_map_nodes": [{"node_id": f"node-{section_index}"}],
                "candidate_text_chunks": candidates,
                "candidate_text_chunk_ids": [row["chunk_id"] for row in candidates],
                "visual_argument_slots": [],
                "claims": [
                    {"claim_id": f"C{section_index}-1", "evidence_requirement": "factual"},
                    {"claim_id": f"C{section_index}-2", "evidence_requirement": "factual"},
                ],
                "transition_from_previous": "Continue the comparison.",
                "transition_to_next": "Continue the comparison.",
            }
        )
    blueprint = {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": sections,
        "argument_dag": {"edge_count": 1, "cross_section_edge_count": 1},
        "claim_decomposition_status": {"real_llm_claims": False},
        "planner_output_status": {},
        "planning_evidence_brief": {"cluster_candidates": [{"cluster_id": "cluster-1"}]},
        "scope_coverage_status": {},
        "integrated_refinement_status": {"sidecar_refinement_removed": True},
    }
    assert planner._validate(blueprint)["passed"] is True

    promoted = sections[0]["candidate_text_chunks"][0]
    promoted["use_permission"] = "factual_support"
    promoted["factual_support_allowed"] = True
    validation = planner._validate(blueprint)

    assert validation["passed"] is False
    assert validation["checks"]["candidate_material_permissions_match_store"] is False
    assert validation["checks"]["abstract_permission_ceiling_preserved"] is False
