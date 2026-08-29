"""Unified Qwen JSON call wrapper with mock fallback."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping

from config.model_router import select_model_tier
from config.qwen_config import get_model_name, get_qwen_client_config
from config.secret_pool import (
    is_key_level_http_error,
    is_model_scoped_allocation_error,
)
from llm.json_guard import extract_json_from_text, repair_json_with_llm_or_rules
from llm.prompt_templates import output_schema_for, system_prompt


def estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return max(1, int(len(text) / 4))


def _http_timeout_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get("QWEN_HTTP_TIMEOUT_SEC", "120")))
    except ValueError:
        return 120.0


def _max_transport_key_candidates() -> int:
    try:
        return max(1, int(os.environ.get("QWEN_MAX_TRANSPORT_KEY_CANDIDATES", "2")))
    except ValueError:
        return 2


def _usage(
    agent_name: str,
    model_tier: str,
    model_name: str,
    task_type: str,
    mock_llm: bool,
    input_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any] | None = None,
    success: bool = True,
    error_type: str = "",
    fallback_used: bool = False,
    **extra: Any,
) -> Dict[str, Any]:
    return {
        "module": agent_name,
        "agent_name": agent_name,
        "selected_model_tier": model_tier,
        "model_tier": model_tier,
        "model_name": model_name,
        "task_type": task_type,
        "mock_llm": bool(mock_llm),
        "estimated_input_tokens": estimate_tokens(input_payload),
        "estimated_output_tokens": estimate_tokens(output_payload or {}),
        "success": bool(success),
        "failure": not bool(success),
        "error_type": error_type,
        "fallback_used": bool(fallback_used),
        "reason": "Qwen JSON call" if not mock_llm else "mock fallback JSON response",
        **extra,
    }


def _error_name(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTPError_{exc.code}"
    return type(exc).__name__


def _mock_result(
    agent_name: str,
    task_type: str,
    input_payload: Dict[str, Any],
    model_tier: str,
    model_name: str,
    mock_response: Dict[str, Any] | None,
    error_type: str = "",
    success: bool = True,
) -> Dict[str, Any]:
    payload = dict(mock_response or {"status": "mock_fallback", "items": []})
    payload["_llm_usage"] = _usage(
        agent_name,
        model_tier,
        model_name,
        task_type,
        True,
        input_payload,
        payload,
        success=success,
        error_type=error_type,
        fallback_used=bool(error_type),
    )
    return payload


def _post_chat_completion(
    api_key: str,
    base_url: str,
    model_name: str,
    agent_name: str,
    input_payload: Dict[str, Any],
    output_schema: Dict[str, Any],
    temperature: float,
    timeout_sec: float,
    response_format: bool = True,
) -> str:
    body = {
        "model": model_name,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt(agent_name)},
            {
                "role": "user",
                "content": json.dumps(
                    {"input": input_payload, "output_schema": output_schema},
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
    }
    if response_format:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"])


def _post_chat_completion_openai_sdk(
    api_key: str,
    base_url: str,
    model_name: str,
    agent_name: str,
    input_payload: Dict[str, Any],
    output_schema: Dict[str, Any],
    temperature: float,
    timeout_sec: float,
) -> str:
    from openai import OpenAI  # type: ignore

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_sec)
    response = client.chat.completions.create(
        model=model_name,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt(agent_name)},
            {
                "role": "user",
                "content": json.dumps(
                    {"input": input_payload, "output_schema": output_schema},
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
    )
    return str(response.choices[0].message.content)


def call_qwen_json(
    agent_name: str,
    task_type: str,
    input_payload: dict,
    output_schema: dict | None = None,
    model_tier: str | None = None,
    max_retries: int = 2,
    temperature: float = 0.1,
    force_mock: bool | None = None,
    mock_response: dict | None = None,
    allow_model_fallback: bool = True,
    timeout_seconds: float | None = None,
) -> dict:
    """Call Qwen for JSON and return a dict with `_llm_usage`.

    API keys are read only through config/qwen_config.py. No API key is returned
    or logged by this function.
    """

    tier = model_tier or select_model_tier(task_type=task_type, agent_name=agent_name)
    cfg = get_qwen_client_config(tier)
    model_name = str(cfg.get("model") or get_model_name(tier))
    fallback_models = [str(item).strip() for item in (cfg.get("fallback_models") or []) if str(item).strip()]
    if not fallback_models:
        fallback_model = str(cfg.get("fallback_model") or "").strip()
        fallback_models = [fallback_model] if fallback_model else []
    model_candidates: list[tuple[str, bool]] = [(model_name, False)]
    if allow_model_fallback:
        seen_models = {model_name}
        for fallback_model in fallback_models:
            if fallback_model not in seen_models:
                seen_models.add(fallback_model)
                model_candidates.append((fallback_model, True))
    schema = output_schema or output_schema_for(agent_name)
    payload = dict(input_payload or {})

    if force_mock is True or bool(cfg.get("mock_llm", True)):
        return _mock_result(agent_name, task_type, payload, tier, model_name, mock_response)

    key_candidates = list(cfg.get("api_key_candidates") or [])
    if not key_candidates and cfg.get("api_key"):
        key_candidates = [
            {
                "api_key": str(cfg.get("api_key") or ""),
                "api_key_source": str(cfg.get("api_key_source") or ""),
                "api_key_masked": str(cfg.get("api_key_masked") or ""),
            }
        ]
    base_url = str(cfg.get("base_url") or "")
    timeout_sec = max(5.0, float(timeout_seconds)) if timeout_seconds is not None else _http_timeout_seconds()
    last_error = ""
    key_failures: list[str] = []
    attempted_models: list[str] = []
    for model_index, (candidate_model, used_model_fallback) in enumerate(model_candidates):
        attempted_models.append(candidate_model)
        transport_key_failures = 0
        stop_key_rotation = False
        for key_index, key_info in enumerate(key_candidates, 1):
            api_key = str(key_info.get("api_key") or "")
            if not api_key:
                continue
            for attempt in range(max(int(max_retries), 0) + 1):
                try:
                    try:
                        text = _post_chat_completion_openai_sdk(
                            api_key=api_key,
                            base_url=base_url,
                            model_name=candidate_model,
                            agent_name=agent_name,
                            input_payload=payload,
                            output_schema=schema,
                            temperature=temperature,
                            timeout_sec=timeout_sec,
                        )
                    except ModuleNotFoundError:
                        try:
                            text = _post_chat_completion(
                                api_key=api_key,
                                base_url=base_url,
                                model_name=candidate_model,
                                agent_name=agent_name,
                                input_payload=payload,
                                output_schema=schema,
                                temperature=temperature,
                                timeout_sec=timeout_sec,
                                response_format=True,
                            )
                        except Exception:
                            text = _post_chat_completion(
                                api_key=api_key,
                                base_url=base_url,
                                model_name=candidate_model,
                                agent_name=agent_name,
                                input_payload=payload,
                                output_schema=schema,
                                temperature=temperature,
                                timeout_sec=timeout_sec,
                                response_format=False,
                            )
                    try:
                        parsed = extract_json_from_text(text)
                    except Exception:
                        parsed = repair_json_with_llm_or_rules(text, schema_name=agent_name, agent_name=agent_name)
                        if parsed.get("error"):
                            raise ValueError(parsed["error"])
                    parsed["_llm_usage"] = _usage(
                        agent_name,
                        tier,
                        candidate_model,
                        task_type,
                        False,
                        payload,
                        parsed,
                        success=True,
                        error_type=last_error if used_model_fallback or key_failures else "",
                        fallback_used=used_model_fallback,
                        model_fallback_used=used_model_fallback,
                        api_key_source=key_info.get("api_key_source", ""),
                        api_key_masked=key_info.get("api_key_masked", ""),
                        api_key_candidate_count=len(key_candidates),
                        api_key_rotation_count=max(0, key_index - 1),
                        key_failures=key_failures[-5:],
                        fallback_chain=[name for name, _ in model_candidates[1:]],
                        attempted_models=list(attempted_models),
                        selected_model_index=model_index,
                    )
                    return parsed
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
                    last_error = _error_name(exc)
                    if is_key_level_http_error(exc):
                        key_failures.append(f"{key_info.get('api_key_masked','') or 'key'}:{last_error}")
                        break
                    if is_model_scoped_allocation_error(exc):
                        stop_key_rotation = True
                        break
                    if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 404, 405, 415, 422}:
                        stop_key_rotation = True
                    if attempt < max(int(max_retries), 0):
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    transport_key_failures += 1
                    if transport_key_failures >= _max_transport_key_candidates():
                        stop_key_rotation = True
                    break
                except Exception as exc:
                    last_error = type(exc).__name__
                    if is_key_level_http_error(exc):
                        key_failures.append(f"{key_info.get('api_key_masked','') or 'key'}:{last_error}")
                        break
                    # Socket-level disconnects (for example
                    # http.client.RemoteDisconnected) do not inherit from
                    # urllib.error.URLError.  Retry them on the same key/model
                    # before treating the whole model route as unavailable.
                    if attempt < max(int(max_retries), 0):
                        time.sleep(0.4 * (attempt + 1))
                        continue
                    transport_key_failures += 1
                    if transport_key_failures >= _max_transport_key_candidates():
                        stop_key_rotation = True
                    break
            if stop_key_rotation:
                break

    failed = _mock_result(
        agent_name,
        task_type,
        payload,
        tier,
        model_name,
        mock_response,
        error_type=last_error or "QwenCallFailed",
        success=False,
    )
    failed["_llm_usage"].update(
        {
            "model_fallback_used": bool(len(attempted_models) > 1),
            "fallback_chain": [name for name, _ in model_candidates[1:]],
            "attempted_models": attempted_models,
        }
    )
    return failed
