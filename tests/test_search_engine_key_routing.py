"""Focused offline tests for SearchEngine Jina/Firecrawl key-lane routing.

Fake keys only: no real key files, environment values, or network are ever
touched, and no secret value is printed or copied into artifacts.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import pytest

import optomind_research.search_engine as search_engine
from optomind_research.provider_key_router import ProviderKeyRouter
from optomind_research.search_engine import SearchEngine


@pytest.fixture(autouse=True)
def _isolate_search_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read real secrets; keep router lanes and caches per-test."""

    ProviderKeyRouter.reset_process_lanes()
    monkeypatch.setattr(search_engine, "configure_secret_environment", lambda: {})
    monkeypatch.setattr(search_engine, "_load_tavily_key", lambda: None)
    monkeypatch.setattr(search_engine, "_load_jina_key", lambda: None)
    monkeypatch.setattr(search_engine, "_load_firecrawl_key", lambda: None)
    monkeypatch.setattr(search_engine, "_load_s2_key", lambda: None)
    monkeypatch.setattr(
        search_engine, "load_secret_candidates", lambda name, filenames=(): []
    )
    monkeypatch.setattr(
        search_engine, "_fulltext_cache_get", lambda url: ""
    )
    monkeypatch.setattr(
        search_engine, "_fulltext_cache_set", lambda url, markdown: None
    )
    yield


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _http_error(code: int, url: str = "https://example.test/") -> Exception:
    return urllib.error.HTTPError(url, code, f"status {code}", {}, None)


def _install_opener(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[urllib.request.Request], object],
    calls: Optional[list[dict[str, object]]] = None,
) -> list[dict[str, object]]:
    recorded = calls if calls is not None else []

    def opener(req: urllib.request.Request, timeout: Optional[float] = None):
        recorded.append(
            {
                "url": req.full_url,
                "authorization": req.get_header("Authorization"),
            }
        )
        outcome = handler(req)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, str):
            outcome = outcome.encode("utf-8")
        return _FakeResponse(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return recorded


def _engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    jina: tuple[str, ...] = (),
    firecrawl: tuple[str, ...] = (),
) -> SearchEngine:
    monkeypatch.setattr(
        search_engine, "_load_jina_key_pool", lambda legacy_key=None: list(jina)
    )
    monkeypatch.setattr(
        search_engine,
        "_load_firecrawl_key_pool",
        lambda legacy_key=None: list(firecrawl),
    )
    return SearchEngine()


def test_pools_load_dedupe_and_legacy_single_key_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        search_engine,
        "load_secret_candidates",
        lambda name, filenames=(): {
            "JINA_API_KEY": ["env-j1", "file-j2", "env-j1"],
            "FIRECRAWL_API_KEY": ["env-f1", "file-f1"],
        }.get(name, []),
    )
    monkeypatch.setattr(search_engine, "_load_jina_key", lambda: "legacy-j0")
    monkeypatch.setattr(search_engine, "_load_firecrawl_key", lambda: None)
    engine = SearchEngine()
    assert engine.jina_key == "legacy-j0"
    assert engine._jina_keys == ["legacy-j0", "env-j1", "file-j2"]
    assert engine._firecrawl_keys == ["env-f1", "file-f1"]


def test_request_time_rotation_across_jina_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_opener(monkeypatch, lambda req: b"# markdown")
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine.fetch_fulltext("https://a.example", method="jina") == "# markdown"
    assert engine.fetch_fulltext("https://b.example", method="jina") == "# markdown"
    assert [call["authorization"] for call in calls] == ["Bearer j1", "Bearer j2"]


def test_concurrent_requests_distribute_without_same_key_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}
    used: set[str] = set()
    errors: list[str] = []

    def handler(req: urllib.request.Request) -> bytes:
        auth = req.get_header("Authorization") or ""
        key = auth.removeprefix("Bearer ")
        with lock:
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
            used.add(key)
        time.sleep(0.02)
        with lock:
            in_flight[key] -= 1
        return b"# markdown"

    _install_opener(monkeypatch, handler)
    engine = _engine(monkeypatch, jina=("j1", "j2"))

    def worker(index: int) -> None:
        try:
            result = engine.fetch_fulltext(
                f"https://page{index}.example", method="jina"
            )
            assert result == "# markdown"
        except Exception as exc:  # pragma: no cover - failure surfacing
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert used == {"j1", "j2"}
    assert max(max_in_flight.values()) == 1


def test_jina_fetch_fails_over_from_429_to_next_healthy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization") == "Bearer j1"
            else b"# ok"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine._fetch_fulltext_jina("https://a.example") == "# ok"
    j1, j2 = router.lanes[0], router.lanes[1]
    assert j1.consecutive_429_count == 1
    assert j1.cool_until == 30.0
    assert j1.quarantined is False
    assert j1.busy is False
    assert j2.consecutive_429_count == 0
    assert j2.cool_until == 0.0
    assert j2.busy is False


def test_jina_fetch_fails_over_from_401_to_next_healthy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(401)
            if req.get_header("Authorization") == "Bearer j1"
            else b"# ok"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine._fetch_fulltext_jina("https://a.example") == "# ok"
    assert [call["authorization"] for call in calls] == ["Bearer j1", "Bearer j2"]
    j1, j2 = router.lanes[0], router.lanes[1]
    assert j1.quarantined is True
    assert j1.busy is False
    assert j2.quarantined is False
    assert j2.busy is False


def test_429_success_resets_penalty_after_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    remaining_429 = [1]
    router = ProviderKeyRouter(
        "jina",
        ["j1", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization") == "Bearer j1"
            and remaining_429[0] > 0
            else b"# ok"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine._fetch_fulltext_jina("https://a.example") == "# ok"
    remaining_429[0] = 0
    j1, j2 = router.lanes[0], router.lanes[1]
    assert j1.consecutive_429_count == 1
    assert j2.consecutive_429_count == 0

    clock[0] = 31.0
    assert engine._fetch_fulltext_jina("https://b.example") == "# ok"
    assert j2.consecutive_429_count == 0
    assert engine._fetch_fulltext_jina("https://c.example") == "# ok"
    assert j1.consecutive_429_count == 0
    assert j1.busy is False


def test_401_quarantines_only_selected_key_and_namespaces_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    jina_router = ProviderKeyRouter(
        "jina",
        ["shared-key", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    firecrawl_router = ProviderKeyRouter(
        "firecrawl",
        ["shared-key"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: jina_router)
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: firecrawl_router
    )

    def handler(req: urllib.request.Request):
        if req.full_url.startswith("https://r.jina.ai/"):
            if req.get_header("Authorization") == "Bearer shared-key":
                return _http_error(401)
            return b"# jina ok"
        if req.get_header("Authorization") == "Bearer shared-key":
            return b'{"data": {"markdown": "# fc ok"}}'
        return b'{"data": {"markdown": "# fc other"}}'

    _install_opener(monkeypatch, handler)
    engine = _engine(
        monkeypatch,
        jina=("shared-key", "j2"),
        firecrawl=("shared-key",),
    )
    assert engine._fetch_fulltext_jina("https://a.example") == "# jina ok"
    jina_shared = jina_router.lanes[0]
    assert jina_shared.quarantined is True
    assert jina_router.lanes[1].quarantined is False
    assert jina_router.lanes[1].busy is False
    assert firecrawl_router.lanes[0].quarantined is False
    assert firecrawl_router.lanes[0].busy is False

    assert engine._fetch_fulltext_firecrawl("https://a.example") == "# fc ok"
    assert firecrawl_router.lanes[0].busy is False
    assert engine._fetch_fulltext_jina("https://b.example") == "# jina ok"
    assert jina_router.lanes[1].busy is False


def test_5xx_transient_cooldown_bounded_single_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(monkeypatch, lambda req: _http_error(500))
    engine = _engine(monkeypatch, jina=("j1",))
    assert engine._fetch_fulltext_jina("https://a.example") == ""
    lane = router.lanes[0]
    assert len(calls) == 1
    assert lane.cool_until > 0
    assert lane.consecutive_429_count == 0
    assert lane.quarantined is False
    assert lane.busy is False


def test_last_failed_attempt_still_updates_health_and_releases_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(monkeypatch, lambda req: _http_error(429))
    engine = _engine(monkeypatch, jina=("j1",))
    assert engine._fetch_fulltext_jina("https://a.example") == ""
    lane = router.lanes[0]
    assert len(calls) == 1
    assert lane.consecutive_429_count == 1
    assert lane.cool_until > 0
    assert lane.busy is False


def test_no_key_anonymous_fallback_and_empty_firecrawl_pool_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_opener(monkeypatch, lambda req: b"# anonymous")
    engine = _engine(monkeypatch)
    assert engine.fetch_fulltext("https://a.example", method="jina") == "# anonymous"
    assert engine._search_firecrawl("q", 3) == []
    assert engine.fetch_fulltext("https://b.example", method="firecrawl") == (
        "# anonymous"
    )
    assert all(call["authorization"] is None for call in calls)


def test_keyed_failure_falls_back_to_anonymous_jina(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization")
            else b"# anonymous"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1",))
    assert engine.fetch_fulltext("https://a.example", method="jina") == (
        "# anonymous"
    )
    keyed = [call for call in calls if call["authorization"]]
    anonymous = [call for call in calls if not call["authorization"]]
    assert len(keyed) == 1
    assert all(call["authorization"] == "Bearer j1" for call in keyed)
    assert len(anonymous) == 1


def test_cache_hit_precedes_key_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_opener(
        monkeypatch,
        lambda req: (_ for _ in ()).throw(
            AssertionError("provider must not be called on cache hit")
        ),
    )
    engine = _engine(monkeypatch, jina=("j1",), firecrawl=("f1",))
    engine._fulltext_cache["https://cached.example"] = "# cached"
    assert engine.fetch_fulltext("https://cached.example") == "# cached"

    monkeypatch.setattr(
        search_engine,
        "_fulltext_cache_get",
        lambda url: "# sqlite" if url == "https://sqlite.example" else "",
    )
    assert engine.fetch_fulltext("https://sqlite.example") == "# sqlite"
    assert calls == []
    assert all(not lane.busy for lane in engine._jina_router_instance().lanes)
    assert all(not lane.busy for lane in engine._firecrawl_router_instance().lanes)


def test_firecrawl_search_fails_over_from_429_to_next_healthy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "firecrawl",
        ["f1", "f2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: router
    )

    def handler(req: urllib.request.Request):
        if req.get_header("Authorization") == "Bearer f1":
            return _http_error(429)
        return (
            b'{"data": [{"url": "https://x.example", "title": "X", '
            b'"description": "d", "content": "c"}]}'
        )

    calls = _install_opener(monkeypatch, handler)
    engine = _engine(monkeypatch, firecrawl=("f1", "f2"))
    results = engine._search_firecrawl("q", 3)
    assert len(results) == 1
    assert results[0]["backend"] == "firecrawl"
    assert [call["authorization"] for call in calls] == ["Bearer f1", "Bearer f2"]
    f1, f2 = router.lanes[0], router.lanes[1]
    assert f1.consecutive_429_count == 1
    assert f2.consecutive_429_count == 0
    assert f1.busy is False
    assert f2.busy is False


def test_firecrawl_scrape_fails_over_from_5xx_to_next_healthy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "firecrawl",
        ["f1", "f2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: router
    )
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(500)
            if req.get_header("Authorization") == "Bearer f1"
            else b'{"data": {"markdown": "# fc ok"}}'
        ),
    )
    engine = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine._fetch_fulltext_firecrawl("https://a.example") == "# fc ok"
    assert [call["authorization"] for call in calls] == ["Bearer f1", "Bearer f2"]
    f1, f2 = router.lanes[0], router.lanes[1]
    assert f1.cool_until == 5.0
    assert f1.consecutive_429_count == 0
    assert f1.quarantined is False
    assert f1.busy is False
    assert f2.consecutive_429_count == 0
    assert f2.busy is False


def test_all_keyed_attempts_fail_bounded_then_fallback_to_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "jina",
        ["j1", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization")
            else b"# anonymous"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine.fetch_fulltext("https://a.example", method="jina") == (
        "# anonymous"
    )
    keyed = [call for call in calls if call["authorization"]]
    anonymous = [call for call in calls if not call["authorization"]]
    assert [call["authorization"] for call in keyed] == ["Bearer j1", "Bearer j2"]
    assert len(anonymous) == 1
    j1, j2 = router.lanes[0], router.lanes[1]
    assert j1.consecutive_429_count == 1
    assert j2.consecutive_429_count == 1
    assert j1.busy is False
    assert j2.busy is False


def test_jina_all_keys_429_no_wait_one_attempt_per_key_then_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def forbidden_sleep(_seconds: float) -> None:
        raise AssertionError(
            "logical request must not sleep/cooldown-wait after pool exhaustion"
        )

    router = ProviderKeyRouter(
        "jina",
        ["j1", "j2"],
        min_interval_seconds=0.0,
        sleep_fn=forbidden_sleep,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(SearchEngine, "_build_jina_router", lambda self: router)
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization")
            else b"# anonymous"
        ),
    )
    engine = _engine(monkeypatch, jina=("j1", "j2"))
    assert engine.fetch_fulltext("https://a.example", method="jina") == (
        "# anonymous"
    )
    keyed = [call for call in calls if call["authorization"]]
    anonymous = [call for call in calls if not call["authorization"]]
    assert [call["authorization"] for call in keyed] == ["Bearer j1", "Bearer j2"]
    assert len(keyed) == 2
    assert len(anonymous) == 1
    j1, j2 = router.lanes[0], router.lanes[1]
    assert j1.consecutive_429_count == 1
    assert j2.consecutive_429_count == 1
    assert j1.busy is False
    assert j2.busy is False


def test_firecrawl_all_keys_429_bounded_without_wait_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def forbidden_sleep(_seconds: float) -> None:
        raise AssertionError(
            "logical request must not sleep/cooldown-wait after pool exhaustion"
        )

    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization")
            else b"# anonymous"
        ),
    )

    def make_firecrawl_router() -> ProviderKeyRouter:
        return ProviderKeyRouter(
            "firecrawl",
            ["f1", "f2"],
            min_interval_seconds=0.0,
            sleep_fn=forbidden_sleep,
            now_fn=lambda: clock[0],
        )

    def make_empty_jina_router() -> ProviderKeyRouter:
        return ProviderKeyRouter(
            "jina",
            [],
            min_interval_seconds=0.0,
            sleep_fn=forbidden_sleep,
            now_fn=lambda: clock[0],
        )

    # Firecrawl search: fresh pool, both keys 429, exactly two calls, no wait.
    ProviderKeyRouter.reset_process_lanes("firecrawl")
    ProviderKeyRouter.reset_process_lanes("jina")
    search_router = make_firecrawl_router()
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: search_router
    )
    monkeypatch.setattr(
        SearchEngine, "_build_jina_router", lambda self: make_empty_jina_router()
    )
    engine = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine._search_firecrawl("q", 3) == []
    assert [call["authorization"] for call in calls] == ["Bearer f1", "Bearer f2"]
    calls.clear()

    # Firecrawl scrape: fresh pool, both keys 429, exactly two calls, no wait.
    ProviderKeyRouter.reset_process_lanes("firecrawl")
    ProviderKeyRouter.reset_process_lanes("jina")
    scrape_router = make_firecrawl_router()
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: scrape_router
    )
    engine_scrape = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine_scrape._fetch_fulltext_firecrawl("https://a.example") == ""
    assert [call["authorization"] for call in calls] == ["Bearer f1", "Bearer f2"]
    calls.clear()

    # Full fetch: fresh Firecrawl pool exhausts, then Jina anonymous fallback.
    ProviderKeyRouter.reset_process_lanes("firecrawl")
    ProviderKeyRouter.reset_process_lanes("jina")
    fetch_router = make_firecrawl_router()
    fetch_jina_router = make_empty_jina_router()
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: fetch_router
    )
    monkeypatch.setattr(
        SearchEngine, "_build_jina_router", lambda self: fetch_jina_router
    )
    engine_fetch = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine_fetch.fetch_fulltext("https://b.example", method="firecrawl") == (
        "# anonymous"
    )
    assert [call["authorization"] for call in calls] == [
        "Bearer f1",
        "Bearer f2",
        None,
    ]
    assert all(not lane.busy for lane in fetch_router.lanes)


def test_precooled_pool_skipped_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]

    def forbidden_sleep(_seconds: float) -> None:
        raise AssertionError(
            "logical request must not sleep/cooldown-wait after pool exhaustion"
        )

    router = ProviderKeyRouter(
        "firecrawl",
        ["f1", "f2"],
        min_interval_seconds=0.0,
        sleep_fn=forbidden_sleep,
        now_fn=lambda: clock[0],
    )
    monkeypatch.setattr(
        SearchEngine, "_build_firecrawl_router", lambda self: router
    )
    calls = _install_opener(
        monkeypatch,
        lambda req: (
            _http_error(429)
            if req.get_header("Authorization")
            else b"# anonymous"
        ),
    )
    engine = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine._search_firecrawl("q", 3) == []
    assert [call["authorization"] for call in calls] == ["Bearer f1", "Bearer f2"]

    # Same fake keys share the process-wide lanes, which are now all cooled.
    # A new logical request must skip the pool immediately, with no call/wait.
    calls.clear()
    engine_2 = _engine(monkeypatch, firecrawl=("f1", "f2"))
    assert engine_2._search_firecrawl("q", 3) == []
    assert calls == []


def test_same_provider_health_shared_across_engine_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _engine(monkeypatch, jina=("j1",))
    second = _engine(monkeypatch, jina=("j1",))
    lane_first = first._jina_router_instance().lanes[0]
    lane_second = second._jina_router_instance().lanes[0]
    assert lane_first is lane_second
    first._jina_router_instance().rate_limit_cool_lane(lane_first, 30)
    assert lane_second.cool_until > 0
