"""Focused resume proof for accumulated level1 tool feedback."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p


E_ROOT = Path(r"F:\OptoMind-Review-2\outputs\e2e_optics_20261012_80cny")
OLD_GATE = "9be2b6e6fb6926f0f6b061f16eb2e7b77bd12bb6473a140e0241593de891e113"
NEW_GATE = "d7e46af959c2217badce67f2677b38687622ae68e3b0b5f124a3b9bc1989035e"


def _request_payload(gate: str) -> dict:
    request = json.loads((E_ROOT / "supervision" / "calls" / gate / "REQUEST.json").read_text(encoding="utf-8"))
    for message in reversed(request.get("messages") or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        start = content.find("{")
        if start >= 0:
            payload, _ = json.JSONDecoder().raw_decode(content[start:])
            if isinstance(payload, dict) and "actual_tool_results" in payload:
                return payload
    raise AssertionError(f"level1 payload not found: {gate}")


class _CountingPlanner:
    model = "qwen3.5-plus"
    chapter_model = "qwen3.7-flash"
    output_tokens = 32768
    thinking_budget = 16384

    def __init__(self, *, model: str | None = None):
        if model is not None:
            self.model = model
        self.calls = 0

    def __call__(self, stage, payload):
        self.calls += 1
        return {"fresh_model_call": self.calls, "stage": stage}


def _evidence_or_skip() -> tuple[dict, dict, dict, Path]:
    output_dir = E_ROOT / "worktree" / "outputs" / "optics_e2e" / "07_progressive_review_plan_docling_20261013"
    state_path = output_dir / "RUN_STATE.json"
    stage_path = output_dir / "stages" / "level1_outline.json"
    old_request = E_ROOT / "supervision" / "calls" / OLD_GATE / "REQUEST.json"
    new_request = E_ROOT / "supervision" / "calls" / NEW_GATE / "REQUEST.json"
    if not all(path.is_file() for path in (state_path, stage_path, old_request, new_request)):
        pytest.skip("V18 level1 resume evidence is unavailable")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    return _request_payload(OLD_GATE), _request_payload(NEW_GATE), state, stage_path


def _planner(tmp_path: Path, model: _CountingPlanner) -> p.ProgressiveReviewPlanner:
    config = p.ProgressivePlannerConfig(
        topic_id="optics-e2e-20261012",
        pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json",
        output_dir=tmp_path,
        planner_model="qwen3.5-plus",
        chapter_model="qwen3.7-flash",
        thinking_budget=16384,
        planner_output_tokens=32768,
    )
    return p.ProgressiveReviewPlanner(config, planner=model)


def _seed_stage(tmp_path: Path, stage_path: Path, old_payload: dict, actual_state: dict) -> dict:
    (tmp_path / "stages").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stage_path, tmp_path / "stages" / "level1_outline.json")
    return {
        "stage_inputs": {"level1_outline": copy.deepcopy(old_payload)},
        "stage_cache_contracts": {
            "level1_outline": actual_state["stage_cache_contracts"]["level1_outline"]
        },
        "completed_stages": ["level1_outline"],
    }


def test_real_level1_tool_feedback_reuses_outline_and_rejects_science_change(tmp_path):
    old_payload, new_payload, actual_state, stage_path = _evidence_or_skip()
    model = _CountingPlanner()
    planner = _planner(tmp_path, model)
    state = _seed_stage(tmp_path, stage_path, old_payload, actual_state)

    assert planner._cache_contract("level1_outline", old_payload) == state["stage_cache_contracts"]["level1_outline"]
    saved_response = json.loads(stage_path.read_text(encoding="utf-8"))
    assert planner._model_stage("level1_outline", new_payload, resume=True, state=state) == saved_response
    assert model.calls == 0
    assert state["stage_inputs"]["level1_outline"] == old_payload
    assert state["stage_cache_contracts"]["level1_outline"] == actual_state["stage_cache_contracts"]["level1_outline"]
    receipt = state["stage_reuse_receipts"]["level1_outline"]
    assert receipt["model_call"] is False
    assert receipt["original_stage_input_preserved"] is True
    assert receipt["projection_changes"]["current"]["ignored_material_free_attempts"] == 9

    changed = copy.deepcopy(new_payload)
    changed["research_question"] += "; changed condition"
    model_changed = _CountingPlanner()
    planner_changed = _planner(tmp_path / "science-change", model_changed)
    state_changed = _seed_stage(tmp_path / "science-change", stage_path, old_payload, actual_state)
    planner_changed._model_stage("level1_outline", changed, resume=True, state=state_changed)
    assert model_changed.calls == 1

    evidence_changed = copy.deepcopy(new_payload)
    evidence_changed["actual_tool_results"]["supplement_results"][7]["results"][0]["evidence"] = ["new evidence"]
    model_evidence = _CountingPlanner()
    planner_evidence = _planner(tmp_path / "evidence-change", model_evidence)
    state_evidence = _seed_stage(tmp_path / "evidence-change", stage_path, old_payload, actual_state)
    planner_evidence._model_stage("level1_outline", evidence_changed, resume=True, state=state_evidence)
    assert model_evidence.calls == 1


def test_real_level1_tool_feedback_rejects_runtime_change(tmp_path):
    old_payload, new_payload, actual_state, stage_path = _evidence_or_skip()
    model = _CountingPlanner(model="qwen3.5-plus-runtime-change")
    planner = _planner(tmp_path, model)
    state = _seed_stage(tmp_path, stage_path, old_payload, actual_state)
    planner._model_stage("level1_outline", new_payload, resume=True, state=state)
    assert model.calls == 1
    assert "stage_reuse_receipts" not in state


def test_level1_tool_feedback_reuse_is_portable_and_science_bound(tmp_path):
    """Exercise the seam without depending on the archived Windows run."""
    def payload(*, suffix: str, duplicate_source: bool, extra_empty_attempt: bool = False) -> dict:
        source = {"source_handle": "P001", "paper_id": "P001"}
        sources = [source, copy.deepcopy(source)] if duplicate_source else [source]
        attempts = [
            {
                "status": "partial",
                "gap_id": "G1",
                "gap_question": "stable gap",
                "source_units": [{"source_handle": "P001", "finding": "stable finding"}],
            },
            {"status": "failed", "results": [{"status": "failed", "error": "timeout"}]},
        ]
        if extra_empty_attempt:
            attempts.append({"status": "unjudgeable", "results": []})
        return {
            "topic_id": "portable-level1",
            "research_question": "stable question",
            "provisional_scope": {"central_question": "stable scope"},
            "original_plan": {"chapters": ["CH1"]},
            "material_theme_inventory": {"P001": "stable material"},
            "full_b_pool_was_semantically_screened": True,
            "unavailable_and_failed_tools_are_scope_feedback": True,
            "actual_tool_results": {
                "phase": "level1",
                "status": "completed",
                "consumed_paper_ids": ["P001"],
                "directed_results": [],
                "supplement_results": attempts,
                "retrieval_loop": {
                    "needs": [{"need_id": f"Ngap-{suffix}", "still_missing": "stable gap"}]
                },
                "tool_materials_by_chapter": {
                    "CH1": [{"need_id": f"Ngap-{suffix}", "conditions": "25 C", "sources": sources}]
                },
            },
        }

    old = payload(suffix="a" * 16, duplicate_source=True)
    new = payload(suffix="b" * 16, duplicate_source=False, extra_empty_attempt=True)
    model = _CountingPlanner()
    planner = _planner(tmp_path / "portable", model)
    state: dict = {}
    planner._model_stage("level1_outline", old, resume=True, state=state)
    assert model.calls == 1
    planner._model_stage("level1_outline", new, resume=True, state=state)
    assert model.calls == 1
    assert state["stage_reuse_receipts"]["level1_outline"]["model_call"] is False

    condition_changed = copy.deepcopy(new)
    condition_changed["actual_tool_results"]["tool_materials_by_chapter"]["CH1"][0]["conditions"] = "30 C"
    planner._model_stage("level1_outline", condition_changed, resume=True, state=state)
    assert model.calls == 2

    evidence_changed = copy.deepcopy(condition_changed)
    evidence_changed["actual_tool_results"]["supplement_results"][0]["source_units"][0]["evidence"] = ["measured"]
    planner._model_stage("level1_outline", evidence_changed, resume=True, state=state)
    assert model.calls == 3

    fulfilled_changed = copy.deepcopy(evidence_changed)
    fulfilled_changed["actual_tool_results"]["supplement_results"][0]["substantive_gap_status"] = "fulfilled"
    planner._model_stage("level1_outline", fulfilled_changed, resume=True, state=state)
    assert model.calls == 4
