"""WO01: real chapter cache handoff with separate assigned/candidate inputs."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p


class Recorder:
    model = "offline-model"
    chapter_model = "offline-chapter"
    output_tokens = 16000
    thinking_budget = 2048

    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        assert stage == "chapter_details"
        self.calls.append(copy.deepcopy(payload))
        return {"chapter_plan": {"thesis": "Explain the measured mechanism", "units": [{
            "unit_id": payload["chapter"]["chapter_id"] + "_U01",
            "substantive_point": "Compare mechanism under stated conditions",
            "paragraph_briefs": [{"point": "Explain the condition", "development": "Compare the measurements"}],
        }]}}


def fixture(tmp_path, *, revision=True):
    rows = {}
    for i in (1, 2):
        card = tmp_path / f"card{i}.json"
        card.write_text(json.dumps({"general_understanding": {"finding": f"mechanism finding {i}"},
                                    "review_planning": {"planning_summary": f"mechanism comparison {i}"}}))
        rows[f"paper-{i}"] = {
            "_paper_id": f"paper-{i}", "_source_handle": f"P{i:04d}", "card_path": str(card),
            "title": f"mechanism study {i}",
            "planning_view": {"paper_identity": {"title": f"mechanism study {i}"}, "planning_summary": "mechanism comparison"},
            "_b_summary": {"planning_summary": "mechanism comparison"},
        }
    model = Recorder()
    engine = p.ProgressiveReviewPlanner(p.ProgressivePlannerConfig(
        topic_id="cache-fixture", plan_path=tmp_path / "PLAN.json", pool_path=tmp_path / "POOL.jsonl",
        output_dir=tmp_path / "run", chapter_workers=1, planning_revision_enabled=revision,
    ), planner=model)
    args = {"chapters": [{"chapter_id": "CH01", "title": "mechanism comparison", "source_ids": ["paper-1"]}],
            "shared_outline": [{"chapter_id": "CH01", "title": "mechanism comparison"}],
            "topic": "How does the mechanism change?", "candidates": rows, "candidate_pool": list(rows.values()),
            "level1_tools": {}, "level2_tools": {}, "state": {}}
    return engine, model, args


def saved(engine):
    return json.loads((engine.config.output_dir / "stages/chapters/CH01.json").read_text())


def test_identical_candidate_handoff_reuses_actual_chapter_packet(tmp_path):
    engine, model, args = fixture(tmp_path)
    first = engine._chapter_details(**args, resume=False)[0]
    assert len(model.calls[0]["source_materials"]) == 1
    assert len(first["source_materials"]) == 2
    assert first["source_materials"][1]["source_role"] == "candidate_navigation"
    second = engine._chapter_details(**args, resume=True)[0]
    assert len(model.calls) == 1
    assert second["chapter_plan"] == first["chapter_plan"] == saved(engine)["chapter_plan"]


@pytest.mark.parametrize("change", ["assigned", "candidate", "task", "scope", "model", "prompt"])
def test_changed_effective_chapter_input_is_not_reused(tmp_path, monkeypatch, change):
    engine, model, args = fixture(tmp_path)
    engine._chapter_details(**args, resume=False)
    if change in {"assigned", "candidate"}:
        row = args["candidates"]["paper-1" if change == "assigned" else "paper-2"]
        row["supplement_gap_material"] = {"usable_content": "Corrected comparison under a new condition"}
    elif change == "task":
        args["chapters"][0]["purpose"] = "Explain a different mechanism comparison"
    elif change == "scope":
        args["shared_outline"][0]["scope"] = "Include boundary conditions"
    elif change == "model":
        model.chapter_model = "offline-alternative"
    else:
        old = p._messages_for
        monkeypatch.setattr(p, "_messages_for", lambda stage, payload: old(stage, payload) + [{"role": "system", "content": "changed contract"}])
    engine._chapter_details(**args, resume=True)
    assert len(model.calls) == 2
    assert saved(engine)["chapter_plan"]["units"]


def test_normal_unsplit_path_and_legacy_provenance_fail_closed(tmp_path):
    engine, model, args = fixture(tmp_path, revision=False)
    first = engine._chapter_details(**args, resume=False)[0]
    engine._chapter_details(**args, resume=True)
    assert len(model.calls) == 1
    assert "chapter_details_batch" not in model.calls[0]
    path = engine.config.output_dir / "stages/chapters/CH01.json"
    legacy = saved(engine)
    legacy.pop("_chapter_details_input_contract", None)
    path.write_text(json.dumps(legacy))
    engine._chapter_details(**args, resume=True)
    assert len(model.calls) == 2
    assert saved(engine)["chapter_plan"] == first["chapter_plan"]


def test_assigned_metadata_move_reuses_plan_but_refreshes_handoff(tmp_path):
    engine, model, args = fixture(tmp_path)
    engine._chapter_details(**args, resume=False)
    row = args["candidates"]["paper-1"]
    old = Path(row["card_path"])
    new = tmp_path / "moved-card.json"
    new.write_text(old.read_text())
    row["card_path"] = str(new)
    engine._chapter_details(**args, resume=True)
    assert len(model.calls) == 1
    assert saved(engine)["source_materials"][0]["card_path"] == str(new)


def test_only_affected_chapter_task_is_regenerated(tmp_path):
    engine, model, args = fixture(tmp_path, revision=False)
    args["chapters"].append({"chapter_id": "CH02", "title": "Second comparison", "source_ids": ["paper-2"]})
    args["shared_outline"].append({"chapter_id": "CH02", "title": "Second comparison"})
    first = engine._chapter_details(**args, resume=False)
    assert len(model.calls) == 2
    args["chapters"][0]["purpose"] = "Explain an additional condition"
    second = engine._chapter_details(**args, resume=True)
    assert len(model.calls) == 3
    assert model.calls[-1]["chapter"]["chapter_id"] == "CH01"
    assert second[1]["chapter_plan"] == first[1]["chapter_plan"]
    persisted = json.loads((engine.config.output_dir / "stages/chapters/CH02.json").read_text())
    assert persisted["chapter_plan"] == first[1]["chapter_plan"]
