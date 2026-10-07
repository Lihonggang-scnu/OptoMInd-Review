"""Offline owner -> complete chapter -> next group -> arrangement contracts."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from test_outline_strengthening import _payload, _plan, _source


def _unit(unit_id: str, point: str = "Bounded observation") -> dict:
    unit = _plan(point)["units"][0]
    unit["unit_id"] = unit_id
    unit["paragraph_briefs"][0]["paragraph_id"] = unit_id + "_P01"
    unit["case_objects"] = [{"source_handle": "P0001", "finding": "Explicit owner finding", "limits": "Case-specific boundary"}]
    unit["supporting_studies"] = [{"source_handle": "P0001", "conditions": "Measured condition", "limits": "No extrapolation"}]
    unit["synthesis_and_transition"] = "Carry the measured boundary into the next comparison."
    return unit


def _chapters() -> dict:
    plan = _plan()
    plan["units"] = [_unit(f"CH01_U0{index}") for index in range(1, 5)]
    chapter = _payload(
        chapter_plan=plan, source_materials=[_source(), _source("P0002")],
        candidate_materials=[_source("P0003")],
        tool_materials=[{"source_handle": "T0001", "usable_content": "Tool context"}],
        modifiable_unit_ids=["CH01_U01", "CH01_U02", "CH01_U03"],
        read_only_unit_ids=["CH01_U04", "CH02_U01"],
        readonly_neighbor_unit_roles=[{"unit_id": "CH02_U01", "read_only": True, "substantive_point": "External context"}],
    )
    chapter["existing_unit_ids"] = [unit["unit_id"] for unit in plan["units"]]
    return {"CH01": chapter, "CH02": {"chapter_plan": {"units": [_unit("CH02_U01")]}, "opaque_context": ["Retain exactly"]}}


def _owner(chapters: dict, unit_ids: list[str]) -> dict:
    return selection.project_selection_group(chapters, {
        "group_id": "merge-regression", "unit_ids": unit_ids,
        "selection_reason": "Strengthen this local comparison using supplied observations.",
        "improvement_focus": ["Keep source-linked boundaries explicit"],
    })[0]["payload"]


def _accepted(owner: dict, units: list[dict], remap: dict | None = None) -> dict:
    # Exercise the actual production owner classifier, not a hand-labeled
    # accepted result.  The client returns controlled offline JSON only.
    updated = {**deepcopy(owner["chapter_plan"]), "units": units}
    response = {"status": "updated", "updated_plan": updated, "unit_id_remap": remap or {}}

    def client(messages, **kwargs):
        return {"content": json.dumps(response), "complete": True, "finish_reason": "stop"}

    result = strengthening.run_strengthening(
        owner, client=client, model="offline-owner", thinking_budget=1, max_output_tokens=1,
    )
    assert result["status"] == "updated", result["structural_errors"]
    assert result["structural_errors"] == []
    return result


def _assert_arrangement(tmp_path, packet: dict, expected_ids: list[str], expected_remap: dict) -> None:
    packet_path = tmp_path / "CH01.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert [unit.unit_id for unit in view.units] == expected_ids
    assert view.unit_id_remap == expected_remap


def test_valid_split_merge_repeated_group_composes_lineage_and_preserves_full_scope(tmp_path):
    chapters = _chapters()
    original = deepcopy(chapters)
    owner = _owner(chapters, ["CH01_U01"])
    split_units = [_unit("CH01_U01A", "Split first"), _unit("CH01_U01B", "Split second")]
    # Deliberately reverse map key order: plan order controls split order.
    split_map = {"CH01_U01B": ["CH01_U01"], "CH01_U01A": ["CH01_U01"]}
    result = _accepted(owner, split_units, split_map)
    chapters = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    packet = chapters["CH01"]
    ids = ["CH01_U01A", "CH01_U01B", "CH01_U02", "CH01_U03", "CH01_U04"]
    assert packet["chapter_plan"]["units"] == split_units + original["CH01"]["chapter_plan"]["units"][1:]
    assert packet["modifiable_unit_ids"] == ids[:-1]
    assert packet["unit_identity_contract"]["existing_unit_ids"] == ids
    assert packet["existing_unit_ids"] == ids
    assert packet["read_only_unit_ids"] == original["CH01"]["read_only_unit_ids"]
    assert packet["readonly_neighbor_unit_roles"] == original["CH01"]["readonly_neighbor_unit_roles"]
    _assert_arrangement(tmp_path, packet, ids, split_map)

    # A valid later group can select a newly created ID and another current
    # unit.  Its merge spans an unselected neighbor, which must survive.
    owner = _owner(chapters, ["CH01_U01B", "CH01_U03"])
    assert owner["unit_identity_contract"]["existing_unit_ids"] == ["CH01_U01B", "CH01_U03"]
    assert owner["full_chapter_context"]["full_unit_order"] == ids
    merged_unit = _unit("CH01_UM", "Merged bounded comparison")
    result = _accepted(owner, [merged_unit], {"CH01_UM": ["CH01_U03", "CH01_U01B"]})
    chapters = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    packet = chapters["CH01"]
    ids = ["CH01_U01A", "CH01_UM", "CH01_U02", "CH01_U04"]
    lineage = {"CH01_U01A": ["CH01_U01"], "CH01_UM": ["CH01_U03", "CH01_U01"]}
    assert packet["chapter_plan"]["units"] == [split_units[0], merged_unit, original["CH01"]["chapter_plan"]["units"][1], original["CH01"]["chapter_plan"]["units"][3]]
    assert packet["unit_id_remap"] == lineage
    assert packet["modifiable_unit_ids"] == ids[:-1]
    assert packet["unit_identity_contract"]["existing_unit_ids"] == ids
    _assert_arrangement(tmp_path, packet, ids, lineage)

    # An identity remap in a repeated group must not erase original lineage.
    owner = _owner(chapters, ["CH01_UM"])
    repeated = _unit("CH01_UM", "Updated same stable identity")
    result = _accepted(owner, [repeated], {"CH01_UM": ["CH01_UM"]})
    chapters = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    assert chapters["CH01"]["unit_id_remap"] == lineage
    _assert_arrangement(tmp_path, chapters["CH01"], ids, lineage)

    # A separate unchanged-ID group preserves other groups' composed remaps.
    owner = _owner(chapters, ["CH01_U02"])
    result = _accepted(owner, [_unit("CH01_U02", "Independent update")])
    chapters = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    assert chapters["CH01"]["unit_id_remap"] == lineage
    _assert_arrangement(tmp_path, chapters["CH01"], ids, lineage)
    assert chapters["CH02"] == original["CH02"]
    for field in ("candidate_materials", "candidate_navigation", "tool_materials", "actual_local_body", "source_identity_map", "full_chapter_context"):
        assert chapters["CH01"][field] == original["CH01"][field]
    assert chapters["CH01"]["source_materials"] == original["CH01"]["source_materials"]
    assert chapters["CH01"]["input_integrity"] == strengthening._input_integrity(chapters["CH01"])
    assert original == _chapters()  # No caller-owned structure was mutated.


def test_split_preserving_original_id_needs_no_redundant_identity_remap(tmp_path):
    chapters = _chapters()
    owner = _owner(chapters, ["CH01_U01"])
    units = [_unit("CH01_U01A"), _unit("CH01_U01")]
    remap = {"CH01_U01A": ["CH01_U01"]}
    result = _accepted(owner, units, remap)
    merged = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    assert merged["CH01"]["chapter_plan"]["units"][:2] == units
    _assert_arrangement(tmp_path, merged["CH01"], ["CH01_U01A", "CH01_U01", "CH01_U02", "CH01_U03", "CH01_U04"], remap)


def test_merge_into_retained_id_accepts_producer_contract_without_redundant_self_source(tmp_path):
    chapters = _chapters()
    owner = _owner(chapters, ["CH01_U01", "CH01_U02"])
    result = _accepted(owner, [_unit("CH01_U01")], {"CH01_U01": ["CH01_U02"]})
    merged = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    _assert_arrangement(tmp_path, merged["CH01"], ["CH01_U01", "CH01_U03", "CH01_U04"], {"CH01_U01": ["CH01_U02"]})


def test_plan_embedded_lineage_is_refreshed_with_packet_lineage(tmp_path):
    chapters = _chapters()
    chapters["CH01"]["chapter_plan"]["unit_id_remap"] = {"CH01_U01": ["ORIGINAL_U01"]}
    chapters["CH01"]["input_integrity"] = strengthening._input_integrity(chapters["CH01"])
    owner = _owner(chapters, ["CH01_U01"])
    result = _accepted(owner, [_unit("CH01_U01A"), _unit("CH01_U01B")], {
        "CH01_U01A": ["CH01_U01"], "CH01_U01B": ["CH01_U01"],
    })
    merged = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    lineage = {"CH01_U01A": ["ORIGINAL_U01"], "CH01_U01B": ["ORIGINAL_U01"]}
    assert merged["CH01"]["chapter_plan"]["unit_id_remap"] == lineage
    assert merged["CH01"]["unit_id_remap"] == lineage
    _assert_arrangement(tmp_path, merged["CH01"], ["CH01_U01A", "CH01_U01B", "CH01_U02", "CH01_U03", "CH01_U04"], lineage)


def test_merging_split_siblings_deduplicates_original_lineage(tmp_path):
    chapters = _chapters()
    owner = _owner(chapters, ["CH01_U01"])
    result = _accepted(owner, [_unit("CH01_U01A"), _unit("CH01_U01B")], {
        "CH01_U01A": ["CH01_U01"], "CH01_U01B": ["CH01_U01"],
    })
    chapters = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    owner = _owner(chapters, ["CH01_U01A", "CH01_U01B"])
    result = _accepted(owner, [_unit("CH01_UM")], {"CH01_UM": ["CH01_U01A", "CH01_U01B"]})
    merged = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    _assert_arrangement(tmp_path, merged["CH01"], ["CH01_UM", "CH01_U02", "CH01_U03", "CH01_U04"], {"CH01_UM": ["CH01_U01"]})


def test_no_change_material_addition_refreshes_integrity_without_changing_plan():
    chapters = _chapters()
    before = deepcopy(chapters)
    owner = _owner(chapters, ["CH01_U01"])
    result = {"status": "no_change", "accepted_source_materials": [_source("P0005")]}
    merged = strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    assert merged["CH01"]["chapter_plan"] == before["CH01"]["chapter_plan"]
    assert merged["CH01"]["modifiable_unit_ids"] == before["CH01"]["modifiable_unit_ids"]
    assert merged["CH01"]["source_materials"] == before["CH01"]["source_materials"] + [_source("P0005")]
    strengthening._verify_envelope(merged["CH01"])
    assert _owner(merged, ["CH01_U02"])["modifiable_unit_ids"] == ["CH01_U02"]
    assert chapters == before


@pytest.mark.parametrize(("ids", "remap", "error"), [
    (["CH01_U01A"], {"CH01_U01": ["CH01_U01A"]}, "remap_target_missing"),
    (["CH01_U01A"], {"CH01_U01A": ["CH01_U02"]}, "remap_outside_editable_scope"),
    (["CH01_U01A"], {"CH01_U01A": []}, "remap_invalid_sources"),
    (["CH01_U01A"], {}, "returned_unit_outside_editable_scope"),
    (["CH01_U02"], {"CH01_U02": ["CH01_U01"]}, "readonly_unit_returned"),
    (["CH02_U01"], {"CH02_U01": ["CH01_U01"]}, "readonly_unit_returned"),
    (["CH01_U01", "CH01_U01"], {}, "invalid_unit_ids"),
])
def test_merge_rejects_invalid_mapping_or_scope_without_mutating_inputs(ids, remap, error):
    chapters = _chapters()
    before = deepcopy(chapters)
    owner = _owner(chapters, ["CH01_U01"])
    result = {"status": "updated", "updated_plan": {"units": [_unit(unit_id) for unit_id in ids]}, "unit_id_remap": remap}
    with pytest.raises(strengthening.OutlineStrengtheningError, match=error):
        strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
    assert chapters == before


def test_merge_cannot_edit_base_readonly_unit_even_with_forged_owner_scope():
    chapters = _chapters()
    owner = _owner(chapters, ["CH01_U01"])
    owner["modifiable_unit_ids"] = ["CH01_U04"]
    owner["read_only_unit_ids"] = []
    result = {"status": "updated", "updated_plan": {"units": [_unit("CH01_U04")]}}
    with pytest.raises(strengthening.OutlineStrengtheningError, match="editable_scope_invalid"):
        strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)


@pytest.mark.parametrize("occupied", ["CH01_U02", "CH02_U01"])
def test_id_collision_is_rejected_even_when_projection_omits_readonly_ids(occupied):
    chapters = _chapters()
    chapters["CH01"]["read_only_unit_ids"] = ["CH01_U04"]
    owner = _owner(chapters, ["CH01_U01"])
    owner["read_only_unit_ids"] = []
    result = {"status": "updated", "updated_plan": {"units": [_unit(occupied)]}, "unit_id_remap": {occupied: ["CH01_U01"]}}
    with pytest.raises(strengthening.OutlineStrengtheningError, match="returned_unit_outside_editable_scope"):
        strengthening.merge_owner_result_into_chapter_payloads(chapters, owner, result)
