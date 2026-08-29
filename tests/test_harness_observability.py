from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from optomind_research.runtime.artifact_store import append_jsonl
from optomind_research.runtime import harness_observability as observability_module
from optomind_research.runtime.harness_observability import (
    HarnessObservability,
)


def test_run_metrics_include_stage_time_tokens_and_cny(tmp_path: Path):
    observer = HarnessObservability(tmp_path, "run-observe")
    observer.start_run(entry_mode="natural_language_question")
    observer.start_stage("query_planner")
    # Windows monotonic clocks can advance in ~15.6 ms quanta.
    time.sleep(0.04)
    observer.finish_stage(
        "query_planner",
        "completed",
        estimated_cost_cny=0.12,
        input_tokens=120,
        output_tokens=30,
    )
    metrics = observer.finish_run(
        status="completed",
        current_stage="packaging",
        stage_costs={
            "query_planner": {
                "estimated_cost_cny": 0.12,
                "input_tokens": 120,
                "output_tokens": 30,
            }
        },
        harness_state={
            "created_at": "2026-01-01T00:00:00+00:00",
            "stages": {"query_planner": {"status": "completed"}},
        },
    )
    assert metrics["active_wall_time_seconds"] > 0
    assert metrics["total_input_tokens"] == 120
    assert metrics["total_output_tokens"] == 30
    assert metrics["estimated_cost_cny"] == 0.12
    assert (
        metrics["stage_metrics"]["query_planner"]["wall_time_seconds"] > 0
    )
    assert (tmp_path / "HARNESS_RUN_REPORT.md").exists()


def test_intermediate_snapshots_do_not_double_count_wall_time(tmp_path: Path):
    observer = HarnessObservability(tmp_path, "run-live")
    observer.start_run(entry_mode="query_plan")
    time.sleep(0.04)
    first = observer.snapshot(
        status="running",
        current_stage="review_lead",
        stage_costs={},
        harness_state={"stages": {}},
    )
    time.sleep(0.04)
    second = observer.snapshot(
        status="running",
        current_stage="section_coverage",
        stage_costs={},
        harness_state={"stages": {}},
    )
    # The second value is elapsed time since run start, not first + elapsed.
    assert second["active_wall_time_seconds"] < (
        first["active_wall_time_seconds"] + 0.08
    )


def test_snapshot_persistence_is_not_active_time(
    tmp_path: Path,
    monkeypatch,
):
    """Slow checkpoint writes must not inflate the next live measurement."""

    clock = [0.0]
    monkeypatch.setattr(
        observability_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    real_write_json = observability_module.atomic_write_json
    real_write_text = observability_module.atomic_write_text

    def slow_write_json(path, value):
        clock[0] += 0.05
        return real_write_json(path, value)

    def slow_write_text(path, value):
        clock[0] += 0.05
        return real_write_text(path, value)

    monkeypatch.setattr(
        observability_module, "atomic_write_json", slow_write_json
    )
    monkeypatch.setattr(
        observability_module, "atomic_write_text", slow_write_text
    )

    observer = HarnessObservability(tmp_path, "run-slow-snapshot")
    observer.start_run(entry_mode="query_plan")
    clock[0] += 0.04
    first = observer.snapshot(
        status="running",
        current_stage="review_lead",
        stage_costs={},
        harness_state={"stages": {}},
    )
    clock[0] += 0.04
    second = observer.snapshot(
        status="running",
        current_stage="section_coverage",
        stage_costs={},
        harness_state={"stages": {}},
    )

    assert first["active_wall_time_seconds"] == 0.04
    assert second["active_wall_time_seconds"] == 0.08
    assert second["current_invocation_wall_time_seconds"] == 0.08


def test_final_snapshot_commits_active_time_for_resume(
    tmp_path: Path,
    monkeypatch,
):
    """A resumed invocation keeps committed work but not prior checkpoint I/O."""

    clock = [0.0]
    monkeypatch.setattr(
        observability_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    observer = HarnessObservability(tmp_path, "run-resume-time")
    observer.start_run(entry_mode="query_plan")
    clock[0] += 0.04
    first = observer.finish_run(
        status="partial",
        current_stage="review_lead",
        stage_costs={},
        harness_state={"stages": {}},
    )

    # The next process starts after the first process's final persistence.
    resumed = HarnessObservability(tmp_path, "run-resume-time")
    resumed.start_run(entry_mode="query_plan", resumed=True)
    clock[0] += 0.04
    second = resumed.finish_run(
        status="completed",
        current_stage="packaging",
        stage_costs={},
        harness_state={"stages": {}},
    )

    assert first["committed_active_wall_time_seconds"] == 0.04
    assert second["active_wall_time_seconds"] == 0.08
    assert second["committed_active_wall_time_seconds"] == 0.08


def test_log_index_aggregates_leaf_events_and_redacts_secrets(
    tmp_path: Path,
):
    task_dir = tmp_path / "authoring" / "tasks" / "S01"
    append_jsonl(
        task_dir / "EVENTS.jsonl",
        {
            "event": "model_call_end",
            "model": "qwen-test",
            "input_tokens": 100,
            "output_tokens": 20,
        },
    )
    append_jsonl(
        task_dir / "EVENTS.jsonl",
        {"event": "tool_call", "tool": "read_section_material"},
    )
    append_jsonl(
        task_dir / "EVENTS.jsonl",
        {
            "event": "error",
            "error_type": "NetworkError",
            "detail": "temporary failure",
        },
    )
    observer = HarnessObservability(tmp_path, "run-index")
    observer.start_run(
        entry_mode="natural_language_question",
    )
    observer.emit("diagnostic", authorization="Bearer sk-secret-value")
    metrics = observer.finish_run(
        status="failed",
        current_stage="authoring_revision",
        stage_costs={},
        harness_state={"stages": {}},
    )
    index = json.loads(
        (tmp_path / "HARNESS_LOG_INDEX.json").read_text(encoding="utf-8")
    )
    timeline = (tmp_path / "HARNESS_EVENTS.jsonl").read_text(
        encoding="utf-8"
    )
    assert index["event_log_count"] == 1
    assert index["aggregate_event_counts"]["model_call_end"] == 1
    assert index["aggregate_tool_call_counts"]["read_section_material"] == 1
    assert len(index["errors"]) == 1
    assert metrics["operations"]["error_count"] == 1
    assert "sk-secret-value" not in timeline
    assert "[REDACTED]" in timeline


def test_failed_harness_still_packages_observability(tmp_path: Path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "missing-query.json",
        base_kb_sqlite=tmp_path / "missing-kb.sqlite",
        output_root=tmp_path,
    )
    run_dir = tmp_path / "failed-run"
    result = ReviewHarnessOrchestrator(config, run_dir=run_dir).run()
    package = json.loads(
        result.package_path.read_text(encoding="utf-8")
    )
    assert result.status == "failed"
    assert Path(package["artifacts"]["run_metrics"]).exists()
    assert Path(package["artifacts"]["run_timeline"]).exists()
    assert Path(package["artifacts"]["log_index"]).exists()
    assert package["active_wall_time_seconds"] >= 0


def test_log_index_keeps_archived_attempt_errors(tmp_path: Path):
    archived = (
        tmp_path
        / "section_coverage"
        / "sections"
        / "S01"
        / "_runtime_archive"
        / "retry_1"
    )
    append_jsonl(
        archived / "EVENTS.jsonl",
        {
            "event": "error",
            "error_type": "ExceedMaxIters",
            "detail": "max_iters exceeded",
        },
    )
    (archived / "RESULT.json").write_text(
        json.dumps(
            {
                "errors": [
                    "PermissionDeniedError: free quota exhausted"
                ]
            }
        ),
        encoding="utf-8",
    )
    observer = HarnessObservability(tmp_path, "archive-run")
    observer.start_run(entry_mode="query_plan")
    metrics = observer.finish_run(
        status="partial",
        current_stage="section_coverage",
        stage_costs={},
        harness_state={"stages": {}},
    )
    index = json.loads(
        (tmp_path / "HARNESS_LOG_INDEX.json").read_text(encoding="utf-8")
    )
    assert index["event_logs"][0]["archived_attempt"] is True
    assert metrics["operations"]["error_count"] == 2


def test_preflight_reservation_is_not_written_as_spend(tmp_path: Path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "query.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
        upstream_cost_cny=1.0,
    )
    run_dir = tmp_path / "preflight"
    harness = ReviewHarnessOrchestrator(config, run_dir=run_dir)
    # Budget caps were raised (global 49→85, authoring 17.5→28, article_completion
    # 2→18, section_coverage 10→14, visual 2.5→5, translation 1→3).  The
    # expected allocation is the sum of all active stage caps plus the
    # upstream_cost_cny=1.0 fixture value.
    assert harness.preflight()["allocated_max_cny"] == 81.0
    assert not (run_dir / "HARNESS_COST.json").exists()
