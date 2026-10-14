"""One durable owner output-contract repair; all provider calls are fake."""
import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, _atomic_json,
    _classify_owner_response, _planner_call_settings, _messages_for, _owner_contract_repair_payload,
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
    assert {key: value for key, value in repair.items() if key not in {"call_id", "contract_repair", "chapter", "chapter_background"}} == {
        key: value for key, value in original["cache_inputs"].items() if key not in {"call_id", "chapter"}}
    assert repair["chapter_background"] == original["cache_inputs"]["chapter"]
    assert repair["contract_repair"]["rejected_responses"][0]["response"] == original["response"]
    assert repair["contract_repair"]["diagnostics"][:len(original["structural_errors"])] == original["structural_errors"]
    assert repair["contract_repair"]["diagnostics"][-1].startswith("updated_plan.units_missing:")
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
    assert boundary.owners()[0]["contract_repair"]["rejected_responses"][0]["response"] == {}


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


def test_v2_messages_choose_current_units_and_keep_full_background_without_guessing_ids():
    item = packet()
    item["chapter"]["substantive_threads"] = [{"thread_name": f"Background {i}"} for i in range(5)]
    item["chapter_plan"]["units"] *= 4
    payload = {**item, "chapter_id": "CH1", "planning_revision_mode": True}
    repair = _owner_contract_repair_payload(payload, invalid_response(), ["missing units"])
    assert repair["chapter_background"] == item["chapter"]
    assert repair["chapter_plan"] == item["chapter_plan"]
    assert repair["source_materials"] == item["source_materials"]
    assert repair["chapter"] == {"chapter_id": "CH1"}
    assert repair["contract_repair"]["current_unit_count"] == 4
    schema = repair["contract_repair"]["output_schema"]["chapter_updates"][0]["updated_plan"]
    assert schema["units"][0]["paragraph_briefs"][0]["source_handles"] == ["string"]
    assert "substantive_threads" not in schema and "unit_id" not in schema["units"][0]
    messages = _messages_for("affected_chapter_revision", repair)
    for text in (messages[0]["content"], messages[1]["content"].rsplit("【本轮交付】", 1)[1]):
        assert "chapter_plan 是唯一可编辑" in text
        assert "原单元没有ID时不要求补ID" in text
        assert "当前计划有 4 个单元" in text
    assert "Original argument" not in json.dumps(schema)


def seed_legacy_repair(root, original, response=None):
    legacy = root / "stages/affected_chapter_revision/contract_repairs/CH1" / original["owner_cache_contract"]
    _atomic_json(legacy / "first_attempt.json", original)
    _atomic_json(legacy / "request.json", {"version": 1, "payload": original["cache_inputs"]})
    _atomic_json(legacy / "result.json", {"response": response or invalid_response(),
                                          "telemetry": {"test_charge_reference": "legacy-v1-charge"}})
    return legacy


def test_failed_v1_gets_one_new_v2_contract_without_mutating_old_paid_records(tmp_path):
    original = seed_paid_partial(tmp_path)
    legacy = seed_legacy_repair(tmp_path, original)
    before = {p.name: p.read_bytes() for p in legacy.glob("*.json")}
    boundary = Boundary()
    review(tmp_path, boundary)
    assert len(boundary.owners()) == 1
    repair = boundary.owners()[0]
    assert repair["contract_repair"]["version"] == 2
    assert [r["origin"] for r in repair["contract_repair"]["rejected_responses"]] == ["original_owner", "contract_repair_v1"]
    saved = read(latest(tmp_path))
    assert saved["contract_repair"]["repair_cache_contract"]
    assert Path(saved["contract_repair"]["request_path"]).parent.parent.name == "v2"
    assert {p.name: p.read_bytes() for p in legacy.glob("*.json")} == before
    boundary.calls.clear()
    review(tmp_path, boundary)
    assert boundary.calls == []


def test_v2_accepts_new_ids_without_mapping_to_nonexistent_old_ids(tmp_path):
    seed_paid_partial(tmp_path)
    class InventedIdentity(Boundary):
        def __call__(self, stage, payload):
            result = super().__call__(stage, payload)
            if stage == "affected_chapter_revision":
                result["response"]["chapter_updates"][0]["updated_plan"]["units"][0]["unit_id"] = "invented-old-ID"
            return result
    boundary = InventedIdentity()
    output, improvement, _ = review(tmp_path, boundary)
    assert read(latest(tmp_path))["status"] == "complete"
    assert output[0]["chapter_plan"]["units"][0]["unit_id"] == "invented-old-ID"
    boundary.calls.clear()
    review(tmp_path, boundary)
    assert boundary.calls == []
    assert improvement["owner_revision_unresolved"] == []


@pytest.mark.parametrize("invalid", ["empty_units", "type_schema", "unknown_old_id"])
def test_v2_rejects_contentless_schema_and_fabricated_old_mapping(tmp_path, invalid):
    seed_paid_partial(tmp_path)
    class InvalidRepair(Boundary):
        def __call__(self, stage, payload):
            result = super().__call__(stage, payload)
            if stage == "affected_chapter_revision":
                response = result["response"]
                plan = response["chapter_updates"][0]["updated_plan"]
                if invalid == "empty_units":
                    plan["units"] = [{} for unit in payload["chapter_plan"]["units"]]
                elif invalid == "type_schema":
                    response["chapter_updates"][0]["updated_plan"] = copy.deepcopy(payload["contract_repair"]["output_schema"]["chapter_updates"][0]["updated_plan"])
                else:
                    plan["units"][0]["unit_id"] = "new-ID"
                    response["unit_id_remap"] = {"new-ID": ["nonexistent-old-ID"]}
            return result
    boundary = InvalidRepair()
    output, improvement, _ = review(tmp_path, boundary)
    expected = {"empty_units": "missing_substantive_units", "type_schema": "returned_type_schema",
                "unknown_old_id": "unknown_old_unit_identity"}[invalid]
    assert read(latest(tmp_path))["structural_errors"] == ["owner_contract_repair:" + expected]
    assert output[0]["chapter_plan"] == packet()["chapter_plan"]
    assert improvement["owner_revision_unresolved"] == ["CH1"]
    boundary.calls.clear()
    review(tmp_path, boundary)
    assert boundary.calls == []


def test_v2_after_v1_failure_reuses_four_other_owners_and_coordinator(tmp_path):
    items = []
    for number in range(1, 6):
        item = packet()
        item["chapter"]["chapter_id"] = f"CH{number}"
        items.append(item)
    shared = {"chapters": [item["chapter"] for item in items]}
    class FiveOwners(Boundary):
        seed = True
        def __call__(self, stage, payload):
            if stage == "whole_plan_improvement":
                self.calls.append((stage, copy.deepcopy(payload)))
                return {"chapter_updates": [{"chapter_id": item["chapter"]["chapter_id"], "feedback": "Clarify"} for item in items]}
            self.calls.append((stage, copy.deepcopy(payload)))
            if payload["chapter_id"] == "CH3" and self.seed:
                raise RuntimeError("Seed paid partial separately")
            return {"status": "updated", "chapter_updates": [{"chapter_id": payload["chapter_id"],
                                                                "updated_plan": copy.deepcopy(payload["chapter_plan"])}]}
    boundary = FiveOwners()
    config = ProgressivePlannerConfig(topic_id="topic", pool_path=tmp_path / "unused", plan_path=tmp_path / "unused",
                                      output_dir=tmp_path, planning_revision_enabled=True, chapter_workers=1)
    def run():
        planner = ProgressiveReviewPlanner(config, planner=boundary)
        state_path = tmp_path / "RUN_STATE.json"
        return planner._post_case_review(root=tmp_path, topic="question", harmonized={"shared_outline": shared},
            level1_outline={}, detail_records=copy.deepcopy(items), baseline_detail_records=copy.deepcopy(items),
            case_record={}, level1_tool_result={}, level2_tool_result={}, chapter_tool_result={}, editorial_feedback={},
            resume=True, state=read(state_path) if state_path.is_file() else {})
    run()
    path = tmp_path / "stages/affected_chapter_revision/CH3.json"
    original = read(path)
    response = invalid_response()
    response["chapter_updates"][0]["chapter_id"] = "CH3"
    original.update(status="partial", response=response, structural_errors=["structural_unit_change_requires_explicit_unit_ids_and_remap"])
    _atomic_json(path, original)
    legacy = path.parent / "contract_repairs/CH3" / original["owner_cache_contract"]
    _atomic_json(legacy / "first_attempt.json", original)
    _atomic_json(legacy / "request.json", {"version": 1})
    _atomic_json(legacy / "result.json", {"response": response, "telemetry": {"test_charge_reference": "v1"}})
    original_successes = {cid: (path.parent / f"{cid}.json").read_bytes() for cid in ("CH1", "CH2", "CH4", "CH5")}
    boundary.seed = False
    boundary.calls.clear()
    run()
    assert len(boundary.calls) == 1
    assert boundary.calls[0][0] == "affected_chapter_revision"
    assert boundary.calls[0][1]["chapter_id"] == "CH3"
    assert boundary.calls[0][1]["contract_repair"]["version"] == 2
    assert {cid: (path.parent / f"{cid}.json").read_bytes() for cid in original_successes} == original_successes
    boundary.calls.clear()
    run()
    assert boundary.calls == []
