"""Offline cache proof for harmonized-scope tool-feedback retries."""
from __future__ import annotations

import copy

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning


class _CountingPlanner:
    model = "planner-a"
    chapter_model = "chapter-a"
    output_tokens = 18000
    thinking_budget = 8192

    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        return {"call_number": len(self.calls), "stage": stage}


def _planner(tmp_path, model):
    config = planning.ProgressivePlannerConfig(
        topic_id="harmonized-cache-control",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=tmp_path,
        planner_model="planner-a",
        chapter_model="chapter-a",
        planner_output_tokens=18000,
        thinking_budget=8192,
    )
    return planning.ProgressiveReviewPlanner(config, planner=model)


def _payload(*, empty_attempts, duplicate_source, conditions="25 C"):
    source = {"source_handle": "P001", "paper_id": "P001"}
    sources = [source, copy.deepcopy(source)] if duplicate_source else [source]
    attempts = [{
        "status": "partial",
        "gap_id": "G1",
        "gap_question": "stable harmonized gap",
        "source_units": [{"source_handle": "P001", "finding": "stable finding"}],
    }]
    attempts.extend({"status": "unjudgeable", "results": []} for _ in range(empty_attempts))
    return {
        "topic_id": "harmonized-cache-control",
        "research_question": "stable research question",
        "shared_level1_outline": {"chapters": [{"chapter_id": "CH1", "title": "One"}]},
        "chapter_proposals": [{"chapter_id": "CH1", "scope": "stable scope"}],
        "source_routing": [{"source_handle": "P001", "chapter_ids": ["CH1"]}],
        "level1_tool_results": {
            "phase": "level1",
            "status": "completed",
            "supplement_results": attempts,
            "retrieval_loop": {"needs": [{"need_id": "Ngap-abcdef0123456789", "still_missing": "stable gap"}]},
            "tool_materials_by_chapter": {
                "CH1": [{"need_id": "Ngap-abcdef0123456789", "conditions": conditions, "sources": sources}],
            },
        },
        "global_unique_deep_read_budget": 40,
    }


def test_harmonized_scope_reuses_only_empty_feedback_history(tmp_path):
    model = _CountingPlanner()
    planner = _planner(tmp_path, model)
    state = {}
    old = _payload(empty_attempts=1, duplicate_source=True)
    current = _payload(empty_attempts=6, duplicate_source=False)

    planner._model_stage("harmonized_scope", old, resume=True, state=state)
    planner._model_stage("harmonized_scope", current, resume=True, state=state)

    assert len(model.calls) == 1
    assert state["stage_inputs"]["harmonized_scope"] == old
    receipt = state["stage_reuse_receipts"]["harmonized_scope"]
    assert receipt["mode"] == "harmonized_scope_tool_feedback_compatible"
    assert receipt["feedback_key"] == "level1_tool_results"
    assert receipt["model_call"] is False
    assert receipt["projection_changes"]["current"]["ignored_material_free_attempts"] == 6

    changed = _payload(empty_attempts=6, duplicate_source=False, conditions="30 C")
    planner._model_stage("harmonized_scope", changed, resume=True, state=state)
    assert len(model.calls) == 2

    model.model = "planner-b"
    planner._model_stage("harmonized_scope", changed, resume=True, state=state)
    assert len(model.calls) == 3
