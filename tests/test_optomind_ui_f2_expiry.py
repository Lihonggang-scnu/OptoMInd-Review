"""F2 / GAP-6 regression: background expiry without GET write-duties."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import optomind_ui.server as server_module
from optomind_research.runtime.human_decision_gate import (
    list_pending,
    request_decision,
)
from optomind_ui.server import _expire_cycle_once, create_app


def _run_with_due_decision(tmp_path: Path, seconds: float = 0.05):
    run = tmp_path / "rhr_expiry202601"
    run.mkdir(parents=True)
    (run / "HARNESS_STATE.json").write_text(
        json.dumps(
            {
                "run_id": "rhr_expiry202601",
                "status": "running",
                "current_stage": "review_lead",
                "error_count": 0,
            }
        ),
        encoding="utf-8",
    )
    decision_id = request_decision(
        run,
        kind="query_plan_review",          # NOT in _MANDATORY_HUMAN_KINDS
        subject_id="subject-1",
        context={"note": "gap6-test"},
        options=["approve", "reject"],
        auto_accept_after_seconds=seconds,
    )
    return run, decision_id


def test_cycle_expires_due_decision_once(tmp_path: Path) -> None:
    run, _decision_id = _run_with_due_decision(tmp_path)
    assert len(list_pending(run)) == 1
    time.sleep(0.15)                          # let the timeout become due
    result = asyncio.run(_expire_cycle_once(str(tmp_path)))
    assert result["scanned"] == 1
    assert result["expired"] >= 1
    assert list_pending(run) == []


def test_get_decisions_remains_pure_read(tmp_path: Path) -> None:
    run, _decision_id = _run_with_due_decision(tmp_path)
    time.sleep(0.15)                          # due, but nobody expired it yet
    client = TestClient(create_app(run_root=tmp_path))
    body = client.get("/api/runs/rhr_expiry202601/decisions").json()
    # P3-3 preserved: the GET did NOT expire anything as a side effect.
    assert len(body.get("pending", [])) == 1
    # ...while the scheduler sweep does the sanctioned write.
    asyncio.run(_expire_cycle_once(str(tmp_path)))
    assert list_pending(run) == []


def test_lifespan_expires_within_two_cycles_then_cancels(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(server_module._EXPIRY_ENV_VAR, "0.05")
    run, _decision_id = _run_with_due_decision(tmp_path)
    time.sleep(0.15)
    with TestClient(create_app(run_root=tmp_path)):   # lifespan ON
        deadline = time.time() + 3                    # <= 2 cycles of 0.05 s
        while time.time() < deadline and len(list_pending(run)) > 0:
            time.sleep(0.02)
        assert list_pending(run) == []
    # leaving the with-block cancels the loop cleanly; a second sweep is idle


def test_interval_env_parsing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(server_module._EXPIRY_ENV_VAR, "2.5")
    assert server_module._expire_interval_seconds() == 2.5
    monkeypatch.setenv(server_module._EXPIRY_ENV_VAR, "-1")
    assert server_module._expire_interval_seconds() == -1.0   # disabled
    monkeypatch.setenv(server_module._EXPIRY_ENV_VAR, "notanumber")
    assert server_module._expire_interval_seconds() == 60.0   # fallback
