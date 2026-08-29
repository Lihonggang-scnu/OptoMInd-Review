"""F2 regression tests: intent routing (mocked model) + spawn credential gate.

No test here touches the network or spawns any process: the model is a
fake object and subprocess.Popen is stubbed for the positive create_task
path. The REAL-model acceptance runs are executed once outside pytest
(see work log G5).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optomind_ui import intent_router, server as server_module
from optomind_ui.server import create_app


class _FakeResponse:
    def __init__(self, text: str):
        self.content = text


class _FakeModel:
    def __init__(self, text: str = "", delay: float = 0.0):
        self._text = text
        self._delay = delay
        self.calls = 0

    async def __call__(self, messages=None, **kwargs):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return _FakeResponse(self._text)


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(intent_router, "_get_model", lambda: model)


def _verdict_text(verdict: str, title: str = "研究主题", reply: str = "好的") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "display_title": title,
            "reply": reply,
            "confidence": 0.9,
        },
        ensure_ascii=False,
    )


# ---------------- classify: three verdicts ----------------


def test_classify_research_verdict_issues_credential(monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeModel(_verdict_text("research")))
    result = asyncio.run(intent_router.classify("综述日间辐射制冷的光学机制与应用"))
    assert result["verdict"] == "research"
    assert result["degraded"] is False
    assert result["display_title"]
    ok, reason = intent_router.verify_credential(result["token"], "综述日间辐射制冷的光学机制与应用")
    assert ok is True, reason
    # credential is bound to the question: a different one must fail
    ok2, reason2 = intent_router.verify_credential(result["token"], "另一个问题")
    assert ok2 is False


def test_classify_self_and_irrelevant_get_no_credential(monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeModel(_verdict_text("self", "我是 OptoMind")))
    self_result = asyncio.run(intent_router.classify("你是谁？能做什么？"))
    assert self_result["verdict"] == "self"
    assert self_result["reply"]
    assert "token" not in self_result           # never spawn on self-talk

    _patch_model(monkeypatch, _FakeModel(_verdict_text("irrelevant", "聊天气")))
    off = asyncio.run(intent_router.classify("今天天气怎么样"))
    assert off["verdict"] == "irrelevant"
    assert off["reply"]
    assert "token" not in off


def test_classify_unparseable_output_fails_closed(monkeypatch) -> None:
    question = "随便聊聊量子力学的历史好了"
    _patch_model(monkeypatch, _FakeModel("我觉得这个问题挺好的！没有 JSON。"))
    result = asyncio.run(intent_router.classify(question))
    assert result["verdict"] == "research"       # fail-closed, not fail-open
    assert result["degraded"] is True
    assert result["confidence"] == 0.0
    ok, reason = intent_router.verify_credential(result["token"], question)
    assert ok is True, reason                    # degraded credential exists,
    # and it carries the degraded flag so the UI must double-confirm.


def test_classify_timeout_fails_closed(monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeModel(_verdict_text("research"), delay=5.0))
    monkeypatch.setattr(intent_router, "_TIMEOUT_SECONDS", 0.05)
    result = asyncio.run(intent_router.classify("一个正常的研究问题"))
    assert result["degraded"] is True
    assert "timeout" in result["degraded_reason"]


def test_classify_without_model_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(intent_router, "_get_model", lambda: None)
    result = asyncio.run(intent_router.classify("钙钛矿太阳能电池的稳定性综述"))
    assert result["verdict"] == "research"
    assert result["degraded"] is True


def test_short_question_is_irrelevant_without_model(monkeypatch) -> None:
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("model must not be built for short input")

    monkeypatch.setattr(intent_router, "_get_model", boom)
    result = asyncio.run(intent_router.classify("嗯"))
    assert result["verdict"] == "irrelevant"
    assert called["n"] == 0


def test_credential_expiry(monkeypatch) -> None:
    token = intent_router.issue_credential("某个研究问题", "research")
    ok, reason = intent_router.verify_credential(token, "某个研究问题")
    assert ok is True
    # forge an expired payload by re-signing with an old expiry
    topic_hash = intent_router._normalized_topic("某个研究问题")
    import hashlib as _hashlib

    topic_hash = _hashlib.sha256(topic_hash.encode("utf-8")).hexdigest()[:24]
    payload_body = intent_router._b64u(
        json.dumps({"q": topic_hash,
                    "v": "research", "d": 0, "exp": 1, "n": "deadbeef"},
                   sort_keys=True).encode("utf-8")
    )
    import hashlib, hmac as hmac_mod
    digest = hmac_mod.new(
        intent_router._SIGNING_KEY, payload_body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    ok2, reason2 = intent_router.verify_credential(payload_body + "." + digest, "某个研究问题")
    assert ok2 is False and "expired" in reason2


# ---------------- HTTP wiring + spawn gate ----------------


def _prime_app(tmp_path: Path):
    client = TestClient(create_app(run_root=tmp_path))
    return client


def test_intent_endpoint_forwards_to_classifier(tmp_path: Path, monkeypatch) -> None:
    _patch_model(monkeypatch, _FakeModel(_verdict_text("research")))
    client = _prime_app(tmp_path)
    response = client.post("/api/intent", json={"question": "超材料隐身斗篷的研究进展"})
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "research"
    assert body["token"]


def test_create_task_without_credential_rejected(tmp_path: Path) -> None:
    client = _prime_app(tmp_path)
    response = client.post("/api/tasks", json={"question": "今天天气怎么样？我想聊天"})
    assert response.status_code == 400
    assert "意图确认" in response.json()["detail"]
    # no run dir appeared in the run root
    assert [p.name for p in tmp_path.iterdir()] == []


def test_create_task_with_valid_token_spawns_once(tmp_path: Path, monkeypatch) -> None:
    spawned = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["pid"] = 424242
            self.pid = 424242
            self._rc = None

        def poll(self):
            return self._rc

    monkeypatch.setattr(server_module.subprocess, "Popen", FakePopen)
    question = "拓扑光子学的实验进展综述"
    token = intent_router.issue_credential(question, "research")
    client = _prime_app(tmp_path)
    response = client.post(
        "/api/tasks", json={"question": question, "intent_token": token}
    )
    assert response.status_code == 200, response.text
    assert spawned["pid"] == 424242              # our fake, not a real harness
    run_dirs = [p.name for p in tmp_path.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1 and run_dirs[0].startswith("rhr_")
