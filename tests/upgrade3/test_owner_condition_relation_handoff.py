"""Offline production messages retain the owner's task conditions and relations."""
from copy import deepcopy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing


def _packet(conditions, relations, *, legacy=False):
    briefs = [
        {"paragraph_id": "B1", "point": "A measurement", "development": "Describe the observed response.",
         "source_handles": ["P0001"], "finding_conditions": deepcopy(conditions)},
        {"paragraph_id": "B2", "point": "An independent boundary", "development": "Explain the observation window.",
         "source_handles": ["P0002"], "finding_conditions": {"window": "three sessions", "replicates": 9}},
    ]
    return {
        "chapter_id": "CH01", "chapter": {"chapter_id": "CH01", "title": "Controlled fixture"},
        "chapter_plan": {"units": [{
            "unit_id": "U1", "substantive_point": "Measure with explicit conditions",
            "ordered_development" if legacy else "paragraph_briefs": briefs,
            "argument_relations": deepcopy(relations),
            "synthesis_and_conflicts": "Keep this legacy synthesis separate.",
        }]},
        "source_materials": [
            {"source_handle": handle, "paper_id": handle, "study_summary_A": {"finding": "Fixture material"}}
            for handle in ("P0001", "P0002")],
    }


def _view(tmp_path, packet):
    path = tmp_path / "PACKET.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return arranging.build_chapter_view(path, id_map_path=tmp_path / "ID_MAP.json")


def _arranged(view, *, merged=False, refs=True):
    tasks = [{"paragraph_id": b.paragraph_id, "point": "Editor placement", "development": "Editor organization",
              "source_uses": [{"source_handle": h} for h in b.source_handles],
              **({"source_briefs": [b.paragraph_id]} if refs else {})}
             for b in view.units[0].paragraph_briefs]
    if merged:
        tasks = [{"source_briefs": ["B2", "B1"], "portion": "joint explanation",
                  "point": "Editor merged placement", "development": "Editor organization"}]
    return {"chapter_id": view.chapter_id, "units": [{"unit_id": "U1", "paragraph_tasks": tasks}]}


def _writer(tmp_path, view, arranged):
    exported = {**arranged, "source_catalog": arranging.build_source_catalog(view, arranged)}
    path = tmp_path / "CHAPTER_ARRANGEMENT.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    arranging.write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    return writing.build_unit_view(path, "U1")


class Capture:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), deepcopy(kwargs)))
        return {"content": json.dumps(self.response), "complete": True, "finish_reason": "stop"}


@pytest.mark.parametrize("conditions,relations", [
    ("Only the first three sessions; " + "unabridged " * 100, "B1 provides context; B2 defines its boundary."),
    (["fixed setting", {"replicates": 9, "period": "short"}], [{"from": "B1", "to": "B2", "relation": "qualifies"}]),
    ({"settings": {"mode": "low", "replicates": 9}, "excluded": ["long-term inference"]},
     {"parallel": ["B1", "B2"], "reason": "Different observation windows"}),
    ([], {}),
    (None, None),
])
@pytest.mark.parametrize("mode", [False, True])
def test_conditions_and_relations_reach_actual_arrangement_and_writer_calls(tmp_path, conditions, relations, mode):
    packet = _packet(conditions, relations)
    view = _view(tmp_path, packet)
    editor = Capture(_arranged(view))
    result = arranging.run_arrangement(
        view, client=editor, model="offline", prompt="Controlled fixture only.",
        view_payload=view.arrangement_payload(max_source_chars=8), planning_revision=mode)
    editor_payload = json.loads(editor.calls[0][0][-1]["content"])
    unit = editor_payload["units"][0]
    assert unit["existing_paragraph_tasks"][0]["finding_conditions"] == conditions
    assert unit["argument_relations"] == relations
    assert "argument_relations" in unit
    saved = view.to_dict()["units"][0]
    assert saved["synthesis"] == "Keep this legacy synthesis separate."
    assert saved["argument_relations"] == relations
    assert result["validation"]["ok"]
    writer_view = _writer(tmp_path, view, result)
    writer = Capture({"body_markdown": "Fixture response [P0001]."})
    writing.run_unit_writing(writer_view, client=writer, prompt="Controlled fixture only.", planning_revision=mode)
    writer_payload = json.loads(writer.calls[0][0][-1]["content"])
    assert writer_payload["paragraph_tasks"][0]["source_brief_details"][0]["finding_conditions"] == conditions
    assert writer_payload["owner_unit_context"]["argument_relations"] == relations
    assert writer_payload["owner_unit_context"]["synthesis"] == saved["synthesis"]
    for brief, original in zip(saved["paragraph_briefs"], packet["chapter_plan"]["units"][0]["paragraph_briefs"]):
        assert brief["point"] == original["point"]
        assert brief["development"] == original["development"]


@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_merged_briefs_keep_each_condition_and_completion_context(tmp_path, mode, legacy):
    conditions = {"range": [2, 6], "unit": "fixture units"}
    relations = [{"from": "B1", "to": "B2", "relation": "limits"}]
    view = _view(tmp_path, _packet(conditions, relations, legacy=legacy))
    arranged = arranging.validate_arrangement(_arranged(view, merged=True), view, planning_revision=mode)
    assert arranged["validation"]["ok"]
    wview = _writer(tmp_path, view, arranged)
    payload = json.loads(writing.unit_messages(wview, prompt="Fixture", planning_revision=mode)[-1]["content"])
    task = payload["paragraph_tasks"][0]
    assert [b["paragraph_id"] for b in task["source_brief_details"]] == ["B2", "B1"]
    assert task["source_brief_details"][1]["finding_conditions"] == conditions
    assert task["source_brief_details"][0]["finding_conditions"] == {"window": "three sessions", "replicates": 9}
    completion = json.loads(writing.completion_messages(
        wview, "Existing text", [task["paragraph_id"]], prompt="Fixture", planning_revision=mode)[-1]["content"])
    assert completion["paragraph_tasks"][0]["source_brief_details"] == task["source_brief_details"]
    assert completion["owner_unit_context"]["argument_relations"] == relations
    if not mode:
        assert task["point"] == "Editor merged placement"
        assert task["development"] == "Editor organization"


def test_legacy_normal_exact_ids_recover_owner_context_without_rewriting_editor_text(tmp_path):
    view = _view(tmp_path, _packet("Keep the setting", {"B2": "bounds B1"}))
    raw = _arranged(view, refs=False)
    validated = arranging.validate_arrangement(raw, view)
    assert validated["units"][0]["paragraph_tasks"][0]["source_brief_details"][0]["finding_conditions"] == "Keep the setting"
    # Simulate an older persisted arrangement that has no added owner context.
    wview = _writer(tmp_path, view, raw)
    payload = json.loads(writing.unit_messages(wview, prompt="Fixture")[-1]["content"])
    assert payload["paragraph_tasks"][0]["source_brief_details"][0]["finding_conditions"] == "Keep the setting"
    assert payload["owner_unit_context"]["argument_relations"] == {"B2": "bounds B1"}
    assert payload["paragraph_tasks"][0]["point"] == "Editor placement"
    assert payload["paragraph_tasks"][0]["development"] == "Editor organization"


def test_missing_new_fields_remain_compatible_and_do_not_gain_invented_values(tmp_path):
    packet = _packet("unused", "unused", legacy=True)
    unit = packet["chapter_plan"]["units"][0]
    unit.pop("argument_relations")
    for row in unit["ordered_development"]:
        row.pop("finding_conditions")
    view = _view(tmp_path, packet)
    serialized = view.to_dict()["units"][0]
    assert "argument_relations" not in serialized
    assert all("finding_conditions" not in b for b in serialized["paragraph_briefs"])
    arranged = arranging.validate_arrangement(_arranged(view), view)
    assert arranged["validation"]["ok"]
    payload = json.loads(writing.unit_messages(_writer(tmp_path, view, arranged), prompt="Fixture")[-1]["content"])
    assert "argument_relations" not in payload["owner_unit_context"]
    assert payload["owner_unit_context"]["synthesis"] == unit["synthesis_and_conflicts"]


def test_normal_generated_positional_ids_do_not_claim_owner_conditions(tmp_path):
    packet = _packet("Owner-only condition", "Unit relation")
    for index, brief in enumerate(packet["chapter_plan"]["units"][0]["paragraph_briefs"], 1):
        brief["paragraph_id"] = f"U1_P{index:02d}"
    view = _view(tmp_path, packet)
    # Normal legacy mode can generate a positional id equal to an owner id.
    # The validator's carried_over=False proves it was not an explicit claim.
    raw = {"chapter_id": "CH01", "units": [{"unit_id": "U1", "paragraph_tasks": [
        {"point": "New connective task", "source_uses": [{"source_handle": h}]}
        for h in ["P0001", "P0002"]]}]}
    arranged = arranging.validate_arrangement(raw, view)
    assert arranged["validation"]["ok"]
    assert all(t["carried_over"] is False for t in arranged["units"][0]["paragraph_tasks"])
    payload = json.loads(writing.unit_messages(_writer(tmp_path, view, arranged), prompt="Fixture")[-1]["content"])
    assert all("source_brief_details" not in task for task in payload["paragraph_tasks"])
    assert payload["owner_unit_context"]["argument_relations"] == "Unit relation"
