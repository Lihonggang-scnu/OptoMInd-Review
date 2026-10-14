"""One durable owner output-contract repair; all provider calls are fake."""
import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, _atomic_json,
    _classify_owner_response, _planner_call_settings,
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def packet():
    chapter = {"chapter_id": "CH1", "title": "Example", "scope": "conditions"}
    return {"chapter": chapter, "shared_outline": {"chapters": [chapter]},
            "chapter_plan": {"thesis": "Original argument", "reader_objective": "Explain conditions",
                             "units": [{"point": "Original unit", "paragraph_briefs": [
                                 {"point": "A bounded finding", "development": "Explain the supplied condition",
                                  "source_handles": ["P0001"]}]}]},
            "source_materials": [{"source_handle": "P0001", "paper_id": "paper-1",
                                  "study_summary_A": {"finding": "Example finding", "conditions": "Example condition"}}]}


def invalid_response():
    # Synthetic schema failure, containing no real E2E scientific answer.
    return {"status": "updated", "chapter_updates": [{"chapter_id": "CH1", "updated_plan": {
        "thesis": "Preserved model content", "substantive_threads": [{"point": "Example thread"}]}}]}


class Boundary:
    def __init__(self, *, base="invalid", repair="valid"):
        self.base, self.repair, self.calls = base, repair, []
        self.feedback = "Clarify the output contract"

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "whole_plan_improvement":
            return {"chapter_updates": [{"chapter_id": "CH1", "feedback": self.feedback}]}
        assert stage == "affected_chapter_revision"
        action = self.repair if "contract_repair" in payload else self.base
        if action == "raise":
            raise RuntimeError("Offline failure, charge status unknown")
        if action == "invalid":
            response = invalid_response()
        elif action == "unresolved":
            response = {"status": "unresolved", "reason": "Cannot preserve the complete plan"}
        elif action == "no_change":
            response = {"status": "no_change"}
        else:
            plan = copy.deepcopy(payload["chapter_plan"])
            plan["thesis"] = "Validated model output"
            response = {"status": "updated", "chapter_updates": [{"chapter_id": "CH1", "updated_plan": plan}]}
        return {"_planner_call": True, "response": response,
                "telemetry": {"call_id": payload["call_id"], "usage": {"input_tokens": 123},
                              "test_charge_reference": "repair-charge" if "contract_repair" in payload else "base-charge"}}

    def owners(self):
        return [payload for stage, payload in self.calls if stage == "affected_chapter_revision"]


def review(root, boundary, item=None, *, model="offline-model", resume=True):
    item = copy.deepcopy(item or packet())
    config = ProgressivePlannerConfig(topic_id="topic", pool_path=root / "unused-pool", plan_path=root / "unused-plan",
                                      output_dir=root, planning_revision_enabled=True, chapter_workers=1, planner_model=model)
    planner = ProgressiveReviewPlanner(config, planner=boundary)
    state_path = root / "RUN_STATE.json"
    state = read(state_path) if state_path.is_file() else {}
    output, improvement, _ = planner._post_case_review(
        root=root, topic="question", harmonized={"shared_outline": item["shared_outline"]},
        level1_outline={}, detail_records=[item], baseline_detail_records=[item], case_record={},
        level1_tool_result={}, level2_tool_result={}, chapter_tool_result={}, editorial_feedback={},
        resume=resume, state=state,
    )
    return output, improvement, planner


def latest(root):
    return root / "stages/affected_chapter_revision/CH1.json"


def seed_paid_partial(root):
    # Capture a real native input/contract, then install a legacy paid-return
    # record with the synthetic complete JSON. No repair has been attempted.
    boundary = Boundary(base="raise")
    review(root, boundary)
    saved = read(latest(root))
    response = invalid_response()
    status, plan, remap, errors = _classify_owner_response(
        saved["cache_inputs"]["chapter_plan"], response, saved["cache_inputs"]["source_materials"],
        expected_chapter_id="CH1")
    assert status == "unresolved" and errors
    saved.update(status="partial", owner_status=status, response=response, updated_plan=plan,
                 unit_id_remap=remap, structural_errors=errors,
                 telemetry={"call_id": "original-paid-call", "test_charge_reference": "original-charge"})
    saved.pop("error", None)
    saved.pop("error_detail", None)
    _atomic_json(latest(root), saved)
    return saved


def test_saved_paid_partial_buys_only_repair_and_preserves_inputs_and_charges(tmp_path):
    original = seed_paid_partial(tmp_path)
    boundary = Boundary()
    output, improvement, planner = review(tmp_path, boundary)
    assert len(boundary.owners()) == 1
    repair = boundary.owners()[0]
    assert {key: value for key, value in repair.items() if key not in {"call_id", "contract_repair"}} == {
        key: value for key, value in original["cache_inputs"].items() if key != "call_id"}
    assert repair["contract_repair"]["previous_response"] == original["response"]
    assert repair["contract_repair"]["diagnostics"] == original["structural_errors"]
    assert repair["unit_identity_contract"]["existing_unit_ids"] == []
    assert repair["call_id"] != original["cache_inputs"]["call_id"]
    assert _planner_call_settings(planner.planner, "affected_chapter_revision", config=planner.config)["model"] == "offline-model"
    saved = read(latest(tmp_path))
    first = read(Path(saved["contract_repair"]["first_attempt_path"]))
    assert first["response"] == original["response"]
    assert first["telemetry"] == original["telemetry"]
    assert saved["telemetry"]["test_charge_reference"] == "repair-charge"
    assert saved["owner_cache_contract"] == original["owner_cache_contract"]
    assert saved["cache_inputs"] == original["cache_inputs"]
    assert output[0]["chapter_plan"]["thesis"] == "Validated model output"
    assert not improvement["owner_revision_unresolved"]


def test_successful_owner_and_coordinator_cache_make_zero_calls(tmp_path):
    boundary = Boundary(base="valid")
    review(tmp_path, boundary)
    boundary.calls.clear()
    review(tmp_path, boundary)
    assert boundary.calls == []
    assert not (tmp_path / "stages/affected_chapter_revision/contract_repairs").exists()


@pytest.mark.parametrize("outcome", ["invalid", "raise", "unresolved"])
def test_failed_repair_once_per_contract_retains_valid_original_plan(tmp_path, outcome):
    original = seed_paid_partial(tmp_path)
    boundary = Boundary(repair=outcome)
    output, improvement, _ = review(tmp_path, boundary)
    saved = read(latest(tmp_path))
    assert saved["status"] == "partial"
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]
    assert improvement["owner_revision_unresolved"] == ["CH1"]
    assert len(boundary.owners()) == 1
    boundary.calls.clear()
    output, _, _ = review(tmp_path, boundary, resume=False)
    assert boundary.owners() == []
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]
    saved = read(latest(tmp_path))
    assert "contract_repair" in saved
    first = read(Path(saved["contract_repair"]["first_attempt_path"]))
    assert first["response"] == original["response"]


def test_in_flight_unknown_or_unreadable_claim_is_never_repaid(tmp_path):
    original = seed_paid_partial(tmp_path)
    claim = tmp_path / "stages/affected_chapter_revision/contract_repairs/CH1" / original["owner_cache_contract"] / "request.json"
    claim.parent.mkdir(parents=True)
    claim.write_text("{interrupted write", encoding="utf-8")
    boundary = Boundary()
    output, _, _ = review(tmp_path, boundary)
    assert boundary.owners() == []
    assert read(latest(tmp_path))["contract_repair"]["status"] == "in_flight_or_unknown"
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]


@pytest.mark.parametrize("changed", ["material", "task", "model"])
def test_changed_input_or_model_gets_new_contract_without_losing_first_attempt(tmp_path, changed):
    seed_paid_partial(tmp_path)
    boundary = Boundary(repair="invalid")
    review(tmp_path, boundary)
    before = read(latest(tmp_path))
    first_path = Path(before["contract_repair"]["first_attempt_path"])
    first_bytes = first_path.read_bytes()
    item, model = packet(), "offline-model"
    if changed == "material":
        item["source_materials"][0]["study_summary_A"]["conditions"] = "Changed condition"
    elif changed == "task":
        boundary.feedback = "Changed chapter task"
    else:
        model = "offline-model-v2"
    boundary.calls.clear()
    review(tmp_path, boundary, item, model=model, resume=False)
    assert len(boundary.owners()) == 2
    assert "contract_repair" not in boundary.owners()[0]
    assert "contract_repair" in boundary.owners()[1]
    assert read(latest(tmp_path))["owner_cache_contract"] != before["owner_cache_contract"]
    assert first_path.read_bytes() == first_bytes


def test_restart_after_repair_return_before_latest_save_uses_journal_without_call(tmp_path):
    seed_paid_partial(tmp_path)
    boundary = Boundary()
    review(tmp_path, boundary)
    latest(tmp_path).unlink()
    (tmp_path / "stages/affected_chapter_revision/successful/CH1.json").unlink()
    boundary.calls.clear()
    output, _, _ = review(tmp_path, boundary)
    assert boundary.calls == []
    assert output[0]["chapter_plan"]["thesis"] == "Validated model output"


def test_explicit_unresolved_answer_does_not_trigger_structure_repair(tmp_path):
    boundary = Boundary(base="unresolved")
    output, _, _ = review(tmp_path, boundary)
    assert len(boundary.owners()) == 1
    assert "contract_repair" not in read(latest(tmp_path))
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]


def test_model_may_choose_no_change_without_production_placeholder_plan(tmp_path):
    seed_paid_partial(tmp_path)
    boundary = Boundary(repair="no_change")
    output, improvement, _ = review(tmp_path, boundary)
    assert len(boundary.owners()) == 1
    assert read(latest(tmp_path))["owner_status"] == "no_change"
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]
    assert improvement["owner_revision_unresolved"] == []


def test_empty_returned_json_is_still_reused_as_a_paid_partial(tmp_path):
    original = seed_paid_partial(tmp_path)
    original["response"] = {}
    original["structural_errors"] = ["owner_response_missing_complete_plan"]
    _atomic_json(latest(tmp_path), original)
    boundary = Boundary()
    review(tmp_path, boundary)
    assert len(boundary.owners()) == 1
    assert boundary.owners()[0]["contract_repair"]["previous_response"] == {}


@pytest.mark.parametrize("missing", ["first_attempt", "unreadable_first_attempt", "result"])
def test_damaged_journal_fails_closed_without_another_paid_call(tmp_path, missing):
    seed_paid_partial(tmp_path)
    boundary = Boundary(repair="invalid")
    review(tmp_path, boundary)
    saved = read(latest(tmp_path))
    if missing == "first_attempt":
        Path(saved["contract_repair"]["first_attempt_path"]).unlink()
        latest(tmp_path).unlink()
    elif missing == "unreadable_first_attempt":
        Path(saved["contract_repair"]["first_attempt_path"]).write_text("{interrupted write", encoding="utf-8")
    else:
        Path(saved["contract_repair"]["result_path"]).write_text("[]", encoding="utf-8")
    boundary.calls.clear()
    output, improvement, _ = review(tmp_path, boundary)
    assert boundary.owners() == []
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]
    assert improvement["owner_revision_unresolved"] == ["CH1"]
