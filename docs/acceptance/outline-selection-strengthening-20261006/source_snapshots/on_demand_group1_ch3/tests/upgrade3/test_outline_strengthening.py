"""Offline contracts for the generic autonomous outline strengthening seam."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile


def _source(handle: str = "P0001", *, extra: str = "") -> dict:
    return {
        "source_handle": handle,
        "paper_id": f"paper-{handle}",
        "title": f"Study {handle}",
        "study_summary_A": {"finding": f"Observed finding {handle}"},
        "review_planning_B": {"use": f"Use {handle} with its measured condition."},
        "extra": extra,
    }


def _plan(point: str = "Original point") -> dict:
    return {
        "chapter_id": "CH01",
        "thesis": "A material-backed thesis.",
        "reader_objective": "Understand the bounded relation.",
        "units": [{
            "unit_id": "CH01_U01",
            "substantive_point": point,
            "ordered_development": "State the finding, condition, and scope.",
            "argument_relations": {"supports": ["CH01_U01_P01"]},
            "paragraph_briefs": [{
                "paragraph_id": "CH01_U01_P01",
                "point": point,
                "development": "Explain the supplied result under its setting.",
                "source_handles": ["P0001"],
            }],
            "evidence_conditions_and_limits": "The result is limited to the supplied setting.",
            "transition": "Next, examine boundary conditions.",
        }],
    }


def _payload(**kwargs):
    values = dict(
        research_question="How does a measured relation depend on setting?",
        chapter_id="CH01",
        chapter={"chapter_id": "CH01", "title": "Synthetic chapter"},
        chapter_plan=_plan(),
        source_materials=[_source()],
        readonly_neighbor_unit_roles=[{"unit_id": "CH01_U02", "substantive_point": "Neighbor duty", "read_only": True}],
        full_chapter_context={"chapter_id": "CH01", "reader_objective": "Keep the boundary visible."},
        actual_local_body="Existing body with [REF:P0001].",
        shared_outline=[{"chapter_id": "CH02", "title": "Adjacent chapter"}],
        review_argument="The relation is conditional.",
        source_identity_map={"P0001": {"paper_id": "paper-P0001"}},
    )
    values.update(kwargs)
    return strengthening.build_strengthening_payload(**values)


def test_payload_dedupes_only_exact_material_copies_and_keeps_neighbors():
    duplicate = _source()
    unique_candidate = _source("P0002", extra="candidate-only detail")
    payload = _payload(
        candidate_materials=[duplicate, unique_candidate],
        candidate_navigation={"candidates": [{"source_handle": "P0002"}], "candidate_materials": [duplicate, _source("P0003")]},
        tool_materials=[{"source_handle": "P0004", "usable_content": "tool-only"}],
    )
    assert payload["candidate_materials"] == [unique_candidate]
    assert payload["candidate_navigation"]["candidate_materials"] == [_source("P0003")]
    assert payload["tool_materials"][0]["source_handle"] == "P0004"
    assert payload["readonly_neighbor_unit_roles"][0]["read_only"] is True
    assert payload["actual_local_body_sha256"]
    assert "review_targets" not in payload
    messages = strengthening.strengthening_messages(payload)
    text = json.dumps(messages, ensure_ascii=False)
    assert "candidate-only detail" in text
    assert "Neighbor duty" in text
    assert "自主细纲强化模式" in text


def test_model_material_projection_is_lossless_and_only_refs_large_material_subtrees():
    long_material = {"finding": "same evidence " * 120, "conditions": ["same setting"]}
    source = _source()
    source["review_planning_B"] = long_material
    candidate = _source("P0002", extra="unique candidate field")
    candidate["review_planning_B"] = long_material
    payload = _payload(source_materials=[source], candidate_materials=[candidate])
    projection = strengthening.model_material_projection(payload)
    projected = projection["payload"]
    assert projection["material_ref_count"] >= 1
    assert strengthening.expand_material_projection(projected) == payload
    assert projected["candidate_materials"][0]["review_planning_B"] == {
        "$material_ref": "#/source_materials/0/review_planning_B"
    }
    assert projected["candidate_materials"][0]["extra"] == "unique candidate field"
    short = _payload(candidate_materials=[_source("P0003")])
    short_projection = strengthening.model_material_projection(short)
    assert short_projection["material_ref_count"] == 0


def test_model_material_projection_is_used_only_for_request_and_owner_validates_full_payload():
    long_material = {"finding": "same evidence " * 120, "conditions": ["same setting"]}
    source = _source()
    source["review_planning_B"] = long_material
    candidate = _source("P0002")
    candidate["review_planning_B"] = long_material
    payload = _payload(source_materials=[source], candidate_materials=[candidate])
    model_payload = strengthening.model_material_projection(payload)["payload"]
    captured = []

    def fake_client(messages, **kwargs):
        captured.append(json.dumps(messages, ensure_ascii=False))
        return {
            "content": json.dumps({"status": "no_change", "chapter_updates": []}),
            "complete": True, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }

    result = strengthening.run_strengthening(
        payload, client=fake_client, model="qwen3.8-max",
        thinking_budget=32768, max_output_tokens=32768, model_payload=model_payload,
    )
    assert result["status"] == "no_change"
    assert "$material_ref" in captured[0]
    assert len(result["accepted_source_materials"]) == len(payload["source_materials"])


def test_arrangement_projection_keeps_original_material_pool_when_owner_accepts_subset():
    source_one = _source("P0001")
    source_two = _source("P0002")
    candidate = _source("C0001")
    tool = {"source_handle": "T0001", "usable_content": "Tool supplied context."}
    payload = _payload(
        source_materials=[source_one, source_two],
        candidate_materials=[candidate],
        tool_materials=[tool],
    )
    result = {
        "status": "updated",
        "updated_plan": _plan("Updated bounded point"),
        "accepted_source_materials": [source_one, _source("P0003")],
    }
    projected = strengthening.project_plan_for_arrangement(payload, result)
    assert {row["source_handle"] for row in projected["source_materials"]} == {"P0001", "P0002", "P0003"}
    assert projected["candidate_materials"] == [candidate]
    assert projected["tool_materials"] == [tool]


def test_owner_result_merges_selected_units_into_latest_complete_plan():
    first = _source("P0001")
    second = _source("P0002")
    third = _source("P0003")
    full_plan = _plan("Original U01")
    full_plan["units"].append({
        "unit_id": "CH01_U02", "substantive_point": "Unselected unit stays intact",
        "ordered_development": "Keep this duty.", "paragraph_briefs": [],
    })
    base = _payload(
        source_materials=[first, second], chapter_plan=full_plan, candidate_materials=[third],
        modifiable_unit_ids=["CH01_U01"], read_only_unit_ids=["CH01_U02"],
    )
    owner_payload = dict(base)
    owner_payload["chapter_plan"] = {"chapter_id": "CH01", "units": [{
        "unit_id": "CH01_U01", "substantive_point": "Updated selected duty",
        "ordered_development": "Update selected duty only.", "paragraph_briefs": [],
    }]}
    owner_payload["modifiable_unit_ids"] = ["CH01_U01"]
    owner_payload["read_only_unit_ids"] = ["CH01_U02"]
    result = {
        "status": "updated", "updated_plan": owner_payload["chapter_plan"],
        "accepted_source_materials": [first, _source("P0004")],
    }
    merged = strengthening.merge_owner_result_into_chapter_payloads(
        {"CH01": base}, owner_payload, result,
    )
    rows = merged["CH01"]["chapter_plan"]["units"]
    assert [row["unit_id"] for row in rows] == ["CH01_U01", "CH01_U02"]
    assert rows[0]["substantive_point"] == "Updated selected duty"
    assert rows[1]["substantive_point"] == "Unselected unit stays intact"
    assert {row["source_handle"] for row in merged["CH01"]["source_materials"]} == {"P0001", "P0002", "P0004"}
    assert merged["CH01"]["candidate_materials"] == [third]


def test_owner_result_merge_remap_does_not_duplicate_merged_new_unit():
    first = _source("P0001")
    full_plan = _plan("First")
    full_plan["units"].append({"unit_id": "CH01_U02", "substantive_point": "Second", "paragraph_briefs": []})
    base = _payload(
        source_materials=[first], chapter_plan=full_plan,
        readonly_neighbor_unit_roles=[],
        modifiable_unit_ids=["CH01_U01", "CH01_U02"], read_only_unit_ids=[],
    )
    owner_payload = dict(base)
    owner_payload["chapter_plan"] = {"chapter_id": "CH01", "units": [{"unit_id": "CH01_UM", "substantive_point": "Merged", "paragraph_briefs": []}]}
    owner_payload["modifiable_unit_ids"] = ["CH01_U01", "CH01_U02"]
    owner_payload["read_only_unit_ids"] = []
    result = {
        "status": "updated", "updated_plan": owner_payload["chapter_plan"],
        "unit_id_remap": {"CH01_U01": ["CH01_UM"], "CH01_U02": ["CH01_UM"]},
        "accepted_source_materials": [first],
    }
    merged = strengthening.merge_owner_result_into_chapter_payloads({"CH01": base}, owner_payload, result)
    assert [row["unit_id"] for row in merged["CH01"]["chapter_plan"]["units"]] == ["CH01_UM"]


def test_owner_result_split_remap_preserves_both_new_units_in_place():
    first = _source("P0001")
    base = _payload(source_materials=[first], readonly_neighbor_unit_roles=[], modifiable_unit_ids=["CH01_U01"], read_only_unit_ids=[])
    owner_payload = dict(base)
    owner_payload["modifiable_unit_ids"] = ["CH01_U01"]
    owner_payload["read_only_unit_ids"] = []
    owner_payload["chapter_plan"] = {"chapter_id": "CH01", "units": [
        {"unit_id": "CH01_U01A", "substantive_point": "Split A", "paragraph_briefs": []},
        {"unit_id": "CH01_U01B", "substantive_point": "Split B", "paragraph_briefs": []},
    ]}
    result = {
        "status": "updated", "updated_plan": owner_payload["chapter_plan"],
        "unit_id_remap": {"CH01_U01": ["CH01_U01A", "CH01_U01B"]},
        "accepted_source_materials": [first],
    }
    merged = strengthening.merge_owner_result_into_chapter_payloads({"CH01": base}, owner_payload, result)
    assert [row["unit_id"] for row in merged["CH01"]["chapter_plan"]["units"]] == ["CH01_U01A", "CH01_U01B"]


def test_standalone_outline_entry_defaults_to_quality_profile_not_plus():
    from scripts.upgrade3 import outline_strengthening as cli

    assert strengthening.ROLE == "strong_outline"
    assert cli.DEFAULT_PROFILE == "strong_outline"
    assert cli.DEFAULT_REVIEWER_PROFILE == "strong_outline"
    assert cli.DEFAULT_OWNER_PROFILE == "strong_outline"


def test_autonomous_scope_contract_is_repeated_for_system_and_user_without_domain_targets():
    payload = _payload()
    messages = strengthening.strengthening_messages(payload)
    system = messages[0]["content"]
    user = messages[1]["content"]
    marker = "【自主范围与返回合同】"
    assert system.count(marker) == 1
    assert user.count(marker) == 1
    assert "modifiable_unit_ids" in system and "readonly_neighbor_unit_roles" in system
    assert "modifiable_unit_ids" in user and "readonly_neighbor_unit_roles" in user
    assert "microbiome" not in json.dumps(messages, ensure_ascii=False).casefold()


def test_manual_problem_controls_are_rejected():
    with pytest.raises(strengthening.OutlineStrengtheningError, match="forbidden_autonomous_control"):
        _payload(review_argument={"review_targets": ["leak this"]})


def test_no_change_is_a_valid_owner_result():
    payload = _payload()

    def fake_client(messages, **kwargs):
        assert kwargs["thinking_budget"] == 32768
        return {
            "content": json.dumps({"status": "no_change", "chapter_updates": []}, ensure_ascii=False),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 12},
        }

    result = strengthening.run_strengthening(
        payload,
        client=fake_client,
        model="qwen3.5-plus",
        thinking_budget=32768,
        max_output_tokens=32768,
    )
    assert result["status"] == "no_change"
    assert result["updated_plan"] == payload["chapter_plan"]
    assert strengthening.project_plan_for_arrangement(payload, result)["outline_strengthening_status"] == "no_change"


def test_readonly_unit_cannot_be_returned_as_an_edit():
    payload = _payload()
    invalid = _plan("Invalid neighbor edit")
    invalid["units"][0]["unit_id"] = "CH01_U02"
    invalid["units"][0]["paragraph_briefs"][0]["source_handles"] = ["P9999"]

    def fake_client(messages, **kwargs):
        return {
            "content": json.dumps({
                "status": "updated",
                "chapter_updates": [{"chapter_id": "CH01", "updated_plan": invalid}],
            }, ensure_ascii=False),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    result = strengthening.run_strengthening(
        payload, client=fake_client, model="qwen3.5-plus",
        thinking_budget=32768, max_output_tokens=32768,
    )
    assert result["status"] == "unresolved"
    assert "readonly_unit_returned_for_edit" in result["structural_errors"]
    assert "updated_unit_sources_unavailable:P9999" in result["structural_errors"]


def test_valid_split_with_explicit_remap_reaches_arrangement_contract():
    payload = _payload()
    split = _plan("Split parent point")
    child_a = dict(split["units"][0], unit_id="CH01_U01A")
    child_b = dict(split["units"][0], unit_id="CH01_U01B")
    child_a["paragraph_briefs"] = [dict(child_a["paragraph_briefs"][0], paragraph_id="CH01_U01A_P01")]
    child_b["paragraph_briefs"] = [dict(child_b["paragraph_briefs"][0], paragraph_id="CH01_U01B_P01")]
    split["units"] = [child_a, child_b]

    def fake_client(messages, **kwargs):
        return {
            "content": json.dumps({
                "status": "updated",
                "unit_id_remap": {"CH01_U01A": ["CH01_U01"], "CH01_U01B": ["CH01_U01"]},
                "chapter_updates": [{"chapter_id": "CH01", "updated_plan": split}],
            }, ensure_ascii=False),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    result = strengthening.run_strengthening(
        payload, client=fake_client, model="qwen3.5-plus",
        thinking_budget=32768, max_output_tokens=32768,
    )
    assert result["status"] == "updated"
    assert result["structural_errors"] == []
    assert result["unit_id_remap"] == {"CH01_U01A": ["CH01_U01"], "CH01_U01B": ["CH01_U01"]}


def test_review_mediated_material_without_local_ab_is_accepted_and_reaches_arrangement(tmp_path):
    mediated = {
        "source_handle": "P0002",
        "paper_id": "review-reported-paper-2",
        "title": "Reported study with resolved identity",
        "deep_read_material": {"finding": "A review reports a bounded finding under a named setting."},
    }
    payload = _payload(candidate_materials=[mediated])
    changed = _plan("Use the review-mediated bounded finding")
    changed["units"][0]["paragraph_briefs"][0]["source_handles"] = ["P0002"]

    def fake_client(messages, **kwargs):
        return {
            "content": json.dumps({
                "status": "updated",
                "chapter_updates": [{"chapter_id": "CH01", "updated_plan": changed}],
            }, ensure_ascii=False),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    result = strengthening.run_strengthening(
        payload, client=fake_client, model="qwen3.5-plus",
        thinking_budget=32768, max_output_tokens=32768,
    )
    assert result["status"] == "updated"
    packet = strengthening.project_plan_for_arrangement(payload, result)
    assert any(row.get("source_handle") == "P0002" for row in packet["source_materials"])
    packet_path = tmp_path / "PACKET.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert any(source.source_handle == "P0002" for source in view.sources)


def test_plus_profile_reaches_complete_http_capacity(monkeypatch, tmp_path):
    from optomind_research.runtime.upgrade3.module4 import runtime

    captured = []

    class Response:
        status = 200
        headers = {}

        def read(self):
            return json.dumps({
                "model": "qwen3.5-plus",
                "id": "offline",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            return Response()

    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *args: Opener())
    ledger = runtime.GlobalBudgetLedger(path=tmp_path / "budget.sqlite", limit_cny=20.0)
    client = strengthening.make_strengthening_client(
        role="autonomous_outline",
        key_file="not-opened", budget_ledger=ledger, raw_response_dir=tmp_path / "raw",
    )
    client([{"role": "user", "content": "Return JSON."}])
    body = captured[-1]
    assert body["model"] == load_quality_profile("autonomous_outline")["model"] == "qwen3.5-plus"
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 32768
    assert body["max_completion_tokens"] == 65536
    assert "max_tokens" not in body


def test_changed_result_reaches_existing_arrangement_input(tmp_path):
    candidate = _source("P0002", extra="candidate-only evidence")
    payload = _payload(candidate_materials=[candidate])
    changed = _plan("Changed point grounded in P0002")
    changed["units"][0]["paragraph_briefs"][0]["source_handles"] = ["P0002"]

    def fake_client(messages, **kwargs):
        return {
            "content": json.dumps({
                "status": "updated",
                "chapter_updates": [{"chapter_id": "CH01", "updated_plan": changed}],
            }, ensure_ascii=False),
            "complete": True,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    result = strengthening.run_strengthening(
        payload,
        client=fake_client,
        model="qwen3.5-plus",
        thinking_budget=32768,
        max_output_tokens=32768,
    )
    assert result["status"] == "updated"
    packet = strengthening.project_plan_for_arrangement(payload, result)
    packet_path = tmp_path / "production" / "PACKET.json"
    packet_path.parent.mkdir()
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "production" / "ID_MAP.json")
    arrangement_input = arranging.write_view(view, tmp_path / "production" / "ARRANGEMENT_INPUT.json")
    saved = json.loads(Path(arrangement_input).read_text(encoding="utf-8"))
    assert saved["chapter_id"] == "CH01"
    assert saved["units"][0]["substantive_point"] == "Changed point grounded in P0002"
    assert saved["units"][0]["paragraph_briefs"][0]["source_handles"] == ["P0002"]
    assert {row["source_handle"] for row in packet["source_materials"]} == {"P0001", "P0002"}


def test_on_demand_catalog_covers_tool_candidate_navigation_and_mediated_identity():
    mediated = {
        "source_handle": "P0003", "paper_id": "original-3", "title": "Review reported study",
        "reviewed_original_identity": {"paper_id": "original-3"},
        "deep_read_material": {"usable_content": "Reported bounded result under condition C."},
    }
    payload = _payload(
        candidate_materials=[mediated],
        candidate_navigation={"candidate_materials": [{**mediated, "local_passages": ["passage"]}]},
        tool_materials=[{"source_handle": "T0001", "paper_id": "tool-1", "usable_content": "Tool reported comparison."}],
    )
    catalog = on_demand.build_material_catalog(payload)
    assert catalog["coverage"]["source_materials"] == 1
    assert catalog["coverage"]["candidate_materials"] == 1
    assert catalog["coverage"]["candidate_navigation"] == 1
    assert catalog["coverage"]["tool_materials"] == 1
    text = json.dumps(on_demand.access_messages(payload, catalog), ensure_ascii=False)
    assert "P0003" in text and "original-3" in text and "condition" in text
    assert "Reported bounded result" in text
    assert "updated_plan" not in on_demand.access_messages(payload, catalog)[1]["content"]


def test_on_demand_readonly_only_selection_is_diagnosed_and_preserved():
    payload = _payload(
        candidate_materials=[_source("P0002", extra="shared evidence")],
        modifiable_unit_ids=["CH01_U01"],
        read_only_unit_ids=["CH01_U02"],
    )
    catalog = on_demand.build_material_catalog(payload)
    trace = on_demand.resolve_material_requests(
        payload,
        catalog,
        {
            "status": "partial",
            "material_requests": [{
                "access_id": "candidate_materials[0]",
                "unit_ids": ["CH01_U02"],
                "reason": "The neighbour role identifies a relevant boundary.",
            }],
        },
    )
    diagnostics = trace["selection_diagnostics"]
    assert diagnostics["readonly_only_materials"] == [{
        "access_id": "candidate_materials[0]",
        "unit_ids": ["CH01_U02"],
    }]
    assert diagnostics["evidence_preserved"] is True
    assert diagnostics["action"] == "owner_may_use_shared_evidence; no_automatic_reselection"
    owner = on_demand.owner_messages(payload, catalog, trace)
    rendered = json.dumps(owner, ensure_ascii=False)
    assert "selection_diagnostics" in rendered
    assert "shared evidence" in rendered
    assert "CH01_U02" in rendered


def test_on_demand_selected_catalog_points_to_full_record_without_candidate_duplicate():
    candidate = _source("P0002", extra="candidate full record")
    payload = _payload(
        candidate_materials=[candidate],
        candidate_navigation={"candidate_materials": [candidate]},
    )
    catalog = on_demand.build_material_catalog(payload)
    trace = on_demand.resolve_material_requests(
        payload,
        catalog,
        {
            "status": "partial",
            "material_requests": [
                {"access_id": "candidate_materials[0]", "unit_ids": ["CH01_U01"]},
            ],
        },
    )
    model_payload = on_demand._model_visible_payload(
        on_demand._owner_payload(payload, catalog, trace), catalog, trace,
    )
    assert len(model_payload["candidate_materials"]) == 1
    assert model_payload["candidate_navigation"]["candidate_materials"] == []
    pointer = model_payload["candidate_navigation"]["candidate_material_refs"][0]["material_ref"]
    assert pointer == "#/candidate_materials/0"
    entry = next(item for item in model_payload["on_demand_material_access"]["catalog"]["entries"]
                 if item["access_id"] == "candidate_materials[0]")
    assert entry["material_ref"] == "#/candidate_materials/0"
    assert entry["full_record_in_request"] is True
    assert "source_supplied_locators" not in entry
    assert model_payload["candidate_materials"][0] == candidate


def test_on_demand_nested_record_points_to_selected_parent_without_duplicate_full_copy():
    child = _source("P0002", extra="nested child full record")
    parent = _source("P0001", extra="parent full record")
    parent["sources"] = [child]
    payload = _payload(source_materials=[parent])
    catalog = on_demand.build_material_catalog(payload)
    trace = on_demand.resolve_material_requests(
        payload,
        catalog,
        {
            "status": "partial",
            "material_requests": [
                {"access_id": "source_materials[0]", "unit_ids": ["CH01_U01"]},
                {"access_id": "source_materials[0].sources[0]", "unit_ids": ["CH01_U01"]},
            ],
        },
    )
    model_payload = on_demand._model_visible_payload(
        on_demand._owner_payload(payload, catalog, trace), catalog, trace,
    )
    assert len(model_payload["source_materials"]) == 1
    child_entry = next(item for item in model_payload["on_demand_material_access"]["catalog"]["entries"]
                       if item["access_id"] == "source_materials[0].sources[0]")
    assert child_entry["material_ref"] == "#/source_materials/0/sources/0"
    assert model_payload["source_materials"][0]["sources"][0] == child


def test_on_demand_resolves_unselected_material_into_one_max_request_and_arrangement(tmp_path):
    candidate = {
        "source_handle": "P0002", "paper_id": "paper-P0002", "title": "Candidate evidence",
        "deep_read_material": {"usable_content": "Unselected real passage under condition D."},
    }
    payload = _payload(candidate_materials=[candidate])
    access_seen = []
    owner_seen = []
    changed = _plan("Changed point grounded in the resolved candidate")
    changed["units"][0]["paragraph_briefs"][0]["source_handles"] = ["P0002"]

    def access_client(messages, **kwargs):
        access_seen.append(messages)
        return {"content": json.dumps({
            "status": "partial",
            "material_requests": [{"access_id": "candidate_materials[0]", "material_paths": ["deep_read_material"], "unit_ids": ["CH01_U01"], "reason": "Read the unselected candidate passage."}],
        }), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 20, "completion_tokens": 12}}

    def owner_client(messages, **kwargs):
        owner_seen.append(messages)
        rendered = json.dumps(messages, ensure_ascii=False)
        assert "Unselected real passage under condition D." in rendered
        assert "candidate_materials[0]" in rendered
        return {"content": json.dumps({
            "status": "updated",
            "chapter_updates": [{"chapter_id": "CH01", "updated_plan": changed}],
        }, ensure_ascii=False), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 30, "completion_tokens": 24}}

    result = on_demand.run_on_demand_strengthening(
        payload,
        access_client=access_client, access_model="qwen3.5-plus", access_thinking_budget=32768, access_max_output_tokens=32768,
        owner_client=owner_client, owner_model="qwen3.8-max", owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "updated"
    assert len(access_seen) == 1 and len(owner_seen) == 1
    trace = result["on_demand_strengthening"]["material_access_trace"]
    assert trace["trace"][0]["identity"]["paper_id"] == "paper-P0002"
    packet = strengthening.project_plan_for_arrangement(payload, result)
    assert any(row.get("source_handle") == "P0002" for row in packet["source_materials"])
    packet_path = tmp_path / "PACKET.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert view.units[0].substantive_point == "Changed point grounded in the resolved candidate"


def test_on_demand_owner_can_request_one_continuation_and_no_change_is_valid():
    payload = _payload(candidate_materials=[_source("P0002", extra="late material")])
    owner_calls = []

    def access_client(messages, **kwargs):
        return {"content": json.dumps({"status": "no_change", "material_requests": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 20, "completion_tokens": 8}}

    def owner_client(messages, **kwargs):
        owner_calls.append(messages)
        if len(owner_calls) == 1:
            return {"content": json.dumps({"status": "needs_materials", "material_requests": [{"access_id": "candidate_materials[0]", "paths": ["review_planning_B"]}]}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 30, "completion_tokens": 10}}
        return {"content": json.dumps({"status": "no_change", "chapter_updates": []}), "complete": True, "finish_reason": "stop", "usage": {"prompt_tokens": 30, "completion_tokens": 12}}

    result = on_demand.run_on_demand_strengthening(
        payload,
        access_client=access_client, access_model="qwen3.5-plus", access_thinking_budget=32768, access_max_output_tokens=32768,
        owner_client=owner_client, owner_model="qwen3.8-max", owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "no_change"
    assert result["on_demand_strengthening"]["owner_call_count"] == 2
    assert len(owner_calls) == 2
    assert result["on_demand_strengthening"]["material_access_trace"]["resolved_count"] == 1


def test_on_demand_checkpoint_resume_reuses_compatible_paid_stages(tmp_path):
    payload = _payload()
    calls = {"access": 0, "owner": 0}
    profile = {"model": "qwen3.8-max", "thinking_budget": 32768, "max_output_tokens": 32768}
    access_profile = {"model": "qwen3.5-plus", "thinking_budget": 32768, "max_output_tokens": 32768}

    def access_client(messages, **kwargs):
        calls["access"] += 1
        return {"content": json.dumps({"status": "no_change", "material_requests": []}),
                "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 20, "completion_tokens": 8}}

    def owner_client(messages, **kwargs):
        calls["owner"] += 1
        return {"content": json.dumps({"status": "no_change", "chapter_updates": []}),
                "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 30, "completion_tokens": 12}}

    first = on_demand.run_on_demand_strengthening(
        payload,
        access_client=access_client, access_model=access_profile["model"],
        access_thinking_budget=access_profile["thinking_budget"],
        access_max_output_tokens=access_profile["max_output_tokens"],
        owner_client=owner_client, owner_model=profile["model"],
        owner_thinking_budget=profile["thinking_budget"], owner_max_output_tokens=profile["max_output_tokens"],
        checkpoint_dir=tmp_path / "stages", resume=True,
        access_profile=access_profile, owner_profile=profile,
    )
    assert first["status"] == "no_change"
    assert calls == {"access": 1, "owner": 1}
    assert (tmp_path / "stages" / "ACCESS_STAGE.json").is_file()
    assert (tmp_path / "stages" / "OWNER_STAGE.json").is_file()

    def should_not_call(messages, **kwargs):
        raise AssertionError("compatible checkpoint should prevent a second paid call")

    second = on_demand.run_on_demand_strengthening(
        payload,
        access_client=should_not_call, access_model=access_profile["model"],
        access_thinking_budget=access_profile["thinking_budget"],
        access_max_output_tokens=access_profile["max_output_tokens"],
        owner_client=should_not_call, owner_model=profile["model"],
        owner_thinking_budget=profile["thinking_budget"], owner_max_output_tokens=profile["max_output_tokens"],
        checkpoint_dir=tmp_path / "stages", resume=True,
        access_profile=access_profile, owner_profile=profile,
    )
    assert second["status"] == "no_change"
    assert second["on_demand_strengthening"]["access_reused"] is True
    assert second["on_demand_strengthening"]["owner_reused"] is True


def test_on_demand_raw_checkpoint_recovers_parse_failure_without_new_call(tmp_path, monkeypatch):
    payload = _payload()
    access_calls = []
    owner_calls = []

    def access_client(messages, **kwargs):
        access_calls.append(messages)
        return {"content": json.dumps({"status": "no_change", "material_requests": []}),
                "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 20, "completion_tokens": 8}}

    def owner_client(messages, **kwargs):
        owner_calls.append(messages)
        return {"content": json.dumps({"status": "no_change", "chapter_updates": []}),
                "complete": True, "finish_reason": "stop",
                "usage": {"prompt_tokens": 30, "completion_tokens": 12}}

    original_parser = planning._parse_planner_response
    failed_once = {"value": True}

    def fail_access_parse(raw):
        if failed_once["value"]:
            failed_once["value"] = False
            raise ValueError("synthetic access parse failure")
        return original_parser(raw)

    monkeypatch.setattr(planning, "_parse_planner_response", fail_access_parse)
    access_checkpoint = tmp_path / "access_failure"
    with pytest.raises(ValueError, match="synthetic access parse failure"):
        on_demand.run_on_demand_strengthening(
            payload,
            access_client=access_client, access_model="qwen3.5-plus",
            access_thinking_budget=32768, access_max_output_tokens=32768,
            owner_client=owner_client, owner_model="qwen3.8-max",
            owner_thinking_budget=32768, owner_max_output_tokens=32768,
            checkpoint_dir=access_checkpoint, resume=True,
        )
    assert len(access_calls) == 1
    assert (access_checkpoint / "ACCESS_RAW_STAGE.json").is_file()

    monkeypatch.setattr(planning, "_parse_planner_response", original_parser)

    def should_not_call(messages, **kwargs):
        raise AssertionError("raw response recovery must not repeat a paid call")

    recovered = on_demand.run_on_demand_strengthening(
        payload,
        access_client=should_not_call, access_model="qwen3.5-plus",
        access_thinking_budget=32768, access_max_output_tokens=32768,
        owner_client=owner_client, owner_model="qwen3.8-max",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
        checkpoint_dir=access_checkpoint, resume=True,
    )
    assert recovered["status"] == "no_change"
    assert recovered["on_demand_strengthening"]["access_raw_reused"] is True
    assert len(access_calls) == 1
    assert len(owner_calls) == 1

    owner_checkpoint = tmp_path / "owner_failure"
    parse_calls = {"count": 0}

    def fail_owner_parse(raw):
        parse_calls["count"] += 1
        if parse_calls["count"] == 2:
            raise ValueError("synthetic owner parse failure")
        return original_parser(raw)

    monkeypatch.setattr(planning, "_parse_planner_response", fail_owner_parse)
    with pytest.raises(ValueError, match="synthetic owner parse failure"):
        on_demand.run_on_demand_strengthening(
            payload,
            access_client=access_client, access_model="qwen3.5-plus",
            access_thinking_budget=32768, access_max_output_tokens=32768,
            owner_client=owner_client, owner_model="qwen3.8-max",
            owner_thinking_budget=32768, owner_max_output_tokens=32768,
            checkpoint_dir=owner_checkpoint, resume=True,
        )
    assert (owner_checkpoint / "ACCESS_STAGE.json").is_file()
    assert (owner_checkpoint / "OWNER_RAW_STAGE.json").is_file()
    access_count_before_recovery = len(access_calls)
    owner_count_before_recovery = len(owner_calls)

    monkeypatch.setattr(planning, "_parse_planner_response", original_parser)
    recovered_owner = on_demand.run_on_demand_strengthening(
        payload,
        access_client=should_not_call, access_model="qwen3.5-plus",
        access_thinking_budget=32768, access_max_output_tokens=32768,
        owner_client=should_not_call, owner_model="qwen3.8-max",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
        checkpoint_dir=owner_checkpoint, resume=True,
    )
    assert recovered_owner["status"] == "no_change"
    assert recovered_owner["on_demand_strengthening"]["owner_raw_reused"] is True
    assert len(access_calls) == access_count_before_recovery
    assert len(owner_calls) == owner_count_before_recovery


def test_on_demand_prepare_has_explicit_two_owner_cost_guard_and_lightweight_owner_view(tmp_path):
    from scripts.upgrade3 import outline_strengthening as cli

    payload = _payload(tool_materials=[{
        "source_handle": "T0001", "paper_id": "tool-1", "usable_content": "Tool supplied condition and comparison.",
    }])
    input_path = tmp_path / "INPUT.json"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    args = type("Args", (), {
        "input": str(input_path), "output": str(tmp_path / "prepared"),
        "mode": "on_demand", "profile": "strong_outline", "access_profile": "autonomous_outline",
        "tokenizer": "", "deduplicate_materials": False,
    })()
    report = cli.prepare_on_demand(args)
    request = json.loads((tmp_path / "prepared" / "REQUEST.json").read_text(encoding="utf-8"))
    assert report["status"] == "prepared_no_paid_calls"
    assert request["worst_case_estimated_cost_cny"] == pytest.approx(
        request["access_estimate"]["estimated_cost_cny"] + 2 * request["owner_upper_bound_estimate"]["estimated_cost_cny"]
    )
    owner_user = request["owner_upper_bound_messages"][1]["content"]
    assert '"source_materials"' in owner_user
    assert "material_ref" in owner_user or "source_supplied_locators" in owner_user
    assert request["catalog"]["coverage"]["tool_materials"] == 1


def test_reviewed_strengthening_passes_only_validated_machine_issues_to_owner(tmp_path):
    payload = _payload()
    owner_seen = []
    changed = _plan("Reordered point after independent review")

    def reviewer(messages, **kwargs):
        text = json.dumps(messages, ensure_ascii=False)
        assert "reviewer" in text.casefold() or "审阅" in text
        assert "replacement_text" not in text
        assert "Return chapter_updates" not in text
        assert "Do not return notes without updated_plan" not in text
        assert "本轮只返回chapter_updates" not in text
        return {
            "content": json.dumps({
                "status": "reviewed",
                "issues": [{
                    "issue_id": "I-1",
                    "affected_unit_ids": ["CH01_U01"],
                    "description": "The supplied condition is separated from the finding.",
                    "evidence_handles": ["P0001"],
                    "reason_to_change": "The source material states the condition together with the finding.",
                }],
            }, ensure_ascii=False),
            "complete": True, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def owner(messages, **kwargs):
        owner_seen.append(messages)
        rendered = json.dumps(messages, ensure_ascii=False)
        assert "I-1" in rendered
        assert "review_decisions" in rendered
        return {
            "content": json.dumps({
                "status": "updated",
                "review_decisions": [{
                    "issue_id": "I-1", "decision": "adopt",
                    "rationale": "Keep the condition beside its finding.",
                }],
                "chapter_updates": [{"chapter_id": "CH01", "updated_plan": changed}],
            }, ensure_ascii=False),
            "complete": True, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    result = strengthening.run_reviewed_strengthening(
        payload,
        reviewer_client=reviewer, reviewer_model="qwen3.5-plus",
        reviewer_thinking_budget=32768, reviewer_max_output_tokens=32768,
        owner_client=owner, owner_model="qwen3.5-plus",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "updated"
    assert len(owner_seen) == 1
    reviewed = result["reviewed_strengthening"]
    assert reviewed["review_issues"][0]["evidence_handles"] == ["P0001"]
    assert reviewed["adoption_reasons"][0]["decision"] == "adopt"
    packet = strengthening.project_plan_for_arrangement(payload, result)
    packet_path = tmp_path / "PACKET.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    view = arranging.build_chapter_view(packet_path, id_map_path=tmp_path / "ID_MAP.json")
    assert view.units[0].substantive_point == "Reordered point after independent review"


def test_reviewed_no_change_is_explicit_and_skips_owner():
    payload = _payload()
    owner_called = []

    def reviewer(messages, **kwargs):
        return {
            "content": json.dumps({"status": "no_change", "issues": []}),
            "complete": True, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }

    def owner(messages, **kwargs):
        owner_called.append(True)
        return {}

    result = strengthening.run_reviewed_strengthening(
        payload,
        reviewer_client=reviewer, reviewer_model="qwen3.5-plus",
        reviewer_thinking_budget=32768, reviewer_max_output_tokens=32768,
        owner_client=owner, owner_model="qwen3.5-plus",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "no_change"
    assert result["updated_plan"] == payload["chapter_plan"]
    assert result["reviewed_strengthening"]["owner_not_called"] is True
    assert owner_called == []


def test_reviewed_missing_status_or_issues_is_not_silent_no_change():
    payload = _payload()

    def reviewer(messages, **kwargs):
        return {
            "content": json.dumps({}), "complete": True,
            "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }

    result = strengthening.run_reviewed_strengthening(
        payload,
        reviewer_client=reviewer, reviewer_model="qwen3.5-plus",
        reviewer_thinking_budget=32768, reviewer_max_output_tokens=32768,
        owner_client=lambda *args, **kwargs: {}, owner_model="qwen3.5-plus",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "unresolved"
    assert "review_status_required" in result["structural_errors"]


def test_reviewed_strengthening_rejects_fabricated_reviewer_evidence_without_calling_owner():
    payload = _payload()
    owner_called = []

    def reviewer(messages, **kwargs):
        return {
            "content": json.dumps({
                "status": "reviewed",
                "issues": [{
                    "issue_id": "I-fake",
                    "affected_unit_ids": ["CH01_U01"],
                    "description": "A purported improvement.",
                    "evidence_handles": ["P9999"],
                    "reason_to_change": "The fabricated source would be useful.",
                }],
            }),
            "complete": True, "finish_reason": "stop",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def owner(messages, **kwargs):
        owner_called.append(True)
        return {}

    result = strengthening.run_reviewed_strengthening(
        payload,
        reviewer_client=reviewer, reviewer_model="qwen3.5-plus",
        reviewer_thinking_budget=32768, reviewer_max_output_tokens=32768,
        owner_client=owner, owner_model="qwen3.5-plus",
        owner_thinking_budget=32768, owner_max_output_tokens=32768,
    )
    assert result["status"] == "unresolved"
    assert "review_issue_evidence_invalid:I-fake" in result["structural_errors"]
    assert owner_called == []
