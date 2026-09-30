"""Manual offline fixtures: contract/lifecycle coverage, not model-quality claims."""
import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, ProgressivePlanError,
    PARTS_PLAN_SCHEMA_VERSION, _planner_instructions,
)


def parts(tag="initial", mode="standalone"):
    return {"context": "Tutorial for readers who know elementary probability", **{
        name: {"purpose": f"{tag}: explain {name} responsibility for conditional uncertainty",
               "focus": ["Which assumptions allow posterior uncertainty to inform decisions?"],
               "boundary": ["CH01 develops the mathematical derivation; this part positions its assumptions"],
               "placement": {"mode": mode if name == "conclusion" else "standalone", "anchor": "CH02 Outlook" if name == "conclusion" else name},
               "finalize_from": ["actual BODY", "review_argument", "material evidence"]}
        for name in ("abstract", "introduction", "conclusion")}}


class OfflinePlanner:
    def __init__(self, update=False):
        self.calls = []
        self.update = update
        self.outline = [{"chapter_id": "CH01", "title": "Introduction: mathematical tutorial", "purpose": "Derive a posterior with assumptions", "scope": "Deep mathematical teaching"},
                        {"chapter_id": "CH02", "title": "Outlook", "purpose": "Compare deployment limitations", "scope": "Evidence-backed technical prospects"}]

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "provisional_scope":
            return {"review_title": "Conditional uncertainty", "central_question": "What makes uncertainty useful?", "material_theme_inventory": ["assumptions", "deployment"], "provisional_outline": self.outline, "manuscript_parts_plan": parts("v0"), "supplement_requests": [], "directed_reads": []}
        if stage == "level1_outline":
            return {"shared_outline": self.outline, "shared_scope": {"scope": "posterior assumptions"}, "review_argument": "Use uncertainty only within validated assumptions", "manuscript_parts_plan": parts("v1", "embedded")}
        if stage == "source_routing":
            return {"source_routes": [{"source_handle": r["source_handle"], "chapter_ids": ["CH01", "CH02"], "specific_usable_material": "The source explains posterior assumptions and deployment caveats"} for r in payload["candidate_batch"]]}
        if stage == "chapter_proposals":
            return {"chapter_proposals": [payload["chapter"]]}
        if stage in {"harmonize_scope", "finalize_chapter_scope"}:
            return {"shared_outline": self.outline, "chapters": self.outline}
        if stage == "chapter_details":
            return {"chapter_plan": {"thesis": payload["chapter"]["purpose"], "reader_objective": "Explain conditional results", "units": [{"unit_id": "U01", "substantive_point": "Posterior validity depends on model assumptions", "source_handles": ["P0001"], "paragraph_briefs": [{"point": "State assumptions", "development": "Derive the posterior then explain applicability", "source_handles": ["P0001"]}]}]}}
        if stage == "case_groups":
            return {"additions": []}
        if stage == "whole_plan_improvement":
            return {"manuscript_parts_plan_status": "updated" if self.update else "no_change", **({"manuscript_parts_plan": parts("v2", "distributed")} if self.update else {})}
        if stage == "affected_chapter_revision":
            return {"status": "no_change"}
        raise AssertionError(stage)


def make(tmp_path, model=None, enabled=True):
    plan = tmp_path / "PLAN.json"
    pool = tmp_path / "pool.jsonl"
    if not plan.exists():
        plan.write_text(json.dumps({"question": "How should uncertainty guide decisions?", "facets": [{"facet_id": "F1", "question": "Which assumptions?"}]}))
        pool.write_text(json.dumps({"paper_id": "paper1", "title": "Posterior foundations", "summary_view": {"finding": "conditional mathematical result"}, "planning_view": {"planning_summary": "derive posterior assumptions"}}) + "\n")
    model = model or OfflinePlanner()
    config = ProgressivePlannerConfig(topic_id="fixture", plan_path=plan, pool_path=pool, output_dir=tmp_path / "out", chapter_workers=1, planning_revision_enabled=enabled)
    return ProgressiveReviewPlanner(config, planner=model), model


def test_full_flow_preserves_body_and_embedded_closure(tmp_path):
    planner, model = make(tmp_path)
    result = planner.run()
    assert result["schema_version"] == PARTS_PLAN_SCHEMA_VERSION
    assert result["manuscript_parts_plan"] == parts("v1", "embedded")
    assert result["manuscript_parts_plan_revision"] == "v2"
    assert result["manuscript_parts_plan_frozen"] is True
    assert result["review_argument"].startswith("Use uncertainty")
    assert [r["chapter_id"] for r in result["shared_outline"]] == ["CH01", "CH02"]
    assert len(result["writer_packets"]) == 2
    assert result["candidate_screening"]["unique_sources_assigned"] == 1
    assert all(r["chapter_plan"]["units"][0]["paragraph_briefs"] for r in result["chapters"])
    assert all(r["manuscript_parts_boundary"] == result["chapters"][0]["manuscript_parts_boundary"] for r in result["chapters"])
    calls = [s for s, _ in model.calls]
    assert calls.index("case_groups") < calls.index("whole_plan_improvement")
    assert calls.count("whole_plan_improvement") == 1
    for stage, payload in model.calls:
        assert payload["planning_revision_mode"] is True
        if stage not in {"provisional_scope", "level1_outline", "whole_plan_improvement"}:
            assert "manuscript_parts_plan" not in payload
            assert "manuscript_parts_boundary" in payload
    assert "文章级职责规划" in (tmp_path / "out/DETAILED_REVIEW_PLAN.md").read_text()


def test_updated_contract_and_resume_no_new_calls(tmp_path):
    planner, model = make(tmp_path, OfflinePlanner(update=True))
    result = planner.run()
    assert result["manuscript_parts_plan"] == parts("v2", "distributed")
    for packet in result["chapters"]:
        assert packet["manuscript_parts_boundary"]["introduction"]["purpose"].startswith("v2:")
        assert packet["planning_result_path"] == "../DETAILED_REVIEW_PLAN.json"
        saved = json.loads((tmp_path / "out/writer_packets" / (packet["chapter"]["chapter_id"] + ".json")).read_text())
        assert saved["manuscript_parts_boundary"] == packet["manuscript_parts_boundary"]
        assert saved["schema_version"] == PARTS_PLAN_SCHEMA_VERSION
    resumed, fresh_model = make(tmp_path, OfflinePlanner(update=True))
    again = resumed.run(resume=True)
    assert again["manuscript_parts_plan"] == result["manuscript_parts_plan"]
    assert fresh_model.calls == []


@pytest.mark.parametrize("stop_after", ["level1", "level2"])
def test_partial_resume_keeps_contract(tmp_path, stop_after):
    planner, _ = make(tmp_path)
    partial = planner.run(stop_after=stop_after)
    assert partial["manuscript_parts_plan_revision"] == "v1"
    assert partial["manuscript_parts_plan"] == parts("v1", "embedded")
    resumed, model = make(tmp_path)
    final = resumed.run(resume=True)
    assert final["manuscript_parts_plan_revision"] == "v2"
    assert not any(stage in {"provisional_scope", "level1_outline"} for stage, _ in model.calls)


def test_reject_legacy_resume(tmp_path):
    planner, _ = make(tmp_path)
    planner.run(stop_after="level1")
    path = tmp_path / "out/RUN_STATE.json"
    state = json.loads(path.read_text())
    state["schema_version"] = "optomind.progressive_review_plan.v1"
    path.write_text(json.dumps(state))
    with pytest.raises(ProgressivePlanError, match="resume_legacy"):
        planner.run(resume=True)


def test_reject_missing_state(tmp_path):
    planner, _ = make(tmp_path)
    with pytest.raises(ProgressivePlanError, match="resume_state_required"):
        planner.run(resume=True)


@pytest.mark.parametrize("failure", ["missing", "units", "bad_status", "patch", "contradiction"])
def test_invalid_parts_fail_closed(tmp_path, failure):
    base = OfflinePlanner()
    def model(stage, payload):
        value = base(stage, payload)
        if stage == "provisional_scope" and failure == "missing":
            value.pop("manuscript_parts_plan")
        if stage == "level1_outline" and failure == "units":
            value["manuscript_parts_plan"]["introduction"]["units"] = []
        if stage == "whole_plan_improvement":
            if failure == "bad_status":
                value = {}
            elif failure == "patch":
                value = {"manuscript_parts_plan_status": "updated", "manuscript_parts_plan": {"context": "patch only"}}
            elif failure == "contradiction":
                value["manuscript_parts_plan"] = parts("changed")
        return value
    planner, _ = make(tmp_path, model)
    with pytest.raises(ProgressivePlanError):
        planner.run()


def test_changed_cached_v1_boundary_invalidates_body_cache(tmp_path):
    planner, _ = make(tmp_path)
    planner.run()
    path = tmp_path / "out/stages/level1_outline.json"
    cached = json.loads(path.read_text())
    cached["response"]["manuscript_parts_plan"]["introduction"]["boundary"] = ["Changed teaching handoff to CH01"]
    path.write_text(json.dumps(cached))
    resumed, model = make(tmp_path)
    result = resumed.run(resume=True)
    assert any(stage == "chapter_details" for stage, _ in model.calls)
    assert any(stage == "chapter_proposals" for stage, _ in model.calls)
    assert any(stage == "whole_plan_improvement" for stage, _ in model.calls)
    assert result["manuscript_parts_plan"]["introduction"]["boundary"] == ["Changed teaching handoff to CH01"]


def test_new_prompt_removes_title_based_shallowness():
    new = _planner_instructions("chapter_details", planning_revision=True)
    old = _planner_instructions("chapter_details")
    assert "miniature full review" not in new
    assert "miniature full review" in old
    assert "regardless of publication heading" in new


@pytest.mark.parametrize("stage", ["provisional_scope", "level1_outline", "whole_plan_improvement"])
def test_live_planner_call_repeats_parts_contract_after_full_payload(tmp_path, monkeypatch, stage):
    captured = {}

    class StubQwenDirectClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

    def fake_counter(_tokenizer_path):
        return lambda _request_bytes, _messages: 100

    def fake_invoke(client, messages, **kwargs):
        captured["messages"] = messages
        captured["invoke_kwargs"] = kwargs
        return {"content": "{}", "complete": True, "finish_reason": "stop"}

    from optomind_research.runtime.upgrade3.module4 import runtime
    monkeypatch.setattr(planning, "qwen_local_token_counter", fake_counter)
    monkeypatch.setattr(runtime, "QwenDirectClient", StubQwenDirectClient)
    monkeypatch.setattr(runtime, "invoke_client", fake_invoke)

    planner = planning.QwenProgressivePlanner(
        model="qwen3.5-plus",
        key_file=tmp_path / "missing-key-file.txt",
        budget_ledger_path=tmp_path / "budget.sqlite",
        budget_limit_cny=1.0,
        output_dir=tmp_path / "planner-output",
        tokenizer_path=tmp_path / "unused-tokenizer.json",
    )
    planner(stage, {"planning_revision_mode": True, "topic_id": "fixture", "candidate_pool": []})

    user_message = captured["messages"][1]["content"]
    delivery = user_message[user_message.index("【本轮交付】"):]
    assert "manuscript_parts_plan" in delivery
    assert "context" in delivery and "abstract" in delivery
    assert "purpose" in delivery and "focus" in delivery
    assert "boundary" in delivery and "finalize_from" in delivery
    assert "standalone" in delivery and "embedded" in delivery and "distributed" in delivery
    assert captured["client_kwargs"]["model"] == "qwen3.5-plus"
    assert captured["invoke_kwargs"]["model"] == "qwen3.5-plus"
    if stage == "whole_plan_improvement":
        assert "manuscript_parts_plan_status" in delivery


def test_legacy_message_keeps_generic_delivery_tail():
    messages = planning._messages_for("provisional_scope", {"planning_revision_mode": False})
    assert messages[1]["content"].endswith(
        "【本轮交付】请用中文撰写本阶段要求的内容，只返回本阶段的JSON结果，不照抄输入字段。"
    )
    assert "manuscript_parts_plan" not in messages[1]["content"]


@pytest.mark.parametrize("stage", ["chapter_details", "affected_chapter_revision"])
def test_reject_partplan_as_body_plan(tmp_path, stage):
    from optomind_research.runtime.upgrade3.manuscript_parts import ManuscriptPartsError
    response = {"chapter_plan": parts()["introduction"]} if stage == "chapter_details" else {
        "chapter_updates": [{"chapter_id": "CH01", "updated_plan": parts()["conclusion"]}]}
    planner, _ = make(tmp_path, lambda *_: response)
    planner._parts_plan = parts()
    with pytest.raises(ManuscriptPartsError, match="part_plan_is_not_body_task"):
        planner._call(stage, {})


def test_legacy_mode_keeps_legacy_order_and_no_new_top_level_contract(tmp_path):
    planner, model = make(tmp_path, enabled=False)
    result = planner.run()
    assert result["schema_version"] == "optomind.progressive_review_plan.v1"
    assert "manuscript_parts_plan" not in result
    calls = [s for s, _ in model.calls]
    assert calls.index("whole_plan_improvement") < calls.index("case_groups")
    assert all("manuscript_parts_contract_version" not in payload for _, payload in model.calls)


def test_changed_prior_reading_invalidates_global_cache(tmp_path):
    planner, _ = make(tmp_path)
    planner.run(stop_after="level1")
    resumed, model = make(tmp_path)
    resumed.prior_readings = [{"paper_id": "paper1", "finding": "New conditional assumption"}]
    resumed.run(resume=True, stop_after="level1")
    assert [s for s, _ in model.calls] == ["provisional_scope", "level1_outline"]
