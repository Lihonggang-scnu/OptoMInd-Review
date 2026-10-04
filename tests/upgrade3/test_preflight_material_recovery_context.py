"""Independent F2/F3 context regressions, offline real adapter/cache/message paths.

Only local-triage and external-service boundaries are controlled. The empty
index marker is never queried; actual SQLite compatibility has separate tests.
No key, network, full planning run or source eligibility policy changes.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket

import pytest

from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.planning_material_triage import (
    LocalGap, LocalReadingPassage, TriageJudgment, build_writer_material,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Independent consumer/recovery tests forbid network access")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def _setup(tmp_path, monkeypatch, triage, external=None):
    index = tmp_path / "index-marker.sqlite"
    index.touch()
    config = planning.ProgressivePlannerConfig(
        topic_id="independent-context-review", pool_path=tmp_path / "POOL.jsonl",
        plan_path=tmp_path / "PLAN.json", output_dir=tmp_path / "run", chapter_workers=1,
    )
    monkeypatch.setattr(supplement, "run_gap_local_triage", triage)
    adapter = planning.make_retrieval_loop_runner(
        config, local_index_path=index, allow_external=external is not None,
        supplement_runner=external,
    )
    return config, adapter


def _gap(*, outputs=("condition",), retry=False):
    return {
        "gap_id": "G1", "gap_question": "Which temperature changes the mechanism?",
        "chapter_ids": ["CH01"], "required_outputs": list(outputs),
        "targeted_queries": [{"query_type": "keyword", "query_text": "temperature mechanism"}],
        **({"retry_empty_result": True} if retry else {}),
    }


def _local(number, text, *, missing="", decision="direct_use", limits=()):
    judgment = TriageJudgment(
        gap=LocalGap("fixture", "Which temperature changes the mechanism?"),
        decision=decision, usable_content=text, still_missing=missing,
        answers_requested_question=decision == "direct_use",
        quantitative_comparisons=[{"research_object": f"P{number}_CONDITION"}],
        passages=[LocalReadingPassage(
            source_handle=f"P000{number}", paper_id=f"paper-{number}",
            title=f"Review-derived source {number}", year="", doi="",
            reading_role="direct_evidence", text=text, best_sentence=text,
            section_path=("Reported findings",), material_depth="review_derived",
            reading_path="", card_path="", from_existing_field=True,
        )],
    )
    result = judgment.to_dict()
    result["writer_material"] = build_writer_material(judgment)
    # A real builder creates the mirrored current gap; add independent source
    # constraints to prove that clearing it cannot erase genuine limitations.
    result["writer_material"]["limits"] = list(dict.fromkeys([
        *result["writer_material"]["limits"], *limits,
    ]))
    return result


def _next_messages(config, result):
    captured = []
    def model(stage, payload):
        assert stage == "level1_outline"
        captured.extend(planning._messages_for(stage, payload))
        return {"shared_outline": []}
    engine = planning.ProgressiveReviewPlanner(config, planner=model)
    engine._model_stage("level1_outline", {
        "topic_id": config.topic_id, "research_question": "Temperature mechanism",
        "actual_tool_results": engine._compact_tool_feedback(result),
    }, resume=False, state={})
    return captured


def _record(name, result, messages, **extra):
    root = os.environ.get("PREFLIGHT_CONTEXT_EVIDENCE_ROOT")
    if root:
        path = Path(root) / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately exclude temporary paths, raw transports and repeated
        # attempt journals. Keep actual rendered messages and active material.
        value = {
            "live_calls": 0,
            "needs": [{key: row[key] for key in ("need_id", "status", "action", "usable_content", "still_missing")
                       if key in row} for row in result["retrieval_loop"]["needs"]],
            "tool_materials_by_chapter": result["tool_materials_by_chapter"],
            "actual_messages": messages, **extra,
        }
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def test_explicit_retry_local_completion_clears_old_active_gap(tmp_path, monkeypatch):
    recovered = False
    external_calls = []
    def triage(_gap, **_kwargs):
        if recovered:
            return _local(1, "LOCAL_COMPLETE boundary at 60 C.", limits=["TRUE_SOURCE_LIMIT"])
        return _local(1, "LOCAL_PARTIAL condition at 25 C.", missing="OLD_ACTIVE_GAP",
                      decision="external_research", limits=["OLD_ACTIVE_GAP", "TRUE_SOURCE_LIMIT"])
    def external(*_args, **_kwargs):
        external_calls.append(1)
        return {"status": "unmet", "still_missing": "OLD_ACTIVE_GAP"}
    config, adapter = _setup(tmp_path, monkeypatch, triage, external)
    first = adapter(phase="level1", supplement_requests=[_gap()], resume=True)
    assert first["retrieval_loop"]["needs"][0]["status"] != "answered"
    recovered = True
    second = adapter(phase="level1", supplement_requests=[_gap(retry=True)], resume=True)
    messages = _next_messages(config, second)
    _record("local_retry_complete", second, messages, external_calls=len(external_calls))
    need = second["retrieval_loop"]["needs"][0]
    assert need["status"] == "answered"
    assert need["still_missing"] == ""
    active = second["tool_materials_by_chapter"]["CH01"][0]
    assert active["still_missing"] == ""
    assert "OLD_ACTIVE_GAP" not in active.get("limits", [])
    assert "TRUE_SOURCE_LIMIT" in active["limits"]
    assert "LOCAL_COMPLETE" in json.dumps(messages)
    before = len(external_calls)
    third = adapter(phase="level1", supplement_requests=[_gap()], resume=True)
    assert third["retrieval_loop"]["needs"][0]["status"] == "answered"
    assert not third["retrieval_loop"]["needs"][0]["still_missing"]
    assert len(external_calls) == before


def test_external_completion_clears_mirrored_gap_preserving_source_limit(tmp_path, monkeypatch):
    def triage(_gap, **_kwargs):
        return _local(1, "PARTIAL supported observation.", missing="RESOLVED_LOCAL_GAP",
                      decision="external_research", limits=["RESOLVED_LOCAL_GAP", "TRUE_SOURCE_LIMIT"])
    def external(*_args, **_kwargs):
        return {"status": "fulfilled", "usable_content": "EXTERNAL_COMPLETE boundary at 60 C."}
    config, adapter = _setup(tmp_path, monkeypatch, triage, external)
    result = adapter(phase="level1", supplement_requests=[_gap()])
    messages = _next_messages(config, result)
    _record("external_complete", result, messages)
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
    material = result["tool_materials_by_chapter"]["CH01"][0]
    assert material["still_missing"] == ""
    assert "RESOLVED_LOCAL_GAP" not in material.get("limits", [])
    assert "TRUE_SOURCE_LIMIT" in material["limits"]
    prompt = json.dumps(messages)
    assert "EXTERNAL_COMPLETE" in prompt and "TRUE_SOURCE_LIMIT" in prompt
    assert "RESOLVED_LOCAL_GAP" not in prompt


def test_added_requirement_keeps_inherited_local_source_conditions_and_limits(tmp_path, monkeypatch):
    calls = []
    def triage(gap, **_kwargs):
        calls.append(copy.deepcopy(gap))
        if len(calls) == 1:
            return _local(1, "P1_ANSWER condition at 25 C.", limits=["P1_LIMIT"])
        return _local(2, "P2_ANSWER boundary at 60 C.", limits=["P2_LIMIT"])
    config, adapter = _setup(tmp_path, monkeypatch, triage)
    adapter(phase="level1", supplement_requests=[_gap()])
    result = adapter(phase="level2", supplement_requests=[_gap(outputs=("condition", "boundary"))])
    messages = _next_messages(config, result)
    _record("added_local_requirement", result, messages, local_calls=len(calls))
    assert len(calls) == 2
    assert calls[1]["success_criteria"] == ["boundary"]
    assert "P1_ANSWER" in calls[1]["reusable_material"]
    material = result["tool_materials_by_chapter"]["CH01"][0]
    assert "P1_ANSWER" in material["usable_content"] and "P2_ANSWER" in material["usable_content"]
    assert {row["source_handle"] for row in material["sources"]} == {"P0001", "P0002"}
    assert {"P1_CONDITION", "P2_CONDITION"} <= set(material["conditions"])
    assert {"P1_LIMIT", "P2_LIMIT"} <= set(material["limits"])
    assert {row["material_depth"] for row in material["sources"]} == {"review_derived"}
    prompt = json.dumps(messages)
    for marker in ("P1_ANSWER", "P2_ANSWER", "P0001", "P0002", "P1_CONDITION", "P2_CONDITION", "P1_LIMIT", "P2_LIMIT"):
        assert marker in prompt
    assert result["retrieval_loop"]["needs"][0]["status"] == "answered"
