"""Auditable Module 4 runtime helpers and a strict direct Qwen transport.

The legacy chat wrapper intentionally is not used here: it may downgrade the
model and can turn partial/fallback responses into ordinary content.  The
reader needs the raw response and terminal finish reason before accepting a
page as complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import socket
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504}

# Official Model Studio North China 2 (Beijing) real-time prices, CNY per
# million tokens. Model IDs stay explicit; never silently downgrade a call.
# Output/context capabilities and prices checked 2026-10-05:
# https://help.aliyun.com/en/model-studio/qwen3-7-flash
# https://help.aliyun.com/en/model-studio/qwen3-5-plus
# https://help.aliyun.com/en/model-studio/qwen3-8-max
# https://help.aliyun.com/en/model-studio/qwen-api-via-openai-chat-completions
MODEL_PRICING_CNY: dict[str, dict[str, Any]] = {
    "qwen3.7-flash": {
        "tiers": ((32_000, 0.2, 0.8), (256_000, 0.6, 2.4), (991_808, 1.2, 4.8)),
        "max_input_tokens": 991_808,
        "max_output_tokens": 131_072,
        "context_window": 1_000_000,
        "completion_token_parameter": "max_completion_tokens",
        "thinking_json": True,
    },
    "qwen3.5-plus": {
        "tiers": ((128_000, 0.8, 4.8), (256_000, 2.0, 12.0), (1_000_000, 4.0, 24.0)),
        "max_input_tokens": 991_808,
        "max_output_tokens": 65_536,
        "context_window": 1_000_000,
        "completion_token_parameter": "max_completion_tokens",
        # Alibaba does not guarantee strict JSON together with thinking for
        # this model. Preserve the existing contract: disable json_mode and
        # let the reader own JSON parsing and schema validation.
        "thinking_json": False,
    },
    "qwen3.8-max": {
        "tiers": ((1_000_000, 12.0, 36.0),),
        "max_input_tokens": 991_808,
        "max_output_tokens": 131_072,
        "context_window": 1_000_000,
        "completion_token_parameter": "max_completion_tokens",
        "thinking_json": True,
        "thinking_budget_maps_to_effort": True,
    },
}


def model_pricing(model: str) -> dict[str, Any]:
    key = _text(model).strip()
    try:
        return MODEL_PRICING_CNY[key]
    except KeyError as exc:
        raise QwenTransportError("unsupported_m4_model", transient=False, record={"model": key, "allowed_models": sorted(MODEL_PRICING_CNY)}) from exc


def _tier_rates(model: str, input_tokens: int) -> tuple[float, float]:
    pricing = model_pricing(model)
    tokens = max(1, int(input_tokens))
    if tokens > int(pricing["max_input_tokens"]):
        raise QwenTransportError("model_input_context_exceeded", transient=False, record={"model": model, "input_tokens": tokens, "max_input_tokens": pricing["max_input_tokens"]})
    for upper, input_rate, output_rate in pricing["tiers"]:
        if tokens <= upper:
            return float(input_rate), float(output_rate)
    raise QwenTransportError("model_pricing_tier_missing", transient=False, record={"model": model, "input_tokens": tokens})


def _conservative_prompt_token_upper_bound(request_bytes: bytes, messages: Sequence[Mapping[str, Any]]) -> int:
    """Return a hard-budget upper bound for prompt tokens before dispatch.

    A byte is the smallest possible UTF-8/BPE unit, so dividing UTF-8 bytes by
    four is only an estimate and can under-reserve Chinese and other
    non-ASCII prompts.  The serialized request already includes messages,
    role labels, JSON syntax, and escaped fields; add a fixed protocol margin
    for tokenizer framing and provider message wrappers.
    """

    protocol_margin = 8_192 + (256 * max(1, len(messages)))
    return max(1, len(request_bytes) + protocol_margin)


def _safe_error_code(raw: bytes) -> str:
    """Extract only a bounded provider error code; never retain its message."""

    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError, UnicodeError):
        return ""
    if not isinstance(value, Mapping):
        return ""
    error = value.get("error") if isinstance(value.get("error"), Mapping) else value
    code = error.get("code") or error.get("error_code") or value.get("code")
    code = _text(code).strip()
    if not code:
        return ""
    return "".join(char for char in code[:80] if char.isalnum() or char in "-_ .").strip().replace(" ", "_")


def _account_rejection(status_code: int, error_code: str) -> bool:
    normalized = _text(error_code).casefold().replace("-", "").replace("_", "").replace(" ", "")
    # A bare 401 is the one safe status-only signal: it is the provider's
    # authentication challenge and retrying it with another configured key is
    # bounded by max_keys.  A bare 403 is deliberately *not* rotated because
    # it can mean a policy/scope denial that applies to every credential.
    markers = ("invalidapikey", "invalidkey", "unauthorized", "authentication", "arrearage", "insufficientbalance", "accountbalance", "paymentrequired", "accountdisabled")
    return (status_code == 401 and not normalized) or (
        status_code in {400, 401, 402, 403} and any(marker in normalized for marker in markers)
    )


def _provider_request_id(raw: bytes, headers: Any) -> str:
    """Return a bounded request id from headers or a JSON error envelope."""

    header_id = ""
    if hasattr(headers, "get"):
        header_id = _text(headers.get("x-request-id") or headers.get("X-Request-ID") or headers.get("request-id"))
    if header_id:
        return header_id[:200]
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError, UnicodeError):
        return ""
    if not isinstance(value, Mapping):
        return ""
    error = value.get("error") if isinstance(value.get("error"), Mapping) else {}
    for key in ("request_id", "requestId", "requestid", "request-id"):
        candidate = value.get(key) or error.get(key)
        if candidate:
            return _text(candidate)[:200]
    return ""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _economy_enabled() -> bool:
    return _text(os.environ.get("OPTOMIND_ECONOMY_TEXT_CEILING")).strip().casefold() in {"1", "true", "yes", "on"}


class QwenTransportError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False, status_code: int | None = None, record: Mapping[str, Any] | None = None, reason_code: str | None = None):
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code
        self.record = dict(record or {})
        raw_reason = _text(reason_code or self.record.get("error") or message)
        self.reason_code = "".join(char for char in raw_reason[:80] if char.isalnum() or char in "-_ .").strip().replace(" ", "_")


def _transport_exception_record(error: BaseException) -> dict[str, Any]:
    """Return exception classes and numeric OS/SSL codes without messages."""
    names = [type(error).__name__]
    nested = getattr(error, "reason", None)
    if isinstance(nested, BaseException):
        names.append(type(nested).__name__)
    codes: dict[str, int] = {}
    for source in (error, nested if isinstance(nested, BaseException) else None):
        for field_name in ("errno", "winerror", "verify_code"):
            value = getattr(source, field_name, None) if source is not None else None
            if isinstance(value, int) and not isinstance(value, bool):
                codes.setdefault(field_name, value)
    reason_code = "__".join(names + [f"{key}_{value}" for key, value in sorted(codes.items())])
    return {"error": names[0], "reason_code": reason_code, **codes}


def _uncertain_telemetry(error: QwenTransportError, *, key_index: int, attempt: int, effective_request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Persist only bounded error identity when a request has unknown billing."""
    error_class = "".join(char for char in type(error).__name__[:40] if char.isalnum() or char in "-_")
    reason = "".join(char for char in (error.reason_code or "unknown")[:80] if char.isalnum() or char in "-_")
    telemetry = {
        "provider_error_code": f"{error_class}:{reason}"[:128],
        "status_code": error.status_code,
        "key_index": key_index,
        "attempt": attempt + 1,
        "effective_request": dict(effective_request or {}),
    }
    for key in ("stream", "transport_events", "transport_phase", "partial_raw_path", "raw_stream_path"):
        if key in error.record:
            telemetry[key] = error.record[key]
    return telemetry


def _cap_pressure(usage: Mapping[str, Any], effective_request: Mapping[str, Any], finish_reason: str) -> dict[str, Any]:
    """Report budget pressure, never reject an otherwise complete response.

    Answer tokens are an allocation, not a separate wire cap: unused thinking
    capacity can be used by the answer. Do not infer reasoning use when the
    provider omits its counter.
    """

    def count(value: Any) -> int | None:
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    completion = count(usage.get("completion_tokens") if usage.get("completion_tokens") is not None else usage.get("output_tokens"))
    details = usage.get("completion_tokens_details")
    reasoning = count(details.get("reasoning_tokens")) if isinstance(details, Mapping) else None
    if reasoning is None and not effective_request["enable_thinking"]:
        reasoning = 0
    answer = max(0, completion - reasoning) if completion is not None and reasoning is not None else None
    near = []
    for name, used, allocated in (
        ("completion", completion, effective_request["total_output_tokens"]),
        ("thinking", reasoning, effective_request["thinking_budget"]),
        ("answer_allocation", answer, effective_request["answer_tokens"]),
    ):
        if used is not None and allocated > 0 and used >= 0.9 * allocated:
            near.append(name)
    return {
        "near_limit": bool(near) or finish_reason == "length",
        "near_limits": near,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "answer_tokens": answer,
        "finish_reason_length": finish_reason == "length",
        "diagnostic_only": True,
    }


_SAFE_RESPONSE_HEADERS = {
    "date", "server", "content-type", "content-length", "transfer-encoding",
    "connection", "x-request-id", "request-id", "x-dashscope-partialresponse",
}


def _safe_response_headers(headers: Any) -> dict[str, str]:
    """Keep only bounded, non-credential response headers in transport audits."""

    if headers is None or not hasattr(headers, "items"):
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        if _text(key).casefold() in _SAFE_RESPONSE_HEADERS:
            result[_text(key).casefold()] = _text(value)[:256]
    return result


def _emit_transport_event(observer: Any, event: Mapping[str, Any]) -> None:
    """Best-effort audit callback; observer failures never fail a paid call."""

    if observer is None:
        return
    try:
        if callable(observer):
            observer(dict(event))
        elif hasattr(observer, "append"):
            observer.append(dict(event))
    except Exception:
        return


def _stream_event_record(
    *, stage: str, started: float, **fields: Any,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "epoch": time.time(),
        "elapsed_seconds": time.monotonic() - started,
        **fields,
    }


def _stream_response(
    response: Any, *, started: float, observer: Any, raw_path: Path,
    overall_timeout_seconds: float,
) -> dict[str, Any]:
    """Read Qwen SSE without treating partial content as a completed answer."""

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw_bytes = bytearray()
    data_lines: list[bytes] = []
    usage: dict[str, Any] = {}
    finish_reason = ""
    returned_model = ""
    request_id = ""
    done = False
    event_count = 0
    content_bytes = 0
    reasoning_bytes = 0
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = raw_path.open("wb")

    def emit(stage: str, **fields: Any) -> None:
        # Call sites below already select first chunks, periodic progress, and
        # terminal events. Do not apply a second counter here: an audit event
        # such as ``stream_read_start`` must not shift the every-128-chunk
        # boundary. The raw SSE file remains the complete evidence.
        _emit_transport_event(observer, _stream_event_record(stage=stage, started=started, **fields))

    def set_read_timeout(seconds: float) -> None:
        """Best-effort socket inactivity bound for urllib's HTTPResponse."""

        bounded = max(0.1, float(seconds))
        candidates = [response]
        fp = getattr(response, "fp", None)
        if fp is not None:
            candidates.append(fp)
            raw = getattr(fp, "raw", None)
            if raw is not None:
                candidates.append(raw)
                sock = getattr(raw, "_sock", None)
                if sock is not None:
                    candidates.append(sock)
        for candidate in candidates:
            setter = getattr(candidate, "settimeout", None)
            if callable(setter):
                try:
                    setter(bounded)
                except (OSError, ValueError):
                    continue

    def fail(reason: str, *, stage: str, **fields: Any) -> None:
        emit("stream_error", reason=reason, phase=stage, **fields)
        raise QwenTransportError(
            reason,
            transient=False,
            record={
                "transport_phase": stage,
                "stream_event_count": event_count,
                "stream_done": done,
                "partial_raw_path": str(raw_path),
                "partial_raw_bytes": len(raw_bytes),
                "partial_content_bytes": content_bytes,
                "partial_reasoning_bytes": reasoning_bytes,
            },
        )

    def process_data(data: bytes) -> None:
        nonlocal done, event_count, content_bytes, reasoning_bytes, usage, finish_reason, returned_model, request_id
        if not data:
            return
        if data.strip() == b"[DONE]":
            done = True
            emit("stream_done", event_count=event_count, raw_bytes=len(raw_bytes))
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            fail("qwen_stream_event_not_json", stage="stream_event", event_sha256=hashlib.sha256(data).hexdigest())
        if not isinstance(payload, Mapping):
            fail("qwen_stream_event_not_object", stage="stream_event")
        if payload.get("error") is not None:
            fail("qwen_stream_error_event", stage="stream_event")
        event_count += 1
        model = _text(payload.get("model"))
        if model:
            returned_model = model
        payload_id = _text(payload.get("id"))
        if payload_id:
            request_id = payload_id[:200]
        candidate_usage = payload.get("usage")
        if isinstance(candidate_usage, Mapping):
            usage = dict(candidate_usage)
        choices = payload.get("choices")
        if not isinstance(choices, Sequence) or not choices:
            emit("stream_chunk", event_count=event_count, choices=0, usage_present=bool(candidate_usage))
            return
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        if choice.get("finish_reason") is not None:
            finish_reason = _text(choice.get("finish_reason"))
        delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
        piece = delta.get("content")
        if isinstance(piece, list):
            piece = "".join(_text(item.get("text")) if isinstance(item, Mapping) else _text(item) for item in piece)
        piece = _text(piece)
        if piece:
            content_parts.append(piece)
            content_bytes += len(piece.encode("utf-8"))
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, list):
            reasoning = "".join(_text(item.get("text")) if isinstance(item, Mapping) else _text(item) for item in reasoning)
        reasoning = _text(reasoning)
        if reasoning:
            reasoning_parts.append(reasoning)
            reasoning_bytes += len(reasoning.encode("utf-8"))
        if event_count <= 8 or event_count % 128 == 0:
            emit("stream_chunk", event_count=event_count, choices=1, content_bytes=content_bytes, reasoning_bytes=reasoning_bytes, usage_present=bool(candidate_usage))

    try:
        while not done:
            remaining = overall_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                fail("qwen_stream_overall_timeout", stage="stream_read", event_count=event_count)
            set_read_timeout(remaining)
            emit("stream_read_start", event_count=event_count) if event_count == 0 else None
            try:
                line = response.readline()
            except (socket.timeout, TimeoutError):
                fail("qwen_stream_read_timeout", stage="stream_read", event_count=event_count)
            except OSError:
                fail("qwen_stream_read_error", stage="stream_read", event_count=event_count)
            if not line:
                if data_lines:
                    process_data(b"\n".join(data_lines))
                    data_lines.clear()
                if not done:
                    fail("qwen_stream_incomplete", stage="stream_eof", event_count=event_count)
                break
            first_wire_byte = not raw_bytes
            raw_bytes.extend(line)
            raw_handle.write(line)
            raw_handle.flush()
            if first_wire_byte:
                emit("stream_first_byte", bytes=len(line))
            stripped = line.rstrip(b"\r\n")
            if stripped == b"":
                if data_lines:
                    process_data(b"\n".join(data_lines))
                    data_lines.clear()
                continue
            if stripped.startswith(b":"):
                continue
            if stripped.startswith(b"data:"):
                data_lines.append(stripped[5:].lstrip())
                continue
        if not done:
            fail("qwen_stream_incomplete", stage="stream_eof", event_count=event_count)
    finally:
        raw_handle.close()
    if finish_reason != "stop" or not content_parts:
        fail("qwen_stream_incomplete", stage="stream_terminal", finish_reason=finish_reason, event_count=event_count)
    if not usage or not any(usage.get(key) is not None for key in ("completion_tokens", "output_tokens")):
        fail("qwen_stream_usage_missing", stage="stream_terminal", event_count=event_count)
    normalized = {
        "id": request_id or None,
        "model": returned_model or None,
        "choices": [{
            "message": {
                "content": "".join(content_parts),
                **({"reasoning_content": "".join(reasoning_parts)} if reasoning_parts else {}),
            },
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }
    emit("stream_complete", event_count=event_count, content_bytes=content_bytes, reasoning_bytes=reasoning_bytes, usage_present=True)
    return {
        "raw_bytes": bytes(raw_bytes),
        "normalized": normalized,
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "usage": usage,
        "finish_reason": finish_reason,
        "returned_model": returned_model,
        "request_id": request_id,
        "event_count": event_count,
    }


class MissingCredentialError(QwenTransportError):
    pass


@dataclass
class GlobalBudgetLedger:
    """One explicit experiment-wide CNY ledger shared across paper runs."""

    limit_cny: float | None = None
    reserved_cny: float = 0.0
    actual_cny: float = 0.0
    reservations: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None

    def __post_init__(self) -> None:
        if self.path:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS budget_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS reservations (reservation_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, amount_cny REAL NOT NULL, actual_cny REAL, status TEXT NOT NULL, created_at REAL NOT NULL, usage_json TEXT, returned_model TEXT, finish_reason TEXT, request_id TEXT, raw_response_sha256 TEXT, status_code INTEGER)")
                columns = {row[1] for row in db.execute("PRAGMA table_info(reservations)").fetchall()}
                for name, sql_type in (("usage_json", "TEXT"), ("returned_model", "TEXT"), ("finish_reason", "TEXT"), ("request_id", "TEXT"), ("raw_response_sha256", "TEXT"), ("status_code", "INTEGER"), ("provider_error_code", "TEXT"), ("key_index", "INTEGER"), ("attempt", "INTEGER"), ("request_metadata_json", "TEXT")):
                    if name not in columns:
                        db.execute(f"ALTER TABLE reservations ADD COLUMN {name} {sql_type}")
                if self.limit_cny is not None:
                    row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
                    if row is not None and abs(float(row[0]) - float(self.limit_cny)) > 1e-9:
                        raise QwenTransportError("global_budget_limit_conflict", transient=False, record={"existing_limit_cny": float(row[0]), "requested_limit_cny": float(self.limit_cny)})
                    db.execute("INSERT OR IGNORE INTO budget_meta(key,value) VALUES ('limit_cny',?)", (str(float(self.limit_cny)),))
                db.commit()

    def _refresh_from_db(self) -> None:
        if not self.path:
            return
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if self.limit_cny is None and row:
                self.limit_cny = float(row[0])
            row = db.execute("SELECT COALESCE(SUM(amount_cny),0) FROM reservations WHERE status IN ('reserved','uncertain')").fetchone()
            self.reserved_cny = float(row[0] or 0.0)
            row = db.execute("SELECT COALESCE(SUM(actual_cny),0) FROM reservations").fetchone()
            self.actual_cny = float(row[0] or 0.0)

    def reserve(self, amount_cny: float, call_id: str) -> dict[str, Any]:
        amount = max(0.0, float(amount_cny))
        if self.path:
            reservation_id = "res-" + uuid.uuid4().hex[:16]
            with sqlite3.connect(self.path, timeout=30.0) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
                limit = self.limit_cny if self.limit_cny is not None else (float(row[0]) if row else None)
                used = db.execute("SELECT COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_cny,amount_cny) ELSE amount_cny END),0) FROM reservations").fetchone()[0]
                if limit is not None and float(used or 0.0) + amount > float(limit) + 1e-9:
                    raise QwenTransportError("global_budget_exceeded", transient=False, record={"call_id": call_id, "amount_cny": amount})
                db.execute("INSERT INTO reservations(reservation_id,call_id,amount_cny,actual_cny,status,created_at) VALUES (?,?,?,?,?,?)", (reservation_id, call_id, amount, None, "reserved", time.time()))
                db.commit()
            self._refresh_from_db()
            row = {"reservation_id": reservation_id, "call_id": call_id, "amount_cny": amount, "status": "reserved"}
            self.reservations.append(row)
            return row
        # The in-memory mode is used by offline callers, but it must have the
        # same cap semantics as the durable ledger: settled spend remains
        # spent and open reservations remain held.
        if self.limit_cny is not None and self.actual_cny + self.reserved_cny + amount > float(self.limit_cny) + 1e-9:
            raise QwenTransportError("global_budget_exceeded", transient=False, record={"call_id": call_id, "amount_cny": amount})
        row = {"reservation_id": "res-" + uuid.uuid4().hex[:16], "call_id": call_id, "amount_cny": amount, "status": "reserved"}
        self.reserved_cny += amount
        self.reservations.append(row)
        return row

    def settle(self, reservation_id: str, actual_cny: float | None, *, uncertain: bool = False, telemetry: Mapping[str, Any] | None = None) -> None:
        amount = max(0.0, float(actual_cny or 0.0))
        telemetry = dict(telemetry or {})
        if self.path:
            status = "uncertain" if uncertain else "settled"
            with sqlite3.connect(self.path, timeout=30.0) as db:
                row = db.execute("SELECT status FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                if row is None:
                    raise QwenTransportError("unknown_budget_reservation", transient=False, record={"reservation_id": reservation_id})
                if row[0] in {"settled", "uncertain"}:
                    raise QwenTransportError("budget_reservation_already_settled", transient=False, record={"reservation_id": reservation_id})
                db.execute("UPDATE reservations SET actual_cny=?, status=?, usage_json=?, returned_model=?, finish_reason=?, request_id=?, raw_response_sha256=?, status_code=?, provider_error_code=?, key_index=?, attempt=?, request_metadata_json=? WHERE reservation_id=?", (
                    amount if actual_cny is not None else None, status,
                    json.dumps(telemetry.get("usage"), ensure_ascii=False, sort_keys=True) if telemetry.get("usage") is not None else None,
                    _text(telemetry.get("returned_model")) or None, _text(telemetry.get("finish_reason")) or None,
                    _text(telemetry.get("request_id")) or None, _text(telemetry.get("raw_response_sha256")) or None,
                    int(telemetry["status_code"]) if telemetry.get("status_code") is not None else None,
                    _text(telemetry.get("provider_error_code")) or None,
                    int(telemetry["key_index"]) if telemetry.get("key_index") is not None else None,
                    int(telemetry["attempt"]) if telemetry.get("attempt") is not None else None,
                    json.dumps({key: telemetry[key] for key in ("effective_request", "cap_pressure", "stream", "transport_events", "transport_phase", "partial_raw_path", "raw_stream_path") if key in telemetry}, ensure_ascii=False, sort_keys=True),
                    reservation_id,
                ))
                db.commit()
            self._refresh_from_db()
            for row in self.reservations:
                if row.get("reservation_id") == reservation_id:
                    row["status"] = status
                    if actual_cny is not None:
                        row["actual_cny"] = amount
                    row["telemetry"] = telemetry
            return
        for row in self.reservations:
            if row.get("reservation_id") == reservation_id:
                if row.get("status") in {"settled", "uncertain"}:
                    raise QwenTransportError("budget_reservation_already_settled", transient=False, record={"reservation_id": reservation_id})
                if row.get("status") not in {"reserved", "uncertain"}:
                    raise QwenTransportError("budget_reservation_invalid_status", transient=False, record={"reservation_id": reservation_id, "status": row.get("status")})
                if uncertain:
                    row["status"] = "uncertain"
                    row["actual_cny"] = None
                else:
                    row["status"] = "settled"
                    row["actual_cny"] = amount
                    self.reserved_cny = max(0.0, self.reserved_cny - float(row.get("amount_cny") or 0.0))
                    self.actual_cny += amount
                row["telemetry"] = telemetry
                return
        raise QwenTransportError("unknown_budget_reservation", transient=False, record={"reservation_id": reservation_id})

    def as_dict(self) -> dict[str, Any]:
        self._refresh_from_db()
        if self.path:
            with sqlite3.connect(self.path) as db:
                rows = db.execute("SELECT reservation_id,call_id,amount_cny,actual_cny,status,usage_json,returned_model,finish_reason,request_id,raw_response_sha256,status_code,provider_error_code,key_index,attempt,request_metadata_json FROM reservations ORDER BY created_at,reservation_id").fetchall()
            reservations = []
            for row in rows:
                item = {"reservation_id": row[0], "call_id": row[1], "amount_cny": row[2], "actual_cny": row[3], "status": row[4]}
                telemetry = {"usage": json.loads(row[5]) if row[5] else None, "returned_model": row[6], "finish_reason": row[7], "request_id": row[8], "raw_response_sha256": row[9], "status_code": row[10], "provider_error_code": row[11], "key_index": row[12], "attempt": row[13]}
                if row[14]:
                    telemetry.update(json.loads(row[14]))
                if any(value is not None for value in telemetry.values()):
                    item["telemetry"] = telemetry
                reservations.append(item)
        else:
            reservations = list(self.reservations)
        return {"limit_cny": self.limit_cny, "reserved_cny": self.reserved_cny, "actual_cny": self.actual_cny, "ledger_path": str(self.path) if self.path else "", "reservations": reservations}


class QwenDirectClient:
    """Direct Qwen client with explicit model and opt-in SSE transport."""

    def __init__(
        self,
        *,
        model: str = "qwen3.7-flash",
        key_file: str | Path | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout_seconds: float = 300.0,
        max_output_tokens: int = 32768,
        thinking: bool = True,
        thinking_budget: int = 8192,
        json_mode: bool = True,
        max_keys: int = 3,
        raw_response_dir: str | Path | None = None,
        budget_ledger: GlobalBudgetLedger | None = None,
        reserved_attempt_cny: float | None = None,
        prompt_token_counter: Callable[[bytes, Sequence[Mapping[str, Any]]], int] | None = None,
        prompt_token_multiplier: float = 1.0,
        prompt_token_framing_margin: int = 0,
        stream_overall_timeout_seconds: float = 3600.0,
    ):
        self.model = str(model)
        pricing = model_pricing(self.model)
        if _economy_enabled() and self.model != "qwen3.7-flash":
            raise QwenTransportError("economy_text_ceiling_would_downgrade_explicit_model", transient=False)
        self.key_file = Path(key_file) if key_file else None
        self.base_url = str(base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.max_retries = max(0, int(max_retries))
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.max_output_tokens = max(64, int(max_output_tokens))
        if self.max_output_tokens > int(pricing["max_output_tokens"]):
            raise QwenTransportError("model_max_output_exceeded", transient=False, record={"model": self.model, "requested": self.max_output_tokens, "max_output_tokens": pricing["max_output_tokens"]})
        self.thinking = bool(thinking)
        self.json_mode = bool(json_mode)
        if self.thinking and self.json_mode and not bool(pricing["thinking_json"]):
            raise QwenTransportError("thinking_json_unsupported_for_model", transient=False, record={"model": self.model, "thinking": True, "response_format": {"type": "json_object"}})
        try:
            configured_thinking_budget = int(thinking_budget)
        except (TypeError, ValueError) as exc:
            raise QwenTransportError("invalid_thinking_budget", transient=False, record={"model": self.model}) from exc
        if configured_thinking_budget < 0:
            raise QwenTransportError("invalid_thinking_budget", transient=False, record={"model": self.model})
        # Keep the configured allowance even when the default is non-thinking:
        # an explicit per-call thinking=True must be honored with that budget.
        self._configured_thinking_budget = configured_thinking_budget
        self.thinking_budget = configured_thinking_budget if self.thinking else 0
        if self.max_output_tokens + self.thinking_budget > int(pricing["max_output_tokens"]):
            raise QwenTransportError(
                "model_total_output_exceeded",
                transient=False,
                record={
                    "model": self.model,
                    "requested_output_tokens": self.max_output_tokens,
                    "thinking_budget": self.thinking_budget,
                    "max_output_tokens": pricing["max_output_tokens"],
                },
            )
        self.max_keys = max(1, int(max_keys))
        self.raw_response_dir = Path(raw_response_dir) if raw_response_dir else None
        self.budget_ledger = budget_ledger
        self.reserved_attempt_cny = reserved_attempt_cny
        self.prompt_token_counter = prompt_token_counter
        try:
            self.prompt_token_multiplier = float(prompt_token_multiplier)
            self.prompt_token_framing_margin = int(prompt_token_framing_margin)
        except (TypeError, ValueError) as exc:
            raise QwenTransportError("invalid_prompt_token_estimator", transient=False) from exc
        if self.prompt_token_multiplier < 1.0 or self.prompt_token_framing_margin < 0:
            raise QwenTransportError("invalid_prompt_token_estimator", transient=False)
        try:
            self.stream_overall_timeout_seconds = max(self.timeout_seconds, float(stream_overall_timeout_seconds))
        except (TypeError, ValueError) as exc:
            raise QwenTransportError("invalid_stream_overall_timeout", transient=False) from exc
        if self.stream_overall_timeout_seconds <= 0:
            raise QwenTransportError("invalid_stream_overall_timeout", transient=False)

    def _prompt_token_estimate(self, request_bytes: bytes, messages: Sequence[Mapping[str, Any]]) -> int:
        if self.prompt_token_counter is None:
            return _conservative_prompt_token_upper_bound(request_bytes, messages)
        try:
            measured = int(self.prompt_token_counter(request_bytes, messages))
        except Exception as exc:
            raise QwenTransportError(
                "prompt_token_estimator_failed",
                transient=False,
                record={"error": type(exc).__name__},
            ) from exc
        if measured < 0:
            raise QwenTransportError("prompt_token_estimator_returned_negative", transient=False)
        return max(1, int(measured * self.prompt_token_multiplier + 0.999999) + self.prompt_token_framing_margin)

    def _keys(self) -> list[str]:
        try:
            from config.qwen_config import get_qwen_api_key_candidates_ordered
            rows = get_qwen_api_key_candidates_ordered(self.key_file)
            keys = [_text(row.get("api_key")) for row in rows if isinstance(row, Mapping) and _text(row.get("api_key"))]
        except Exception as exc:
            raise MissingCredentialError("qwen_key_resolution_failed", record={"error": type(exc).__name__}) from exc
        if not keys:
            raise MissingCredentialError("qwen_credentials_missing")
        return keys

    @staticmethod
    def _message_content(payload: Mapping[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, Sequence) or not choices:
            return ""
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
        content = message.get("content")
        if isinstance(content, list):
            return "".join(_text(item.get("text")) if isinstance(item, Mapping) else _text(item) for item in content)
        return _text(content)

    def __call__(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
        call_id = _text(kwargs.get("call_id")) or "call-" + uuid.uuid4().hex[:16]
        safe_call_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in call_id)[:100] or "call"
        model = _text(kwargs.get("model")) or self.model
        if model != self.model:
            raise QwenTransportError("runtime_model_mismatch", transient=False, record={"requested": model, "configured": self.model})
        pricing = model_pricing(model)
        thinking = bool(kwargs.get("thinking", self.thinking))
        stream = bool(kwargs.get("stream", False))
        transport_observer = kwargs.get("transport_observer")
        audit_enabled = stream or transport_observer is not None
        audit_events: list[dict[str, Any]] = []
        audit_sequence = 0

        def record_transport_event(event: Mapping[str, Any]) -> None:
            nonlocal audit_sequence
            if not audit_enabled:
                return
            audit_sequence += 1
            # Keep only a bounded structured audit in memory. Raw SSE is
            # written incrementally and is the complete transport evidence.
            stage = _text(event.get("stage"))
            event_count = event.get("event_count")
            keep = len(audit_events) < 16 or stage in {"stream_done", "stream_error", "stream_eof", "stream_complete", "transport_exception"}
            if isinstance(event_count, int) and event_count > 0 and event_count % 128 == 0:
                keep = True
            if keep and len(audit_events) < 64:
                item = dict(event)
                item["sequence"] = audit_sequence
                audit_events.append(item)
            _emit_transport_event(transport_observer, event)

        try:
            stream_overall_timeout_seconds = max(
                self.timeout_seconds,
                float(kwargs.get("stream_overall_timeout_seconds", self.stream_overall_timeout_seconds)),
            )
        except (TypeError, ValueError) as exc:
            raise QwenTransportError("invalid_stream_overall_timeout", transient=False, record={"model": model}) from exc
        if stream_overall_timeout_seconds <= 0:
            raise QwenTransportError("invalid_stream_overall_timeout", transient=False, record={"model": model})
        if thinking and self.json_mode and not bool(pricing["thinking_json"]):
            raise QwenTransportError("thinking_json_unsupported_for_model", transient=False, record={"model": model, "thinking": True, "response_format": {"type": "json_object"}})
        if self.json_mode and not any("json" in _text(item.get("content")).casefold() for item in messages if isinstance(item, Mapping)):
            raise QwenTransportError("json_keyword_required_for_json_object", transient=False, record={"model": model})
        requested_output_tokens = int(kwargs.get("max_output_tokens") or self.max_output_tokens)
        if requested_output_tokens > int(pricing["max_output_tokens"]):
            raise QwenTransportError("model_max_output_exceeded", transient=False, record={"model": model, "requested": requested_output_tokens, "max_output_tokens": pricing["max_output_tokens"]})
        if requested_output_tokens < 64:
            requested_output_tokens = 64
        try:
            thinking_budget = int(kwargs.get("thinking_budget", self._configured_thinking_budget)) if thinking else 0
        except (TypeError, ValueError) as exc:
            raise QwenTransportError("invalid_thinking_budget", transient=False, record={"model": model}) from exc
        if thinking_budget < 0:
            raise QwenTransportError("invalid_thinking_budget", transient=False, record={"model": model})
        total_output_tokens = requested_output_tokens + thinking_budget
        if total_output_tokens > int(pricing["max_output_tokens"]):
            raise QwenTransportError(
                "model_total_output_exceeded",
                transient=False,
                record={
                    "model": model,
                    "requested_output_tokens": requested_output_tokens,
                    "thinking_budget": thinking_budget,
                    "max_output_tokens": pricing["max_output_tokens"],
                },
            )
        # For these supported Qwen generations, max_completion_tokens counts
        # reasoning + answer. Sending answer alone would steal answer capacity.
        # Keep a legacy max_tokens path for explicitly registered older models.
        token_parameter = pricing.get("completion_token_parameter", "max_tokens")
        body = {
            "model": model,
            "messages": [dict(item) for item in messages],
            token_parameter: total_output_tokens if token_parameter == "max_completion_tokens" else requested_output_tokens,
            "temperature": float(kwargs.get("temperature", 0.1)),
            "enable_thinking": thinking,
            "stream": stream,
        }
        if stream:
            # DashScope includes token usage in the final SSE event only when
            # explicitly requested. This is opt-in; the default non-stream
            # request remains byte-for-byte compatible.
            body["stream_options"] = {"include_usage": True}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if thinking:
            body["thinking_budget"] = thinking_budget
        effective_request = {
            "model": model,
            "enable_thinking": thinking,
            "thinking_budget": thinking_budget,
            "answer_tokens": requested_output_tokens,
            "total_output_tokens": total_output_tokens,
            "max_completion_tokens": body.get("max_completion_tokens"),
            "max_tokens": body.get("max_tokens"),
            "response_format": body.get("response_format"),
        }
        if stream:
            effective_request["stream"] = True
            effective_request["stream_options"] = {"include_usage": True}
        if thinking and pricing.get("thinking_budget_maps_to_effort"):
            # Informational provider mapping, not another request parameter:
            # Qwen3.8 rejects reasoning_effort together with thinking_budget.
            effective_request["mapped_reasoning_effort"] = "low" if thinking_budget <= 4096 else "medium" if thinking_budget <= 16384 else "xhigh"
        request_bytes = _json_bytes(body)
        last_error: QwenTransportError | None = None
        for key_index, key in enumerate(self._keys()[: self.max_keys]):
            for attempt in range(self.max_retries + 1):
                reservation = None
                if self.budget_ledger is not None:
                    prompt_estimate = self._prompt_token_estimate(request_bytes, messages)
                    required_reservation = estimated_cost_cny({"prompt_tokens": prompt_estimate, "completion_tokens": total_output_tokens}, model=model, conservative=True)
                    conservative = float(self.reserved_attempt_cny or 0.0)
                    if conservative > 0 and conservative + 1e-12 < required_reservation:
                        raise QwenTransportError("reserved_attempt_cny_too_low", transient=False, record={"provided_cny": conservative, "required_cny": required_reservation, "model": model})
                    if conservative <= 0:
                        conservative = required_reservation
                    reservation = self.budget_ledger.reserve(conservative, f"{call_id}:key{key_index}:attempt{attempt}")
                reservation_settled = False
                started = time.monotonic()
                request = urllib.request.Request(
                    self.base_url.rstrip("/") + "/chat/completions",
                    data=request_bytes,
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Connection": "close"},
                    method="POST",
                )
                record_transport_event(_stream_event_record(
                    stage="request_open_start", started=started,
                    stream=stream, timeout_seconds=self.timeout_seconds,
                    overall_timeout_seconds=stream_overall_timeout_seconds if stream else None,
                    request_body_bytes=len(request_bytes),
                    request_body_sha256=hashlib.sha256(request_bytes).hexdigest(),
                ))
                partial_path: Path | None = None
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=self.timeout_seconds) as response:
                        status_code = int(getattr(response, "status", 200))
                        headers = dict(response.headers.items()) if getattr(response, "headers", None) else {}
                        record_transport_event(_stream_event_record(
                            stage="response_opened", started=started,
                            status_code=status_code, headers=_safe_response_headers(headers),
                        ))
                        if stream:
                            self.raw_response_dir.mkdir(parents=True, exist_ok=True) if self.raw_response_dir else None
                            if self.raw_response_dir:
                                partial_path = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.sse.partial")
                            else:
                                fd, temp_name = tempfile.mkstemp(prefix=safe_call_id + "-", suffix=".sse.partial")
                                os.close(fd)
                                partial_path = Path(temp_name)
                            stream_result = _stream_response(
                                response,
                                started=started,
                                observer=record_transport_event,
                                raw_path=partial_path,
                                overall_timeout_seconds=stream_overall_timeout_seconds,
                            )
                            raw = stream_result["raw_bytes"]
                            if self.raw_response_dir:
                                target = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.sse.raw")
                                os.replace(partial_path, target)
                                partial_path = target
                            else:
                                partial_path.unlink(missing_ok=True)
                                partial_path = None
                            payload = stream_result["normalized"]
                            record_transport_event(_stream_event_record(
                                stage="read_complete", started=started,
                                bytes=len(raw), stream=True,
                            ))
                        else:
                            record_transport_event(_stream_event_record(stage="read_start", started=started, stream=False))
                            raw = response.read()
                            record_transport_event(_stream_event_record(
                                stage="read_complete", started=started,
                                bytes=len(raw), stream=False,
                            ))
                            if self.raw_response_dir:
                                self.raw_response_dir.mkdir(parents=True, exist_ok=True)
                                temp = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.tmp")
                                target = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.raw")
                                temp.write_bytes(raw)
                                os.replace(temp, target)
                    try:
                        if not stream:
                            payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise QwenTransportError("qwen_response_not_json", transient=False, status_code=status_code, record={"raw_sha256": hashlib.sha256(raw).hexdigest()}) from exc
                    if not isinstance(payload, Mapping):
                        raise QwenTransportError("qwen_response_not_object", transient=False, status_code=status_code)
                    choices = payload.get("choices") if isinstance(payload.get("choices"), Sequence) else []
                    choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
                    finish_reason = _text(choice.get("finish_reason"))
                    content = stream_result["content"] if stream else self._message_content(payload)
                    usage_payload = stream_result["usage"] if stream else payload.get("usage")
                    complete = finish_reason == "stop" and bool(content)
                    result = {
                        "content": content,
                        "raw_response": raw.decode("utf-8", errors="replace"),
                        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                        "requested_model": model,
                        "returned_model": _text(payload.get("model")) or None,
                        "finish_reason": finish_reason,
                        "complete": complete,
                        "request_id": _text((stream_result.get("request_id") if stream else payload.get("id")) or headers.get("x-request-id") or headers.get("X-Request-ID")),
                        "usage": dict(usage_payload or {}) if isinstance(usage_payload, Mapping) else {},
                        "status_code": status_code,
                        "call_id": call_id,
                        "attempt": attempt + 1,
                        "key_index": key_index,
                        "elapsed_seconds": time.monotonic() - started,
                        "effective_request": dict(effective_request),
                    }
                    if stream:
                        result["normalized_response"] = payload
                        result["normalized_response_json"] = _json_bytes(payload).decode("utf-8")
                        result["raw_stream_sha256"] = hashlib.sha256(raw).hexdigest()
                        result["reasoning_content"] = stream_result.get("reasoning_content", "")
                        result["raw_stream_path"] = str(partial_path) if partial_path else None
                        result["stream_event_count"] = stream_result.get("event_count", 0)
                    if audit_events:
                        result["transport_events"] = list(audit_events)
                    result["cap_pressure"] = _cap_pressure(result["usage"], effective_request, finish_reason)
                    if self.budget_ledger is not None and reservation:
                        usage = result.get("usage") or {}
                        telemetry = {"usage": usage, "returned_model": result.get("returned_model"), "finish_reason": result.get("finish_reason"), "request_id": result.get("request_id"), "raw_response_sha256": result.get("raw_response_sha256"), "status_code": status_code, "effective_request": dict(effective_request), "cap_pressure": result["cap_pressure"], "stream": stream, "transport_events": list(audit_events)}
                        has_usage = usage and (usage.get("prompt_tokens") is not None or usage.get("input_tokens") is not None) and (usage.get("completion_tokens") is not None or usage.get("output_tokens") is not None)
                        if has_usage:
                            self.budget_ledger.settle(reservation["reservation_id"], estimated_cost_cny(usage, model=model), uncertain=False, telemetry=telemetry)
                            reservation_settled = True
                        else:
                            self.budget_ledger.settle(reservation["reservation_id"], None, uncertain=True, telemetry=telemetry)
                            reservation_settled = True
                    actual_model = _text(result.get("returned_model"))
                    if actual_model and actual_model != model:
                        raise QwenTransportError("qwen_returned_model_mismatch", transient=False, status_code=status_code, record=result)
                    if not complete:
                        raise QwenTransportError("qwen_incomplete_response", transient=False, status_code=status_code, record=result)
                    return result
                except QwenTransportError as exc:
                    exc.record.setdefault("effective_request", dict(effective_request))
                    exc.record.setdefault("stream", stream)
                    if audit_events:
                        exc.record.setdefault("transport_events", list(audit_events))
                    if partial_path is not None:
                        exc.record.setdefault("partial_raw_path", str(partial_path))
                    if self.budget_ledger is not None and reservation and not reservation_settled:
                        self.budget_ledger.settle(
                            reservation["reservation_id"], None, uncertain=True,
                            telemetry=_uncertain_telemetry(exc, key_index=key_index, attempt=attempt, effective_request=effective_request),
                        )
                        reservation_settled = True
                    raise
                except urllib.error.HTTPError as exc:
                    raw = exc.read() if hasattr(exc, "read") else b""
                    status = int(exc.code)
                    if self.raw_response_dir:
                        self.raw_response_dir.mkdir(parents=True, exist_ok=True)
                        temp = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.tmp")
                        target = self.raw_response_dir / (safe_call_id + f"-key{key_index}-attempt{attempt}.raw")
                        temp.write_bytes(raw)
                        os.replace(temp, target)
                    headers = getattr(exc, "headers", None) or {}
                    request_id = _provider_request_id(raw, headers)
                    error_code = _safe_error_code(raw)
                    rotate_key = _account_rejection(status, error_code)
                    provider_error_code = error_code or (f"http_{status}")
                    record = {
                        "status_code": status, "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                        "call_id": call_id, "request_id": request_id or None, "provider_error_code": provider_error_code,
                        "key_index": key_index, "attempt": attempt + 1, "rotate_key": rotate_key,
                        "effective_request": dict(effective_request),
                    }
                    if audit_events:
                        record["transport_events"] = list(audit_events)
                    transient = status in TRANSIENT_HTTP
                    last_error = QwenTransportError("qwen_account_rejection" if rotate_key else "qwen_http_error", transient=transient, status_code=status, record=record)
                    if self.budget_ledger is not None and reservation and not reservation_settled:
                        telemetry = {"usage": None, "request_id": request_id or None, "raw_response_sha256": record["raw_response_sha256"], "status_code": status, "provider_error_code": provider_error_code, "key_index": key_index, "attempt": attempt + 1, "effective_request": dict(effective_request), "stream": stream, "transport_events": list(audit_events)}
                        # A definitive 4xx, including an account rejection,
                        # consumed no model tokens.  Server errors and rate
                        # limits remain uncertain because the provider may
                        # have accepted the request before returning the
                        # error.
                        self.budget_ledger.settle(reservation["reservation_id"], 0.0, uncertain=False, telemetry=telemetry) if not last_error.transient else self.budget_ledger.settle(reservation["reservation_id"], None, uncertain=True, telemetry=telemetry)
                        reservation_settled = True
                    if not last_error.transient:
                        if rotate_key:
                            break
                        raise last_error
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    transport_record = _transport_exception_record(exc)
                    record_transport_event(_stream_event_record(
                        stage="transport_exception", started=started,
                        error=transport_record.get("error"),
                        reason_code=transport_record.get("reason_code"),
                    ))
                    last_error = QwenTransportError(
                        "qwen_transport_error",
                        transient=True,
                        record={
                            "call_id": call_id,
                            "effective_request": dict(effective_request),
                            "stream": stream,
                            "transport_events": list(audit_events),
                            **transport_record,
                        },
                        reason_code=transport_record["reason_code"],
                    )
                if last_error and last_error.record.get("rotate_key"):
                    # Account/auth rejection is definitive for this key.  Do
                    # not spend retry attempts on the same credential; move
                    # to the next bounded key in the pool.
                    break
                if last_error and not last_error.transient:
                    if self.budget_ledger is not None and reservation and not reservation_settled:
                        self.budget_ledger.settle(
                            reservation["reservation_id"], None, uncertain=True,
                            telemetry=_uncertain_telemetry(last_error, key_index=key_index, attempt=attempt, effective_request=effective_request),
                        )
                        reservation_settled = True
                    raise last_error
                if self.budget_ledger is not None and reservation and not reservation_settled:
                    self.budget_ledger.settle(
                        reservation["reservation_id"], None, uncertain=True,
                        telemetry=_uncertain_telemetry(last_error, key_index=key_index, attempt=attempt, effective_request=effective_request),
                    )
                    reservation_settled = True
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
            if last_error is not None and not last_error.record.get("rotate_key"):
                raise last_error
        raise last_error or QwenTransportError("qwen_call_failed", transient=False)


def invoke_client(client: Any, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Call an injected test/live client without hiding its telemetry."""

    if client is None:
        raise QwenTransportError("reader_client_required", transient=False)
    if hasattr(client, "complete") and callable(client.complete):
        result = client.complete(messages, **kwargs)
    elif callable(client):
        result = client(messages, **kwargs)
    else:
        raise QwenTransportError("reader_client_not_callable", transient=False)
    if isinstance(result, Mapping):
        return dict(result)
    return {"content": result, "complete": True, "finish_reason": "stop"}


def estimated_cost_cny(
    usage: Mapping[str, Any],
    *,
    model: str = "qwen3.7-flash",
    input_rate: float | None = None,
    output_rate: float | None = None,
    conservative: bool = False,
) -> float:
    """Estimate CNY from provider usage using the model's Beijing tiers.

    Missing token counters are an accounting error, never free usage.  The
    conservative mode uses the highest applicable tier for both counters for
    a pre-dispatch reservation; settled usage uses the exact input tier.
    """

    inp_value = usage.get("prompt_tokens") if usage.get("prompt_tokens") is not None else usage.get("input_tokens")
    out_value = usage.get("completion_tokens") if usage.get("completion_tokens") is not None else usage.get("output_tokens")
    if inp_value is None or out_value is None:
        raise QwenTransportError("usage_token_counts_missing", transient=False, record={"model": model, "usage_keys": sorted(str(key) for key in usage)})
    inp = max(0, int(float(inp_value)))
    out = max(0, int(float(out_value)))
    if input_rate is None or output_rate is None:
        pricing = model_pricing(model)
        if conservative:
            _, input_rate, output_rate = pricing["tiers"][-1]
        else:
            input_rate, output_rate = _tier_rates(model, max(1, inp))
    return (inp * float(input_rate) + out * float(output_rate)) / 1_000_000.0


__all__ = ["QwenDirectClient", "QwenTransportError", "MissingCredentialError", "GlobalBudgetLedger", "invoke_client", "estimated_cost_cny", "model_pricing", "MODEL_PRICING_CNY"]
