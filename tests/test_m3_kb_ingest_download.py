"""Focused tests for bounded, deadline-guarded M3 OA/publisher downloads."""

from __future__ import annotations

import optomind_research.m3_kb_ingest as m3


class _FakeClock:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def __call__(self):
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class _FakeResponse:
    def __init__(self, chunks, status=200):
        self.buffer = b"".join(chunks)
        self.status = status
        self.read_sizes = []
        self._offset = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            data = self.buffer[self._offset:]
            self._offset = len(self.buffer)
            return data
        data = self.buffer[self._offset:self._offset + size]
        self._offset += len(data)
        if not data:
            return b""
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _fake_opener(response):
    def opener(req, timeout=None):
        return response

    return opener


def test_read_response_bounded_success_accumulates_chunks() -> None:
    response = _FakeResponse([
        b"a" * 1024,
        b"b" * 2048,
        b"",
    ])
    result = m3._read_response_bounded(
        response,
        clock=_FakeClock([0.0, 0.0, 0.0, 0.0, 0.0]),
        total_deadline_seconds=60,
        max_payload_bytes=10**9,
        chunk_size=1024,
    )

    assert result == b"a" * 1024 + b"b" * 2048
    assert response.read_sizes[:3] == [1024, 1024, 1024]


def test_read_response_bounded_deadline_aborts_trickle() -> None:
    response = _FakeResponse([
        b"x" * 100,
        b"y" * 100,
        b"",
    ])
    result = m3._read_response_bounded(
        response,
        clock=_FakeClock([0.0, 61.0]),
        total_deadline_seconds=60,
        max_payload_bytes=10**9,
        chunk_size=1024,
    )

    assert result is None


def test_read_response_bounded_oversize_aborts() -> None:
    response = _FakeResponse([
        b"x" * 80,
        b"y" * 80,
        b"",
    ])
    result = m3._read_response_bounded(
        response,
        clock=_FakeClock([0.0, 0.0, 0.0, 0.0]),
        total_deadline_seconds=60,
        max_payload_bytes=100,
        chunk_size=1024,
    )

    assert result is None


def test_try_download_bytes_success_uses_bounded_reader(
    monkeypatch,
) -> None:
    response = _FakeResponse([b"a" * 64, b""], status=200)
    monkeypatch.setattr(m3, "is_openalex_content_url", lambda url: False)
    monkeypatch.setattr(m3.urllib.request, "urlopen", _fake_opener(response))

    result = m3._try_download_bytes(
        "https://publisher.example/file.pdf",
        clock=_FakeClock([0.0, 0.0, 0.0, 0.0]),
        max_payload_bytes=10**9,
        chunk_size=64,
    )

    assert result == b"a" * 64
    assert response.read_sizes == [64, 64]


def test_try_download_bytes_deadline_returns_none(monkeypatch) -> None:
    response = _FakeResponse([b"a" * 64, b""], status=200)
    monkeypatch.setattr(m3, "is_openalex_content_url", lambda url: False)
    monkeypatch.setattr(m3.urllib.request, "urlopen", _fake_opener(response))

    result = m3._try_download_bytes(
        "https://publisher.example/file.pdf",
        clock=_FakeClock([0.0, 61.0]),
        total_deadline_seconds=60,
        max_payload_bytes=10**9,
        chunk_size=64,
    )

    assert result is None


def test_try_download_bytes_network_failure_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(m3, "is_openalex_content_url", lambda url: False)

    def boom(req, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(m3.urllib.request, "urlopen", boom)

    assert m3._try_download_bytes("https://publisher.example/file.pdf") is None


def test_try_download_bytes_openalex_failure_returns_none(
    monkeypatch,
) -> None:
    monkeypatch.setattr(m3, "is_openalex_content_url", lambda url: True)

    def boom(url, *, timeout, headers):
        raise OSError("openalex timeout")

    monkeypatch.setattr(m3, "fetch_openalex_content", boom)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("OpenAlex route must not use urllib")

    monkeypatch.setattr(m3.urllib.request, "urlopen", unexpected)

    assert m3._try_download_bytes("https://openalex.example/W123") is None


def test_openalex_helper_behavior_unchanged(monkeypatch) -> None:
    calls = []

    def fake_fetch(url, *, timeout, headers):
        calls.append((url, timeout, headers))
        return b"openalex-data", {}

    monkeypatch.setattr(m3, "is_openalex_content_url", lambda url: True)
    monkeypatch.setattr(m3, "fetch_openalex_content", fake_fetch)

    def boom(*_args, **_kwargs):
        raise AssertionError("OpenAlex route must not use urllib")

    monkeypatch.setattr(m3.urllib.request, "urlopen", boom)

    result = m3._try_download_bytes("https://openalex.example/W123")

    assert result == b"openalex-data"
    assert calls[0][1] == m3._DOWNLOAD_TIMEOUT
    assert calls[0][2] == m3._HEADERS
