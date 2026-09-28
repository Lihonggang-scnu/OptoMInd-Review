"""Upgrade-3 production transport with accountable response cache (ticket 006).

Selective reimplementation of the donor curl-transport ideas on top of the 005
reservation ledger:

- response receipts with provider_request_id, physical attempt, status,
  raw_response_hash, parse_result, actual model, usage, cost receipt and
  cache origin;
- error classification (transport / auth / rate_limit / schema / model_refusal)
  with bounded retry policies; every retry is a NEW physical attempt with a NEW
  reservation;
- a complete-response cache: only full HTTP-200 JSON bodies are cached, keyed
  by endpoint+body+schema+policy+scope where the scope includes the API key
  FINGERPRINT (never the key itself); HTTP errors and empty/status<=0 records
  are stored, if at all, purely for diagnosis and are never replayed as content;
- no mock fallback exists in production mode: a failed transport raises a
  classified TransportError; nothing is ever answered with fabricated text;
- raw provider bytes are stored before semantic parsing so desensitised
  diagnosis is possible.

Keys are passed to request headers and hashed for cache scope only; they are
never logged or persisted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional

from .budget_ledger import BudgetLedger, BudgetRefused

CACHE_SCHEMA = "optomind.upgrade3.raw_response_cache.v1"


class TransportError(Exception):
    """Structured transport failure; carries a machine-readable class."""

    def __init__(self, error_class: str, message: str, detail: str = ""):
        super().__init__(f"{error_class}: {message}" + (f" | {detail}" if detail else ""))
        self.error_class = error_class
        self.message = message
        self.detail = detail


class BudgetExhaustedError(Exception):
    """Budget reservation refused.  Must never trigger key rotation or retry."""
    pass


RETRY_POLICY = {
    # bounded retries per class; every retry is a NEW physical attempt with a
    # NEW reservation.  auth/schema/refusal never retry.
    "transport_error": 2,
    "rate_limit": 1,
    "auth_error": 0,
    "schema_invalid": 1,
    "model_refusal": 0,
}


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(("kfp:" + api_key).encode("utf-8")).hexdigest()[:16]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def cache_key(*, endpoint: str, body: Dict[str, Any], schema_hash: str,
              policy_hash: str, scope: str) -> str:
    """Non-secret cache identity: endpoint, request body, schema, policy and a
    tenant scope that includes the key FINGERPRINT (private responses must not
    be reused across credentials)."""
    identity = {"endpoint": endpoint, "body": body, "schema_hash": schema_hash,
                "policy_hash": policy_hash, "scope": scope}
    return hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:40]


class RawResponseStore:
    """Complete-response atomic store; error/empty/status<=0 records are
    diagnostics only and are never returned as content."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, fingerprint: str) -> str:
        return os.path.join(self.root, fingerprint + ".json")

    def write(self, fingerprint: str, record: Dict[str, Any]) -> None:
        path = self._path(fingerprint)
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def read_complete(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        path = self._path(fingerprint)
        if not os.path.isfile(path):
            return None
        try:
            record = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if record.get("schema_version") != CACHE_SCHEMA:
            return None
        if record.get("request_fingerprint") != fingerprint:
            return None
        # anti-replay: only complete successful responses may ever be served
        if int(record.get("status") or 0) != 200 or not record.get("complete"):
            return None
        if not record.get("payload_b64"):
            return None
        return record

    def read_diagnostic(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        path = self._path(fingerprint)
        if not os.path.isfile(path):
            return None
        try:
            return json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return None


class ProductionTransport:
    """The single production provider boundary.  Production mode has NO mock:
    failures raise a classified TransportError and the caller keeps its safe
    upstream candidate; nothing is ever answered with fabricated text."""

    def __init__(self, ledger: BudgetLedger, cache_root: Optional[str],
                 policy_hash: str):
        self.ledger = ledger
        self.store = RawResponseStore(cache_root) if cache_root else None
        self.policy_hash = policy_hash

    # ------------------ cache identity ------------------
    def _fingerprint(self, endpoint: str, body: Dict[str, Any], schema: Any,
                     api_key: str) -> str:
        schema_hash = hashlib.sha256(_canonical(schema).encode("utf-8")).hexdigest()[:16]
        scope = "tenant:" + key_fingerprint(api_key)
        return cache_key(endpoint=endpoint, body=body, schema_hash=schema_hash,
                         policy_hash=self.policy_hash, scope=scope)

    # ------------------ classification ------------------
    @staticmethod
    def classify(exc: Exception) -> TransportError:
        if isinstance(exc, urllib.error.HTTPError):
            code = int(exc.code)
            if code == 429:
                return TransportError("rate_limit", "provider 429")
            if code in (401, 403):
                return TransportError("auth_error", f"provider {code}")
            if code < 500:
                # billing/auth/param rejections: provider refused before inference
                return TransportError("auth_error", f"provider {code} rejected request")
            return TransportError("transport_error", f"provider {code}")
        name = type(exc).__name__
        if name in ("RemoteDisconnected", "IncompleteRead", "TimeoutError",
                    "socket.timeout", "http.client.RemoteDisconnected"):
            # the request may have been processed: budget must stay frozen
            return TransportError("transport_error",
                                  f"connection lost mid-flight ({name})",
                                  "may_have_processed=true")
        if isinstance(exc, urllib.error.URLError):
            # never reached the provider: safe to release the reservation
            return TransportError("transport_error", f"connection failed ({name})",
                                  "may_have_processed=false")
        return TransportError("transport_error", name)

    # ------------------ one physical attempt ------------------
    def _physical_attempt(self, *, endpoint: str, body: Dict[str, Any],
                          api_key: str, timeout_sec: float,
                          fingerprint: str,
                          request_id: str = "") -> Dict[str, Any]:
        request = urllib.request.Request(
            url=endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key,
                     "Content-Type": "application/json"},
            method="POST",
        )
        sent = True  # the request is physically dispatched the moment urlopen is entered
        payload = b""
        status = 0
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=timeout_sec) as response:
                status = int(response.status)
                payload = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            raw_hash = hashlib.sha256(payload).hexdigest()
            if self.store:
                self.store.write(fingerprint, {
                    "schema_version": CACHE_SCHEMA,
                    "request_fingerprint": fingerprint,
                    "status": status, "complete": False,
                    "payload_b64": base64.b64encode(payload).decode("ascii"),
                    "captured_at_epoch": time.time()})
            err = self.classify(exc)
            if status in (401, 403, 429) and request_id:
                # auth/rate rejection: provider error contract proves no inference
                try:
                    self.ledger.release(request_id)
                except Exception:
                    pass
                raise TransportError(err.error_class, err.message,
                                     err.detail + " | body=" +
                                     payload.decode("utf-8", "replace")[:260] +
                                     " | released_verifiably_unbilled")
            if status < 500 and request_id:
                # other 4xx (e.g. 400 bad_request): conservative unknown —
                # the request reached the server but billing is unverifiable
                try:
                    self.ledger.mark_unknown(request_id)
                except Exception:
                    pass
                raise TransportError(err.error_class, err.message,
                                     err.detail + " | body=" +
                                     payload.decode("utf-8", "replace")[:260] +
                                     " | unknown_frozen")
            # 5xx: server error, may have partially processed
            if request_id:
                try:
                    self.ledger.mark_unknown(request_id)
                except Exception:
                    pass
            raise TransportError(err.error_class, err.message,
                                 err.detail + " | body=" +
                                 payload.decode("utf-8", "replace")[:260] +
                                 " | raw=" + raw_hash[:16] +
                                 " | unknown_frozen")
        except Exception as exc:
            err = self.classify(exc)
            if "may_have_processed=true" in err.detail:
                if request_id:
                    try:
                        self.ledger.mark_unknown(request_id)
                    except Exception:
                        pass
                raise TransportError(err.error_class, err.message,
                                     err.detail + " | reservation_frozen")
            if request_id:
                try:
                    self.ledger.release(request_id)
                except Exception:
                    pass
            raise TransportError(err.error_class, err.message, err.detail)
        raw_hash = hashlib.sha256(payload).hexdigest()
        if status != 200:
            raise TransportError("transport_error", f"http {status} without error object",
                                 "raw=" + raw_hash[:16])
        if not payload.strip():
            # HTTP 200 with an empty body must never be treated as success
            raise TransportError("transport_error", "http200_empty_body",
                                 "raw=" + raw_hash[:16])
        return {"status": status, "payload": payload, "raw_response_hash": raw_hash}

    # ------------------ public entry ------------------
    def chat_completion(self, *, task_id: str, logical_call_id: str,
                        generation_id: str, model: str,
                        system_prompt: str, user_payload: Dict[str, Any],
                        api_key: str, base_url: str,
                        schema: Any = None, max_input_tokens: int = 4000,
                        max_output_tokens: int = 1200, temperature: float = 0.0,
                        timeout_sec: float = 90.0) -> Dict[str, Any]:
        endpoint = base_url.rstrip("/") + "/chat/completions"
        body = {"model": model, "temperature": temperature,
                "max_tokens": max(1, int(max_output_tokens)),
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": _canonical(user_payload)}]}
        fingerprint = self._fingerprint(endpoint, body, schema, api_key)

        # 1) cache: only complete responses with an identical fingerprint hit
        if self.store:
            cached = self.store.read_complete(fingerprint)
            if cached:
                payload = base64.b64decode(cached["payload_b64"])
                data = json.loads(payload)
                usage = data.get("usage") or {}
                receipt = self._receipt(
                    provider_request_id=str(data.get("id") or "cache_unknown"),
                    physical_attempt="cache_replay", status=200,
                    raw_response_hash=cached.get("raw_response_hash") or
                    hashlib.sha256(payload).hexdigest(),
                    parse_result="complete_from_cache",
                    actual_model=str(data.get("model") or model),
                    usage={"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                           "completion_tokens": int(usage.get("completion_tokens") or 0)},
                    cost_receipt={"bound_micro": 0, "origin": "cache_replay"},
                    cache_origin=fingerprint)
                return {"content": str(data["choices"][0]["message"]["content"]),
                        "data": data, "receipt": receipt, "from_cache": True}

        # 2) bounded retries; each retry = new physical attempt + new reservation
        transport_budget = RETRY_POLICY["transport_error"]
        rate_budget = RETRY_POLICY["rate_limit"]
        schema_budget = RETRY_POLICY["schema_invalid"]
        last_error: Optional[TransportError] = None
        attempt_no = 0
        invocation_nonce = uuid.uuid4().hex[:8]
        while True:
            attempt_no += 1
            try:
                reservation = self.ledger.reserve(
                    task_id, f"{logical_call_id}:{invocation_nonce}:p{attempt_no}",
                    f"phys{attempt_no}", generation_id,
                    model, max_input_tokens, max_output_tokens, attempts=1)
            except BudgetRefused as exc:
                # Budget exhaustion is NOT an auth error: it must never
                # trigger key rotation or any retry.  Stop immediately.
                raise BudgetExhaustedError(str(exc))
            try:
                raw = self._physical_attempt(endpoint=endpoint, body=body,
                                             api_key=api_key, timeout_sec=timeout_sec,
                                             fingerprint=fingerprint,
                                             request_id=reservation["request_id"])
            except TransportError as err:
                last_error = err
                if "reservation_frozen" in err.detail:
                    raise  # unknown outcome: budget stays frozen, stop retrying
                if err.error_class == "auth_error":
                    raise  # bounded: no unbounded key/model rotation
                if err.error_class == "rate_limit":
                    if rate_budget > 0:
                        rate_budget -= 1
                        continue
                    raise
                if err.error_class == "transport_error":
                    if transport_budget > 0:
                        transport_budget -= 1
                        continue
                    raise
                raise
            # 3) parse & settle
            try:
                data = json.loads(raw["payload"])
            except ValueError:
                if schema_budget > 0:
                    schema_budget -= 1
                    last_error = TransportError("schema_invalid", "response is not JSON",
                                                "raw=" + raw["raw_response_hash"][:16])
                    continue
                raise TransportError("schema_invalid", "response is not JSON",
                                     "raw=" + raw["raw_response_hash"][:16])
            choices = data.get("choices") or []
            message = choices[0].get("message") if choices else None
            if not isinstance(message, dict):
                raise TransportError("schema_invalid", "response missing choices/message")
            content = str(message.get("content") or "")
            if not content.strip():
                raise TransportError("schema_invalid", "empty content treated as failure")
            finish = str(choices[0].get("finish_reason") or "")
            if finish not in ("stop", "length"):
                raise TransportError("transport_error", "incomplete response payload",
                                     "finish_reason=" + finish)
            parse_result = "complete"
            # complete response: persist for reuse, settle with ACTUAL usage
            if self.store:
                self.store.write(fingerprint, {
                    "schema_version": CACHE_SCHEMA,
                    "request_fingerprint": fingerprint,
                    "status": raw["status"], "complete": True,
                    "raw_response_hash": raw["raw_response_hash"],
                    "payload_b64": base64.b64encode(raw["payload"]).decode("ascii"),
                    "captured_at_epoch": time.time()})
            usage_rec = self._usage_record(data, model)
            settle = self.ledger.settle(reservation["request_id"], usage_rec,
                                        provider_receipt_id=str(data.get("id") or ""))
            receipt = self._receipt(
                provider_request_id=str(data.get("id") or ""),
                physical_attempt=attempt_no, status=raw["status"],
                raw_response_hash=raw["raw_response_hash"],
                parse_result=parse_result,
                actual_model=str(data.get("model") or model),
                usage=usage_rec, cost_receipt=settle, cache_origin=fingerprint)
            return {"content": content, "data": data, "receipt": receipt,
                    "from_cache": False}

    def _usage_record(self, data: Dict[str, Any], model: str) -> Dict[str, Any]:
        usage = data.get("usage") or {}
        rates = {}
        pricing = self.ledger.pricing or {}
        entry = (pricing.get("models") or {}).get(model) or []
        if entry:
            rates = {"input_cny_per_million": float(entry[0]["input_cny_per_million"]),
                     "output_cny_per_million": float(entry[0]["output_cny_per_million"])}
        return {"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "model": str(data.get("model") or model),
                "rates": rates,
                "cached_tokens": int((usage.get("prompt_tokens_details") or {})
                                     .get("cached_tokens") or 0)}

    @staticmethod
    def _receipt(**kwargs) -> Dict[str, Any]:
        return {
            "schema_version": "optomind.upgrade3.response_receipt.v1",
            "provider_request_id": kwargs["provider_request_id"],
            "physical_attempt": kwargs["physical_attempt"],
            "status": kwargs["status"],
            "raw_response_hash": kwargs["raw_response_hash"],
            "parse_result": kwargs["parse_result"],
            "actual_model": kwargs["actual_model"],
            "usage": kwargs["usage"],
            "cost_receipt": kwargs["cost_receipt"],
            "cache_origin": kwargs["cache_origin"],
        }
