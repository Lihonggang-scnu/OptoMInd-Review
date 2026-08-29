"""Focused offline tests for the OpenAlex multi-key lane integration."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from optomind_research.provider_key_router import ProviderKeyRouter
from tools.academic_backends.openalex_backend import (
    OpenAlexBackend,
    _openalex_api_keys,
)


@pytest.fixture(autouse=True)
def _reset_lanes() -> None:
    ProviderKeyRouter.reset_process_lanes()
    yield


@pytest.fixture()
def isolated_keys(tmp_path: Path, monkeypatch) -> Path:
    key_file = tmp_path / "openalex-keys.txt"
    key_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENALEX_API_KEYS_FILE", str(key_file))
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_API_KEYS", raising=False)
    monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
    return key_file


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.openalex.org/works",
        code,
        "error",
        {},
        io.BytesIO(b"error"),
    )


def _key_from_url(url: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return query.get("api_key", [""])[0]


def _sample_payload() -> dict:
    return {
        "results": [
            {
                "id": "https://openalex.org/W123",
                "title": "Optical metasurface review",
                "authorships": [
                    {"author": {"display_name": "A. Researcher"}}
                ],
                "publication_year": 2025,
                "doi": "https://doi.org/10.1000/example",
                "abstract_inverted_index": None,
                "cited_by_count": 4,
                "primary_location": {"source": {"display_name": "Journal"}},
                "open_access": {"is_oa": True, "oa_url": "https://x"},
                "best_oa_location": {},
            }
        ]
    }


def test_key_pool_loading_deduplicates(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    isolated_keys.write_text("fake-c\nfake-a\nfake-b\nfake-c\n", encoding="utf-8")
    monkeypatch.setenv("OPENALEX_API_KEYS", "fake-a,fake-b")
    assert _openalex_api_keys() == ["fake-a", "fake-b", "fake-c"]


def test_concurrent_requests_distribute_without_same_key_overlap(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "conc-1,conc-2,conc-3")
    lock = threading.Lock()
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = _key_from_url(request.full_url)
        with lock:
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
        time.sleep(0.02)
        with lock:
            in_flight[key] -= 1
        return _FakeResponse(_sample_payload())

    backend = OpenAlexBackend(opener=opener, sleep_fn=lambda _: None)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda query: backend.search(query, max_results=5),
                [f"query {index}" for index in range(6)],
            )
        )
    assert all(len(result) == 1 for result in results)
    assert max(max_in_flight.values()) == 1
    assert set(in_flight) == {"conc-1", "conc-2", "conc-3"}


def test_429_cools_only_affected_key(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "rate-a,rate-b")
    calls = 0
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        key = _key_from_url(request.full_url)
        keys_seen.append(key)
        if key == "rate-a" and calls == 1:
            raise _http_error(429)
        return _FakeResponse(_sample_payload())

    backend = OpenAlexBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("metasurface", max_results=5)
    assert len(result) == 1
    assert keys_seen == ["rate-a", "rate-b"]
    keys_seen.clear()
    backend.search("second query", max_results=5)
    assert keys_seen == ["rate-b"]


def test_401_isolates_bad_key(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "bad-a,good-b")
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = _key_from_url(request.full_url)
        keys_seen.append(key)
        if key == "bad-a":
            raise _http_error(401)
        return _FakeResponse(_sample_payload())

    backend = OpenAlexBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("metasurface", max_results=5)
    assert len(result) == 1
    assert keys_seen == ["bad-a", "good-b"]
    keys_seen.clear()
    backend.search("second query", max_results=5)
    assert keys_seen == ["good-b"]


def test_no_key_public_polite_fallback(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_EMAIL", "test@example.com")
    seen_urls: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        seen_urls.append(request.full_url)
        return _FakeResponse(_sample_payload())

    backend = OpenAlexBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("metasurface", max_results=5)
    assert len(result) == 1
    assert "api_key=" not in seen_urls[0]
    assert "mailto=test%40example.com" in seen_urls[0]


def test_normalized_output_unchanged(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "norm-key")

    def opener(request: Any, **_: Any) -> _FakeResponse:
        return _FakeResponse(_sample_payload())

    backend = OpenAlexBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("metasurface", max_results=5)
    assert result[0]["backend"] == "openalex"
    assert result[0]["title"] == "Optical metasurface review"
    assert result[0]["doi"] == "10.1000/example"
    assert result[0]["citation_count"] == 4


def test_final_attempt_429_still_cools_key(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "final-a,final-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "openalex",
        ["final-a", "final-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise _http_error(429)

    backend = OpenAlexBackend(
        router=router,
        opener=opener,
        sleep_fn=lambda _: None,
    )
    assert backend.search("metasurface", max_results=5) == []
    for lane in router.lanes:
        assert lane.consecutive_429_count > 0
        assert lane.cool_until > 0
        assert lane.quarantined is False
        assert lane.busy is False
        assert "final-a" not in backend.last_error
        assert "final-b" not in backend.last_error


def test_5xx_transient_cooldown_without_penalty_or_quarantine(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "fivexx-a,fivexx-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "openalex",
        ["fivexx-a", "fivexx-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise urllib.error.HTTPError(
            "https://api.openalex.org/works",
            503,
            "unavailable",
            {},
            io.BytesIO(b"error"),
        )

    backend = OpenAlexBackend(
        router=router,
        opener=opener,
        sleep_fn=lambda _: None,
    )
    assert backend.search("metasurface", max_results=5) == []
    for lane in router.lanes:
        assert lane.quarantined is False
        assert lane.consecutive_429_count == 0
        assert lane.cool_until > 0
        assert lane.busy is False


def test_exceptions_always_release_busy_lanes(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "exc-a,exc-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "openalex",
        ["exc-a", "exc-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise RuntimeError("transport failure")

    backend = OpenAlexBackend(
        router=router,
        opener=opener,
        sleep_fn=lambda _: None,
    )
    assert backend.search("metasurface", max_results=5) == []
    for lane in router.lanes:
        assert lane.busy is False
        assert lane.quarantined is False


def test_all_keys_429_tries_each_key_once_without_cooldown_waits(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("OPENALEX_API_KEYS", "once-a,once-b,once-c")
    clock = [0.0]
    waits: list[float] = []

    def advance(seconds: float) -> None:
        clock[0] += seconds
        waits.append(seconds)

    router = ProviderKeyRouter(
        "openalex",
        ["once-a", "once-b", "once-c"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(_key_from_url(request.full_url))
        raise _http_error(429)

    backend = OpenAlexBackend(
        router=router,
        opener=opener,
        sleep_fn=lambda _: None,
    )
    assert backend.search("metasurface", max_results=5) == []
    assert keys_seen == ["once-a", "once-b", "once-c"]
    assert len(keys_seen) == 3
    assert waits == []
    for lane in router.lanes:
        assert lane.consecutive_429_count > 0
        assert lane.cool_until > 0
        assert lane.busy is False
        assert lane.quarantined is False
