"""Small offline probes for cloud BODY preflight findings.

The script imports current production functions but uses only synthetic files,
fake model boundaries, and temporary output below this directory.  It never
creates a provider client and never reads a key.
"""

from __future__ import annotations

import copy
import json
import shutil
from argparse import Namespace
from pathlib import Path

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from scripts.upgrade3.chapter_arrangement import saved_payload_from
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from scripts.upgrade3 import review_feedback_loop as feedback_cli


ROOT = Path(__file__).resolve().parent / "probe_runs"


def dump(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def packet(path: Path, *, tools=None):
    value = {
        "chapter_id": "CH01",
        "research_question": "Which conditions are supported?",
        "review_argument": "Retain conditions and limits.",
        "chapter_plan": {
            "units": [{
                "unit_id": "U1",
                "substantive_point": "Compare the conditions",
                "paragraph_briefs": [
                    {"paragraph_id": "B1", "point": "Finding one", "development": "Condition one", "source_handles": ["P0001"]},
                    {"paragraph_id": "B2", "point": "Finding two", "development": "Condition two", "source_handles": ["P0002"]},
                ],
            }],
        },
        "source_materials": [
            {"source_handle": "P0001", "paper_id": "paper-1", "title": "Paper one", "study_summary_A": {"finding": "A"}},
            {"source_handle": "P0002", "paper_id": "paper-2", "title": "Paper two", "study_summary_A": {"finding": "B"}},
        ],
    }
    if tools is not None:
        value["chapter_tool_materials"] = tools
    dump(path, value)
    return value


class FakeModel:
    model = "offline-model"
    chapter_model = "offline-chapter"
    output_tokens = 18000
    thinking_budget = 8192

    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(payload)))
        if stage == "chapter_details":
            return {"chapter_plan": {"units": [{"unit_id": "U1", "substantive_point": "ok", "paragraph_briefs": []}]}}
        return {"status": "partial"}


def f1_arrangement_cache_projection():
    run = ROOT / "f1"
    run.mkdir(parents=True, exist_ok=True)
    packet_path = run / "PACKET.json"
    packet(packet_path)
    view = arranging.build_chapter_view(packet_path, id_map_path=run / "ID_MAP.json")
    fresh_payload = {
        "chapter_id": "CH01",
        "chapter_argument": "Retain conditions and limits.",
        "units": [{
            "unit_id": "U1",
            "focus": "Compare the conditions",
            "paragraph_tasks": [{
                "source_briefs": ["B1", "B2"],
                "portion": "merged findings and limits",
                "point": "Editor placement",
                "development": "Editor placement text",
                "source_uses": [{"source_handle": "P0001"}, {"source_handle": "P0002"}],
            }],
        }],
        "issues": [{"issue_id": "I1", "problem": "Keep both conditions", "status": "open"}],
    }
    fresh = arranging.validate_arrangement(fresh_payload, view, planning_revision=True)
    cached_payload = saved_payload_from(fresh)
    cached = arranging.validate_arrangement(cached_payload, view, planning_revision=True)
    exported = dict(cached)
    exported["source_catalog"] = arranging.build_source_catalog(view, cached)
    arrangement_path = run / "CHAPTER_ARRANGEMENT.json"
    dump(arrangement_path, exported)
    view_path = arranging.write_view(view, run / "ARRANGEMENT_INPUT.json")
    writer_view = writing.build_unit_view(arrangement_path, "U1", view_path=view_path)
    writer_payload = writing.unit_payload(writer_view)
    result = {
        "fresh_status": fresh["validation"]["status"],
        "fresh_ok": fresh["validation"]["ok"],
        "fresh_issue_count": len(fresh.get("issues") or []),
        "cached_status": cached["validation"]["status"],
        "cached_ok": cached["validation"]["ok"],
        "cached_errors": cached["validation"]["errors"],
        "cached_issue_count": len(cached.get("issues") or []),
        "saved_fields": sorted(cached_payload["units"][0]["paragraph_tasks"][0]),
        "writer_view_succeeded": True,
        "writer_source_brief_details_present": bool(writer_payload["paragraph_tasks"][0].get("source_brief_details")),
    }
    dump(run / "RESULT.json", result)
    return result


def f2_tool_outer_cache():
    run = ROOT / "f2"
    run.mkdir(parents=True, exist_ok=True)
    config = planning.ProgressivePlannerConfig(
        topic_id="f2",
        pool_path=run / "POOL.jsonl",
        plan_path=run / "PLAN.json",
        output_dir=run / "planner",
    )
    model = FakeModel()
    calls = []

    def runner(**kwargs):
        calls.append({"phase": kwargs["phase"], "resume": kwargs["resume"]})
        return {
            "phase": kwargs["phase"],
            "status": "partial",
            "provider_failed": True,
            "directed_results": [],
            "supplement_results": [],
            "tool_materials_by_chapter": {},
        }

    planner = planning.ProgressiveReviewPlanner(config, planner=model, retrieval_loop_runner=runner)
    state = {}
    arguments = {
        "phase": "level1",
        "supplement_requests": [{"gap_id": "G1", "gap_question": "Need a boundary"}],
        "directed_requests": [],
        "pool_rows": [{"_paper_id": "paper-1", "_source_handle": "P0001", "_b_summary": {"finding": "x"}}],
        "plan": {"research_question": "Q"},
        "prior_directed": None,
        "prior_tool_results": None,
        "source_handle_map": {"P0001": "paper-1"},
        "resume": True,
        "state": state,
    }
    first = planner._tool_cycle(**arguments)
    second = planner._tool_cycle(**arguments)
    unit_rows = [{"unit_key": "CH01:1", "unit": {"unit_id": "U1", "paragraph_briefs": [{"point": "p"}]}}]
    cache_context = {
        "topic_id": "f2", "source_routing": [{"source_handle": "P0001"}],
        "source_routing_total": 1, "batch_count": 1, "pool_sources": 1,
        "review_sources_in_unit_catalog": 1, "planning_revision_mode": True,
    }
    task_signature_before = planning._case_unit_task_signature(unit_rows, context=cache_context)
    model.model = "different-model"
    model.output_tokens = 4000
    model.thinking_budget = 1024
    task_signature_after = planning._case_unit_task_signature(unit_rows, context=cache_context)
    result = {
        "first_status": first["status"],
        "second_status": second["status"],
        "runner_calls": len(calls),
        "runner_calls_detail": calls,
        "outer_stage_record_status": json.loads((config.output_dir / "stages" / "level1_tools.json").read_text(encoding="utf-8"))["status"],
        "completed_stages": json.loads((config.output_dir / "RUN_STATE.json").read_text(encoding="utf-8"))["completed_stages"],
        "case_task_signature_unchanged_after_model_settings_change": task_signature_before == task_signature_after,
        "case_prompt_contract": planning.CASE_GROUPS_PROMPT_CONTRACT,
    }
    dump(run / "RESULT.json", result)
    return result


def f3_compact_feedback():
    full = {
        "phase": "level1",
        "supplement_results": [],
        "directed_results": [],
        "tool_materials_by_chapter": {"CH01": [{"need_id": "G1", "usable_content": "LOCAL_ANSWER", "still_missing": ""}]},
        "consumed_paper_ids": [],
    }
    compact = planning.ProgressiveReviewPlanner._compact_tool_feedback(full)
    result = {
        "full_has_local_answer": "LOCAL_ANSWER" in json.dumps(full, ensure_ascii=False),
        "compact": compact,
        "compact_has_local_answer": "LOCAL_ANSWER" in json.dumps(compact, ensure_ascii=False),
        "compact_has_tool_materials_by_chapter": "tool_materials_by_chapter" in compact,
    }
    dump(ROOT / "f3" / "RESULT.json", result)
    return result


def f4_identity_and_prior():
    run = ROOT / "f4"
    run.mkdir(parents=True, exist_ok=True)
    card = run / "CARD_B.json"
    dump(card, {
        "paper_identity": {"paper_id": "paper-B", "title": "Title B", "doi": "10.old/b"},
        "general_understanding": {"finding": "CARD_B_ONLY"},
        "review_planning": {"planning_summary": "B planning only"},
    })
    candidate = {
        "_paper_id": "paper-A", "_source_handle": "P0001", "title": "Title A",
        "doi": "10.current/a", "card_path": str(card),
        "planning_view": {"paper_identity": {"paper_id": "paper-A", "title": "Title A", "doi": "10.current/a"}},
        "_b_summary": {"declared_content_depth": "card"},
    }
    config = planning.ProgressivePlannerConfig(
        topic_id="f4", pool_path=run / "POOL.jsonl", plan_path=run / "PLAN.json", output_dir=run / "planner",
        planning_revision_enabled=True,
    )
    model = FakeModel()
    planner = planning.ProgressiveReviewPlanner(config, planner=model)
    records = planner._chapter_details(
        chapters=[{"chapter_id": "CH01", "title": "Chapter", "source_ids": ["paper-A"]}],
        shared_outline={}, topic="Q", candidates={"paper-A": candidate},
        level1_tools={}, level2_tools={}, chapter_tools={}, resume=False, state={},
        candidate_pool=[candidate],
    )
    model_source = model.calls[0][1]["source_materials"][0]
    guarded = planning.build_local_material_payload(candidate)

    task = {"paper_id": "paper-1", "questions": [{"question_id": "Q1", "question": "What finding?", "purpose": "Use it"}],
            "required_outputs": [{"output_id": "O1", "description": "finding"}], "knowledge_gaps": ["gap"]}
    prior = {
        "paper_id": "paper-1", "paper_identity": {"title": "Same title", "doi": "10.old/x"},
        "_progressive_task_signature": planning._directed_task_signature(task),
        "question_material": [{"question_id": "Q1", "explanation": "OLD_ANSWER", "remaining_points": ""}],
        "snapshot_id": "snapshot-old",
    }
    prior_path = run / "DIRECTED_READING.json"
    dump(prior_path, {"output": prior})
    pool = [{"_paper_id": "paper-1", "planning_view": {"paper_identity": {"title": "Same title", "doi": "10.current/x"}}}]
    loaded = planning.load_prior_readings([prior_path], pool)
    reused = planning._directed_material_compatible(task, loaded[0]) if loaded else False
    reader_config = planning.ProgressivePlannerConfig(
        topic_id="f4-reader", pool_path=run / "POOL.jsonl", plan_path=run / "PLAN.json", output_dir=run / "reader"
    )
    reader = planning.make_directed_reading_runner(
        reader_config, key_file=run / "NO_KEY", budget_ledger_path=run / "NO_LEDGER.sqlite",
        budget_limit_cny=1, prior_readings=loaded,
    )
    reader_result = reader([task], output_dir=run / "reader_phase", pool_by_id={"paper-1": pool[0]})
    result = {
        "chapter_details_model_saw_card_B": model_source.get("study_summary_A", {}).get("finding") == "CARD_B_ONLY",
        "chapter_details_model_title": model_source.get("title"),
        "build_local_material_payload_rejects_conflict": bool(guarded.get("material_identity_conflict")) and not guarded.get("study_summary_A"),
        "prior_loaded_despite_doi_conflict": bool(loaded),
        "prior_reuse_accepted_without_snapshot_check": reused,
        "reader_result_status": reader_result["results"][0]["status"],
        "reader_reused_before_snapshot_lookup": reader_result["results"][0]["status"] == "reused_prior_deep_read",
        "chapter_record_count": len(records),
    }
    dump(run / "RESULT.json", result)
    return result


def case_only_deep_missing():
    run = ROOT / "case_only_deep"
    run.mkdir(parents=True, exist_ok=True)
    card = run / "CARD.json"
    dump(card, {"paper_identity": {"paper_id": "paper-deep", "title": "Deep only"}})
    candidate = {"_source_handle": "P0602", "_paper_id": "paper-deep", "title": "Deep only",
                 "card_path": str(card), "planning_view": {"paper_identity": {"paper_id": "paper-deep", "title": "Deep only"}}}
    records = [{"chapter": {"chapter_id": "CH01"}, "chapter_plan": {"units": [{"unit_id": "U1", "source_handles": []}]}, "source_materials": []}]
    deep = {"paper_id": "paper-deep", "usable_content": "DEEP_ONLY_ANSWER", "question_material": [{"explanation": "DEEP_ONLY_ANSWER"}]}
    rows = planning._case_selection_material_rows(["P0602"], records, [candidate], {"paper-deep": deep})
    attached = planning._attach_case_groups(
        records,
        {"additions": [{"unit_key": "CH01:1", "studies": [{"source_handle": "P0602", "contribution": "Use deep only"}]}]},
        planning_revision=True, body_case_additions=True, candidate_rows=[candidate],
    )
    unit = attached[0]["chapter_plan"]["units"][0]
    result = {
        "case_selection_material_has_deep": "DEEP_ONLY_ANSWER" in json.dumps(rows, ensure_ascii=False),
        "case_selection_material_available": bool(rows and rows[0].get("material_available")),
        "attach_supporting_studies": unit.get("supporting_studies") or [],
        "attach_source_materials": attached[0].get("source_materials") or [],
        "case_only_deep_dropped_at_attach": not unit.get("supporting_studies") and not attached[0].get("source_materials"),
    }
    dump(run / "RESULT.json", result)
    return result


def f5_feedback_tool_projection():
    run = ROOT / "f5"
    run.mkdir(parents=True, exist_ok=True)
    tool = {"gap_id": "G_MULTI", "usable_content": "MULTI_SOURCE_TOOL_ANSWER", "sources": [
        {"source_handle": "P0001", "paper_id": "paper-1"}, {"source_handle": "P0002", "paper_id": "paper-2"}
    ]}
    packet_path = run / "PACKET.json"
    packet(packet_path, tools=[tool])
    view = arranging.build_chapter_view(packet_path, id_map_path=run / "ID_MAP.json")
    arrangement = {"chapter_id": "CH01", "units": [{"unit_id": "U1", "paragraph_tasks": [
        {"paragraph_id": "B1", "source_briefs": ["B1"], "source_uses": [{"source_handle": "P0001"}]},
        {"paragraph_id": "B2", "source_briefs": ["B2"], "source_uses": [{"source_handle": "P0002"}]},
    ]}]}
    updated_packet = dict(packet_path and json.loads(packet_path.read_text(encoding="utf-8")))
    updated_packet["chapter"] = {"chapter_id": "CH01", "title": "Chapter"}
    feedback_dir = run / "feedback"
    args = Namespace(
        arrangement_model="offline-arrangement", key_file=str(run / "NO_KEY"),
        timeout_seconds=1, arrangement_output_tokens=100, arrangement_thinking_budget=0,
        max_source_chars=240,
    )
    loop = object.__new__(feedback_cli.FeedbackLoop)
    loop.args = args
    loop.ledger = None
    loop.counter = lambda _prefix, _messages: 0

    class StubClient:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _messages, **_kwargs):
            return {"content": json.dumps(arrangement, ensure_ascii=False), "complete": True,
                    "finish_reason": "stop", "usage": {}}

    original_client = feedback_cli.QwenDirectClient
    feedback_cli.QwenDirectClient = StubClient
    try:
        feedback_dir.mkdir(parents=True, exist_ok=True)
        dump(feedback_dir / "UPDATED_WRITER_PACKET.json", updated_packet)
        feedback_result = loop.arrangement_runner(updated_packet, {}, feedback_dir)
    finally:
        feedback_cli.QwenDirectClient = original_client
    feedback_arrangement_path = run / "FEEDBACK_ARRANGEMENT.json"
    dump(feedback_arrangement_path, feedback_result)
    feedback_view = arranging.build_chapter_view(feedback_dir / "UPDATED_WRITER_PACKET.json", id_map_path=feedback_dir / "ID_MAP.json")
    feedback_view_path = feedback_dir / "ARRANGEMENT_INPUT.json"
    feedback_writer_view = writing.build_unit_view(feedback_arrangement_path, "U1", view_path=feedback_view_path)
    feedback_payload = writing.unit_payload(feedback_writer_view)

    ordinary_dir = run / "ordinary"
    ordinary_dir.mkdir(parents=True, exist_ok=True)
    arrangement_cli._export(ordinary_dir, feedback_view, dict(feedback_result))
    ordinary_arrangement_path = ordinary_dir / "CHAPTER_ARRANGEMENT.json"
    ordinary_writer_view = writing.build_unit_view(ordinary_arrangement_path, "U1", view_path=feedback_view_path)
    ordinary_payload = writing.unit_payload(ordinary_writer_view)
    result = {
        "view_has_multi_source_tool": "MULTI_SOURCE_TOOL_ANSWER" in json.dumps(feedback_view.chapter_tool_materials, ensure_ascii=False),
        "feedback_export_has_chapter_tool_materials": "chapter_tool_materials" in feedback_result,
        "feedback_writer_payload_has_multi_source_tool": "MULTI_SOURCE_TOOL_ANSWER" in json.dumps(feedback_payload, ensure_ascii=False),
        "feedback_writer_chapter_tool_material_count": len(feedback_payload.get("chapter_tool_materials") or []),
        "ordinary_export_has_chapter_tool_materials": "chapter_tool_materials" in json.loads(ordinary_arrangement_path.read_text(encoding="utf-8")),
        "ordinary_writer_payload_has_multi_source_tool": "MULTI_SOURCE_TOOL_ANSWER" in json.dumps(ordinary_payload, ensure_ascii=False),
        "ordinary_export_contrast_confirmed": "MULTI_SOURCE_TOOL_ANSWER" in json.dumps(ordinary_payload, ensure_ascii=False),
        "note": "Actual FeedbackLoop.arrangement_runner omits the field; current scripts/upgrade3/chapter_arrangement.py::_export adds it and its writer payload retains it.",
    }
    dump(run / "RESULT.json", result)
    return result


def main():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    results = {
        "F1": f1_arrangement_cache_projection(),
        "F2": f2_tool_outer_cache(),
        "F3": f3_compact_feedback(),
        "F4": f4_identity_and_prior(),
        "CASE_ONLY_DEEP": case_only_deep_missing(),
        "F5": f5_feedback_tool_projection(),
    }
    dump(ROOT / "ALL_RESULTS.json", results)
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
