"""Event logger - writes UTF-8 event files without leaking keys."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .artifact_store import append_jsonl

logger = logging.getLogger(__name__)

_KEY_PATTERNS = ("sk-", "api_key", "apikey", "authorization", "bearer", "api-key")
_KEY_SOURCE_FIELDS = {"key_source", "from_key_source", "to_key_source"}


def _safe_key_source_label(value: Any) -> str:
    """Keep only a non-secret source basename and its one-based ordinal."""

    basename = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_.-]+#[1-9][0-9]*", basename):
        return basename
    return "[REDACTED]" if basename else ""


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact anything that looks like an API key."""
    if depth > 8:
        return obj
    if isinstance(obj, str):
        lower = obj.lower()
        if any(p in lower for p in _KEY_PATTERNS) and len(obj) > 16:
            return "[REDACTED]"
        return obj
    if isinstance(obj, dict):
        safe: dict[Any, Any] = {}
        for key, value in obj.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _KEY_SOURCE_FIELDS:
                safe[key] = _safe_key_source_label(value)
            elif any(pattern in normalized_key for pattern in _KEY_PATTERNS):
                safe[key] = "[REDACTED]"
            else:
                safe[key] = _redact(value, depth + 1)
        return safe
    if isinstance(obj, (list, tuple)):
        return [_redact(i, depth + 1) for i in obj]
    return obj


class EventLogger:
    """Appends structured events to EVENTS.jsonl in the task work directory.

    Rules:
    - No API keys (redacted).
    - No ThinkingBlock content.
    - No full LLM outputs (only type, tool name, status, duration, short result).
    """

    def __init__(self, work_dir: Path) -> None:
        self._events_path = work_dir / "EVENTS.jsonl"
        self._observations_path = work_dir / "OBSERVATIONS.jsonl"
        self._t0 = time.monotonic()
        self._pending_tool_calls: Dict[str, list[Dict[str, Any]]] = {}

    def _record(self, record: Dict[str, Any]) -> None:
        record.setdefault("elapsed_s", round(time.monotonic() - self._t0, 3))
        safe = _redact(record)
        append_jsonl(self._events_path, safe)

    def log_task_start(self, run_id: str, task_id: str, goal: str) -> None:
        self._record({"event": "task_start", "run_id": run_id, "task_id": task_id, "goal": goal[:200]})

    def log_task_end(self, status: str, stop_reason: Optional[str]) -> None:
        self._flush_interrupted_tool_calls()
        self._record({"event": "task_end", "status": status, "stop_reason": stop_reason})

    def log_iter_start(self, iter_num: int) -> None:
        self._record({"event": "iter_start", "iter": iter_num})

    def _flush_interrupted_tool_calls(self) -> None:
        """Make starts without results explicit before a task terminates."""

        for pending in self._pending_tool_calls.values():
            for call in pending:
                self._record(
                    {
                        "event": "tool_call_interrupted",
                        "tool": call["tool"],
                        "call_id": call["call_id"],
                        "pairing_status": "interrupted",
                        "reason": "task_ended_before_tool_result",
                    }
                )
        self._pending_tool_calls.clear()

    def log_model_call_start(
        self,
        model_name: str,
        *,
        key_fingerprint: str = "",
        key_source: str = "",
    ) -> None:
        self._record({
            "event": "model_call_start",
            "model": model_name,
            "key_source": key_source,
        })

    def log_model_call_end(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
    ) -> None:
        self._record({
            "event": "model_call_end",
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_ms": round(duration_ms, 1),
        })

    def log_tool_call(self, tool_name: str, tool_call_id: str) -> None:
        call_id = str(tool_call_id or "").strip()
        stable = call_id not in {"", "?", "unknown"}
        pending = self._pending_tool_calls.setdefault(call_id, [])
        pairing_status = "started" if stable else "unkeyed_start"
        if stable and pending:
            pairing_status = "duplicate_start"
        pending.append({"tool": str(tool_name or ""), "call_id": call_id})
        self._record(
            {
                "event": "tool_call",
                "tool": tool_name,
                "call_id": call_id,
                "pairing_status": pairing_status,
            }
        )

    def log_tool_result(
        self,
        tool_name: str,
        tool_call_id: str,
        status: str,
        summary: str,
        duration_ms: float,
    ) -> None:
        call_id = str(tool_call_id or "").strip()
        stable = call_id not in {"", "?", "unknown"}
        pending = self._pending_tool_calls.get(call_id, [])
        if stable and pending:
            pending.pop(0)
            if not pending:
                self._pending_tool_calls.pop(call_id, None)
            pairing_status = "paired"
        else:
            pairing_status = "orphan_result" if stable else "unkeyed_result"
        self._record({
            "event": "tool_result",
            "tool": tool_name,
            "call_id": call_id,
            "status": status,
            "summary": summary[:300],
            "duration_ms": round(duration_ms, 1),
            "pairing_status": pairing_status,
        })

    def log_validation(self, passed: bool, detail: str) -> None:
        self._record({"event": "validation", "passed": passed, "detail": detail[:500]})

    def log_model_switch(self, old_model: str, new_model: str, reason: str) -> None:
        self._record({"event": "model_switch", "from": old_model, "to": new_model, "reason": reason[:200]})

    def log_error(self, error_type: str, detail: str) -> None:
        self._record({"event": "error", "error_type": error_type, "detail": detail[:500]})

    def log_permission_request(self, tool_names: list) -> None:
        self._record({
            "event": "permission_request",
            "tools": tool_names,
            "note": "RequireUserConfirmEvent - unexpected under DONT_ASK mode",
        })

    def log_recovery(
        self,
        category: str,
        old_model: str,
        new_model: str,
        reason: str,
        *,
        old_key_fingerprint: str = "",
        new_key_fingerprint: str = "",
        old_key_source: str = "",
        new_key_source: str = "",
    ) -> None:
        self._record({
            "event": "recovery",
            "category": category,
            "from_model": old_model,
            "to_model": new_model,
            "reason": reason[:200],
            "from_key_source": old_key_source,
            "to_key_source": new_key_source,
        })

    def log_observation(self, observation: Dict[str, Any]) -> None:
        safe = _redact(observation)
        safe.setdefault("elapsed_s", round(time.monotonic() - self._t0, 3))
        append_jsonl(self._observations_path, safe)
