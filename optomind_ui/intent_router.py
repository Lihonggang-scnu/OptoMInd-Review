"""Intent router (F2) -- the ONLY optomind_ui module allowed to call a model.

server.py forwards HTTP here and nowhere else; the file-level contract
"No model calls happen anywhere in server.py" stays intact.

Three verdicts:
* research    -- looks like a literature-review request; a signed,
                short-lived credential is issued so create_task may spawn.
* self        -- "who are you / what can you do"; answered directly, NO spawn.
* irrelevant  -- off-mission input; answered with examples, NO spawn.

Failure policy is FAIL-CLOSED to human confirmation: unreachable model,
timeout (> 8 s) or unparseable output yields verdict="research" with
"degraded": true -- the frontend must ask the user to confirm before
calling create_task (whose credential carries the degraded flag).
fail-open would burn a full harness budget on "今天天气怎么样"; a hard
refusal would lock out legitimate questions. Double-confirm is the only
middle ground.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 8.0
_TOKEN_TTL_SECONDS = 600
_SIGNING_KEY = secrets.token_bytes(32)  # per-process; restart invalidates
_used_nonces = set()
_NONCE_CAP = 4096
_VERDICTS = ("research", "self", "irrelevant")

# Deterministic fast path for identity questions: these are self-evident,
# cost zero, and several are shorter than the 4-character research guard.
_SELF_PHRASES = (
    "你是谁",
    "你是什么",
    "介绍一下你自己",
    "自我介绍",
    "你能做什么",
    "你可以做什么",
    "你会什么",
    "怎么用",
    "如何使用",
)

_SYSTEM_PROMPT = (
    "你是 OptoMind 的意图路由器。OptoMind 是一个自动化科研文献综述系统："
    "接收一个研究问题，自动完成文献检索、筛选、综述撰写、图表挂载、"
    "LaTeX/PDF 编译，一次完整运行约需数十分钟且消耗真实预算（上限约百元级）。"
    "它只做科研文献综述这一件事，不做闲聊、天气、代码问答或普通搜索。"
    "\n你的任务：判断用户输入属于哪一类，只输出一个 JSON 对象，不要输出任何其他文字："
    '{"verdict": "research" | "self" | "irrelevant", '
    '"display_title": "给界面用的中文短语，不超过20字", '
    '"reply": "verdict 为 self/irrelevant 时直接展示给用户的中文回答；research 时给一句话确认", '
    '"confidence": 0.0到1.0}\n'
    "判定标准：research=明确想获得某个研究主题的文献综述；"
    "self=询问 OptoMind 是谁/能做什么/怎么用；"
    "irrelevant=与科研文献综述无关的其它一切输入。"
)

_factory: Any = None
_factory_lock = threading.Lock()


def _get_model() -> Any:
    """Lazily build the shared DashScope model pool; None when unavailable."""

    global _factory
    with _factory_lock:
        if _factory is None:
            try:
                from optomind_research.runtime.agent_model_factory import (
                    AgentScopeModelFactory,
                )

                _factory = AgentScopeModelFactory(model_tier="standard_model")
            except Exception as exc:  # pragma: no cover - config/key issues
                logger.warning("intent router: model factory unavailable: %s", exc)
                _factory = False
        if _factory is False or getattr(_factory, "mock_mode", True):
            return None
        return _factory.current_model


def reset_factory_for_tests() -> None:
    global _factory
    with _factory_lock:
        _factory = None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                pieces.append(block["text"])
            elif isinstance(block, str):
                pieces.append(block)
            else:
                # agentscope 2.x TextBlock pydantic objects expose .text
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    text_attr = getattr(content, "text", None)
    return text_attr if isinstance(text_attr, str) else str(content or "")


def _parse_verdict_json(text: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _normalized_topic(question: str) -> str:
    return re.sub(r"\s+", "", str(question or "")).lower()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(payload: Dict[str, Any]) -> str:
    body = _b64u(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest = hmac.new(_SIGNING_KEY, body.encode("ascii"), hashlib.sha256).hexdigest()
    return body + "." + digest


def issue_credential(question: str, verdict: str, degraded: bool = False) -> str:
    """Sign a short-lived spawn credential bound to this exact question."""

    payload = {
        "q": hashlib.sha256(_normalized_topic(question).encode("utf-8")).hexdigest()[:24],
        "v": verdict,
        "d": 1 if degraded else 0,
        "exp": round(time.time() + _TOKEN_TTL_SECONDS),
        "n": secrets.token_hex(8),
    }
    return _sign(payload)


def verify_credential(token: str, question: str) -> Tuple[bool, str]:
    """Return (ok, reason). ok=True only for fresh, untampered, matching tokens."""

    if not token or "." not in str(token):
        return False, "missing credential"
    body, _, digest = str(token).partition(".")
    expected = hmac.new(_SIGNING_KEY, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        return False, "bad signature"
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return False, "malformed payload"
    if float(payload.get("exp") or 0) < time.time():
        return False, "credential expired"
    nonce = str(payload.get("n") or "")
    if nonce in _used_nonces:
        return False, "credential already used"
    topic_hash = hashlib.sha256(_normalized_topic(question).encode("utf-8")).hexdigest()[:24]
    if payload.get("q") != topic_hash:
        return False, "credential bound to a different question"
    if len(_used_nonces) >= _NONCE_CAP:
        _used_nonces.clear()
    _used_nonces.add(nonce)
    return True, str(payload.get("v") or "")


def _degraded_result(question: str, reason: str) -> Dict[str, Any]:
    logger.warning("intent router degraded (%s): fail-closed to confirm", reason)
    return {
        "verdict": "research",
        "display_title": "需要你确认一下",
        "reply": (
            "我没法确定这是不是一个科研综述请求。OptoMind 只会为科研文献综述"
            "启动完整运行（耗时较长、消耗真实预算）。如果它确实是，请在下方"
            "二次确认后继续；否则换个研究问题吧。"
        ),
        "confidence": 0.0,
        "degraded": True,
        "degraded_reason": reason,
        "token": issue_credential(question, "research", degraded=True),
    }


def _build_result(question: str, parsed: Dict[str, Any], usage_note: str = "") -> Dict[str, Any]:
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in _VERDICTS:
        return _degraded_result(question, "unknown verdict")
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    display_title = str(parsed.get("display_title") or "").strip()
    if len(display_title) > 20:
        display_title = display_title[:20]
    reply = str(parsed.get("reply") or "").strip()
    if verdict != "research" and not reply:
        reply = (
            "我是 OptoMind，一个自动化科研文献综述系统。给我一个研究问题，"
            "我会检索文献、筛选证据、写出结构化综述并编译成 PDF。"
        )
    result = {
        "verdict": verdict,
        "display_title": display_title or question[:20],
        "reply": reply,
        "confidence": round(confidence, 3),
        "degraded": False,
    }
    if usage_note:
        result["usage_note"] = usage_note
    if verdict == "research":
        # Only research verdicts ever receive a spawn credential.
        result["token"] = issue_credential(question, "research", degraded=False)
    return result


async def classify(question: str) -> Dict[str, Any]:
    """Classify one user question; NEVER spawns anything by itself."""

    question = str(question or "").strip()
    if any(phrase in question for phrase in _SELF_PHRASES):
        return {
            "verdict": "self",
            "display_title": "我是 OptoMind",
            "reply": (
                "我是 OptoMind，一个自动化科研文献综述系统：给我一个研究问题，"
                "我会自动检索文献、筛选证据、撰写结构化综述并编译成 PDF，"
                "一次完整运行约需数十分钟、消耗真实预算（有费用上限），"
                "开始前需要你确认研究问题。"
            ),
            "confidence": 1.0,
            "degraded": False,
        }
    if len(question) < 4:
        return {
            "verdict": "irrelevant",
            "display_title": "再说具体一点",
            "reply": "请把想研究的主题写完整，例如「日间辐射制冷材料的光学机制与应用进展」。",
            "confidence": 1.0,
            "degraded": False,
        }
    model = await asyncio.to_thread(_get_model)
    if model is None:
        return _degraded_result(question, "model unavailable (mock mode or no keys)")

    user_prompt = f"用户输入：{question}\n请按系统说明只输出 JSON。"
    try:
        from agentscope.message import Msg

        # agentscope 2.x requires block-list content, not a bare string;
        # and the router instructions go in the SYSTEM message.
        messages = [
            Msg(
                name="system",
                role="system",
                content=[{"type": "text", "text": _SYSTEM_PROMPT}],
            ),
            Msg(
                name="user",
                role="user",
                content=[{"type": "text", "text": user_prompt}],
            ),
        ]
    except Exception:  # pragma: no cover - agentscope always present in repo
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    try:
        response = await asyncio.wait_for(
            model(messages=messages),
            timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _degraded_result(question, "timeout after 8s")
    except Exception as exc:
        return _degraded_result(question, f"model error: {type(exc).__name__}")
    text = _extract_text(getattr(response, "content", response))
    parsed = _parse_verdict_json(text)
    if parsed is None:
        return _degraded_result(question, "unparseable model output")
    note = ""
    try:
        usage = getattr(response, "meta", None)
        usage_obj = getattr(usage, "usage", None) if usage is not None else None
        if usage_obj is not None:
            in_tok = getattr(usage_obj, "input_tokens", None)
            out_tok = getattr(usage_obj, "output_tokens", None)
            if in_tok is not None or out_tok is not None:
                note = f"tokens in={in_tok} out={out_tok}"
    except (KeyError, AttributeError, TypeError):
        note = ""  # response container varies across agent versions
    return _build_result(question, parsed, usage_note=note)
