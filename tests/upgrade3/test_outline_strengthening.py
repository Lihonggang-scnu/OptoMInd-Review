"""Offline contracts for the generic autonomous outline strengthening seam."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
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
    assert "source_supplied_locators" in owner_user
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
