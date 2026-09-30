"""Manual offline fixtures: contract/lifecycle coverage, not model-quality claims."""
import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import planning_retrieval_loop as retrieval_loop
from optomind_research.runtime.upgrade3.progressive_review_plan import (
    ProgressivePlannerConfig, ProgressiveReviewPlanner, ProgressivePlanError,
    PARTS_PLAN_SCHEMA_VERSION, PARTS_CONTRACT_VERSION, RETRIEVAL_STAGE_CONTRACT,
    LOCAL_SOURCE_IDENTITY_CONTRACT,
    LEVEL1_OUTLINE_PROMPT_CONTRACT, _planner_instructions, make_retrieval_loop_runner,
    _carry_gap_query_fields, _normalize_gaps,
)
from optomind_research.runtime.upgrade3.planning_retrieval_loop import (
    InformationNeed, LoopConfig, run_retrieval_loop,
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


def _retrieval_fixture(tmp_path, *, max_rounds=1):
    return LoopConfig(
        index_path=tmp_path / "missing-index.sqlite",
        journal_path=tmp_path / "retrieval_loop.jsonl",
        max_rounds=max_rounds,
        max_empty_rounds=2,
        max_queries_per_round=3,
        shared_deep_read_budget=40,
    )


def _external_stub(calls):
    def external(*, need, queries, **_kwargs):
        calls.append((need.need_id, [dict(item) for item in queries]))
        return {"status": "fulfilled", "usable_content": f"material for {need.need_id}"}
    return external


def test_factory_runner_passes_revision_flag_to_loop(tmp_path, monkeypatch):
    flags = []

    def fake_loop(*_args, **kwargs):
        flags.append(kwargs["refine_empty_first_round"])
        return {"needs": []}

    monkeypatch.setattr(retrieval_loop, "run_retrieval_loop", fake_loop)
    for enabled in (False, True):
        config = ProgressivePlannerConfig(
            topic_id=f"factory-{enabled}", plan_path=tmp_path / f"{enabled}-PLAN.json",
            pool_path=tmp_path / f"{enabled}-POOL.jsonl", output_dir=tmp_path / str(enabled),
            planning_revision_enabled=enabled,
        )
        runner = make_retrieval_loop_runner(config, allow_external=False)
        runner(
            phase="chapters", supplement_requests=[{"gap_id": "G", "gap_question": "specific gap"}],
            pool_rows=[], plan={"question": "review scope"}, source_handle_map={}, resume=False,
        )
    assert flags == [False, True]


def test_legacy_harmonized_response_without_supplement_field_stays_absent(tmp_path):
    planner, _ = make(tmp_path, enabled=False)
    observed = []
    original = planner._harmonized_chapters

    def capture(harmonized, proposals):
        observed.append(dict(harmonized))
        return original(harmonized, proposals)

    planner._harmonized_chapters = capture
    planner.run()
    assert observed
    assert "supplement_requests" not in observed[0]


def test_queryless_archived_chapter_gaps_refine_once_and_resume_without_repay(tmp_path):
    """The seven archived chapter gaps are recoverable without rerunning planning."""
    ids = [
        "N25ad9e23fde4", "Nfa6f3ab1f972", "Nc19e59f8d065",
        "N0d2029cb5b49", "N8855db84289c", "N9ae5308936be", "Naf87f3d23fd8",
    ]
    needs = [
        InformationNeed(need_id=need_id, question=f"archived chapter gap {need_id}", owners=("CH02",))
        for need_id in ids
    ]
    local = lambda **_kwargs: {"decision": "external_research", "still_missing": "missing evidence"}
    external_calls = []
    # This recreates the old cached run: the gaps reached the loop with no
    # query and were recorded as stopped_no_query.
    first = run_retrieval_loop(
        needs, _retrieval_fixture(tmp_path), local_triage=local,
        external_closure=_external_stub(external_calls), resume=True,
    )
    assert all(row["status"] == "stopped" for row in first["needs"])
    assert external_calls == []

    refine_calls = []

    def refine(need, round_index, previous_queries, usable_content, still_missing):
        refine_calls.append((need.need_id, round_index, list(previous_queries), still_missing))
        return [{"query_type": "keyword", "query_text": f"specific query {need.need_id}"}]

    second = run_retrieval_loop(
        needs, _retrieval_fixture(tmp_path), local_triage=local,
        refine_queries=refine, refine_empty_first_round=True,
        external_closure=_external_stub(external_calls), resume=True,
    )
    assert len(refine_calls) == 7
    assert len(external_calls) == 7
    assert all(row["status"] == "answered" for row in second["needs"])
    assert all(call[1][0]["query_text"].startswith("specific query N") for call in external_calls)

    # A resume sees the generated query and the completed result in the same
    # journal; it must not call the refiner or external closure again.
    third = run_retrieval_loop(
        needs, _retrieval_fixture(tmp_path), local_triage=local,
        refine_queries=refine, refine_empty_first_round=True,
        external_closure=_external_stub(external_calls), resume=True,
    )
    assert len(refine_calls) == 7
    assert len(external_calls) == 7
    assert all(row["status"] == "answered" for row in third["needs"])


def test_empty_first_round_refinement_is_persisted_and_not_repaid(tmp_path):
    need = InformationNeed(need_id="Nempty", question="a genuinely unresolved gap")
    refine_calls = []

    def refine(*args):
        refine_calls.append(args[0].need_id)
        return []

    local = lambda **_kwargs: {"decision": "external_research", "still_missing": "still missing"}
    first = run_retrieval_loop(
        [need], _retrieval_fixture(tmp_path, max_rounds=3), local_triage=local,
        refine_queries=refine, refine_empty_first_round=True, resume=True,
    )
    assert first["needs"][0]["status"] == "stopped"
    assert refine_calls == ["Nempty"]
    second = run_retrieval_loop(
        [need], _retrieval_fixture(tmp_path, max_rounds=3), local_triage=local,
        refine_queries=refine, refine_empty_first_round=True, resume=True,
    )
    assert second["needs"][0]["status"] in {"stopped", "stopped_no_query"}
    assert refine_calls == ["Nempty"]


def test_initialized_query_checkpoint_precedes_interrupted_external_and_resume(tmp_path):
    need = InformationNeed(need_id="Ninterrupt", question="an interrupted external gap")
    config = _retrieval_fixture(tmp_path, max_rounds=1)
    config.max_provider_retries = 0
    refine_calls = []
    observed = []

    def refine(need, round_index, previous_queries, usable_content, still_missing):
        refine_calls.append(need.need_id)
        return [{"query_type": "keyword", "query_text": "initialized query"}]

    def interrupted_external(*, queries, **_kwargs):
        entries = [json.loads(line) for line in config.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        observed.append(entries[-1])
        raise RuntimeError("simulated interruption")

    local = lambda **_kwargs: {"decision": "external_research", "still_missing": "missing"}
    run_retrieval_loop(
        [need], config, local_triage=local, refine_queries=refine,
        refine_empty_first_round=True, external_closure=interrupted_external, resume=True,
    )
    assert refine_calls == ["Ninterrupt"]
    assert observed[-1]["status"] == "initialized_queries"
    assert observed[-1]["counts_as_round"] is False
    assert observed[-1]["queries"][0]["query_text"] == "initialized query"

    resumed_external = []

    def resumed_external_call(*, queries, **_kwargs):
        resumed_external.append(list(queries))
        return {"status": "fulfilled", "usable_content": "recovered material"}

    run_retrieval_loop(
        [need], config, local_triage=local,
        refine_queries=lambda *args: (_ for _ in ()).throw(AssertionError("refinement repaid")),
        refine_empty_first_round=True, external_closure=resumed_external_call, resume=True,
    )
    assert resumed_external == [[{"query_text": "initialized query", "query_type": "keyword", "facet_id": ""}]]
    assert refine_calls == ["Ninterrupt"]


@pytest.mark.parametrize(
    "need_id,supplied",
    [
        (
            "Nd68c6827caa3",
            [
                {"query_type": "keyword", "query_text": "inosine tissue concentration", "facet_id": "F1"},
                {"query_type": "question", "query_text": "How does tissue inosine relate to ICI response?", "facet_id": "F1"},
            ],
        ),
        (
            "Nb46ff4756f67",
            [
                {"query_type": "keyword", "query_text": "FMT immune adverse events regimen", "facet_id": "F1"},
                {"query_type": "question", "query_text": "How do FMT adverse events differ by ICI regimen?", "facet_id": "F1"},
            ],
        ),
        (
            "N2c6c12ab7c7b",
            [
                {"query_type": "keyword", "query_text": "phase III microbiome intervention ICI trial", "facet_id": "F1"},
                {"query_type": "question", "query_text": "Which phase III microbiome ICI trials are registered?", "facet_id": "F1"},
            ],
        ),
    ],
)
def test_supplied_round_queries_are_preserved_and_skip_refinement(tmp_path, need_id, supplied):
    need = InformationNeed(
        need_id=need_id, question="supplied query gap", concepts=tuple(row["query_text"] for row in supplied),
        round_specs=({"round": 1, "targeted_queries": supplied},),
    )
    refine_calls = []
    external_calls = []
    result = run_retrieval_loop(
        [need], _retrieval_fixture(tmp_path),
        local_triage=lambda **_kwargs: {"decision": "external_research", "still_missing": "missing"},
        refine_queries=lambda *args: refine_calls.append(args) or [],
        external_closure=_external_stub(external_calls), resume=True,
    )
    assert result["needs"][0]["status"] == "answered"
    assert refine_calls == []
    assert external_calls == [(need_id, supplied)]


def test_gap_queries_carry_only_by_explicit_identity():
    supplied = [{"query_type": "keyword", "query_text": "object relation", "facet_id": "F1"}]
    replacement = [{"query_type": "keyword", "query_text": "new object relation", "facet_id": "F1"}]
    rows = [
        {"gap_id": "same-gap", "gap_question": "rewritten question"},
        {"gap_id": "different-gap", "gap_question": "same words do not establish identity"},
        {"gap_id": "replacement-gap", "gap_question": "new query wins",
         "targeted_queries": replacement},
    ]
    carried = _carry_gap_query_fields(
        rows,
        [
            {"gap_id": "same-gap", "targeted_queries": supplied,
             "round_specs": [{"round": 1, "targeted_queries": supplied}]},
            {"gap_id": "replacement-gap", "targeted_queries": supplied,
             "round_specs": [{"round": 1, "targeted_queries": supplied}]},
        ],
    )
    normalized = _normalize_gaps(carried)
    assert normalized[0]["targeted_queries"] == supplied
    assert normalized[0]["round_specs"] == [{"round": 1, "targeted_queries": supplied}]
    assert normalized[1]["targeted_queries"] == []
    assert normalized[1]["round_specs"] == []
    assert normalized[2]["targeted_queries"] == replacement
    assert normalized[2]["round_specs"] == [{"round": 1, "targeted_queries": replacement}]


def test_chapter_query_contract_is_revision_only():
    legacy = _planner_instructions("chapter_proposals", planning_revision=False)
    revised = _planner_instructions("chapter_proposals", planning_revision=True)
    assert "Every supplement_requests entry must include gap_id" not in legacy
    assert "Every supplement_requests entry must include gap_id" in revised


def test_revision_tool_cache_contract_reenters_downstream_loop_only(tmp_path):
    calls = []

    def retrieval_runner(**kwargs):
        calls.append(kwargs["phase"])
        return {"tool_materials_by_chapter": {}}

    config = ProgressivePlannerConfig(
        topic_id="fixture", plan_path=tmp_path / "PLAN.json", pool_path=tmp_path / "POOL.jsonl",
        output_dir=tmp_path / "out", planning_revision_enabled=True,
    )
    planner = ProgressiveReviewPlanner(config, planner=lambda *_args: {}, retrieval_loop_runner=retrieval_runner)
    planner._run_input_signature = "fixture-input"
    planner._parts_plan = {}
    state = {}
    planner._tool_cycle(
        phase="level2", supplement_requests=[], directed_requests=[], pool_rows=[], plan={},
        prior_directed=None, prior_tool_results=None, source_handle_map={}, resume=False, state=state,
    )
    assert calls == ["level2"]
    saved_state = json.loads((tmp_path / "out/RUN_STATE.json").read_text(encoding="utf-8"))
    stage_input = saved_state["stage_inputs"]["level2_tools"]
    assert stage_input["stage_inputs"]["retrieval_loop_contract"] == RETRIEVAL_STAGE_CONTRACT
    assert stage_input["stage_inputs"]["local_source_identity_contract"] == LOCAL_SOURCE_IDENTITY_CONTRACT
    stage_input["stage_inputs"].pop("retrieval_loop_contract")
    (tmp_path / "out/RUN_STATE.json").write_text(json.dumps(saved_state), encoding="utf-8")
    planner._tool_cycle(
        phase="level2", supplement_requests=[], directed_requests=[], pool_rows=[], plan={},
        prior_directed=None, prior_tool_results=None, source_handle_map={}, resume=True, state=saved_state,
    )
    assert calls == ["level2", "level2"]
    assert stage_input["contract"] == PARTS_CONTRACT_VERSION


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
    planner(stage, {
        "planning_revision_mode": True,
        "topic_id": "fixture",
        "candidate_pool": [],
        "original_plan": {"question": "A material-led question", "facets": [{"facet_id": "F1", "question": "A facet"}]},
        "upstream_material_theme_inventory": ["an upstream material theme"],
        "global_material_authority": {
            "upstream_complete_pool_row_count": 0,
            "upstream_material_summary_is_material_base": True,
            "tool_results_are_bounded_local_feedback": True,
            "organizing_claims_must_be_material_driven": True,
        },
    })

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
    if stage == "level1_outline":
        assert "原始研究计划、上游主题库存和已供上游阶段筛选的完整材料池是全局材料依据" in delivery
        assert "工具结果只是受影响章节的局部反馈" in delivery
        assert "不得凭空添加证据等级、A/B/C 分级或证据层级" in delivery
        assert "本阶段所有说明性文本使用中文" in delivery


def test_level1_prompt_contract_only_invalidates_level1_cache(tmp_path):
    planner, model = make(tmp_path)
    planner.run(stop_after="level1")

    level1_payload = next(payload for stage, payload in model.calls if stage == "level1_outline")
    assert level1_payload["original_plan"]["facets"]
    assert level1_payload["upstream_material_theme_inventory"] == ["assumptions", "deployment"]
    assert level1_payload["global_material_authority"]["tool_results_are_bounded_local_feedback"] is True

    state_path = tmp_path / "out/RUN_STATE.json"
    state = json.loads(state_path.read_text())
    level1_inputs = state["stage_inputs"]["level1_outline"]["stage_inputs"]
    assert level1_inputs["prompt_contract"] == LEVEL1_OUTLINE_PROMPT_CONTRACT
    level1_tool_inputs = state["stage_inputs"]["level1_tools"]["stage_inputs"]
    assert level1_tool_inputs is None or "prompt_contract" not in level1_tool_inputs
    # Model the cache written by the previous prompt contract: only level1
    # loses its contract marker, while provisional/tools remain reusable.
    level1_inputs.pop("prompt_contract")
    state_path.write_text(json.dumps(state))

    resumed, model = make(tmp_path)
    resumed.run(resume=True, stop_after="level1")
    assert [stage for stage, _ in model.calls] == ["level1_outline"]

    unchanged, fresh_model = make(tmp_path)
    unchanged.run(resume=True, stop_after="level1")
    assert fresh_model.calls == []


def test_legacy_level1_payload_keeps_original_shape(tmp_path):
    planner, model = make(tmp_path, enabled=False)
    planner.run(stop_after="level1")
    level1_payload = next(payload for stage, payload in model.calls if stage == "level1_outline")
    assert set(level1_payload) == {
        "topic_id", "research_question", "provisional_scope", "actual_tool_results",
        "full_b_pool_was_semantically_screened", "unavailable_and_failed_tools_are_scope_feedback",
    }


@pytest.mark.parametrize("stage", ["provisional_scope", "level1_outline"])
def test_legacy_message_keeps_generic_delivery_tail(stage):
    messages = planning._messages_for(stage, {"planning_revision_mode": False})
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
