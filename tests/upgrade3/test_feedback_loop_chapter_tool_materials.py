"""Feedback arrangement exports retain chapter-scoped tool material routing."""

import json
from argparse import Namespace
from pathlib import Path

from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from scripts.upgrade3 import review_feedback_loop as feedback_cli


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _packet():
    return {
        "chapter_id": "CH01",
        "research_question": "Which conditions explain the findings?",
        "chapter": {"chapter_id": "CH01", "title": "Two units", "purpose": "Tool routing"},
        "chapter_plan": {"chapter_id": "CH01", "units": [
            {"unit_id": "CH01_U01", "substantive_point": "Unit one", "paragraph_briefs": [
                {"paragraph_id": "CH01_U01_P01", "point": "A", "source_handles": ["P0001"]},
            ]},
            {"unit_id": "CH01_U02", "substantive_point": "Unit two", "paragraph_briefs": [
                {"paragraph_id": "CH01_U02_P01", "point": "B", "source_handles": ["P0002"]},
            ]},
        ]},
        "source_materials": [
            {"source_handle": "P0001", "paper_id": "study-a", "study_summary_A": {"key_findings": "A"}},
            {"source_handle": "P0002", "paper_id": "study-b", "study_summary_A": {"key_findings": "B"}},
        ],
        "tool_materials": [
            {"need_id": "N1", "unit_key": "CH01:CH01_U01", "usable_content": "TOOL_ANSWER_A",
             "sources": [{"source_handle": "P0001", "paper_id": "study-a"},
                         {"source_handle": "P0002", "paper_id": "study-b"}]},
            {"need_id": "N2", "unit_key": "CH01:CH01_U02", "usable_content": "TOOL_ANSWER_B",
             "sources": [{"source_handle": "P0002", "paper_id": "study-b"}]},
        ],
    }


def _args():
    return Namespace(
        tokenizer="unused-tokenizer.json", budget_ledger="unused-budget.sqlite", budget_limit_cny=None,
        pool="", plan="", key_file="never-read-key.txt", local_index="", deep_read_budget=0,
        owner_model="offline", owner_thinking_budget=0, owner_output_tokens=100,
        timeout_seconds=1, arrangement_model="offline", arrangement_thinking_budget=0,
        arrangement_output_tokens=100, max_source_chars=900, unit="", writer_model="offline",
        writer_thinking_budget=0, writer_output_tokens=100, max_material_chars=1000,
        language="zh", no_external_tools=True,
    )


def test_actual_feedback_arrangement_export_routes_tool_materials_by_unit(tmp_path, monkeypatch):
    packet = _packet()
    output = tmp_path / "feedback"
    _dump(output / "UPDATED_WRITER_PACKET.json", packet)
    observed = {}

    class StubClient:
        def __init__(self, **_kwargs):
            pass

        def complete(self, messages, **_kwargs):
            observed["messages"] = messages
            payload = json.loads(messages[-1]["content"])
            units = [{
                "unit_id": unit["unit_id"],
                "paragraph_tasks": [{
                    "paragraph_id": task["paragraph_id"],
                    "source_briefs": [task["paragraph_id"]],
                } for task in unit["existing_paragraph_tasks"]],
            } for unit in payload["units"]]
            return {"response": {"chapter_id": payload["chapter_id"], "units": units},
                    "complete": True, "finish_reason": "stop"}

    monkeypatch.setattr(feedback_cli, "QwenDirectClient", StubClient)
    loop = object.__new__(feedback_cli.FeedbackLoop)
    loop.args, loop.ledger, loop.counter = _args(), None, lambda *_: 100
    result = loop.arrangement_runner(packet, {}, output)

    assert [
        (item["unit_key"], item["usable_content"])
        for item in result["chapter_tool_materials"]
    ] == [
        ("CH01:CH01_U01", "TOOL_ANSWER_A"),
        ("CH01:CH01_U02", "TOOL_ANSWER_B"),
    ]
    arrangement_path = output / "ARRANGEMENT.json"
    _dump(arrangement_path, result)
    assert observed["messages"]

    for unit_id, own, other in (
        ("CH01_U01", "TOOL_ANSWER_A", "TOOL_ANSWER_B"),
        ("CH01_U02", "TOOL_ANSWER_B", "TOOL_ANSWER_A"),
    ):
        view = writing.build_unit_view(
            arrangement_path, unit_id, view_path=output / "ARRANGEMENT_INPUT.json",
        )
        message_payload = json.loads(writing.unit_messages(view)[-1]["content"])
        tool_text = [item["usable_content"] for item in message_payload["chapter_tool_materials"]]
        assert tool_text == [own]
        assert other not in tool_text
        source_handles = [
            source["source_handle"]
            for item in message_payload["chapter_tool_materials"]
            for source in item.get("sources") or []
        ]
        if unit_id == "CH01_U01":
            assert source_handles == ["P0001", "P0002"]
        else:
            assert source_handles == ["P0002"]
