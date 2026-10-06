"""Offline contracts for autonomous unit selection and owner projection."""
from __future__ import annotations

import copy
import json

import pytest

from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile


def _unit(chapter_id: str, number: int, *, tail: str = "") -> dict:
    unit_id = f"{chapter_id}_U{number:02d}"
    return {
        "unit_id": unit_id,
        "substantive_point": f"{tail} point {unit_id}",
        "ordered_development": "State the observation, compare it under its condition, then bound the inference.",
        "argument_relations": {"supports": [f"{chapter_id}_U01"] if number > 1 else {}, "depends_on": []},
        "paragraph_briefs": [{
            "paragraph_id": f"{unit_id}_P01",
            "point": f"{tail} paragraph point {unit_id}",
            "development": "Explain the supplied result and its limiting setting.",
            "finding_conditions": "Only within the supplied observation setting.",
            "source_handles": [f"P{number:04d}"],
        }],
        "supporting_studies": [{"source_handle": f"P{number:04d}", "use": "comparison"}],
        "evidence_conditions_and_limits": "Do not extrapolate beyond the supplied setting.",
        "transition": "Carry the comparison into the next responsibility.",
    }


def _source(handle: str, chapter_id: str) -> dict:
    return {
        "source_handle": handle,
        "paper_id": f"study-{handle}",
        "title": f"Study {handle}",
        "study_summary_A": {
            "key_findings": [{"finding": f"Finding {handle}", "conditions": "Measured setting only"}],
            "contribution_and_limits": [{"contribution": "Bounded observation", "limits": "No extrapolation"}],
        },
        "review_planning_B": {"planning_summary": f"Use {handle} in {chapter_id} comparison."},
    }


def _payload(chapter_id: str, count: int = 3, *, tail: str = "") -> dict:
    units = [_unit(chapter_id, number, tail=tail) for number in range(1, count + 1)]
    sources = [_source(f"P{number:04d}", chapter_id) for number in range(1, count + 1)]
    return strengthening.build_strengthening_payload(
        research_question="Which supplied observations belong together under their stated conditions?",
        chapter_id=chapter_id,
        chapter={"chapter_id": chapter_id, "title": f"Synthetic {chapter_id}"},
        chapter_plan={"chapter_id": chapter_id, "thesis": "A bounded thesis.", "units": units},
        source_materials=sources,
        readonly_neighbor_unit_roles=[],
        full_chapter_context={"chapter_id": chapter_id, "responsibility": "Keep conditions visible."},
        actual_local_body=f"Body for {chapter_id} with all source handles.",
        shared_outline=[{"chapter_id": "CH-SHARED", "title": "Shared review duty"}],
        shared_scope={"scope": "Synthetic bounded scope"},
        source_identity_map={row["source_handle"]: {"paper_id": row["paper_id"]} for row in sources},
        candidate_materials=[{"source_handle": f"C-{chapter_id}", "usable_content": "Candidate context"}],
        tool_materials=[{"source_handle": f"T-{chapter_id}", "usable_content": "Tool context", "conditions": "Tool condition"}],
        call_id=f"selection-{chapter_id}",
    )


def _selection_payload() -> tuple[dict, dict[str, dict]]:
    chapters = {"CH-A": _payload("CH-A", tail="ALPHA-UNTRUNCATED"), "CH-B": _payload("CH-B", tail="BETA")}
    return selection.build_selection_payload(
        list(chapters.values()),
        research_question=chapters["CH-A"]["research_question"],
        shared_outline=[{"chapter_id": "CH-A", "title": "Alpha"}, {"chapter_id": "CH-B", "title": "Beta"}],
        shared_scope={"whole_review_duty": "Relate findings without importing facts."},
    ), chapters


def test_selector_messages_include_complete_plan_without_manual_targets():
    payload, _ = _selection_payload()
    messages = selection.selection_messages(payload)
    text = json.dumps(messages, ensure_ascii=False)
    assert "ALPHA-UNTRUNCATED" in text
    assert "paragraph_briefs" in text and "supporting_studies" in text
    assert "review_targets" not in text
    assert "expected_answers" not in text
    assert "updated_plan" in text  # only in the prohibition contract, never as output schema
    assert "跨章逻辑组" in text
    assert all(
        "study_summary_A" not in row and "review_planning_B" not in row
        for chapter in payload["chapters"]
        for rows in chapter["material_index"].values()
        for row in rows
    )


def test_model_visible_projection_keeps_plans_and_deduplicates_metadata_losslessly():
    payload, chapters = _selection_payload()
    original = copy.deepcopy(payload)
    # Same handle with a different identity must remain two catalog entries.
    payload["chapters"][1]["source_identity_map"] = {
        "P0001": {"source_handle": "P0001", "paper_id": "different-paper", "title": "Different identity", "card_path": "F:\\secret\\card.json"}
    }
    visible = selection.model_visible_selection_payload(payload)
    check = selection.verify_model_visible_projection(payload, visible)
    assert check["unit_count"] == len(payload["all_unit_ids"])
    assert all(
        visible_chapter["chapter_plan"] == source_chapter["chapter_plan"]
        for visible_chapter, source_chapter in zip(visible["chapters"], payload["chapters"])
    )
    assert all("source_identity_map" not in chapter and "material_index" not in chapter for chapter in visible["chapters"])
    assert all(not ("card_path" in json.dumps(chapter, ensure_ascii=False) or "excluded_source_ids" in json.dumps(chapter, ensure_ascii=False)) for chapter in visible["chapters"])
    assert all("readonly_neighbor_unit_roles" not in chapter and all(isinstance(unit_id, str) for unit_id in chapter.get("readonly_neighbor_unit_ids", [])) for chapter in visible["chapters"])
    assert "material_identity_catalog" not in visible
    assert all(chapter["material_identity_refs"] == [] for chapter in visible["chapters"])
    indexed = selection.model_visible_selection_payload(payload, include_material_index=True)
    refs = [pointer for chapter in indexed["chapters"] for pointer in chapter["material_identity_refs"]]
    assert refs and all(pointer.startswith("#/material_identity_catalog/") for pointer in refs)
    assert any(set(entry["paper_ids"]) == {"different-paper"} for entry in indexed["material_identity_catalog"])
    assert payload["chapters"][0]["chapter_plan"] == original["chapters"][0]["chapter_plan"]


def test_model_visible_selection_messages_use_projection_and_keep_full_plan_marker():
    payload, _ = _selection_payload()
    visible = selection.model_visible_selection_payload(payload)
    messages = selection.selection_messages(payload, model_payload=visible)
    text = json.dumps(messages, ensure_ascii=False)
    assert "ALPHA-UNTRUNCATED" in text
    assert "material_identity_catalog" not in text
    assert "本地绝对卡片位置" in text
    assert "F:\\secret\\card.json" not in text
    indexed = selection.model_visible_selection_payload(payload, include_material_index=True)
    indexed_text = json.dumps(selection.selection_messages(payload, model_payload=indexed), ensure_ascii=False)
    assert "material_identity_catalog" in indexed_text


def test_selection_profile_is_explicit_high_capacity_max_role():
    profile = load_quality_profile("outline_selection")
    assert profile["model"] == "qwen3.8-max"
    assert profile["thinking_budget"] == 32768
    assert profile["max_output_tokens"] == 32768


def test_cross_chapter_group_projects_local_editors_and_full_readonly_boundaries():
    payload, chapters = _selection_payload()
    parsed = {
        "status": "selected",
        "groups": [{
            "group_id": "model-cross-chapter",
            "unit_ids": ["CH-A_U01", "CH-B_U02"],
            "selection_reason": "The two responsibilities need a shared comparison boundary.",
            "improvement_focus": ["align conditions and cross-chapter transition"],
            "related_read_only_unit_ids": ["CH-A_U03"],
        }],
    }
    checked = selection.validate_selection_response(payload, parsed)
    assert checked["status"] == "selected", checked
    projected = selection.selection_to_on_demand_payloads(chapters, checked)
    assert {row["chapter_id"] for row in projected} == {"CH-A", "CH-B"}
    for row in projected:
        chapter_id = row["chapter_id"]
        selected = set(row["modifiable_unit_ids"])
        assert selected <= {chapter_id + "_U01", chapter_id + "_U02", chapter_id + "_U03"}
        assert not selected & set(row["read_only_unit_ids"])
        assert row["payload"]["selection_context"]["group_id"] == checked["groups"][0]["group_id"]
        assert row["payload"]["selection_context"]["advisory_only"] is True
        roles = {item["unit_id"] for item in row["payload"]["readonly_neighbor_unit_roles"]}
        assert set(row["read_only_unit_ids"]) <= roles | set(chapters[chapter_id]["read_only_unit_ids"])
        assert row["payload"]["source_materials"] == chapters[chapter_id]["source_materials"]
        assert row["payload"]["tool_materials"] == chapters[chapter_id]["tool_materials"]
        owner_messages = strengthening.strengthening_messages(row["payload"])
        owner_text = json.dumps(owner_messages, ensure_ascii=False)
        assert "自主选择器范围说明" in owner_text
        assert "shared comparison boundary" in owner_text


def test_projection_prefers_latest_total_outline_roles_over_stale_neighbor_copy():
    payload, chapters = _selection_payload()
    current_unit = next(row for row in chapters["CH-A"]["chapter_plan"]["units"] if row["unit_id"] == "CH-A_U02")
    current_unit["substantive_point"] = "CURRENT TOTAL OUTLINE DUTY"
    chapters["CH-A"]["readonly_neighbor_unit_roles"] = [{
        "unit_id": "CH-A_U02", "substantive_point": "STALE PROJECTED DUTY", "read_only": True,
    }]
    chapters["CH-A"]["full_chapter_context"]["latest_context_marker"] = "CURRENT_CONTEXT"
    chapters["CH-A"]["input_integrity"] = strengthening._input_integrity(chapters["CH-A"])
    checked = selection.validate_selection_response(payload, {
        "status": "selected",
        "groups": [{
            "group_id": "latest-role-check", "unit_ids": ["CH-A_U01"],
            "selection_reason": "Check current role handoff.", "improvement_focus": ["keep latest duty"],
        }],
    })
    projected = selection.project_selection_group(chapters, checked["groups"][0])[0]["payload"]
    role = next(row for row in projected["readonly_neighbor_unit_roles"] if row["unit_id"] == "CH-A_U02")
    assert role["substantive_point"] == "CURRENT TOTAL OUTLINE DUTY"
    assert projected["full_chapter_context"]["latest_context_marker"] == "CURRENT_CONTEXT"
    assert projected["full_chapter_context"]["full_unit_order"] == [
        "CH-A_U01", "CH-A_U02", "CH-A_U03",
    ]
    assert projected["full_chapter_context"]["adjacent_unit_ids"]["CH-A_U01"] == {
        "previous_unit_id": None, "next_unit_id": "CH-A_U02",
    }
    assert projected["full_chapter_context"]["adjacent_unit_ids"]["CH-A_U03"] == {
        "previous_unit_id": "CH-A_U02", "next_unit_id": None,
    }


def test_selector_same_chapter_group_and_none_are_supported():
    payload, chapters = _selection_payload()
    selected = selection.validate_selection_response(payload, {
        "status": "selected",
        "groups": [{
            "group_id": "same-chapter",
            "unit_ids": ["CH-A_U01", "CH-A_U03"],
            "selection_reason": "These duties share a material-backed comparison boundary.",
            "improvement_focus": ["preserve ordered development"],
        }],
    })
    projected = selection.selection_to_on_demand_payloads(chapters, selected)
    assert len(projected) == 1
    assert projected[0]["modifiable_unit_ids"] == ["CH-A_U01", "CH-A_U03"]
    assert "CH-A_U02" in projected[0]["read_only_unit_ids"]
    no_change = selection.validate_selection_response(payload, {"status": "no_change", "groups": []})
    assert selection.selection_to_on_demand_payloads(chapters, no_change) == []


def test_same_chapter_groups_get_independent_owner_call_ids():
    payload, chapters = _selection_payload()
    first = selection.validate_selection_response(payload, {
        "status": "selected",
        "groups": [{
            "group_id": "first",
            "unit_ids": ["CH-A_U01"],
            "selection_reason": "First independent group.",
            "improvement_focus": ["first focus"],
        }],
    })["groups"][0]
    second = selection.validate_selection_response(payload, {
        "status": "selected",
        "groups": [{
            "group_id": "second",
            "unit_ids": ["CH-A_U02"],
            "selection_reason": "Second independent group.",
            "improvement_focus": ["second focus"],
        }],
    })["groups"][0]
    first_payload = selection.project_selection_group(chapters, first)[0]["payload"]
    second_payload = selection.project_selection_group(chapters, second)[0]["payload"]
    assert first_payload["call_id"] != second_payload["call_id"]
    assert first_payload["call_id"].startswith("outline-selection:")


@pytest.mark.parametrize("response,needle", [
    ({"status": "selected", "groups": [{"unit_ids": ["NO_SUCH"], "selection_reason": "x", "improvement_focus": ["y"]}]}, "unknown_unit_ids"),
    ({"status": "selected", "groups": [{"unit_ids": ["CH-A_U01"], "selection_reason": "x", "improvement_focus": []}]}, "improvement_focus_required"),
    ({"status": "selected", "groups": [{"unit_ids": ["CH-A_U01"], "selection_reason": "x", "improvement_focus": ["y"]}, {"unit_ids": ["CH-A_U01"], "selection_reason": "z", "improvement_focus": ["w"]}]}, "unit_repeated_across_groups"),
])
def test_selector_rejects_invalid_ids_or_groups_without_guessing(response, needle):
    payload, _ = _selection_payload()
    result = selection.validate_selection_response(payload, response)
    assert result["status"] == "invalid"
    assert any(needle in error for error in result["validation_errors"])


def test_selection_context_is_not_accepted_as_manual_feedback():
    payload, _ = _selection_payload()
    payload["chapters"][0]["selection_context"] = {"origin": "manual", "group_id": "x"}
    # Selection input itself remains a selector envelope; arbitrary owner-only
    # guidance is not silently treated as a valid selector field.
    assert selection.validate_selection_response(payload, {"status": "no_change", "groups": []})["status"] == "no_change"
    owner = _payload("CH-C")
    owner["selection_context"] = {"origin": "manual", "group_id": "x"}
    with pytest.raises(strengthening.OutlineStrengtheningError, match="selection_context_origin_invalid"):
        strengthening.strengthening_messages(owner)

