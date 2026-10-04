"""WO05 bounded production arrangement/writer identity and argument seams."""
import copy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing


def _packet():
    return {
        "chapter_id": "CH01", "research_question": "Which relationship is supported?",
        "shared_scope": {"statement": "SCOPE_ONLY"},
        "review_argument": "CALIBRATED_ARGUMENT",
        "review_argument_status": "calibrated",
        "review_argument_source": "whole_plan_improvement",
        "chapter_plan": {"units": [
            {"unit_id": "UNIT_A", "substantive_point": "A claim", "paragraph_briefs": [
                {"paragraph_id": "BRIEF_A1", "point": "A finding", "development": "A setting and limit",
                 "source_handles": ["P0001"]},
                {"paragraph_id": "BRIEF_A2", "point": "A boundary", "development": "Independent boundary",
                 "source_handles": ["P0002"]}]},
            {"unit_id": "UNIT_B", "substantive_point": "B claim", "paragraph_briefs": [
                {"paragraph_id": "BRIEF_B", "point": "B finding", "development": "B setting and limit",
                 "source_handles": ["P0002"]}]}]},
        "source_materials": [
            {"source_handle": h, "paper_id": f"paper-{h}", "study_summary_A": {"key_findings": f"Material {h}"}}
            for h in ["P0001", "P0002"]],
    }


def _view(tmp_path, packet=None):
    packet = packet or _packet()
    path = tmp_path / "PACKET.json"
    path.write_text(json.dumps(packet))
    return arranging.build_chapter_view(path, id_map_path=tmp_path / "ID_MAP.json")


def _payload(view):
    return {"chapter_id": view.chapter_id, "units": [
        {"unit_id": unit.unit_id, "focus": unit.substantive_point, "paragraph_tasks": [
            {"paragraph_id": brief.paragraph_id, "point": brief.point, "development": brief.development,
             "source_briefs": [brief.paragraph_id],
             "source_uses": [{"source_handle": h} for h in brief.source_handles]}
            for brief in unit.paragraph_briefs]}
        for unit in view.units]}


def _writer(tmp_path, view, result, unit_id):
    result = {**result, "source_catalog": arranging.build_source_catalog(view, result)}
    path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    path.write_text(json.dumps(result))
    arranging.write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    wview = writing.build_unit_view(path, unit_id)
    messages = writing.unit_messages(wview, planning_revision=True)
    writing.write_unit_input(wview, messages, tmp_path / "writer", estimate={}, language="en")
    saved = json.loads((tmp_path / "writer" / "UNIT_MESSAGES.json").read_text())
    return wview, json.loads(saved[-1]["content"])


def test_explicit_owner_identity_survives_reorder_and_old_positional_map(tmp_path):
    (tmp_path / "ID_MAP.json").write_text(json.dumps({"units": {"CH01#u1": "CH01_U01"},
                                                    "paragraphs": {"CH01_U01#p1": "CH01_U01_P01"}}))
    packet = _packet()
    before = _view(tmp_path, packet)
    packet["chapter_plan"]["units"].reverse()
    packet["chapter_plan"]["units"][1]["paragraph_briefs"].reverse()
    after = _view(tmp_path, packet)
    assert [u.unit_id for u in before.units] == ["UNIT_A", "UNIT_B"]
    assert [u.unit_id for u in after.units] == ["UNIT_B", "UNIT_A"]
    assert [p.paragraph_id for p in after.units[1].paragraph_briefs] == ["BRIEF_A2", "BRIEF_A1"]
    result = arranging.validate_arrangement(_payload(after), after, planning_revision=True)
    assert result["validation"]["ok"], result["validation"]
    _, message = _writer(tmp_path, after, result, "UNIT_A")
    assert message["paragraph_tasks"][0]["source_brief_details"][0]["development"] == "Independent boundary"
    assert message["paragraph_tasks"][1]["source_brief_details"][0]["source_handles"] == ["P0001"]


@pytest.mark.parametrize("mode", [False, True])
def test_split_merge_reference_ids_are_stable_under_task_reorder(tmp_path, mode):
    view = _view(tmp_path)
    payload = _payload(view)
    refs = [p.paragraph_id for p in view.units[0].paragraph_briefs]
    payload["units"][0]["paragraph_tasks"] = [
        {"source_briefs": refs, "portion": "settings", "point": "Editor settings", "development": "Editor prose"},
        {"source_briefs": refs, "portion": "limits", "point": "Editor limits", "development": "Editor limits prose"}]
    before = arranging.validate_arrangement(payload, view, planning_revision=mode)
    payload["units"][0]["paragraph_tasks"].reverse()
    after = arranging.validate_arrangement(payload, view, planning_revision=mode)
    assert before["validation"]["ok"], before["validation"]
    assert after["validation"]["ok"], after["validation"]
    first = {t["portion"]: t["paragraph_id"] for t in before["units"][0]["paragraph_tasks"]}
    last = {t["portion"]: t["paragraph_id"] for t in after["units"][0]["paragraph_tasks"]}
    assert first == last
    assert len(set(first.values())) == 2
    assert not set(first.values()).intersection(refs)
    _, message = _writer(tmp_path, view, after, view.units[0].unit_id)
    task = message["paragraph_tasks"][0]
    assert task["source_briefs"] == refs
    assert [b["development"] for b in task["source_brief_details"]] == ["A setting and limit", "Independent boundary"]
    assert [use["source_handle"] for use in task["source_uses"]] == ["P0001", "P0002"]
    if not mode:
        assert task["point"] == "Editor limits" and task["development"] == "Editor limits prose"


@pytest.mark.parametrize("kind", ["unit", "paragraph"])
def test_duplicate_owner_ids_rejected_instead_of_silent_overwrite(tmp_path, kind):
    packet = _packet()
    if kind == "unit":
        packet["chapter_plan"]["units"][1]["unit_id"] = "UNIT_A"
    else:
        packet["chapter_plan"]["units"][1]["paragraph_briefs"][0]["paragraph_id"] = "BRIEF_A1"
    with pytest.raises(arranging.ChapterArrangementError, match="duplicated"):
        _view(tmp_path, packet)


@pytest.mark.parametrize("mode", [False, True])
def test_duplicate_derived_tasks_are_ambiguous_without_portion_or_explicit_ids(tmp_path, mode):
    view = _view(tmp_path)
    payload = _payload(view)
    row = {"source_briefs": [p.paragraph_id for p in view.units[0].paragraph_briefs]}
    payload["units"][0]["paragraph_tasks"] = [row, copy.deepcopy(row)]
    result = arranging.validate_arrangement(payload, view, planning_revision=mode)
    assert any("paragraph_id_duplicated:" in e for e in result["validation"]["errors"])


def test_opaque_paragraph_id_ownership_uses_relationship_not_prefix(tmp_path):
    view = _view(tmp_path)
    payload = _payload(view)
    correct = arranging.validate_arrangement(payload, view, planning_revision=True)
    assert correct["validation"]["ok"], correct["validation"]
    payload["units"][0]["paragraph_tasks"][0]["paragraph_id"] = view.units[1].paragraph_briefs[0].paragraph_id
    wrong = arranging.validate_arrangement(payload, view, planning_revision=True)
    assert any("paragraph_id_unit_mismatch:" in e for e in wrong["validation"]["errors"])


def test_legacy_missing_ids_remain_compatible_and_do_not_collide_with_explicit(tmp_path):
    packet = _packet()
    for unit in packet["chapter_plan"]["units"]:
        unit.pop("unit_id")
        for row in unit["paragraph_briefs"]:
            row.pop("paragraph_id")
    legacy = _view(tmp_path, packet)
    assert [u.unit_id for u in legacy.units] == ["CH01_U01", "CH01_U02"]
    packet["chapter_plan"]["units"][1]["unit_id"] = "CH01_U01"
    mixed = _view(tmp_path, packet)
    assert mixed.units[1].unit_id == "CH01_U01"
    assert mixed.units[0].unit_id != "CH01_U01"


def test_writer_refuses_ambiguous_duplicate_unit_selection():
    with pytest.raises(writing.UnitWritingError, match="duplicated"):
        writing.select_unit({"units": [{"unit_id": "U"}, {"unit_id": "U"}]}, "U")


def test_calibrated_argument_and_scope_reach_persisted_production_messages(tmp_path):
    view = _view(tmp_path)
    arranging_message = json.loads(arranging.arrangement_messages(view.arrangement_payload())[-1]["content"])
    assert arranging_message["shared_scope"] == {"statement": "SCOPE_ONLY"}
    assert arranging_message["review_argument"] == "CALIBRATED_ARGUMENT"
    result = arranging.validate_arrangement(_payload(view), view, planning_revision=True)
    _, writer = _writer(tmp_path, view, result, view.units[0].unit_id)
    assert writer["chapter_frame"]["shared_scope"] == {"statement": "SCOPE_ONLY"}
    assert writer["chapter_frame"]["review_argument_status"] == "calibrated"
    assert writer["chapter_frame"]["review_argument_source"] == "whole_plan_improvement"
    assert writer["chapter_frame"]["review_argument"] == "CALIBRATED_ARGUMENT"


def test_writer_packet_fallback_uses_plan_argument_not_scope(tmp_path):
    (tmp_path / "writer_packets").mkdir()
    packet = _packet()
    (tmp_path / "writer_packets" / "CH01.json").write_text(json.dumps(packet))
    (tmp_path / "DETAILED_REVIEW_PLAN.json").write_text(json.dumps({
        "shared_scope": {"statement": "SCOPE_ONLY"}, "review_argument": "NEW_CALIBRATED_ARGUMENT",
        "review_argument_status": "calibrated", "review_argument_source": "whole_plan_improvement"}))
    view = writing._view_from_packet(tmp_path, "CH01")
    assert view["review_argument"] == "NEW_CALIBRATED_ARGUMENT"
    assert view["shared_scope"] == {"statement": "SCOPE_ONLY"}


def test_missing_argument_is_explicitly_missing_not_scope(tmp_path):
    packet = _packet()
    for key in ("review_argument", "review_argument_status", "review_argument_source"):
        packet.pop(key)
    view = _view(tmp_path, packet)
    assert view.to_dict()["review_argument"] == ""
    assert view.to_dict()["review_argument_status"] == "missing"
    assert view.to_dict()["shared_scope"] == {"statement": "SCOPE_ONLY"}


@pytest.mark.parametrize("mode", [False, True])
def test_known_output_id_cannot_be_rebound_to_other_briefs(tmp_path, mode):
    view = _view(tmp_path)
    payload = _payload(view)
    tasks = payload["units"][0]["paragraph_tasks"]
    tasks[0]["source_briefs"], tasks[1]["source_briefs"] = tasks[1]["source_briefs"], tasks[0]["source_briefs"]
    result = arranging.validate_arrangement(payload, view, planning_revision=mode)
    assert any("paragraph_id_brief_conflict:" in e for e in result["validation"]["errors"])


def test_merge_requires_new_identity_instead_of_reusing_one_old_brief(tmp_path):
    view = _view(tmp_path)
    payload = _payload(view)
    payload["units"][0]["paragraph_tasks"][0]["source_briefs"].append(view.units[0].paragraph_briefs[1].paragraph_id)
    result = arranging.validate_arrangement(payload, view, planning_revision=True)
    assert any("paragraph_id_brief_conflict:" in e for e in result["validation"]["errors"])


def test_global_argument_is_not_cut_by_source_material_compaction(tmp_path):
    packet = _packet()
    packet["review_argument"] = "Common context. " * 30 + "CALIBRATED_DIFFERENCE_A"
    first = _view(tmp_path, packet).arrangement_payload(max_source_chars=240)
    packet["review_argument"] = "Common context. " * 30 + "CALIBRATED_DIFFERENCE_B"
    second = _view(tmp_path, packet).arrangement_payload(max_source_chars=240)
    assert first["review_argument"].endswith("CALIBRATED_DIFFERENCE_A")
    assert second["review_argument"].endswith("CALIBRATED_DIFFERENCE_B")


def test_owner_split_and_merge_new_unit_ids_survive_view_and_writer(tmp_path):
    packet = _packet()
    old = packet["chapter_plan"]["units"]
    packet["chapter_plan"]["units"] = [
        {**old[0], "unit_id": "SPLIT_A", "paragraph_briefs": [old[0]["paragraph_briefs"][0]]},
        {**old[0], "unit_id": "MERGED_AB", "paragraph_briefs": [old[0]["paragraph_briefs"][1], old[1]["paragraph_briefs"][0]]}]
    packet["unit_id_remap"] = {"SPLIT_A": ["UNIT_A"], "MERGED_AB": ["UNIT_A", "UNIT_B"]}
    view = _view(tmp_path, packet)
    assert [u.unit_id for u in view.units] == ["SPLIT_A", "MERGED_AB"]
    assert view.to_dict()["unit_id_remap"] == packet["unit_id_remap"]
    result = arranging.validate_arrangement(_payload(view), view, planning_revision=True)
    assert result["validation"]["ok"], result["validation"]
    assert result["unit_id_remap"] == packet["unit_id_remap"]
    _, payload = _writer(tmp_path, view, result, "MERGED_AB")
    assert [task["source_briefs"] for task in payload["paragraph_tasks"]] == [["BRIEF_A2"], ["BRIEF_B"]]
    assert [task["source_brief_details"][0]["development"] for task in payload["paragraph_tasks"]] == [
        "Independent boundary", "B setting and limit"]


@pytest.mark.parametrize("problem", ["duplicate", "unknown_ref", "foreign_ref", "swapped_ref"])
def test_writer_checks_persisted_task_identity_before_model_input(tmp_path, problem):
    view = _view(tmp_path)
    result = arranging.validate_arrangement(_payload(view), view, planning_revision=True)
    tasks = result["units"][0]["paragraph_tasks"]
    if problem == "duplicate":
        tasks[1]["paragraph_id"] = tasks[0]["paragraph_id"]
    elif problem == "swapped_ref":
        tasks[0]["source_briefs"] = [view.units[0].paragraph_briefs[1].paragraph_id]
    else:
        tasks[0]["source_briefs"] = ["UNKNOWN" if problem == "unknown_ref" else view.units[1].paragraph_briefs[0].paragraph_id]
    with pytest.raises(writing.UnitWritingError, match="paragraph_id_|brief_reference_"):
        _writer(tmp_path, view, result, view.units[0].unit_id)


@pytest.mark.parametrize("refs", [None, []])
def test_split_portion_cannot_keep_old_id_without_explicit_refs(tmp_path, refs):
    view = _view(tmp_path)
    response = _payload(view)
    task = response["units"][0]["paragraph_tasks"][0]
    task["portion"] = "settings only"
    if refs is None:
        task.pop("source_briefs")
    else:
        task["source_briefs"] = refs
    result = arranging.validate_arrangement(response, view, planning_revision=True)
    assert any("paragraph_id_brief_conflict:" in e for e in result["validation"]["errors"])


def test_actual_arrangement_message_exposes_identity_contract(tmp_path):
    view = _view(tmp_path)
    message = json.loads(arranging.arrangement_messages(view.arrangement_payload())[-1]["content"])
    contract = message["task_identity_contract"]
    assert contract["split_or_merge_requires_new_paragraph_id"] is True
    assert contract["derived_id_inputs"] == ["source_briefs", "portion"]
    assert contract["unchanged_task_may_retain_original_id"] is True


def test_global_scope_exclusions_are_not_cut_by_source_material_budget(tmp_path):
    packet = _packet()
    packet["shared_scope"] = {"statement": "Context " * 80, "exclusions": ["Only adult settings " * 20 + "EXCLUDE_CHILDREN"]}
    payload = _view(tmp_path, packet).arrangement_payload(max_source_chars=20)
    assert payload["shared_scope"] == packet["shared_scope"]
