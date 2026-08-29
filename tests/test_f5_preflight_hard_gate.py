"""Backend preflight gate: POST /api/tasks must refuse on blocking failures.

The onboarding-page check was frontend-only, so a deep link or a direct
POST could start a PAID run with no API key and fail opaquely minutes
later. These tests prove the gate lives on the server side, that it
refuses BEFORE any run directory or subprocess is created, and that
non-blocking (degraded) items never block a launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optomind_ui import intent_router
import optomind_ui.preflight as preflight
import optomind_ui.server as server_module
from optomind_ui.server import create_app


@pytest.fixture(autouse=True)
def _clean_process_table():
    before = dict(server_module._TASK_PROCESSES)
    yield
    server_module._TASK_PROCESSES.clear()
    server_module._TASK_PROCESSES.update(before)


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.pid = 515151
        self._rc = None

    def poll(self):
        return self._rc


def _spawn_fake(monkeypatch):
    created = []

    def factory(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(server_module.subprocess, "Popen", factory)
    return created


def _token(question: str) -> str:
    return intent_router.issue_credential(question, "research")


_QUESTION = "石墨烯光电探测器响应度研究综述"


def test_blocking_failure_refuses_launch(tmp_path: Path, monkeypatch) -> None:
    # Redirecting PROJECT_ROOT makes api_keys/qwen-api-key.txt unfindable,
    # which is a blocking check (see preflight._check_api_key).
    monkeypatch.setattr(preflight, "PROJECT_ROOT", tmp_path / "empty")
    spawned = _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    response = client.post(
        "/api/tasks", json={"question": _QUESTION, "intent_token": _token(_QUESTION)}
    )
    assert response.status_code == 503, response.text
    body = response.json()
    assert "api_key" in [item["key"] for item in body["preflight_failed"]]
    # nothing was spent and nothing was spawned
    assert spawned == []
    assert not list(tmp_path.glob("rhr_*"))


def test_refusal_names_the_fix_without_leaking_key_contents(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(preflight, "PROJECT_ROOT", tmp_path / "empty")
    _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    response = client.post(
        "/api/tasks", json={"question": _QUESTION, "intent_token": _token(_QUESTION)}
    )
    failed = response.json()["preflight_failed"]
    api_key = next(item for item in failed if item["key"] == "api_key")
    # actionable: the judge is told exactly what to do
    assert "qwen-api-key.txt" in api_key["fix_hint"]
    assert api_key["blocking"] is True
    # the response must state that no money was spent
    assert "未产生任何费用" in response.json()["detail"]


def test_degraded_latex_still_allows_launch(tmp_path: Path, monkeypatch) -> None:
    # LaTeX absence is degraded, NOT blocking: .tex/.md still ship.
    real_which = __import__("shutil").which

    def fake_which(name):
        return "" if name in {"latexmk", "xelatex"} else real_which(name)

    monkeypatch.setattr(preflight.shutil, "which", fake_which)
    spawned = _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    response = client.post(
        "/api/tasks", json={"question": _QUESTION, "intent_token": _token(_QUESTION)}
    )
    assert response.status_code == 200, response.text
    assert len(spawned) == 1


def test_gate_runs_after_intent_check(tmp_path: Path, monkeypatch) -> None:
    # An unverified question must still be rejected as 400 (intent), not
    # masked into a 503 -- the paid-run credential check stays first.
    monkeypatch.setattr(preflight, "PROJECT_ROOT", tmp_path / "empty")
    _spawn_fake(monkeypatch)
    client = TestClient(create_app(run_root=tmp_path))
    response = client.post("/api/tasks", json={"question": _QUESTION})
    assert response.status_code == 400, response.text
