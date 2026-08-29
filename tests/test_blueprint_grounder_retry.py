"""Focused tests for bounded section-grounder retries and progress artifacts."""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.review_blueprint_planner import (
    DynamicReviewBlueprintPlanner,
)


@pytest.fixture()
def grounder_tmp() -> Path:
    root = (
        Path(__file__).resolve().parent.parent
        / f"grounder-retry-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _planner(grounder_tmp: Path, **overrides) -> DynamicReviewBlueprintPlanner:
    defaults = dict(
        concept_map_path=grounder_tmp / "concepts.json",
        output_dir=grounder_tmp / "out",
        user_question="Compare radiative cooling mechanisms.",
        problem_understanding="Compare radiative cooling mechanisms.",
        scope_definition="Compare radiative cooling mechanisms.",
        enable_mentor=False,
        min_sections=4,
        max_sections=4,
    )
    defaults.update(overrides)
    return DynamicReviewBlueprintPlanner(**defaults)


def _architecture(section_count: int = 2) -> dict[str, Any]:
    sections = []
    for index in range(1, section_count + 1):
        sections.append({
            "section_id": f"S{index:02d}",
            "title": f"Chapter {index}",
            "argument_role": "core",
            "unique_contribution": (
                "Establish the radiative cooling mechanism."
            ),
            "must_cover": ["mechanism"],
            "must_not_cover": ["benchmarking"],
            "assigned_user_axes": ["mechanism_axis"],
            "handoff_from_previous": "intro",
            "handoff_to_next": "next",
            "key_questions": [
                "How does radiative cooling improve thermal management?"
            ],
            "claim_seeds": [{
                "claim_seed": (
                    "Radiative cooling materials improve building thermal "
                    "management."
                ),
                "relation_to_section": "support",
            }],
        })
    return {
        "review_thesis": "Radiative cooling materials improve thermal management.",
        "narrative_strategy": "Mechanism-to-application progression.",
        "sections": sections,
    }


def _evidence() -> dict[str, Any]:
    preview = (
        "Radiative cooling materials improve thermal management of buildings "
        "under clear-sky conditions."
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


def _valid_grounder_payload() -> dict[str, Any]:
    return {
        "concept_node_ids": ["c001"],
        "text_chunk_ids": ["c001"],
        "visual_chunk_ids": [],
        "claim_bindings": [],
        "visual_argument_slots": [],
        "uncovered_needs": [],
    }


def _usage(success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "input_tokens": 10,
        "output_tokens": 5,
        "model_name": "qwen3.7-flash",
        "mock_llm": False,
    }


def test_grounder_call_kwargs_single_model_no_retry_bounded_stream(
    grounder_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    calls: list[dict[str, Any]] = []

    def fake_chat(agent_name, messages, **kwargs):
        calls.append({
            "agent_name": agent_name,
            **kwargs,
        })
        return {
            "content": json.dumps(_valid_grounder_payload()),
            "_llm_usage": _usage(),
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    planner = _planner(grounder_tmp)
    grounded = planner._ground_blueprint_architecture(
        _architecture(section_count=2), _evidence()
    )
    assert len(grounded["sections"]) == 2
    assert len(calls) == 2
    for call in calls:
        assert call["agent_name"] == "ReviewBlueprintEvidenceGrounderAgent"
        assert call["model_tier"] == "b_plus_model"
        assert call["allow_model_fallback"] is False
        assert call["max_key_candidates"] == 1
        assert call["max_transport_key_candidates"] == 1
        assert call["max_retries"] == 0
        assert call["stream"] is True
        assert call["accept_partial_stream"] is False
        assert call["timeout_seconds"] == 180.0


def test_grounder_timeout_env_override(grounder_tmp: Path, monkeypatch) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    monkeypatch.setenv("QWEN_GROUNDER_HTTP_TIMEOUT_SEC", "77")
    captured: list[dict[str, Any]] = []

    def fake_chat(agent_name, messages, **kwargs):
        captured.append(kwargs)
        return {
            "content": json.dumps(_valid_grounder_payload()),
            "_llm_usage": _usage(),
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    planner = _planner(grounder_tmp)
    planner._ground_blueprint_architecture(
        _architecture(section_count=1), _evidence()
    )
    assert captured[0]["timeout_seconds"] == 77.0


def test_incremental_progress_writes_and_full_checkpoint_remains_valid(
    grounder_tmp: Path, monkeypatch
) -> None:
    from optomind_research import review_blueprint_planner as planner_module

    writes: list[tuple[str, Any]] = []
    real_write_json = planner_module.write_json

    def recording_write_json(path, value):
        writes.append((str(path), copy.deepcopy(value)))
        real_write_json(path, value)

    monkeypatch.setattr(planner_module, "write_json", recording_write_json)

    def fake_chat(agent_name, messages, **kwargs):
        return {
            "content": json.dumps(_valid_grounder_payload()),
            "_llm_usage": _usage(),
        }

    monkeypatch.setattr(planner_module, "call_qwen_chat", fake_chat)
    planner = _planner(grounder_tmp)
    grounded = planner._ground_blueprint_architecture(
        _architecture(section_count=3), _evidence()
    )

    progress_writes = [
        value
        for path, value in writes
        if path.endswith("review_blueprint_grounding_progress.json")
    ]
    assert len(progress_writes) == 4  # initial + one per completed section
    assert [row["completed_count"] for row in progress_writes] == [0, 1, 2, 3]
    final_progress = progress_writes[-1]
    assert final_progress["remaining_count"] == 0
    assert final_progress["completed_section_ids"] == ["S01", "S02", "S03"]
    assert len(final_progress["sections"]) == 3
    assert final_progress["sections"][0]["attempts"][0]["usage"][
        "input_tokens"
    ] == 10

    progress_path = (
        grounder_tmp / "out" / "review_blueprint_grounding_progress.json"
    )
    assert progress_path.exists()
    on_disk = json.loads(progress_path.read_text(encoding="utf-8"))
    assert on_disk["completed_count"] == 3
    assert on_disk["created_at"]
    assert on_disk["updated_at"]

    checkpoint_path = (
        grounder_tmp / "out" / "review_blueprint.grounded_checkpoint.json"
    )
    assert checkpoint_path.exists()
    loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert planner._is_complete_grounded_architecture(loaded) is True
    assert len(loaded["sections"]) == 3
    assert loaded["_grounding_summary"]["grounded_checkpoint_written"] is True

    audit_path = (
        grounder_tmp / "out" / "review_blueprint_grounding_audit.json"
    )
    assert audit_path.exists()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert len(audit["sections"]) == 3
    assert all(row["attempts"] for row in audit["sections"])
