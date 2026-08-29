import re
import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path(request):
    """Workspace-local temporary directory (system temp is sandbox-blocked)."""
    base = Path(__file__).resolve().parents[1] / ".pytest-basetemp-blueprint-candidate-pool"
    base.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = base / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def test_blueprint_keeps_broad_pool_and_compact_model_context(tmp_path: Path) -> None:
    from optomind_research.review_blueprint_planner import (
        ConceptNode,
        DynamicReviewBlueprintPlanner,
    )

    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concepts.json",
        tmp_path / "out",
        user_question="Compare two methods.",
        problem_understanding="Compare two methods.",
        scope_definition="Compare two methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        served_text_limit=40,
        model_text_context_limit=None,
    )
    planner.concept_nodes = [
        ConceptNode(
            node_id="n1",
            view_id="method_view",
            view_name="Method view",
            label="Method comparison",
            purpose="Compare methods",
            planning_value="Identify tradeoffs",
            evidence_counts={"paper_count": 50},
            raw={},
        )
    ]
    evidence = {
        "retrieved_text_chunks": [
            {
                "chunk_id": f"c{i:03d}",
                "paper_id": f"p{i:03d}",
                "title": "Method comparison",
                "text_preview": "A method comparison reports a bounded optical tradeoff.",
            }
            for i in range(60)
        ],
        "retrieved_visual_chunks": [],
    }
    parsed = {
        "sections": [
            {
                "section_id": f"S{i:02d}",
                "title": f"Method comparison {i}",
                "argument_role": "Compare methods.",
                "concept_node_ids": ["n1"],
                "text_chunk_ids": ["c000", "c001"],
                "candidate_text_pool_ids": [f"c{j:03d}" for j in range(60)],
            }
            for i in range(1, 5)
        ]
    }
    sections = planner._sections_from_llm_plan(parsed, evidence)
    assert len(sections) == 4
    section = sections[0]
    assert len(section["candidate_text_chunks"]) == 40
    assert len(section["candidate_text_context"]) == 40
    assert section["candidate_material_pool"]["served_limit"] == 40
    assert section["candidate_material_pool"]["model_context_limit"] == 40


def test_blueprint_allows_explicit_context_budget_for_constrained_runs(tmp_path: Path) -> None:
    from optomind_research.review_blueprint_planner import DynamicReviewBlueprintPlanner

    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concepts.json",
        tmp_path / "out",
        user_question="Compare two scientific methods.",
        problem_understanding="Compare two scientific methods.",
        scope_definition="Compare two scientific methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        served_text_limit=40,
        model_text_context_limit=12,
        evidence_batch_size=10,
    )
    assert planner.served_text_limit == 40
    assert planner.model_text_context_limit == 12
    assert planner.evidence_batch_size == 10


def test_deterministic_sections_record_the_full_visible_pool(tmp_path: Path) -> None:
    from optomind_research.review_blueprint_planner import ConceptNode, DynamicReviewBlueprintPlanner

    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concepts.json",
        tmp_path / "out",
        user_question="Compare two scientific methods.",
        problem_understanding="Compare two scientific methods.",
        scope_definition="Compare two scientific methods.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
        served_text_limit=90,
        model_text_context_limit=None,
    )
    node = ConceptNode(
        node_id="n1",
        view_id="method_view",
        view_name="Method view",
        label="Method comparison",
        purpose="Compare methods",
        planning_value="Identify tradeoffs",
        evidence_counts={},
        raw={},
    )
    chunks = [
        {"chunk_id": f"c{i:03d}", "paper_id": f"p{i:03d}", "text_preview": "comparison"}
        for i in range(25)
    ]
    section = planner._dynamic_section(
        "S01",
        {"view_name": "Method view", "central_labels": ["Method comparison"]},
        [node],
        chunks,
        [],
    )
    assert len(section["candidate_text_context"]) == 25
    assert section["candidate_material_pool"]["visible_candidate_count"] == 25
    assert section["candidate_material_pool"]["all_candidates_visible_to_grounder"] is True


def test_architecture_payload_keeps_the_full_text_inventory(tmp_path: Path) -> None:
    from optomind_research.review_blueprint_planner import DynamicReviewBlueprintPlanner

    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concepts.json",
        tmp_path / "out",
        user_question="Compare two scientific methods.",
        problem_understanding="Compare two scientific methods.",
        scope_definition="Compare two scientific methods.",
        enable_mentor=False,
    )
    evidence = {
        "retrieved_text_chunks": [
            {"chunk_id": f"c{i:03d}", "paper_id": f"p{i:03d}", "text_preview": "comparison"}
            for i in range(70)
        ],
        "retrieved_visual_chunks": [],
        "cluster_candidates": [],
        "selected_concept_nodes": [],
    }
    compact = planner._compact_evidence_for_llm(evidence)
    assert len(compact["retrieved_text_chunks"]) == 12
    assert compact["evidence_digest"]["chunk_count"] == 70
    assert compact["evidence_digest"]["batch_count"] == 6


def test_claim_payload_uses_batches_instead_of_raw_full_text(tmp_path: Path) -> None:
    from optomind_research.claim_decomposer import ClaimDecomposer
    from optomind_research.review_blueprint_planner import build_evidence_digest

    chunks = [
        {
            "chunk_id": f"c{i:03d}",
            "paper_id": f"p{i:03d}",
            "title": "Method comparison",
            "text_preview": "Long raw source text that should stay in the evidence store.",
            "material_card_binding": {
                "propositions": [
                    {"proposition_id": f"prop-{i}", "statement": "The source reports a bounded comparison."}
                ]
            },
        }
        for i in range(24)
    ]
    section = {
        "section_id": "S01",
        "title": "Method comparison",
        "argument_role": "Compare methods.",
        "candidate_text_chunks": chunks,
        "candidate_text_context": chunks,
        "candidate_text_chunk_ids": [x["chunk_id"] for x in chunks],
        "candidate_evidence_digest": build_evidence_digest(chunks),
    }
    payload = ClaimDecomposer(real_llm=False)._build_input_payload(section)
    assert len(payload["candidate_text_chunks"]) == 24
    assert len(payload["evidence_batches"]) == 2
    assert all(len(row["preview"]) <= 260 for row in payload["candidate_text_chunks"])
    assert "Long raw source text" not in payload["candidate_text_chunks"][0]["preview"]
