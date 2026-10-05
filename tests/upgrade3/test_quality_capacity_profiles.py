"""Production profile loading, including the formerly disabled writer."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile, make_quality_client


@pytest.mark.parametrize("role,thinking,answer", [
    ("strong_outline", 32768, 32768),
    ("independent_review", 32768, 32768),
    ("strong_outline_chapter", 32768, 65536),
    ("owner_revision_chapter", 32768, 65536),
    ("owner_revision", 32768, 32768),
    ("arrangement", 16384, 32768),
    ("writer", 8192, 32768),
    ("completion", 8192, 32768),
    ("task_diagnosis", 16384, 24576),
    ("evaluation", 16384, 24576),
])
def test_role_uses_explicit_quality_capacity(monkeypatch, tmp_path, role, thinking, answer):
    from optomind_research.runtime.upgrade3.module4 import runtime
    received = {}

    def capture(**kwargs):
        received.update(kwargs)
        return kwargs

    monkeypatch.setattr(runtime, "QwenDirectClient", capture)
    client = make_quality_client(role, key_file="not-opened", budget_ledger=object(), raw_response_dir=tmp_path)
    assert client["thinking"] is True
    assert client["thinking_budget"] == thinking
    assert client["max_output_tokens"] == answer
    assert client["json_mode"] is False
    assert client["model"] == load_quality_profile(role)["model"]
    assert client["max_retries"] == 0


def test_profile_values_are_copied_and_unknown_role_has_no_silent_fallback():
    settings = load_quality_profile("writer")
    settings["thinking"] = False
    assert load_quality_profile("writer")["thinking"] is True
    with pytest.raises(ValueError, match="quality_profile_unknown_role"):
        load_quality_profile("typo")


@pytest.mark.parametrize("role,total", [
    ("strong_outline", 65536), ("strong_outline_chapter", 98304),
    ("arrangement", 49152), ("writer", 40960), ("completion", 40960),
])
def test_profile_reaches_actual_http_payload(monkeypatch, tmp_path, role, total):
    from optomind_research.runtime.upgrade3.module4 import runtime
    captured = []

    class Response:
        status = 200
        headers = {}
        def read(self):
            return json.dumps({"model": captured[-1]["model"], "id": "offline",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10}}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass

    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            return Response()

    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *args: Opener())
    client = make_quality_client(role, key_file="not-opened", budget_ledger=runtime.GlobalBudgetLedger(limit_cny=20),
                                 raw_response_dir=tmp_path)
    result = client([{"role": "user", "content": "Return JSON."}])
    assert captured[-1]["max_completion_tokens"] == total
    assert captured[-1]["enable_thinking"] is True
    assert captured[-1]["thinking_budget"] == load_quality_profile(role)["thinking_budget"]
    assert "max_tokens" not in captured[-1]
    assert "reasoning_effort" not in captured[-1]
    assert result["effective_request"]["max_completion_tokens"] == total


def test_post_body_revision_defaults_enable_reasoning_and_validate():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("quality_revision_cli", root / "scripts/upgrade3/post_body_revision.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    for variant in ("A", "B", "C"):
        config = json.loads((root / "config/post_body_revision" / f"{variant}.json").read_text())
        assert cli.validate_config(config, variant)
        for settings in config["model_settings"].values():
            assert settings["thinking"] is True
            assert settings["thinking_budget"] >= 8192
            assert settings["max_output_tokens"] >= 24576
