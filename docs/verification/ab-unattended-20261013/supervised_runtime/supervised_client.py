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
        if receipt["decision"] == "reject":
            raise SupervisionError(phase + "_rejected")

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
    args = vars(parser.parse_args())
    args.pop("command")
    print(json.dumps(decide(**args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
