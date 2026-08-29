"""Focused offline tests for the CORE multi-key lane integration."""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from optomind_research.provider_key_router import ProviderKeyRouter
from tools.academic_backends.core_backend import CoreBackend, _core_keys


@pytest.fixture(autouse=True)
def _reset_lanes() -> None:
    ProviderKeyRouter.reset_process_lanes()
    yield


@pytest.fixture()
def isolated_keys(tmp_path: Path, monkeypatch) -> Path:
    key_file = tmp_path / "core-keys.txt"
    key_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CORE_API_KEYS_FILE", str(key_file))
    monkeypatch.delenv("CORE_API_KEY", raising=False)
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
        "https://api.core.ac.uk/v3/search/works",
        code,
        "error",
        {},
        io.BytesIO(b"error"),
    )


def _bearer_key(headers: Any) -> str:
    authorization = headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ")


def _sample_payload() -> dict:
    return {
        "results": [
            {
                "id": 123,
                "title": "CORE optics paper",
                "authors": [{"name": "B. Researcher"}],
                "yearPublished": 2024,
                "doi": "10.2000/example",
                "downloadUrl": "https://example.org/core.pdf",
                "abstract": "A sufficiently detailed abstract.",
                "publisher": "Publisher",
                "language": {"name": "en"},
            }
        ]
    }


def test_key_pool_loading_deduplicates(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    isolated_keys.write_text("core-c\ncore-a\ncore-b\ncore-c\n", encoding="utf-8")
    monkeypatch.setenv("CORE_API_KEYS", "core-a,core-b")
    assert _core_keys() == ["core-a", "core-b", "core-c"]


def test_no_key_disabled_and_check_status(monkeypatch, isolated_keys: Path) -> None:
    monkeypatch.delenv("CORE_API_KEYS", raising=False)
    backend = CoreBackend(sleep_fn=lambda _: None)
    assert backend.search("optics", max_results=5) == []
    assert backend.check_status() == {
        "enabled": False,
        "has_api_key": False,
        "api_key_env": "CORE_API_KEY",
    }


def test_concurrent_requests_distribute_without_same_key_overlap(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "conc-1,conc-2,conc-3")
    lock = threading.Lock()
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = _bearer_key(request.headers)
        with lock:
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
        time.sleep(0.02)
        with lock:
            in_flight[key] -= 1
        return _FakeResponse(_sample_payload())

    backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
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


def test_429_cools_only_affected_key(monkeypatch, isolated_keys: Path) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "rate-a,rate-b")
    calls = 0
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        key = _bearer_key(request.headers)
        keys_seen.append(key)
        if key == "rate-a" and calls == 1:
            raise _http_error(429)
        return _FakeResponse(_sample_payload())

    backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("optics", max_results=5)
    assert len(result) == 1
    assert keys_seen == ["rate-a", "rate-b"]
    keys_seen.clear()
    backend.search("second query", max_results=5)
    assert keys_seen == ["rate-b"]


def test_401_isolates_bad_key(monkeypatch, isolated_keys: Path) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "bad-a,good-b")
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = _bearer_key(request.headers)
        keys_seen.append(key)
        if key == "bad-a":
            raise _http_error(401)
        return _FakeResponse(_sample_payload())

    backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("optics", max_results=5)
    assert len(result) == 1
    assert keys_seen == ["bad-a", "good-b"]
    keys_seen.clear()
    backend.search("second query", max_results=5)
    assert keys_seen == ["good-b"]


def test_normalized_output_unchanged(monkeypatch, isolated_keys: Path) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "norm-key")

    def opener(request: Any, **_: Any) -> _FakeResponse:
        return _FakeResponse(_sample_payload())

    backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("optics", max_results=5)
    assert result[0]["backend"] == "core"
    assert result[0]["title"] == "CORE optics paper"
    assert result[0]["doi"] == "10.2000/example"
    assert result[0]["verification_status"] == "verified"


def test_final_attempt_429_still_cools_key(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "final-a,final-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "core",
        ["final-a", "final-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise _http_error(429)

    backend = CoreBackend(router=router, opener=opener, sleep_fn=lambda _: None)
    assert backend.search("optics", max_results=5) == []
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
    monkeypatch.setenv("CORE_API_KEYS", "fivexx-a,fivexx-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "core",
        ["fivexx-a", "fivexx-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise urllib.error.HTTPError(
            "https://api.core.ac.uk/v3/search/works",
            503,
            "unavailable",
            {},
            io.BytesIO(b"error"),
        )

    backend = CoreBackend(router=router, opener=opener, sleep_fn=lambda _: None)
    assert backend.search("optics", max_results=5) == []
    for lane in router.lanes:
        assert lane.quarantined is False
        assert lane.consecutive_429_count == 0
        assert lane.cool_until > 0
        assert lane.busy is False


def test_exceptions_always_release_busy_lanes(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "exc-a,exc-b")
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    router = ProviderKeyRouter(
        "core",
        ["exc-a", "exc-b"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )

    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise RuntimeError("transport failure")

    backend = CoreBackend(router=router, opener=opener, sleep_fn=lambda _: None)
    assert backend.search("optics", max_results=5) == []
    for lane in router.lanes:
        assert lane.busy is False
        assert lane.quarantined is False


def test_all_keys_429_tries_each_key_once_without_cooldown_waits(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "once-a,once-b,once-c")
    clock = [0.0]
    waits: list[float] = []

    def advance(seconds: float) -> None:
        clock[0] += seconds
        waits.append(seconds)

    router = ProviderKeyRouter(
        "core",
        ["once-a", "once-b", "once-c"],
        min_interval_seconds=0,
        sleep_fn=advance,
        now_fn=lambda: clock[0],
    )
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(_bearer_key(request.headers))
        raise _http_error(429)

    backend = CoreBackend(router=router, opener=opener, sleep_fn=lambda _: None)
    assert backend.search("optics", max_results=5) == []
    assert keys_seen == ["once-a", "once-b", "once-c"]
    assert len(keys_seen) == 3
    assert waits == []
    for lane in router.lanes:
        assert lane.consecutive_429_count > 0
        assert lane.cool_until > 0
        assert lane.busy is False
        assert lane.quarantined is False


def test_source_fulltext_urls_empty_list_does_not_crash(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "norm-key")
    payload = {
        "results": [
            {
                "id": 1,
                "title": "Empty fulltext URLs paper",
                "authors": [],
                "yearPublished": 2020,
                "doi": "",
                "sourceFulltextUrls": [],
                "abstract": "",
            }
        ]
    }

    def opener(request: Any, **_: Any) -> _FakeResponse:
        return _FakeResponse(payload)

    backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
    result = backend.search("optics", max_results=5)
    assert len(result) == 1
    assert result[0]["url_or_doi"] == ""
    assert result[0]["source_url"] == ""
    assert result[0]["backend"] == "core"
    assert result[0]["verification_status"] == "unverified"
    assert result[0]["title"] == "Empty fulltext URLs paper"


def test_source_fulltext_urls_variants_are_safe(
    monkeypatch,
    isolated_keys: Path,
) -> None:
    monkeypatch.setenv("CORE_API_KEYS", "norm-key")
    cases = [
        (None, "", "missing"),
        ({"sourceFulltextUrls": None}, "", "null-list"),
        ({"sourceFulltextUrls": []}, "", "empty-list"),
        (
            {"sourceFulltextUrls": ["https://a.example", "https://b.example"]},
            "https://a.example",
            "nonempty-list",
        ),
        (
            {
                "downloadUrl": "https://dl.example/paper.pdf",
                "sourceFulltextUrls": [],
            },
            "https://dl.example/paper.pdf",
            "prefer-downloadUrl",
        ),
    ]
    for extra, expected_url, label in cases:
        item = {
            "id": 1,
            "title": f"Variant {label}",
            "authors": [],
            "yearPublished": 2020,
            "doi": "",
            "abstract": "",
        }
        if isinstance(extra, dict):
            item.update(extra)
        payload = {"results": [item]}

        def opener(request: Any, **_: Any) -> _FakeResponse:
            return _FakeResponse(payload)

        backend = CoreBackend(opener=opener, sleep_fn=lambda _: None)
        result = backend.search("optics", max_results=5)
        assert len(result) == 1, label
        assert result[0]["url_or_doi"] == expected_url, label
        assert result[0]["source_url"] == item.get("downloadUrl", ""), label
