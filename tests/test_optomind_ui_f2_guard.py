"""F2 regression: single-flight, idempotency, orphan claim, real stop."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optomind_ui import intent_router
import optomind_ui.server as server_module
from optomind_ui.server import create_app


@pytest.fixture(autouse=True)
def _clean_process_table():
    before = dict(server_module._TASK_PROCESSES)
    yield
    server_module._TASK_PROCESSES.clear()
    server_module._TASK_PROCESSES.update(before)


class FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.pid = 424242
        self._rc = None

    def poll(self):
        return self._rc


def _spawn_fake(monkeypatch):
    created = []

    def factory(cmd, **kwargs):
        proc = FakePopen(cmd, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(server_module.subprocess, "Popen", factory)
    return created


def _token(question: str) -> str:
    return intent_router.issue_credential(question, "research")


def test_double_click_same_topic_rejoins_one_run(
    tmp_path: Path, monkeypatch
) -> None:
    spawned = _spawn_fake(monkeypatch)
    question = "钙钛矿太阳能电池稳定性研究综述"
    client = TestClient(create_app(run_root=tmp_path))
    first = client.post("/api/tasks", json={"question": question, "intent_token": _token(question)})
    assert first.status_code == 200, first.text
    second = client.post("/api/tasks", json={"question": question, "intent_token": _token(question)})
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(spawned) == 1                       # never a second process


def test_single_flight_other_topic_gets_409(tmp_path: Path, monkeypatch) -> None:
    _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    q1 = "拓扑光子学实验进展综述"
    q2 = "超透镜平面光学成像综述"
    first = client.post("/api/tasks", json={"question": q1, "intent_token": _token(q1)})
    assert first.status_code == 200
    second = client.post("/api/tasks", json={"question": q2, "intent_token": _token(q2)})
    assert second.status_code == 409
    assert second.json()["existing_run_id"] == first.json()["run_id"]


def test_orphan_registry_entry_claimed_not_blocking(
    tmp_path: Path, monkeypatch
) -> None:
    registry = {
        "version": 1,
        "tasks": {
            "rhrdead20260101": {
                "pid": 999999900,
                "status": "running",
                "topic_hash": "stale",
                "created_ts": time.time() - 600,
            }
        },
    }
    (tmp_path / ".ui_task_registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    monkeypatch.setattr(server_module, "_pid_alive", lambda pid: False)
    question = "一个全新的研究问题综述"
    _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    response = client.post(
        "/api/tasks", json={"question": question, "intent_token": _token(question)}
    )
    assert response.status_code == 200, response.text
    saved = json.loads(
        (tmp_path / ".ui_task_registry.json").read_text(encoding="utf-8")
    )
    assert saved["tasks"]["rhrdead20260101"]["status"] == "orphan"


def test_stop_kills_real_sleep_subprocess_and_persists_state(tmp_path: Path) -> None:
    run_id = "rhr_sleep202601"  # must match ^rhr_[a-z0-9]{8,32}$
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        server_module._TASK_PROCESSES[run_id] = proc
        client = TestClient(create_app(run_root=tmp_path))
        started = time.time()
        response = client.post(f"/api/tasks/{run_id}/stop")
        elapsed = time.time() - started
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "stopped_by_user"
        assert body["terminated_live_process"] is True
        assert proc.poll() is not None               # process really died
        assert elapsed < _max_grace()
        saved = json.loads(
            (tmp_path / ".ui_task_registry.json").read_text(encoding="utf-8")
        )
        assert saved["tasks"][run_id]["status"] == "stopped_by_user"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        server_module._TASK_PROCESSES.pop(run_id, None)


def _max_grace() -> float:
    return server_module._STOP_GRACE_SECONDS + 5
