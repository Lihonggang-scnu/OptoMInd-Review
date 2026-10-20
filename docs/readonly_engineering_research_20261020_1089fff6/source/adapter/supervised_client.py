"""Two durable review gates around an injected callable; no provider or pricing code."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid


class SupervisionError(RuntimeError):
    pass


_THREAD_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_FORBIDDEN = {"api_key", "api-key", "authorization", "headers", "http_headers",
              "key_file", "credentials", "credential", "password", "secret",
              "access_token", "refresh_token", "bearer_token"}
_SAFE_RESPONSE_HEADERS = {"date", "server", "content-type", "content-length", "transfer-encoding",
                          "connection", "x-request-id", "request-id", "x-dashscope-partialresponse"}


def _json(value):
    """Reject lossy coercions, custom objects, non-string keys and NaN/Infinity."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise SupervisionError("non_string_json_key")
                safe_headers = (key.lower() == "headers" and type(child) is dict
                    and all(type(k) is str and k.lower() in _SAFE_RESPONSE_HEADERS
                            and type(v) is str and len(v) <= 256 for k, v in child.items()))
                if key.lower() in _FORBIDDEN and not safe_headers:
                    raise SupervisionError("credential_or_header_field_forbidden")
                check(child)
            return
        raise SupervisionError("unsafe_non_json_value")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _load(path):
    try:
        def pairs(rows):
            result = {}
            for key, value in rows:
                if key in result:
                    raise SupervisionError("duplicate_json_key")
                result[key] = value
            return result
        value = json.loads(Path(path).read_bytes(), object_pairs_hook=pairs)
        _json(value)
        return value
    except (ValueError, OSError) as exc:
        raise SupervisionError("invalid_or_unreadable_artifact") from exc


def _put_once(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _json(value)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise SupervisionError("immutable_artifact_conflict:" + path.name)
    finally:
        temporary.unlink(missing_ok=True)


def call_directory(root, stage, call_id):
    if not isinstance(stage, str) or not stage.strip() or not isinstance(call_id, str) or not call_id.strip():
        raise SupervisionError("explicit_stage_and_call_id_required")
    identity = _hash({"stage": stage, "call_id": call_id})
    return Path(root).resolve() / "calls" / identity


@contextmanager
def _single_flight(root, poll):
    """One lock per root across threads and processes; OS releases on process death."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = os.path.normcase(str(root))
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.setdefault(name, threading.Lock())
    with lock:
        with (root / ".supervisor.lock").open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    time.sleep(poll)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _response(directory, request_sha256):
    record = _load(directory / "RESPONSE.json")
    if (type(record) is not dict or set(record) != {"schema", "request_sha256", "result_sha256", "result"}
            or record["schema"] != "supervisor.response.v1"
            or record["request_sha256"] != request_sha256
            or record["result_sha256"] != _hash(record["result"])):
        raise SupervisionError("response_integrity_failure")
    return record


_DECISION_KEYS = {"schema", "stage", "call_id", "phase", "request_sha256",
                  "response_sha256", "decision", "reason"}
_RESPONSE_REVIEW_KEYS = {
    "schema", "stage", "call_id", "phase", "reviewer", "review_kind",
    "decision", "reason", "request_sha256", "response_sha256",
    "decision_sha256",
}


def _response_decision(directory, *, stage, call_id, request, response):
    """Load the immutable response decision and bind it to current artifacts."""
    receipt = _load(directory / "RESPONSE_DECISION.json")
    request_sha256 = _hash(request)
    response_sha256 = _hash(response)
    if (type(receipt) is not dict or set(receipt) != _DECISION_KEYS
            or receipt.get("schema") != "supervisor.decision.v1"
            or receipt.get("stage") != stage or receipt.get("call_id") != call_id
            or receipt.get("phase") != "response"
            or receipt.get("request_sha256") != request_sha256
            or receipt.get("response_sha256") != response_sha256
            or receipt.get("decision") not in {"approve", "reject"}):
        raise SupervisionError("decision_integrity_failure")
    return receipt


def _response_review(directory, *, stage, call_id, request, response, decision):
    """Validate an optional root format review without changing old gates."""
    path = directory / "RESPONSE_REVIEW.json"
    if not path.exists():
        return None
    review = _load(path)
    if (type(review) is not dict or set(review) != _RESPONSE_REVIEW_KEYS
            or review.get("schema") != "supervisor.response_review.v1"
            or review.get("stage") != stage or review.get("call_id") != call_id
            or review.get("phase") != "response"
            or review.get("reviewer") != "root"
            or review.get("review_kind") != "format_reparse"
            or review.get("decision") not in {"approve", "reject"}
            or not isinstance(review.get("reason"), str) or not review.get("reason").strip()
            or review.get("request_sha256") != _hash(request)
            or review.get("response_sha256") != _hash(response)
            or review.get("decision_sha256") != _hash(decision)):
        raise SupervisionError("response_review_integrity_failure")
    return review


def _validate_request(request):
    if type(request) is not dict or request.get("schema") != "supervisor.request.v1":
        raise SupervisionError("request_integrity_failure")
    for key in ("messages", "profile", "kwargs"):
        if request.get(key + "_sha256") != _hash(request.get(key)):
            raise SupervisionError("request_integrity_failure")
    return request


def decide(*, root, stage, call_id, phase, decision, reason=""):
    """Root calls this after inspection. No gate lock: the worker holds it while waiting."""
    if phase not in {"request", "response"} or decision not in {"approve", "reject"}:
        raise SupervisionError("invalid_decision")
    directory = call_directory(root, stage, call_id)
    request = _validate_request(_load(directory / "REQUEST.json"))
    if request.get("stage") != stage or request.get("call_id") != call_id:
        raise SupervisionError("request_identity_failure")
    request_hash = _hash(request)
    response_hash = _hash(_response(directory, request_hash)) if phase == "response" else None
    receipt = {"schema": "supervisor.decision.v1", "stage": stage, "call_id": call_id,
               "phase": phase, "request_sha256": request_hash, "response_sha256": response_hash,
               "decision": decision, "reason": reason}
    _put_once(directory / (phase.upper() + "_DECISION.json"), receipt)
    return receipt


def review_response(*, root, stage, call_id, reviewer, decision, reason,
                    review_kind="format_reparse"):
    """Record an explicit root review for a previously rejected response.

    This is an additive artifact.  It never edits RESPONSE_DECISION.json or
    RESPONSE.json, and it only permits the narrowly scoped format reparse
    review.  Provider and ledger calls are outside this function.
    """
    if reviewer != "root":
        raise SupervisionError("root_reviewer_required")
    if review_kind != "format_reparse":
        raise SupervisionError("unsupported_response_review_kind")
    if decision not in {"approve", "reject"}:
        raise SupervisionError("invalid_response_review_decision")
    if not isinstance(reason, str) or not reason.strip():
        raise SupervisionError("response_review_reason_required")
    directory = call_directory(root, stage, call_id)
    request = _validate_request(_load(directory / "REQUEST.json"))
    if request.get("stage") != stage or request.get("call_id") != call_id:
        raise SupervisionError("request_identity_failure")
    response = _response(directory, _hash(request))
    original_decision = _response_decision(
        directory, stage=stage, call_id=call_id, request=request, response=response)
    if original_decision["decision"] != "reject":
        raise SupervisionError("response_review_requires_rejected_response")
    receipt = {
        "schema": "supervisor.response_review.v1",
        "stage": stage,
        "call_id": call_id,
        "phase": "response",
        "reviewer": reviewer,
        "review_kind": review_kind,
        "decision": decision,
        "reason": reason,
        "request_sha256": _hash(request),
        "response_sha256": _hash(response),
        "decision_sha256": _hash(original_decision),
    }
    _put_once(directory / "RESPONSE_REVIEW.json", receipt)
    return receipt


class SupervisedClient:
    def __init__(self, client, *, root, stage, call_id, profile, poll_seconds=0.1, lock_scope="global"):
        if not callable(client):
            raise SupervisionError("callable_client_required")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise SupervisionError("positive_poll_seconds_required")
        required = {"model", "max_output_tokens", "thinking_budget", "max_retries", "max_keys"}
        if type(profile) is not dict or not required.issubset(profile):
            raise SupervisionError("effective_profile_required:" + ",".join(sorted(required)))
        if not isinstance(profile["model"], str) or not profile["model"]:
            raise SupervisionError("profile_model_required")
        for key in required - {"model"}:
            if type(profile[key]) is not int or profile[key] < (1 if key in {"max_output_tokens", "max_keys"} else 0):
                raise SupervisionError("invalid_effective_profile")
        self.client, self.root, self.stage, self.call_id = client, Path(root).resolve(), stage, call_id
        self.profile = json.loads(_json(profile))
        self.directory = call_directory(root, stage, call_id)
        self.poll_seconds = poll_seconds
        if lock_scope not in {"global", "call"}:
            raise SupervisionError("invalid_lock_scope")
        self.lock_root = self.directory if lock_scope == "call" else self.root

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _wait(self, phase, request, response=None):
        path = self.directory / (phase.upper() + "_DECISION.json")
        while not path.exists():
            time.sleep(self.poll_seconds)
        # Re-read actual artifacts after the wait; approval cannot bless later mutation.
        if _load(self.directory / "REQUEST.json") != request:
            raise SupervisionError("request_integrity_failure")
        if response is not None and _response(self.directory, _hash(request)) != response:
            raise SupervisionError("response_integrity_failure")
        receipt = _load(path)
        expected = {"schema": "supervisor.decision.v1", "stage": self.stage, "call_id": self.call_id,
                    "phase": phase, "request_sha256": _hash(request),
                    "response_sha256": _hash(response) if response is not None else None}
        if (type(receipt) is not dict or set(receipt) != set(expected) | {"decision", "reason"}
                or any(receipt.get(key) != value for key, value in expected.items())
                or receipt.get("decision") not in {"approve", "reject"}):
            raise SupervisionError("decision_integrity_failure")
        review = None
        if phase == "response":
            review = _response_review(
                self.directory, stage=self.stage, call_id=self.call_id,
                request=request, response=response, decision=receipt)
        if receipt["decision"] == "reject":
            if phase == "response" and review is not None and review["decision"] == "approve":
                return
            raise SupervisionError(phase + "_rejected")
        if phase == "response" and review is not None and review["decision"] != "approve":
            raise SupervisionError("response_review_rejected")

    def __call__(self, messages, **kwargs):
        # Observer is explicitly nonsemantic. All other kwargs must be exact JSON.
        observer = kwargs.pop("transport_observer", None)
        if observer is not None and not callable(observer):
            raise SupervisionError("transport_observer_must_be_callable")
        request = {"schema": "supervisor.request.v1", "stage": self.stage, "call_id": self.call_id,
                   "messages": messages, "profile": self.profile, "kwargs": kwargs,
                   "messages_sha256": _hash(messages), "profile_sha256": _hash(self.profile),
                   "kwargs_sha256": _hash(kwargs),
                   "excluded_nonsemantic_kwargs": ["transport_observer"] if observer is not None else []}
        request = json.loads(_json(request))
        with _single_flight(self.lock_root, self.poll_seconds):
            _put_once(self.directory / "REQUEST.json", request)
            self._wait("request", request)
            raw_path = self.directory / "RESPONSE.json"
            marker = self.directory / "INVOCATION.json"
            invocation = {"schema": "supervisor.invocation.v1", "request_sha256": _hash(request)}
            if marker.exists() and _load(marker) != invocation:
                raise SupervisionError("invocation_integrity_failure")
            if raw_path.exists():
                if not marker.exists():
                    raise SupervisionError("response_without_invocation")
                response = _response(self.directory, _hash(request))
            else:
                if marker.exists():
                    raise SupervisionError("interrupted_or_failed_invocation_no_automatic_retry")
                _put_once(marker, invocation)
                call_kwargs = json.loads(_json(kwargs))
                if observer is not None:
                    call_kwargs["transport_observer"] = observer
                try:
                    result = self.client(json.loads(_json(messages)), **call_kwargs)
                    response = {"schema": "supervisor.response.v1", "request_sha256": _hash(request),
                                "result_sha256": _hash(result), "result": result}
                    _put_once(raw_path, response)
                except BaseException as exc:
                    # No exception string/traceback: providers can embed credentials in errors.
                    cause_types = []
                    cause = exc
                    seen = set()
                    while cause is not None and id(cause) not in seen:
                        seen.add(id(cause))
                        cause_types.append(type(cause).__name__)
                        cause = cause.__cause__ or cause.__context__
                    _put_once(self.directory / "INVOCATION_ERROR.json", {"schema": "supervisor.error.v1",
                              "request_sha256": _hash(request), "error_type": type(exc).__name__,
                              "cause_types": cause_types})
                    raise
            self._wait("response", request, response)
            return json.loads(_json(response["result"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("decide")
    for key in ("root", "stage", "call-id"):
        command.add_argument("--" + key, required=True)
    command.add_argument("--phase", choices=("request", "response"), required=True)
    command.add_argument("--decision", choices=("approve", "reject"), required=True)
    command.add_argument("--reason", default="")
    review = sub.add_parser("review-response")
    for key in ("root", "stage", "call-id"):
        review.add_argument("--" + key, required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=("approve", "reject"), required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--review-kind", default="format_reparse")
    args = vars(parser.parse_args())
    command_name = args.pop("command")
    action = review_response if command_name == "review-response" else decide
    print(json.dumps(action(**args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
