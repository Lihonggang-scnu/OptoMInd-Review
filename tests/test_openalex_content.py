"""Focused offline tests for routed OpenAlex content downloads."""

from __future__ import annotations

import io
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from optomind_research.provider_key_router import ProviderKeyRouter
import tools.academic_backends.openalex_content as content_mod
from tools.academic_backends.openalex_backend import OpenAlexBackend


@pytest.fixture(autouse=True)
def _reset_lanes() -> None:
    ProviderKeyRouter.reset_process_lanes()
    yield


@pytest.fixture()
def isolated_keys(tmp_path: Path, monkeypatch) -> Path:
    key_file = tmp_path / "openalex-content-keys.txt"
    key_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENALEX_API_KEYS_FILE", str(key_file))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
    return key_file


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://content.openalex.org/works/W1.pdf",
        code,
        "error",
        {},
        io.BytesIO(b"error"),
    )


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _api_key_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return query.get("api_key", [""])[0]


def test_missing_key_returns_marker(monkeypatch, isolated_keys: Path) -> None:
    monkeypatch.delenv("OPENALEX_API_KEYS", raising=False)

    def fake_urlopen(*_args, **_kwargs):
        raise AssertionError("must not call urlopen without keys")

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data is None
    assert error == "openalex_content_key_missing"


def test_success_returns_bytes_without_leaking_url(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "content-ok")
    seen_urls: list[str] = []

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        seen_urls.append(request.full_url)
        return _FakeResponse(b"%PDF-1.7")

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data == b"%PDF-1.7"
    assert error == ""
    assert _api_key_from_url(seen_urls[0]) == "content-ok"


def test_429_cools_lane_and_rotates_to_other_key(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "content-a,content-b")
    router = ProviderKeyRouter(
        "openalex",
        ["content-a", "content-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    calls = 0

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        nonlocal calls
        calls += 1
        key = _api_key_from_url(request.full_url)
        if key == "content-a" and calls == 1:
            raise _http_error(429)
        return _FakeResponse(b"%PDF-1.7")

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data == b"%PDF-1.7"
    assert error == ""
    assert router.lanes[0].consecutive_429_count == 1
    assert router.lanes[0].cool_until > 0
    assert router.lanes[0].quarantined is False
    assert router.lanes[1].cool_until == 0


def test_401_quarantines_lane_and_shared_with_backend(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "content-bad,content-good")
    router = ProviderKeyRouter(
        "openalex",
        ["content-bad", "content-good"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    seen_keys: list[str] = []

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        key = _api_key_from_url(request.full_url)
        seen_keys.append(key)
        if key == "content-bad":
            raise _http_error(401)
        return _FakeResponse(b"%PDF-1.7")

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data == b"%PDF-1.7"
    assert seen_keys == ["content-bad", "content-good"]
    assert router.lanes[0].quarantined is True
    assert "content-bad" not in error

    # A fresh backend on the same provider shares the quarantine.
    backend_seen: list[str] = []

    def backend_opener(request: Any, **_: Any) -> Any:
        key = urllib.parse.parse_qs(
            urllib.parse.urlparse(request.full_url).query
        ).get("api_key", [""])[0]
        backend_seen.append(key)
        return _FakeResponse(b'{"results": []}')

    backend = OpenAlexBackend(
        router=router,
        opener=backend_opener,
        sleep_fn=lambda _: None,
    )
    backend.search("metasurface", max_results=5)
    assert backend_seen == ["content-good"]


def test_5xx_transient_cooldown_and_busy_release(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "content-5xx")
    router = ProviderKeyRouter(
        "openalex",
        ["content-5xx"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        raise _http_error(503)

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data is None
    assert error.startswith("openalex_content_failed:")
    assert router.lanes[0].quarantined is False
    assert router.lanes[0].consecutive_429_count == 0
    assert router.lanes[0].cool_until > 0
    assert router.lanes[0].busy is False


def test_transport_exception_releases_busy_lanes(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "content-exc-a,content-exc-b")
    router = ProviderKeyRouter(
        "openalex",
        ["content-exc-a", "content-exc-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        raise RuntimeError("transport failure")

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data is None
    assert error.startswith("openalex_content_failed:")
    for lane in router.lanes:
        assert lane.busy is False
        assert lane.quarantined is False
        assert "content-exc-a" not in error
        assert "content-exc-b" not in error


def test_all_keys_429_tries_each_key_once(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "once-a,once-b,once-c")
    router = ProviderKeyRouter(
        "openalex",
        ["once-a", "once-b", "once-c"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    keys_seen: list[str] = []

    def fake_urlopen(request: Any, **_kwargs) -> _FakeResponse:
        keys_seen.append(_api_key_from_url(request.full_url))
        raise _http_error(429)

    monkeypatch.setattr(content_mod.urllib.request, "urlopen", fake_urlopen)
    data, error = content_mod.fetch_openalex_content(
        "https://content.openalex.org/works/W1.pdf"
    )
    assert data is None
    assert error.startswith("openalex_content_failed:")
    assert keys_seen == ["once-a", "once-b", "once-c"]
    assert len(keys_seen) == 3
    for lane in router.lanes:
        assert lane.consecutive_429_count > 0
        assert lane.cool_until > 0
        assert lane.busy is False
        assert lane.quarantined is False
