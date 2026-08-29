"""Focused tests for the durable post-grounding checkpoint.

Covers: checkpoint creation after successful section grounding, completeness
contract compliance, reuse via --planner-architecture-path without invoking
the grounder, and no checkpoint on empty sections / grounding failures /
incomplete grounding contracts.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import uuid
from pathlib import Path

import pytest

from optomind_research.review_blueprint_planner import (
    DynamicReviewBlueprintPlanner,
)


@pytest.fixture()
def checkpoint_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"grounded-checkpoint-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _planner(checkpoint_tmp: Path, **overrides) -> DynamicReviewBlueprintPlanner:
    defaults = dict(
        concept_map_path=checkpoint_tmp / "concepts.json",
        output_dir=checkpoint_tmp / "out",
        user_question="Compare radiative cooling mechanisms.",
        problem_understanding="Compare radiative cooling mechanisms.",
        scope_definition="Compare radiative cooling mechanisms.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
    )
    defaults.update(overrides)
    return DynamicReviewBlueprintPlanner(**defaults)


def _architecture() -> dict:
    return {
        "review_thesis": (
            "Radiative cooling materials improve building thermal management."
        ),
        "narrative_strategy": "Mechanism-to-application progression.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Radiative cooling mechanisms",
                "argument_role": (
                    "Explain the governing physics of radiative cooling."
                ),
                "unique_contribution": (
                    "Establish the physical basis of radiative cooling."
                ),
                "must_cover": ["physical mechanism"],
                "must_not_cover": ["application benchmarking"],
                "assigned_user_axes": ["mechanism_axis"],
                "handoff_from_previous": "introduction",
                "handoff_to_next": "material routes",
                "key_questions": [
                    "How does radiative cooling improve thermal management?"
                ],
                "claim_seeds": [
                    {
                        "claim_seed": (
                            "Radiative cooling materials improve building "
                            "thermal management."
                        ),
                        "relation_to_section": "support",
                    }
                ],
            }
        ],
    }


def _evidence(*, relevant: bool = True) -> dict:
    preview = (
        "Radiative cooling materials improve thermal management of buildings "
        "under clear-sky conditions."
        if relevant
        else "Unrelated agricultural irrigation scheduling content."
    )
    return {
        "selected_concept_nodes": [],
        "retrieved_text_chunks": [
            {
                "chunk_id": f"c{index:03d}",
                "paper_id": f"p{index:03d}",
                "title": f"Paper {index}",
                "section_path": "results",
                "text_preview": preview,
                "material_binding_search_text": preview,
                "use_permission": "factual_support",
            }
            for index in range(3)
        ],
        "retrieved_visual_chunks": [],
    }


def _fake_grounder_chat():
    from optomind_research import review_blueprint_planner as planner_module

    def fake_chat(agent_name, messages, **kwargs):
        if agent_name != "ReviewBlueprintEvidenceGrounderAgent":
            raise AssertionError(f"unexpected agent {agent_name}")
        return {
            "content": "{}",
            "_llm_usage": {
                "success": True,
                "input_tokens": 1,
                "output_tokens": 1,
                "model_name": "mock",
                "mock_llm": True,
            },
        }

    return planner_module, fake_chat


def test_grounded_checkpoint_written_and_reusable_without_grounder(
    checkpoint_tmp: Path, monkeypatch
) -> None:
    planner_module, fake_chat = _fake_grounder_chat()
    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    planner = _planner(checkpoint_tmp)
    grounded = planner._ground_blueprint_architecture(
        _architecture(), _evidence(relevant=True)
    )

    checkpoint_path = (
        checkpoint_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert checkpoint_path.exists()
    loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert planner._is_complete_grounded_architecture(loaded) is True
    assert loaded["review_thesis"] == _architecture()["review_thesis"]
    assert loaded["sections"][0]["section_id"] == "S01"
    assert loaded["sections"][0]["_grounding_status"]
    assert loaded["sections"][0]["candidate_material_pool"]["served_chunk_ids"]
    assert loaded["sections"][0]["_text_candidate_admission_audit"]
    summary = loaded["_grounding_summary"]
    assert summary["sections"] == 1
    assert summary["grounded_checkpoint_written"] is True
    assert summary["grounded_checkpoint_path"] == str(checkpoint_path)
    assert grounded["_grounding_summary"]["grounded_checkpoint_written"] is True

    # Reuse through --planner-architecture-path must not invoke the grounder.
    reuse_planner = _planner(
        checkpoint_tmp,
        output_dir=checkpoint_tmp / "out_reuse",
        planner_architecture_path=checkpoint_path,
    )

    def boom_ground(*args, **kwargs):
        raise AssertionError("grounder must not run for a grounded checkpoint")

    monkeypatch.setattr(
        reuse_planner, "_ground_blueprint_architecture", boom_ground
    )
    reused = reuse_planner._production_architecture({})
    assert reused["_reused_grounded_architecture"] is True
    assert reused["review_thesis"] == _architecture()["review_thesis"]
    assert len(reused["sections"]) == 1
    assert reused["sections"][0]["candidate_material_pool"]["served_chunk_ids"]


def test_no_checkpoint_when_no_sections(checkpoint_tmp: Path) -> None:
    planner = _planner(checkpoint_tmp)
    architecture = {"sections": []}
    result = planner._ground_blueprint_architecture(
        architecture, _evidence(relevant=True)
    )
    assert result == architecture
    checkpoint_path = (
        checkpoint_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert not checkpoint_path.exists()


def test_no_checkpoint_when_grounding_raises(
    checkpoint_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    def boom_chat(*args, **kwargs):
        raise RuntimeError("grounder boom")

    monkeypatch.setattr(planner_module, "call_qwen_chat", boom_chat)
    planner = _planner(checkpoint_tmp)
    with pytest.raises(RuntimeError, match="grounder boom"):
        planner._ground_blueprint_architecture(
            _architecture(), _evidence(relevant=True)
        )
    checkpoint_path = (
        checkpoint_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert not checkpoint_path.exists()


def test_no_checkpoint_when_grounding_contract_incomplete(
    checkpoint_tmp: Path, monkeypatch
) -> None:
    planner_module, fake_chat = _fake_grounder_chat()
    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    planner = _planner(checkpoint_tmp)
    grounded = planner._ground_blueprint_architecture(
        _architecture(), _evidence(relevant=False)
    )

    assert planner._is_complete_grounded_architecture(grounded) is False
    summary = grounded["_grounding_summary"]
    assert summary["grounded_checkpoint_written"] is False
    assert summary["grounded_checkpoint_reason"] == (
        "incomplete_grounding_contract"
    )
    checkpoint_path = (
        checkpoint_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert not checkpoint_path.exists()


def _material_unit(
    chunk_id: str,
    *,
    source_kind: str,
    permission: str,
) -> dict:
    return {
        "unit_id": f"unit:{chunk_id}",
        "work_id": f"work:{chunk_id}",
        "identity": {
            "chunk_id": chunk_id,
            "paper_id": f"PAPER_{chunk_id}",
            "title": f"Title {chunk_id}",
        },
        "durable_content": {
            "raw_text": f"Raw durable text for {chunk_id}.",
            "normalized_text": f"Raw durable text for {chunk_id}.",
            "content_depth": "fulltext",
            "section_path": "results",
        },
        "durable_content_card": {
            "content_quality": {
                "source_kind": source_kind,
                "evidence_ceiling": permission,
                "context_complete": True,
            }
        },
        "query_annotations": [{
            "propositions": [{
                "proposition_id": f"prop-{chunk_id}",
                "statement": f"Proposition for {chunk_id}.",
            }],
        }],
        "audit": {
            "source_provenance": {"route": "supplementary_material_cache"},
        },
    }


def test_checkpoint_reuse_rematerializes_full_pool_from_material_units(
    checkpoint_tmp: Path,
) -> None:
    requested_ids = [f"c{index:03d}" for index in range(201)]
    rng = random.Random(7)
    rng.shuffle(requested_ids)
    missing_id = "c150"
    units = {
        chunk_id: _material_unit(
            chunk_id,
            source_kind=(
                "s2_body" if int(chunk_id[1:]) % 2 == 0 else "fulltext"
            ),
            permission="factual_support",
        )
        for chunk_id in requested_ids
        if chunk_id != missing_id
    }
    checkpoint = {
        "_grounding_summary": {
            "mode": "parallel_section_grounding",
            "sections": 1,
            "grounded_checkpoint_written": True,
        },
        "review_thesis": "Radiative cooling materials improve thermal management.",
        "sections": [{
            "section_id": "S01",
            "title": "Radiative cooling mechanisms",
            "argument_role": "Explain the governing physics.",
            "candidate_material_pool": {
                "candidate_chunk_ids": list(requested_ids),
            },
        }],
    }
    planner = _planner(checkpoint_tmp)
    planner.material_units_by_chunk_id = units
    sections = planner._sections_from_llm_plan(
        checkpoint,
        {},
        allow_deterministic_completion=False,
    )
    assert len(sections) == 1
    section = sections[0]
    digest_index = section["candidate_evidence_digest"]["chunk_index"]
    digest_ids = [row["chunk_id"] for row in digest_index]
    assert len(digest_ids) == 200
    assert digest_ids == [
        chunk_id for chunk_id in requested_ids if chunk_id != missing_id
    ]
    assert missing_id not in digest_ids
    assert section["candidate_text_chunk_ids"] == digest_ids

    audit = section["checkpoint_rematerialization_audit"]
    assert audit["enabled"] is True
    assert audit["requested_ids"] == list(requested_ids)
    assert len(audit["reconstructed_ids"]) == 200
    assert audit["missing_ids"] == [missing_id]

    first_digest = digest_index[0]
    assert first_digest["paper_id"] == f"PAPER_{first_digest['chunk_id']}"
    assert first_digest["use_permission"] == "factual_support"
    assert first_digest["propositions"][0]["statement"] == (
        f"Proposition for {first_digest['chunk_id']}."
    )
    first_chunk = section["candidate_text_chunks"][0]
    assert first_chunk["material_unit_id"] == (
        f"unit:{first_chunk['chunk_id']}"
    )
    assert first_chunk["text_preview"].startswith("Raw durable text for")
    assert first_chunk["material_card_binding"]["propositions"][0][
        "statement"
    ] == f"Proposition for {first_chunk['chunk_id']}."
    assert first_chunk["provenance"]["route"] == (
        "supplementary_material_cache"
    )

    s2_rows = [
        row for row in digest_index
        if row["source_kind"] == "s2_body"
    ]
    fulltext_rows = [
        row for row in digest_index
        if row["source_kind"] == "fulltext"
    ]
    assert s2_rows and fulltext_rows
    assert all(row["use_permission"] == "factual_support" for row in s2_rows)
    assert all(
        row["use_permission"] == "factual_support"
        for row in fulltext_rows
    )
