from __future__ import annotations

import json
from pathlib import Path

from optomind_research.runtime.event_logger import EventLogger
from optomind_research.runtime.harness_observability import (
    HarnessObservability,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)


def _jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_utf8_round_trip_preserves_scientific_punctuation(tmp_path: Path) -> None:
    text = (
        "mechanisms\u2014propagation; "
        "Pancharatnam\u2013Berry; "
        "Greek \u03b1 \u03b2 \u0394"
    )
    logger = EventLogger(tmp_path)
    logger.log_observation({"text": text})
    logger.log_tool_call("unicode_tool", "call-utf8")
    logger.log_tool_result("unicode_tool", "call-utf8", "ok", text, 1.0)

    observation = _jsonl(tmp_path / "OBSERVATIONS.jsonl")[0]
    event = _jsonl(tmp_path / "EVENTS.jsonl")[-1]
    assert observation["text"] == text
    assert event["summary"] == text
    assert text.encode("utf-8").decode("utf-8") == text


def test_tool_reconciliation_terminal_and_aggregate_consistency(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "section_coverage" / "sections" / "S01"
    logger = EventLogger(task_dir)
    logger.log_model_call_start("qwen-test")
    logger.log_tool_call("paired_tool", "call-paired")
    logger.log_tool_result("paired_tool", "call-paired", "ok", "done", 1.0)
    logger.log_tool_call("interrupted_tool", "call-interrupted")
    logger.log_tool_result(
        "orphan_tool", "call-orphan", "ok", "orphan", 1.0
    )
    logger.log_model_call_end("qwen-test", 20, 3, 2.0)
    logger.log_task_end("waiting_for_human", "needs_more_literature")

    observer = HarnessObservability(tmp_path, "encoding-reconcile")
    observer.start_run(entry_mode="query_plan")
    metrics = observer.finish_run(
        status="needs_more_literature",
        current_stage="",
        stage_costs={
            "query_planner": {
                "cost_cny": 0.1,
                "input_tokens": 10,
                "output_tokens": 5,
                "model_call_count": 1,
            },
            "section_coverage": {
                "estimated_cost_cny": 0.2,
                "input_tokens": 20,
                "output_tokens": 3,
            },
        },
        harness_state={
            "current_stage": "phase3_argument_orchestration",
            "canonical_stages": [
                "query_planner",
                "section_coverage",
                "phase3_argument_orchestration",
            ],
            "stages": {
                "query_planner": {"status": "completed"},
                "section_coverage": {"status": "partial"},
                "phase3_argument_orchestration": {
                    "status": "needs_more_literature"
                },
            },
        },
    )

    reconciliation = metrics["tool_call_reconciliation"]
    assert reconciliation["start_count"] == 2
    assert reconciliation["result_count"] == 2
    assert reconciliation["paired_count"] == 1
    assert reconciliation["interrupted_count"] == 1
    assert reconciliation["orphan_result_count"] == 1
    assert not reconciliation["balanced"]
    orphan_statuses = {
        item["pairing_status"] for item in reconciliation["orphan_events"]
    }
    assert {"interrupted", "orphan_result"} <= orphan_statuses
    assert metrics["operations"]["tool_call_count"] == 2
    assert metrics["operations"]["model_call_count"] == 2
    assert metrics["total_input_tokens"] == 30
    assert metrics["total_output_tokens"] == 8
    assert metrics["total_tokens"] == 38
    assert metrics["cost_cny"] == 0.3
    assert metrics["canonical_totals"]["cost_cny"] == 0.3
    assert metrics["canonical_totals"]["stages"] == [
        "phase3_argument_orchestration",
        "query_planner",
        "section_coverage",
    ]
    assert metrics["completed_stage"] == "phase3_argument_orchestration"
    report = (tmp_path / "HARNESS_RUN_REPORT.md").read_text(encoding="utf-8")
    assert "CNY 0.3000" in report
    assert "\u00a5" not in report


def test_query_planner_artifact_is_admitted_once(tmp_path: Path) -> None:
    query_cost = tmp_path / "run" / "query_planner" / "QUERY_PLANNER_COST.json"
    query_cost.parent.mkdir(parents=True)
    query_cost.write_text(
        json.dumps(
            {
                "status": "primary_valid",
                "cost_cny": 0.25,
                "input_tokens": 11,
                "output_tokens": 7,
            }
        ),
        encoding="utf-8",
    )
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=tmp_path / "query.json",
            base_kb_sqlite=tmp_path / "kb.sqlite",
            output_root=tmp_path,
            upstream_cost_cny=0.25,
            upstream_input_tokens=11,
            upstream_output_tokens=7,
        ),
        run_dir=tmp_path / "run",
    )

    query_stage = harness.stage_costs["query_planner"]
    assert query_stage["cost_cny"] == 0.25
    assert query_stage["estimated_cost_cny"] == 0.25
    assert query_stage["input_tokens"] == 11
    assert query_stage["output_tokens"] == 7
    assert query_stage["model_call_count"] == 1
    assert harness._total_cost_cny() == 0.25
    assert "query_planner" in harness.state["canonical_stages"]
    harness._save_cost()
    saved_cost = json.loads(
        (tmp_path / "run" / "HARNESS_COST.json").read_text(encoding="utf-8")
    )
    assert saved_cost["canonical_totals"]["model_calls"] == 1
    assert "query_planner" in saved_cost["canonical_totals"]["stages"]

    observer = HarnessObservability(tmp_path / "run", "query-artifact")
    observer.start_run(entry_mode="query_plan")
    metrics = observer.finish_run(
        status="completed",
        current_stage="query_planner",
        stage_costs={},
        harness_state={
            "current_stage": "query_planner",
            "canonical_stages": ["query_planner", "review_lead"],
            "stages": {},
        },
    )
    assert metrics["total_input_tokens"] == 11
    assert metrics["total_output_tokens"] == 7
    assert metrics["cost_cny"] == 0.25
    assert metrics["canonical_totals"]["model_calls"] == 1


def test_blank_terminal_stage_is_repaired(tmp_path: Path) -> None:
    run_dir = tmp_path / "terminal-stage"
    harness = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=tmp_path / "missing-query.json",
            base_kb_sqlite=tmp_path / "missing-kb.sqlite",
            output_root=tmp_path,
        ),
        run_dir=run_dir,
    )
    result = harness._finish(
        "needs_more_literature",
        "",
        None,
        None,
    )

    package = json.loads(
        (run_dir / "REVIEW_CONTENT_PACKAGE.json").read_text(encoding="utf-8")
    )
    state = json.loads(
        (run_dir / "HARNESS_STATE.json").read_text(encoding="utf-8")
    )
    assert result.completed_stage == "orchestrator"
    assert package["completed_stage"] == "orchestrator"
    assert state["current_stage"] == "orchestrator"
    assert package["status"] == "needs_more_literature"
