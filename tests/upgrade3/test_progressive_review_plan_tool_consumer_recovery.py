"""Formal F2/F3 consumers: real SQLite, collector, caches, stage files/messages.

Only model construction/invocation, tokenizer and network boundaries are replaced.
All content is synthetic engineering material; no provider or scientific QA claim.
"""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import planning_material_search as search
from optomind_research.runtime.upgrade3.module4 import runtime
from test_body03_local_lookup_contract import ModelBoundary, make_fixture, request, material_text, read_json


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Consumer recovery is offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def engine_for(f, *, model=None, external=None):
    runner = planning.make_retrieval_loop_runner(
        f.config, key_file=f.config.output_dir / "NEVER_READ.key",
        budget_ledger_path=f.config.output_dir / "offline-ledger.sqlite", budget_limit_cny=1,
        local_index_path=f.index_path, allow_external=external is not None, supplement_runner=external)
    return planning.ProgressiveReviewPlanner(f.config, planner=model or (lambda *_: {}),
                                             retrieval_loop_runner=runner)


def cycle(engine, f, gap, state):
    return engine._tool_cycle(phase="level1", supplement_requests=[gap], directed_requests=[],
        pool_rows=f.pool, plan=f.plan, prior_directed=None, prior_tool_results=None,
        source_handle_map=f.handle_map, resume=True, state=state)


def need(result):
    return result["retrieval_loop"]["needs"][0]


def evidence(root, name, **values):
    planning._atomic_json(root / (name + ".json"), values)


def test_fulfilled_resume_reenters_collector_without_repaying(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    boundary = ModelBoundary(monkeypatch, f.domain)
    engine, state = engine_for(f), {}
    first = cycle(engine, f, request(f), state)
    attempts = {str(p): p.read_bytes() for p in (f.config.output_dir / "local_lookup").rglob("*.json")}
    # New planner/runner and state loaded from disk, as in a real restart.
    second = cycle(engine_for(f), f, request(f), read_json(f.config.output_dir / "RUN_STATE.json"))
    evidence(tmp_path, "fulfilled", first=first, second=second, calls=boundary.calls)
    assert len(boundary.calls) == 1
    assert need(first)["status"] == need(second)["status"] == "answered"
    assert need(second)["reused_answer"] is True
    assert read_json(f.config.output_dir / "stages/level1_tools.json") == second
    assert all(Path(p).read_bytes() == data for p, data in attempts.items())


def test_failed_explicit_resume_preserves_partial_and_retries_only_failed_attempt(tmp_path, monkeypatch):
    f = make_fixture(tmp_path)
    partial = {"decision": "external_research", "answers_requested_question": False,
        "usable_content": "USEFUL initial partial measurement.", "still_missing": "Missing boundary."}
    boundary = ModelBoundary(monkeypatch, f.domain, answers=[partial, TimeoutError("first outage"),
                                                          TimeoutError("second outage")])
    engine, state = engine_for(f), {}
    first = cycle(engine, f, request(f), state)
    saved = {str(p): p.read_bytes() for p in (f.config.output_dir / "local_lookup").rglob("*.json")}
    unchanged = cycle(engine, f, request(f), state)
    assert len(boundary.calls) == 2  # Ordinary resume does not retry a paid failure.
    explicit = {**request(f), "retry_empty_result": True}
    failed_retry = cycle(engine, f, explicit, state)
    assert len(boundary.calls) == 3
    # Same explicit retry intent on a later invocation must reach the collector.
    recovered = cycle(engine_for(f), f, explicit, read_json(f.config.output_dir / "RUN_STATE.json"))
    evidence(tmp_path, "explicit_retry", first=first, unchanged=unchanged, failed_retry=failed_retry,
             recovered=recovered, calls=boundary.calls)
    assert len(boundary.calls) == 4
    assert need(recovered)["status"] == "answered"
    assert need(recovered)["still_missing"] == ""
    assert "USEFUL initial partial measurement." in need(recovered)["usable_content"]
    assert f.domain["answer"] in need(recovered)["usable_content"]
    assert all(Path(p).read_bytes() == data for p, data in saved.items())
    attempts = list((f.config.output_dir / "local_lookup").rglob("attempt-*"))
    assert sum((p / "FAILED.json").is_file() for p in attempts) == 2
    assert read_json(f.config.output_dir / "stages/level1_tools.json") == recovered
    after_success = cycle(engine_for(f), f, explicit, read_json(f.config.output_dir / "RUN_STATE.json"))
    assert len(boundary.calls) == 4 and need(after_success)["status"] == "answered"


def test_same_question_new_output_reaches_actual_collector(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    boundary = ModelBoundary(monkeypatch, f.domain)
    engine, state = engine_for(f), {}
    first = cycle(engine, f, request(f), state)
    second = cycle(engine, f, request(f, outputs=("measured boundary", "new cyclic limitation")), state)
    third = cycle(engine, f, request(f, outputs=("measured boundary", "new cyclic limitation")), state)
    evidence(tmp_path, "new_output", first=first, second=second, third=third, calls=boundary.calls)
    assert len(boundary.calls) == 2
    assert "new cyclic limitation" in json.dumps(boundary.payloads[-1]["success_criteria"])
    assert "measured boundary" not in json.dumps(boundary.payloads[-1]["success_criteria"])
    assert need(first)["reuse_key"] != need(second)["reuse_key"]
    assert need(third)["reused_answer"] is True


def test_real_index_content_change_reaches_actual_collector_and_messages(tmp_path, monkeypatch):
    f = make_fixture(tmp_path, distractors=0)
    boundary = ModelBoundary(monkeypatch, f.domain)
    engine, state = engine_for(f), {}
    first = cycle(engine, f, request(f), state)
    changed_answer = "SYNTHETIC_UPDATED_RESULT: Cycling at 700 C produced earlier cracks in the comparison coupon."
    # Keep the outer pool and plan byte-for-byte unchanged; update actual SQLite material.
    with search.PlanningMaterialIndex(f.index_path) as index:
        index.bulk_replace_paper(f.target_id, [{"segment_kind": "document_block", "ordinal": 0,
            "section_path": ["Results"], "text": f.domain["query"] + ". " + changed_answer}])
        index.commit()
        boundary.answers.append({"decision": "direct_use", "answers_requested_question": True,
            "usable_content": changed_answer, "still_missing": ""})
        second = cycle(engine, f, request(f), state)
    evidence(tmp_path, "index_changed", first=first, second=second, calls=boundary.calls)
    assert len(boundary.calls) == 2
    assert changed_answer in material_text(boundary.payloads[-1])
    assert changed_answer in need(second)["usable_content"]
    assert need(first)["need_id"] != need(second)["need_id"]
    assert read_json(f.config.output_dir / "stages/level1_tools.json") == second


@pytest.mark.parametrize("mode", ["answered", "failed", "external"])
def test_local_feedback_enters_actual_next_outline_messages(tmp_path, monkeypatch, mode):
    failed = mode == "failed"
    partial = mode != "answered"
    f = make_fixture(tmp_path, distractors=6 if failed else 0)
    f.plan["facets"] = [{"id": "F1", "keyword_queries": [f.domain["query"]]}]
    planning._atomic_json(f.config.plan_path, f.plan)
    answer = "LOCAL_PARTIAL_ANSWER: A porous interlayer delayed crack initiation in the measured coupon."
    local_response = {"decision": "external_research" if partial else "direct_use",
        "answers_requested_question": not partial, "usable_content": answer,
        "still_missing": "Unmeasured cyclic boundary remains unresolved." if partial else "",
        "quantitative_comparisons": [{"research_object": "Synthetic coupon", "setting_or_time": "600 C",
            "result_or_measure": "crack onset", "groups": []}]}
    boundary = ModelBoundary(monkeypatch, f.domain,
        answers=[local_response, TimeoutError("bounded lookup outage")] if failed else [local_response])
    original_invoke = runtime.invoke_client
    planner_calls = []
    def invoke(client, messages, **kwargs):
        call_id = kwargs.get("call_id", "")
        if call_id.startswith("progressive-review:"):
            stage = call_id.split(":")[1]
            planner_calls.append({"stage": stage, "messages": copy.deepcopy(messages)})
            response = ({"supplement_requests": [request(f)], "provisional_outline": [
                {"chapter_id": "CH01", "title": "Measured boundary"}]} if stage == "provisional_scope" else
                {"shared_outline": {"chapters": [{"chapter_id": "CH01", "title": "Measured boundary"}]}})
            return {"content": json.dumps(response)}
        return original_invoke(client, messages, **kwargs)
    monkeypatch.setattr(runtime, "invoke_client", invoke)
    monkeypatch.setattr(planning, "qwen_local_token_counter", lambda _: lambda *_: 100)
    model = planning.QwenProgressivePlanner(model="offline", key_file=tmp_path / "NEVER_READ.key",
        budget_ledger_path=tmp_path / "ledger.sqlite", budget_limit_cny=1, output_dir=f.config.output_dir)
    external = (lambda *a, **kw: {"status": "fulfilled", "usable_content": "NEW measured cyclic boundary."}) if mode == "external" else None
    engine = engine_for(f, model=model, external=external)
    result = engine.run(resume=True, stop_after="level1")
    full = read_json(f.config.output_dir / "stages/level1_tools.json")
    next_call = next(row for row in planner_calls if row["stage"] == "level1_outline")
    content = next_call["messages"][-1]["content"]
    payload = json.JSONDecoder().raw_decode(content.split("\n", 1)[1])[0]
    evidence(tmp_path, "next_outline", result=result, full=full, calls=planner_calls, local_calls=boundary.calls)
    assert full["directed_results"] == []
    assert bool(full["supplement_results"]) == (mode == "external")
    compact = payload["actual_tool_results"]
    material = compact["tool_materials_by_chapter"]["CH01"][0]
    assert answer in material["usable_content"]
    assert "Synthetic coupon" in material["conditions"] and "600 C" in material["conditions"]
    assert material["sources"] and all(row["paper_id"] and row["source_handle"] for row in material["sources"])
    assert compact["retrieval_loop"]["needs"][0]["status"] == need(full)["status"]
    assert "candidate_pool" not in payload and "material_found" not in json.dumps(compact)
    assert "local_reading" not in json.dumps(compact) and "attempts" not in json.dumps(compact)
    assert "card_path" not in json.dumps(compact) and "reading_path" not in json.dumps(compact)
    if failed:
        assert material["provider_failed"] is True
        assert material["status"] == "failed" and material["error"] == "TimeoutError: bounded lookup outage"
        assert "Unmeasured cyclic boundary remains unresolved." in material["limits"]
        assert material["still_missing"] == "Unmeasured cyclic boundary remains unresolved."
    else:
        assert material["decision"] == ("external_research" if mode == "external" else "answer_from_local")
        assert material["still_missing"] == ""
        assert material["limits"] == []
        if mode == "external":
            assert "NEW measured cyclic boundary." in material["usable_content"]
    state_payload = read_json(f.config.output_dir / "RUN_STATE.json")["stage_inputs"]["level1_outline"]
    assert state_payload == payload


def external_engine(tmp_path, outcomes):
    """Replace only the external acquisition/provider boundary, not collection."""
    calls, messages = [], []
    def external(requests, **context):
        calls.append(copy.deepcopy(requests))
        return copy.deepcopy(outcomes[min(len(calls) - 1, len(outcomes) - 1)])
    def model(stage, payload):
        messages.append({"stage": stage, "messages": planning._messages_for(stage, payload)})
        return {"chapter_plan": {"thesis": "Explain the condition", "units": [{
            "substantive_point": "Measured condition", "paragraph_briefs": [
                {"point": "Condition", "development": "Use measured finding."}]}]}}
    config = planning.ProgressivePlannerConfig("terminal-retry", tmp_path / "POOL.jsonl",
        tmp_path / "PLAN.json", tmp_path / "run", chapter_workers=1)
    runner = planning.make_retrieval_loop_runner(config, allow_external=True, supplement_runner=external)
    engine = planning.ProgressiveReviewPlanner(config, planner=model, retrieval_loop_runner=runner)
    args = dict(supplement_requests=[{"gap_id": "G1", "gap_question": "Which temperature changes the mechanism?",
        "chapter_ids": ["CH01"], "required_outputs": ["boundary"], "targeted_queries": [
            {"query_type": "keyword", "query_text": "temperature mechanism"}]}],
        directed_requests=[], plan={"research_question": "Temperature mechanism"}, pool_rows=[],
        source_handle_map={"P0001": "paper1"}, resume=True, state={}, prior_tool_results=None, prior_directed=None)
    return engine, args, calls, messages


def external_answer(status, content="", missing=""):
    return {"status": status, "usable_content": content, "still_missing": missing,
        "source_units": [{"record_identity": {"paper_id": "paper1", "source_handle": "P0001",
            "title": "Review-derived experiment", "doi": "10.synthetic/forwarded"},
            "material_depth": "review_derived"}]}


@pytest.mark.parametrize("partial", [False, True])
def test_terminal_explicit_retry_reuses_direction_keeps_journal_and_is_bounded(tmp_path, partial):
    old = "OLD useful thermal finding." if partial else ""
    engine, args, calls, _ = external_engine(tmp_path, [
        external_answer("partial" if partial else "unmet", old, "Missing boundary."),
        external_answer("partial", "RETRY useful comparison.", "Still missing boundary."),
        external_answer("fulfilled", "NEW boundary measured at 60 C.")])
    first = engine._tool_cycle(phase="level1", **args)
    journal = engine.config.output_dir / "level1/retrieval_loop.jsonl"
    old_journal = journal.read_bytes()
    assert len(calls) == 1 and need(first)["action"] == "stop"
    engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 1
    args["supplement_requests"][0]["retry_empty_result"] = True
    second = engine._tool_cycle(phase="level1", **args)
    evidence(tmp_path, "terminal_retry_partial", first=first, second=second, calls=calls)
    assert len(calls) == 2  # One explicit attempt, without opening new research rounds.
    assert need(second)["action"] == "stop" and need(second)["status"] == "partial"
    assert need(second)["still_missing"] == "Still missing boundary."
    assert old in need(second)["usable_content"] and "RETRY useful comparison." in need(second)["usable_content"]
    assert journal.read_bytes().startswith(old_journal)
    assert calls[0][0]["targeted_queries"] == calls[1][0]["targeted_queries"]
    args["supplement_requests"][0].pop("retry_empty_result")
    ordinary = engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 2 and need(ordinary)["action"] == "stop"
    args["supplement_requests"][0]["retry_empty_result"] = True
    recovered = engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 3 and need(recovered)["status"] == "answered"
    assert "RETRY useful comparison." in need(recovered)["usable_content"]
    assert need(recovered)["still_missing"] == ""
    engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 3
    evidence(tmp_path, "terminal_retry_complete", recovered=recovered, calls=calls,
             journal=journal.read_text())


def test_recovered_same_need_supersedes_obsolete_gap_in_actual_chapter_messages(tmp_path):
    engine, args, calls, messages = external_engine(tmp_path, [
        external_answer("partial", "OLD useful thermal finding.", "STALE_MISSING boundary."),
        external_answer("fulfilled", "NEW boundary measured at 60 C.")])
    first = engine._tool_cycle(phase="level1", **args)
    # Keep a genuinely different need active; exact-need replacement must not erase it.
    engine.tool_materials_by_chapter["CH01"].append({"need_id": "different-acceptance",
        "usable_content": "OTHER question context.", "still_missing": "OTHER unmet requirement."})
    second = engine._tool_cycle(phase="level2", **args)
    assert need(first)["need_id"] == need(second)["need_id"]
    engine._chapter_details(chapters=[{"chapter_id": "CH01", "title": "Conditions", "source_ids": []}],
        shared_outline={}, topic="Mechanism conditions", candidates={}, level1_tools={}, level2_tools={},
        resume=False, state={}, tool_materials_by_chapter=engine.tool_materials_by_chapter)
    evidence(tmp_path, "recovered_chapter", first=first, second=second,
        active=engine.tool_materials_by_chapter, messages=messages, calls=calls)
    active = engine.tool_materials_by_chapter["CH01"]
    assert len([row for row in active if row["need_id"] == need(first)["need_id"]]) == 1
    text = json.dumps(messages)
    assert "OLD useful thermal finding." in text and "NEW boundary measured at 60 C." in text
    assert "STALE_MISSING" not in text
    assert "OTHER unmet requirement." in text
    assert "10.synthetic/forwarded" in text and "review_derived" in text
    # Historical snapshots are not rewritten when the active consumer row changes.
    assert "STALE_MISSING" in (engine.config.output_dir / "stages/level1_tools.json").read_text()


def test_explicit_terminal_retry_does_not_reopen_another_need(tmp_path):
    engine, args, calls, _ = external_engine(tmp_path, [
        external_answer("unmet", missing="Temperature missing."),
        external_answer("unmet", missing="Pressure missing."),
        external_answer("fulfilled", "Temperature boundary measured at 60 C.")])
    other = copy.deepcopy(args["supplement_requests"][0])
    other.update(gap_id="G2", gap_question="Which pressure changes the mechanism?")
    args["supplement_requests"].append(other)
    first = engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 2
    assert all(row["action"] == "stop" for row in first["retrieval_loop"]["needs"])
    args["supplement_requests"][0]["retry_empty_result"] = True
    second = engine._tool_cycle(phase="level1", **args)
    evidence(tmp_path, "retry_isolation", first=first, second=second, calls=calls)
    assert len(calls) == 3
    by_question = {row["need"]["question"]: row for row in second["retrieval_loop"]["needs"]}
    assert by_question[args["supplement_requests"][0]["gap_question"]]["status"] == "answered"
    assert by_question[other["gap_question"]]["action"] == "stop"
    engine._tool_cycle(phase="level1", **args)
    assert len(calls) == 3
