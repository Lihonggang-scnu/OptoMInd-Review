"""F1 regression tests: narrative projection wired into endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from optomind_ui.server import create_app


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "rhr_wired20260101"
    s2 = run / "s2_literature_intelligence"
    s2.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_wired20260101",
                "status": "running",
                "current_stage": "s2_literature_intelligence",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    (run / "HARNESS_COST.json").write_text(
        json.dumps({"cost_cny": 0.5, "model_call_count": 3}),
        encoding="utf-8",
    )
    events = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event": "stage_started",
            "stage": "s2_literature_intelligence",
        },
        {
            "timestamp": "2026-01-01T00:01:00+00:00",
            "event": "stage_finished",
            "stage": "topic_scoped_kb",
            "status": "completed",
            "wall_time_seconds": 8.0,
            "selection": ["x" * 100],  # non-whitelisted -> must never surface
        },
    ]
    (run / "HARNESS_EVENTS.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    return run


def test_progress_carries_narrative_fields(tmp_path: Path) -> None:
    _make_run(tmp_path)
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/tasks/rhr_wired20260101/progress").json()
    # new fields present
    assert body["headline"]
    assert isinstance(body["metrics"], dict)
    assert isinstance(body["lines"], list) and body["lines"]
    assert body["detail"]
    # legacy fields preserved (pre-F1 consumers depend on these)
    for legacy in (
        "steps",
        "status",
        "status_label",
        "current_stage",
        "current_label",
        "cost_cny",
        "question",
        "event_count",
    ):
        assert legacy in body, legacy


def test_run_detail_carries_narrative_and_labels(tmp_path: Path) -> None:
    _make_run(tmp_path)
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/runs/rhr_wired20260101").json()
    assert body["headline"]
    assert isinstance(body["lines"], list) and body["lines"]
    assert body["current_stage_label"]  # registry label for the raw code
    for legacy in ("timeline", "cost_by_stage", "delivery_gate", "model_call_count"):
        assert legacy in body, legacy


def test_log_endpoint_serves_narrative_lines(tmp_path: Path) -> None:
    _make_run(tmp_path)
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/tasks/rhr_wired20260101/log?tail=120").json()
    assert body["source"] == "narrative"
    # works even though UI_TASK_STDOUT.log does not exist for this run
    assert len(body["lines"]) == 2
    joined = "\n".join(body["lines"])
    assert "检索文献" in joined or "圈定材料" in joined
    assert "x" * 100 not in joined  # whitelist holds at the HTTP boundary too
    assert body["stdout_tail"] == []
    assert body["entries"][0]["raw_event"] == "stage_started"


def test_log_endpoint_keeps_stdout_as_secondary(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    (run / "UI_TASK_STDOUT.log").write_text("MuPDF error\nsecond line\n", encoding="utf-8")
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/tasks/rhr_wired20260101/log").json()
    assert body["source"] == "narrative"          # primary is still narrative
    assert body["stdout_tail"][-2:] == ["MuPDF error", "second line"]
