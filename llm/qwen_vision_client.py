"""Qwen multimodal client for visual reranking.

Sends image + text to current Qwen multimodal models via DashScope's
OpenAI-compatible API.
Image is base64-encoded from a local file path.

Returns:
  {
    "content": str,
    "_vision_used": bool,
    "_evidence_mode": str,  # vision_image_text | text_only | text_only_image_unavailable
    "_llm_usage": dict,
  }

If image unavailable or API fails, falls back to text-only and sets _evidence_mode accordingly.
Does NOT silently pretend vision was used. Callers must check _vision_used.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config.model_router import select_model_tier
from config.qwen_config import get_model_name, get_qwen_client_config
from config.secret_pool import (
    is_key_level_http_error,
    is_model_scoped_allocation_error,
)

_VISION_TIER = "vision_model"
_VISION_PREMIUM_TIER = "vision_premium_model"

# Supported image MIME types for base64 encoding
_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _http_timeout_seconds() -> float:
    try:
        return max(10.0, float(os.environ.get("QWEN_HTTP_TIMEOUT_SEC", "180")))
    except ValueError:
        return 120.0


def _max_transport_key_candidates() -> int:
    try:
        return max(1, int(os.environ.get("QWEN_MAX_TRANSPORT_KEY_CANDIDATES", "2")))
    except ValueError:
        return 2


def _usage(agent_name, model_tier, model_name, mock, inp, out, success, error, **extra) -> dict:
    return {
        "module": agent_name,
        "agent_name": agent_name,
        "model_tier": model_tier,
        "model_name": model_name,
        "task_type": "vision_rerank",
        "mock_llm": mock,
        "estimated_input_tokens": max(1, len(str(inp)) // 4),
        "estimated_output_tokens": max(1, len(str(out or "")) // 4),
        "success": success,
        "failure": not success,
        "error_type": error or "",
        **extra,
    }


def _encode_image(path: Path) -> tuple[str, str]:
    """Return (mime_type, base64_data). Raises if path unreadable."""
    suffix = path.suffix.lower()
    mime = _MIME_BY_SUFFIX.get(suffix, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return mime, data


def call_qwen_vision(
    agent_name: str,
    text_prompt: str,
    local_image_path: Path | str | None = None,
    model_tier: str = _VISION_TIER,
    max_retries: int = 2,
    temperature: float = 0.1,
    max_tokens: int = 400,
    response_format: dict | None = None,
    force_mock: bool | None = None,
    allow_model_fallback: bool = True,
    timeout_seconds: float | None = None,
    max_transport_key_candidates: int | None = None,
) -> dict:
    """Call a Qwen VL model with optional image + text prompt.

    If local_image_path is provided and the file exists, attaches the image.
    If the file does not exist, falls back to text-only and records evidence_mode.
    If mock_llm is active, returns a deterministic mock response without API calls.

    Returns {"content": str, "_vision_used": bool, "_evidence_mode": str, "_llm_usage": dict}
    """
    tier = model_tier
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

    # --- Mock mode ---
    if force_mock is True or bool(cfg.get("mock_llm", True)):
        return {
            "content": "[mock] Vision rerank: text-only mock (no API).",
            "_vision_used": False,
            "_evidence_mode": "text_only",
            "_llm_usage": _usage(
                agent_name, tier, model_name, True,
                text_prompt[:100], "", False, "MockMode"
            ),
        }

    # --- Resolve image path ---
    image_path: Path | None = None
    evidence_mode: str = "text_only"
    if local_image_path:
        p = Path(local_image_path)
        if p.exists():
            image_path = p
            evidence_mode = "vision_image_text"
        else:
            evidence_mode = "text_only_image_unavailable"

    # --- Build message content ---
    if image_path is not None:
        try:
            mime, b64 = _encode_image(image_path)
            data_url = f"data:{mime};base64,{b64}"
            message_content: list[dict] = [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": text_prompt},
            ]
        except Exception as enc_err:
            # Image unreadable despite path existing
            message_content = [{"type": "text", "text": text_prompt}]
            evidence_mode = "text_only_image_unavailable"
            image_path = None
    else:
        message_content = [{"type": "text", "text": text_prompt}]

    messages = [{"role": "user", "content": message_content}]

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
    timeout_sec = (
        max(10.0, float(timeout_seconds))
        if timeout_seconds is not None
        else _http_timeout_seconds()
    )
    transport_key_limit = (
        max(1, int(max_transport_key_candidates))
        if max_transport_key_candidates is not None
        else _max_transport_key_candidates()
    )
    last_error = ""
    key_failures: list[str] = []
    attempted_models: list[str] = []

    for model_index, (candidate_model, used_fallback) in enumerate(model_candidates):
        attempted_models.append(candidate_model)
        transport_key_failures = 0
        stop_key_rotation = False
        body: dict[str, Any] = {
            "model": candidate_model,
            "temperature": temperature,
            "messages": messages,
            "max_tokens": max(64, int(max_tokens)),
        }
        if response_format:
            body["response_format"] = response_format
        for key_index, key_info in enumerate(key_candidates, 1):
            api_key = str(key_info.get("api_key") or "")
            if not api_key:
                continue
            for attempt in range(max(int(max_retries), 0) + 1):
                try:
                    req = urllib.request.Request(
                        url=base_url.rstrip("/") + "/chat/completions",
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    return {
                        "content": content,
                        "_vision_used": (image_path is not None),
                        "_evidence_mode": evidence_mode,
                        "_llm_usage": _usage(
                            agent_name, tier, candidate_model, False,
                            text_prompt[:100], content, True,
                            last_error if used_fallback or key_failures else "",
                            api_key_source=key_info.get("api_key_source", ""),
                            api_key_masked=key_info.get("api_key_masked", ""),
                            api_key_rotation_count=max(0, key_index - 1),
                            fallback_used=used_fallback,
                            model_fallback_used=used_fallback,
                            fallback_chain=[name for name, _ in model_candidates[1:]],
                            attempted_models=list(attempted_models),
                            selected_model_index=model_index,
                        ),
                    }
                except (urllib.error.HTTPError, urllib.error.URLError,
                        TimeoutError, KeyError, json.JSONDecodeError) as exc:
                    last_error = (
                        f"HTTPError_{exc.code}" if isinstance(exc, urllib.error.HTTPError)
                        else type(exc).__name__
                    )
                    if is_key_level_http_error(exc):
                        key_failures.append(
                            f"{key_info.get('api_key_masked','') or 'key'}:{last_error}"
                        )
                        break
                    if is_model_scoped_allocation_error(exc):
                        stop_key_rotation = True
                        break
                    if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 404, 405, 415, 422}:
                        stop_key_rotation = True
                    if attempt < max(int(max_retries), 0):
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    transport_key_failures += 1
                    if transport_key_failures >= transport_key_limit:
                        stop_key_rotation = True
                    break
                except Exception as exc:
                    last_error = type(exc).__name__
                    if is_key_level_http_error(exc):
                        key_failures.append(
                            f"{key_info.get('api_key_masked','') or 'key'}:{last_error}"
                        )
                        break
                    if attempt < max(int(max_retries), 0):
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    transport_key_failures += 1
                    if transport_key_failures >= transport_key_limit:
                        stop_key_rotation = True
                    break
            if stop_key_rotation:
                break

    return {
        "content": "",
        "_vision_used": False,
        "_evidence_mode": evidence_mode,
        "_llm_usage": _usage(
            agent_name, tier, model_name, False, "", "", False,
            last_error or "QwenVisionFailed",
            api_key_candidate_count=len(key_candidates),
            key_failures=key_failures[-5:],
            fallback_used=True,
            model_fallback_used=bool(len(attempted_models) > 1),
            fallback_chain=[name for name, _ in model_candidates[1:]],
            attempted_models=attempted_models,
        ),
        "_failure_reason": last_error or "QwenVisionFailed",
    }
