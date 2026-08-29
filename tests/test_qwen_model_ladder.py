"""Offline tests for the ordered Qwen model fallback ladder."""

from __future__ import annotations

import http.client
import io
import json
import urllib.error

import pytest

from config.qwen_config import (
    _validate_model_eligibility,
    get_fallback_model_names,
    get_model_name,
    get_qwen_api_key_candidates_ordered,
    load_model_policy,
)
from config.secret_pool import (
    is_key_level_http_error,
    is_model_scoped_allocation_error,
)
from llm import qwen_chat_client


class _JSONResponse:
    def __init__(self, content: str):
        self._payload = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self._payload


def test_policy_exposes_distinct_a_b_and_current_vision_models():
    policy = load_model_policy()
    aliases = policy["model_aliases"]
    assert aliases["a_model"] == "qwen3.8-max"
    assert aliases["a_minus_model"] == "qwen3.7-max-2026-06-08"
    assert aliases["b_plus_model"] == "qwen3.7-flash"
    assert aliases["b_model"] == "qwen3.6-plus"
    assert aliases["b_minus_model"] == "qwen3.6-35b-a3b"
    assert aliases["c2_model"] == "qwen3.7-flash"
    assert aliases["profiler_model"] == "qwen3.7-flash"
    assert aliases["slice_profiler_model"] == "qwen3.7-flash"
    assert not str(aliases["vision_model"]).startswith("qwen-vl-")


def test_fallback_aliases_resolve_to_ordered_concrete_model_ids():
    assert get_model_name("premium_model") == "qwen3.8-max"
    assert get_fallback_model_names("premium_model") == [
        "qwen3.7-max-2026-06-08",
        "qwen3.7-flash",
        "qwen3.6-plus",
    ]
    assert get_fallback_model_names("advanced_model") == [
        "qwen3.6-plus",
        "qwen3.6-35b-a3b",
        "qwen3.6-flash",
    ]


def test_voucher_policy_rejects_promotional_models_before_calls():
    with pytest.raises(ValueError, match="not voucher-eligible"):
        _validate_model_eligibility(
            {
                "model_aliases": {"premium_model": "qwen3.7-max"},
                "model_fallbacks": {},
                "voucher_eligibility": {
                    "reject_promotional_models": True,
                    "excluded_models": ["qwen3.7-max", "qwen3.7-plus"],
                },
            }
        )


def test_chat_client_walks_entire_fallback_chain_and_audits_attempts(monkeypatch):
    monkeypatch.setattr(
        qwen_chat_client,
        "get_qwen_client_config",
        lambda tier: {
            "model": "model-a",
            "fallback_models": ["model-b-plus", "model-b", "model-c"],
            "base_url": "https://example.invalid/v1",
            "api_key_candidates": [{"api_key": "test-key"}],
            "mock_llm": False,
        },
    )
    calls = []

    def fake_urlopen(request, timeout):
        model = json.loads(request.data.decode("utf-8"))["model"]
        calls.append(model)
        if model in {"model-a", "model-b-plus"}:
            raise http.client.RemoteDisconnected("temporary disconnect")
        return _JSONResponse("OK")

    monkeypatch.setattr(qwen_chat_client.urllib.request, "urlopen", fake_urlopen)
    result = qwen_chat_client.call_qwen_chat(
        "ladder-test",
        [{"role": "user", "content": "Reply OK"}],
        model_tier="premium_model",
        max_retries=0,
        max_transport_key_candidates=1,
    )

    assert result["content"] == "OK"
    assert calls == ["model-a", "model-b-plus", "model-b"]
    usage = result["_llm_usage"]
    assert usage["model_name"] == "model-b"
    assert usage["fallback_used"] is True
    assert usage["attempted_models"] == calls
    assert usage["selected_model_index"] == 2


def _provider_http_error(code: str, message: str) -> urllib.error.HTTPError:
    payload = json.dumps(
        {"error": {"code": code, "message": message}}
    ).encode("utf-8")
    return urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions",
        400,
        "Bad Request",
        {},
        io.BytesIO(payload),
    )


def test_dashscope_400_arrearage_is_classified_as_key_failure():
    exc = _provider_http_error(
        "Arrearage",
        "Access denied because the account has an overdue balance.",
    )
    detail = qwen_chat_client._attach_http_error_detail(exc)
    assert detail.startswith("Arrearage")
    assert is_key_level_http_error(exc) is True


def test_chat_client_falls_back_model_before_key_on_free_tier_only(monkeypatch):
    monkeypatch.setattr(
        qwen_chat_client,
        "get_qwen_client_config",
        lambda tier: {
            "model": "model-a",
            "fallback_models": ["model-b"],
            "base_url": "https://example.invalid/v1",
            "api_key_candidates": [
                {
                    "api_key": "bad-account-key",
                    "api_key_masked": "bad***key",
                    "api_key_source": "test",
                },
                {
                    "api_key": "working-key",
                    "api_key_masked": "wor***key",
                    "api_key_source": "test",
                },
            ],
            "mock_llm": False,
        },
    )
    calls = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        auth = request.headers.get("Authorization")
        calls.append((body["model"], auth))
        if body["model"] == "model-a":
            raise _provider_http_error(
                "AllocationQuota.FreeTierOnly",
                "The current API key has no paid allocation.",
            )
        return _JSONResponse("OK")

    monkeypatch.setattr(
        qwen_chat_client.urllib.request,
        "urlopen",
        fake_urlopen,
    )
    result = qwen_chat_client.call_qwen_chat(
        "key-rotation-test",
        [{"role": "user", "content": "Reply OK"}],
        model_tier="premium_model",
        max_retries=0,
    )

    assert result["content"] == "OK"
    assert calls == [
        ("model-a", "Bearer bad-account-key"),
        ("model-b", "Bearer bad-account-key"),
    ]
    usage = result["_llm_usage"]
    assert usage["model_name"] == "model-b"
    assert usage["model_fallback_used"] is True
    assert usage["api_key_rotation_count"] == 0
    assert usage["key_failures"] == []


def test_free_tier_only_is_model_scoped_not_global_key_failure():
    exc = _provider_http_error(
        "AllocationQuota.FreeTierOnly",
        "Disable the use free tier only mode for this model.",
    )
    assert is_model_scoped_allocation_error(exc) is True
    assert is_key_level_http_error(exc) is False


def test_explicit_qwen_key_file_is_an_exclusive_pool(
    monkeypatch,
    tmp_path,
):
    key_file = tmp_path / "dedicated-qwen.txt"
    key_file.write_text("sk-dedicated-one\nsk-dedicated-two\n", encoding="utf-8")
    monkeypatch.setenv("QWEN_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-should-not-be-merged")

    candidates = get_qwen_api_key_candidates_ordered()

    assert [item["api_key"] for item in candidates] == [
        "sk-dedicated-one",
        "sk-dedicated-two",
    ]
    assert all(str(key_file) in item["api_key_source"] for item in candidates)
