"""Offline tests for Qwen chat streaming and partial SSE handling."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from llm import qwen_chat_client


class _SSEResponse:
    def __init__(self, lines, error=None):
        self._lines = [line.encode("utf-8") for line in lines]
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def __iter__(self):
        yield from self._lines
        if self._error is not None:
            raise self._error


def _live_reader_threads():
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "qwen-sse-reader" and thread.is_alive()
    ]


class _SocketBackedResponse:
    """Mimic the real urllib chain ``resp.fp.raw._sock``.

    After yielding ``lines``, iteration parks until the socket is closed.  This
    models an SSE peer that has stopped emitting chunks, and proves the
    deadline cleanup actively unblocks the reader instead of waiting for the
    peer to close the stream itself.
    """

    def __init__(self, lines):
        self._lines = [line.encode("utf-8") for line in lines]
        self.closed = False
        self.sock_closed = False
        self._release = threading.Event()
        self._socket = self._make_socket()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self._socket))

    def _make_socket(self):
        response = self

        class _Socket:
            def close(self):
                response.sock_closed = True
                response._release.set()

        return _Socket()

    def __iter__(self):
        for line in self._lines:
            yield line
        self._release.wait()

    def close(self):
        self.closed = True
        self._release.set()


def _configure_client(monkeypatch):
    monkeypatch.setattr(
        qwen_chat_client,
        "get_qwen_client_config",
        lambda tier: {
            "model": "qwen-test",
            "base_url": "https://example.invalid/v1",
            "api_key_candidates": [{"api_key": "test-key"}],
            "mock_llm": False,
        },
    )


def _call(monkeypatch, response, **kwargs):
    _configure_client(monkeypatch)
    monkeypatch.setattr(qwen_chat_client.urllib.request, "urlopen", lambda *args, **kwargs: response)
    return qwen_chat_client.call_qwen_chat(
        agent_name="test-agent",
        messages=[{"role": "user", "content": "hello"}],
        max_retries=0,
        stream=True,
        **kwargs,
    )


def _call_socket_backed(monkeypatch, response, *, timeout_seconds=None, **kwargs):
    _configure_client(monkeypatch)
    monkeypatch.setattr(qwen_chat_client, "_http_timeout_seconds", lambda: 0.1)
    monkeypatch.setattr(
        qwen_chat_client.urllib.request,
        "urlopen",
        lambda *args, **urlopen_kwargs: response,
    )
    return qwen_chat_client.call_qwen_chat(
        agent_name="test-agent",
        messages=[{"role": "user", "content": "hello"}],
        max_retries=0,
        allow_model_fallback=False,
        max_key_candidates=1,
        max_transport_key_candidates=1,
        stream=True,
        force_mock=False,
        timeout_seconds=timeout_seconds,
        **kwargs,
    )


def test_stream_concatenates_sse_json_content_and_stops_at_done(monkeypatch):
    response = _SSEResponse(
        [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hello "}}]}),
            "",
            "data: " + json.dumps({"choices": [{"delta": {"content": "world"}}]}),
            "data: [DONE]",
            "data: " + json.dumps({"choices": [{"delta": {"content": "ignored"}}]}),
        ]
    )

    result = _call(monkeypatch, response)

    assert result["content"] == "Hello world"
    assert result["_llm_usage"]["success"] is True


def test_stream_returns_already_received_content_after_remote_disconnect(monkeypatch):
    response = _SSEResponse(
        ["data: " + json.dumps({"choices": [{"delta": {"content": "partial"}}]})],
        error=http.client.RemoteDisconnected("stream closed"),
    )

    result = _call(monkeypatch, response)

    assert result["content"] == "partial"
    assert result["_llm_usage"]["success"] is True


class _TimeoutStreamResponse:
    def __init__(self):
        self.closed = False
        self.sock_closed = False
        self.fp = SimpleNamespace(
            raw=SimpleNamespace(_sock=self._make_sock())
        )

    def _make_sock(self):
        sock = SimpleNamespace()

        def close_sock():
            self.sock_closed = True

        sock.close = close_sock
        return sock

    def __iter__(self):
        raise TimeoutError("Qwen stream exceeded total wall-clock limit")

    def close(self):
        self.closed = True


def test_stream_timeout_closes_response_and_socket(monkeypatch) -> None:
    _configure_client(monkeypatch)
    response = _TimeoutStreamResponse()
    captured_requests: list[http.client.HTTPRequest] = []

    def fake_urlopen(req, **kwargs):
        captured_requests.append(req)
        return response

    monkeypatch.setattr(
        qwen_chat_client.urllib.request, "urlopen", fake_urlopen
    )
    result = qwen_chat_client.call_qwen_chat(
        agent_name="test-agent",
        messages=[{"role": "user", "content": "hello"}],
        max_retries=0,
        allow_model_fallback=False,
        max_key_candidates=1,
        max_transport_key_candidates=1,
        stream=True,
        accept_partial_stream=False,
        force_mock=False,
        timeout_seconds=30,
    )

    assert response.closed is True
    assert response.sock_closed is True
    assert len(captured_requests) == 1
    body = json.loads(captured_requests[0].data.decode("utf-8"))
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    usage = result["_llm_usage"]
    assert usage["success"] is False
    assert usage["request_attempt_count"] == 1
    assert usage["retry_count"] == 0
    assert result["content"].startswith("[fallback]")


def test_structured_stream_rejects_partial_content_and_uses_fallback(monkeypatch):
    _configure_client(monkeypatch)
    monkeypatch.setattr(
        qwen_chat_client,
        "get_qwen_client_config",
        lambda tier: {
            "model": "qwen-test",
            "fallback_models": ["qwen-fallback"],
            "base_url": "https://example.invalid/v1",
            "api_key_candidates": [{"api_key": "test-key"}],
            "mock_llm": False,
        },
    )
    responses = iter(
        [
            _SSEResponse(
                ["data: " + json.dumps({"choices": [{"delta": {"content": '{"incomplete":'}}]})],
                error=http.client.RemoteDisconnected("stream closed"),
            ),
            _SSEResponse(
                [
                    "data: " + json.dumps({"choices": [{"delta": {"content": '{"ok": true}'}}]}),
                    "data: [DONE]",
                ]
            ),
        ]
    )
    monkeypatch.setattr(qwen_chat_client.urllib.request, "urlopen", lambda *args, **kwargs: next(responses))

    result = qwen_chat_client.call_qwen_chat(
        "structured-test",
        [{"role": "user", "content": "return json"}],
        max_retries=0,
        stream=True,
        accept_partial_stream=False,
        max_transport_key_candidates=1,
    )

    assert result["content"] == '{"ok": true}'
    assert result["_llm_usage"]["model_name"] == "qwen-fallback"
    assert result["_llm_usage"]["attempted_models"] == ["qwen-test", "qwen-fallback"]


def test_stream_without_content_uses_existing_fallback(monkeypatch):
    response = _SSEResponse(["data: [DONE]"])

    result = _call(monkeypatch, response)

    assert result["content"].startswith("[fallback] Qwen chat failed: ValueError")
    assert result["_llm_usage"]["success"] is False
    assert result["_llm_usage"]["error_type"] == "ValueError"
    assert result["_llm_usage"]["fallback_used"] is True


def test_stream_total_wall_clock_limit_returns_auditable_partial(monkeypatch):
    response = _SSEResponse([
        "data: " + json.dumps({"choices": [{"delta": {"content": "usable partial"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"content": " too late"}}]}),
    ])
    clock = iter([0.0, 1.0, 6.0])
    monkeypatch.setattr(qwen_chat_client.time, "monotonic", lambda: next(clock))

    result = _call(monkeypatch, response, timeout_seconds=5)

    assert result["content"] == "usable partial"
    assert result["_llm_usage"]["partial_stream"] is True
    assert result["_llm_usage"]["stream_interrupted_error"] == "TimeoutError"


def test_blocked_sse_reader_is_bounded_by_deadline() -> None:
    """A peer that never emits the next chunk must not hold the caller."""

    release = threading.Event()
    socket_closed = threading.Event()
    readers_before = _live_reader_threads()

    class _Socket:
        def close(self):
            socket_closed.set()
            release.set()

    class _BlockedResponse:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=_Socket()))

        def __iter__(self):
            release.wait()
            return
            yield  # pragma: no cover - makes this a generator

        def close(self):
            release.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        qwen_chat_client._iter_stream_lines_with_deadline(
            _BlockedResponse(),
            request_started=started,
            timeout_seconds=0.05,
        ).__next__()
    assert time.monotonic() - started < 1.0
    assert socket_closed.is_set()
    assert _live_reader_threads() == readers_before


def test_socket_backed_sse_reader_preserves_normal_lines() -> None:
    class _Socket:
        def close(self):
            pass

    class _SocketResponse:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=_Socket()))

        def __iter__(self):
            yield b"first"
            yield b"second"

        def close(self):
            pass

    lines = list(
        qwen_chat_client._iter_stream_lines_with_deadline(
            _SocketResponse(),
            request_started=time.monotonic(),
            timeout_seconds=1.0,
        )
    )
    assert lines == [b"first", b"second"]


def test_socket_backed_normal_sse_stops_at_done_and_joins_reader(monkeypatch):
    response = _SocketBackedResponse(
        [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hello "}}]}),
            "",
            "data: " + json.dumps({"choices": [{"delta": {"content": "world"}}]}),
            "data: [DONE]",
        ]
    )
    readers_before = _live_reader_threads()

    result = _call_socket_backed(monkeypatch, response, timeout_seconds=30)

    assert result["content"] == "Hello world"
    assert result["_llm_usage"]["success"] is True
    assert response.sock_closed is True
    assert response.closed is True
    assert _live_reader_threads() == readers_before


def test_socket_backed_no_data_timeout_uses_failure_routing_and_joins_reader(monkeypatch):
    response = _SocketBackedResponse([])
    readers_before = _live_reader_threads()
    started = time.monotonic()

    result = _call_socket_backed(
        monkeypatch,
        response,
        accept_partial_stream=False,
    )

    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert result["content"].startswith("[fallback]")
    usage = result["_llm_usage"]
    assert usage["success"] is False
    assert usage["error_type"] == "TimeoutError"
    assert usage["request_attempt_count"] == 1
    assert usage["retry_count"] == 0
    assert response.sock_closed is True
    assert response.closed is True
    assert _live_reader_threads() == readers_before


def test_socket_backed_partial_timeout_accepts_partial_and_joins_reader(monkeypatch):
    response = _SocketBackedResponse(
        [
            "data: " + json.dumps({"choices": [{"delta": {"content": "usable partial"}}]}),
        ]
    )
    readers_before = _live_reader_threads()
    started = time.monotonic()

    result = _call_socket_backed(
        monkeypatch,
        response,
        accept_partial_stream=True,
    )

    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert result["content"] == "usable partial"
    usage = result["_llm_usage"]
    assert usage["success"] is True
    assert usage["partial_stream"] is True
    assert usage["stream_interrupted_error"] == "TimeoutError"
    assert response.sock_closed is True
    assert response.closed is True
    assert _live_reader_threads() == readers_before


def test_socket_backed_partial_timeout_rejects_partial_and_joins_reader(monkeypatch):
    response = _SocketBackedResponse(
        [
            "data: " + json.dumps({"choices": [{"delta": {"content": "unusable partial"}}]}),
        ]
    )
    readers_before = _live_reader_threads()
    started = time.monotonic()

    result = _call_socket_backed(
        monkeypatch,
        response,
        accept_partial_stream=False,
    )

    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert result["content"].startswith("[fallback]")
    usage = result["_llm_usage"]
    assert usage["success"] is False
    assert usage["error_type"] == "TimeoutError"
    assert usage["request_attempt_count"] == 1
    assert usage["retry_count"] == 0
    assert response.sock_closed is True
    assert response.closed is True
    assert _live_reader_threads() == readers_before


def test_response_socket_layers_release_real_socketio_reference():
    """The deferred OS close behind urllib requires closing the SocketIO."""

    parent, child = socket.socketpair()
    file_obj = None
    try:
        file_obj = child.makefile("rb")
        response = SimpleNamespace(fp=file_obj)

        sock, layers = qwen_chat_client._response_socket_layers(response)
        assert sock is child
        assert any(type(layer).__name__ == "SocketIO" for layer in layers)

        qwen_chat_client._close_response_socket(response)

        assert child._closed is True
        assert file_obj.raw.closed is True
    finally:
        if file_obj is not None:
            try:
                file_obj.close()
            except Exception:
                pass
        parent.close()
        try:
            child.close()
        except Exception:
            pass
