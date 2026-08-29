"""F2 regression tests: SSE stream, incremental snapshot, stale semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import optomind_ui.server as server_module
from optomind_ui.server import _progress_snapshot, create_app


def _make_run(tmp_path: Path, status: str = "running", event_count: int = 3) -> Path:
    run = tmp_path / "rhr_stream20260101"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_stream20260101",
                "status": status,
                "current_stage": "review_lead",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    lines = []
    for i in range(event_count):
        lines.append(
            json.dumps(
                {
                    "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
                    "event": "stage_started" if i % 2 == 0 else "stage_finished",
                    "stage": "review_lead" if i % 2 == 0 else "topic_scoped_kb",
                    "status": "completed" if i % 2 else "",
                    # a monster non-whitelisted field on one line
                    **({"selection": ["x" * 5000]} if i == 1 else {}),
                }
            )
        )
    (run / "HARNESS_EVENTS.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return run


def test_stream_replays_projects_and_closes_on_terminal(tmp_path: Path) -> None:
    _make_run(tmp_path, status="completed")
    client = TestClient(create_app(run_root=tmp_path))
    response = client.get("/api/tasks/rhr_stream20260101/stream")
    assert response.status_code == 200
    body = response.text
    assert body.count("event: log") >= 3          # replayed newest lines
    assert "event: done" in body                   # terminal event then close
    assert "terminal" in body
    # whitelist holds on the wire: the monster field never appears
    assert "xxxxx" not in body
    # every pushed data line stays under the 64 KB budget
    for line in body.splitlines():
        if line.startswith("data:"):
            assert len(line.encode("utf-8")) < 64 * 1024


def test_snapshot_second_read_is_incremental(tmp_path: Path) -> None:
    run = _make_run(tmp_path, event_count=40)
    events_file = run / "HARNESS_EVENTS.jsonl"
    total_bytes = events_file.stat().st_size

    _progress_snapshot(run)                       # backfill once
    first_bytes = server_module._LAST_EVENTS_BYTES_READ["bytes"]

    with events_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "stage_started", "stage": "packaging"}) + "\n")
        handle.write(json.dumps({"event": "stage_started", "stage": "latex_publication"}) + "\n")

    snapshot = _progress_snapshot(run)
    second_bytes = server_module._LAST_EVENTS_BYTES_READ["bytes"]

    assert first_bytes >= total_bytes * 0.9       # backfill read (almost) all
    assert second_bytes < total_bytes * 0.05      # second read is O(new bytes)
    assert second_bytes <= 512
    assert snapshot["current_stage"] == "latex_publication" or snapshot["bridging"] is False
    assert snapshot["event_count"] == 42


def test_snapshot_stale_keeps_last_good_value(tmp_path: Path, monkeypatch) -> None:
    run = _make_run(tmp_path)
    good = _progress_snapshot(run)                # primes the TTL cache
    assert good["stale"] is False

    def broken_read(path):
        return None                               # simulate a lost race

    monkeypatch.setattr(server_module, "_read_json", broken_read)
    key = str(run / "HARNESS_STATE.json")
    with server_module._INCREMENTAL_LOCK:
        server_module._STATE_CACHE[key]["expires"] = 0.0  # force a re-read

    stale_body = _progress_snapshot(run)
    assert stale_body["stale"] is True
    assert "HARNESS_STATE.json" in stale_body["stale_sources"]
    # last good values survive -- never silently degraded to empty
    assert stale_body["status"] == good["status"]
    assert len(stale_body["steps"]) == 27


def test_state_cache_ttl_reuses_within_window(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    _progress_snapshot(run)
    state_file = run / "HARNESS_STATE.json"
    state_file.write_text(
        json.dumps(
            {
                "run_id": "rhr_stream20260101",
                "status": "failed",
                "current_stage": "review_lead",
                "error_count": 9,
            }
        ),
        encoding="utf-8",
    )
    body = _progress_snapshot(run)               # within TTL -> old value
    assert body["status"] == "running"
    with server_module._INCREMENTAL_LOCK:
        key = str(state_file)
        server_module._STATE_CACHE[key]["expires"] = 0.0
    body2 = _progress_snapshot(run)              # TTL expired -> fresh read
    assert body2["status"] == "failed"
