"""Focused offline tests for the S2 multi-key routing layer.

Uses fake keys and fake openers only; no real credentials or network calls.
"""

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

from optomind_research.s2_cache import S2PersistentCache
from optomind_research.s2_intelligence_gateway import S2Transport
from optomind_research.s2_key_router import S2KeyRouter, lane_identifier


@pytest.fixture(autouse=True)
def _reset_process_wide_lanes() -> None:
    S2KeyRouter.reset_process_lanes()
    yield


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


def _http_error(code: int, retry_after: str = "") -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        "https://example.test",
        code,
        "error",
        headers,
        io.BytesIO(b"error"),
    )


def test_concurrent_requests_distribute_across_keys_without_overlap(
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    keys_seen: list[str] = []
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = str(request.headers.get("X-api-key", ""))
        with lock:
            keys_seen.append(key)
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
        time.sleep(0.05)
        with lock:
            in_flight[key] -= 1
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1", "k2", "k3"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=2,
    )
    urls = [
        f"https://api.semanticscholar.org/graph/v1/paper/{index}"
        for index in range(9)
    ]
    with ThreadPoolExecutor(max_workers=9) as pool:
        results = list(
            pool.map(lambda url: transport.request_json("GET", url), urls)
        )
    assert all(result.ok for result in results)
    assert set(keys_seen) == {"k1", "k2", "k3"}
    assert max(max_in_flight.values()) == 1


def test_429_cools_affected_key_and_another_succeeds(tmp_path: Path) -> None:
    calls = 0
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        key = str(request.headers.get("X-api-key", ""))
        keys_seen.append(key)
        if key == "k1" and calls == 1:
            raise _http_error(429, "2")
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert result.ok
    assert keys_seen == ["k1", "k2"]
    keys_seen.clear()
    transport.request_json("GET", "https://example.test/b")
    assert keys_seen == ["k2"]


def test_5xx_cools_lane_without_incrementing_429_penalty(
    tmp_path: Path,
) -> None:
    calls = 0

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(503, "1")
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["server-a", "server-b"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert result.ok
    assert calls == 2
    assert transport.router.lanes[0].consecutive_429_count == 0


def test_final_429_still_cools_key_for_next_logical_request(
    tmp_path: Path,
) -> None:
    def opener(request: Any, **_: Any) -> _FakeResponse:
        raise _http_error(429, "5")

    transport = S2Transport(
        keys=["limited-key"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=1,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert not result.ok
    lane = transport.router.lanes[0]
    assert lane.consecutive_429_count == 1
    assert lane.cool_until > time.monotonic()


def test_401_isolates_bad_key_for_transport_lifetime(tmp_path: Path) -> None:
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = str(request.headers.get("X-api-key", ""))
        keys_seen.append(key)
        if key == "k1":
            raise _http_error(401)
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    first = transport.request_json("GET", "https://example.test/a")
    assert first.ok
    assert keys_seen == ["k1", "k2"]
    keys_seen.clear()
    second = transport.request_json("GET", "https://example.test/b")
    assert second.ok
    assert keys_seen == ["k2"]
    assert transport.router.quarantined_count == 1


def test_single_key_stays_conservative_with_pacing(tmp_path: Path) -> None:
    waits: list[float] = []
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(str(request.headers.get("X-api-key", "")))
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=1.1,
    )
    assert transport.request_json(
        "GET", "https://example.test/a"
    ).ok
    assert transport.request_json(
        "GET", "https://example.test/b"
    ).ok
    assert keys_seen == ["k1", "k1"]
    assert sum(waits) >= 1.0


def test_single_key_429_waits_bounded_and_retries_same_key(
    tmp_path: Path,
) -> None:
    calls = 0
    waits: list[float] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(429, "2")
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["k1"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=0,
        max_attempts=3,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert result.ok
    assert calls == 2
    assert sum(waits) >= 1.9


def test_no_key_public_conservative_pacing(tmp_path: Path) -> None:
    waits: list[float] = []
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(str(request.headers.get("X-api-key", "")))
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=[],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=1.1,
    )
    assert transport.request_json(
        "GET", "https://example.test/a"
    ).ok
    assert transport.request_json(
        "GET", "https://example.test/b"
    ).ok
    assert keys_seen == ["", ""]
    assert sum(waits) >= 1.0


def test_all_keys_quarantined_fails_bounded(tmp_path: Path) -> None:
    calls = 0

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        raise _http_error(401)

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=4,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert not result.ok
    assert result.status_category == "authentication_failure"
    assert calls == 2


def test_multi_key_cache_first_avoids_routing(tmp_path: Path) -> None:
    calls: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        calls.append(str(request.headers.get("X-api-key", "")))
        return _FakeResponse({"data": [{"paperId": "p1"}]})

    transport = S2Transport(
        keys=["k1", "k2"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
    )
    first = transport.request_json(
        "GET",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": "BIC"},
    )
    second = transport.request_json(
        "GET",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": "BIC"},
    )
    assert first.ok and not first.cache_hit
    assert second.ok and second.cache_hit
    assert len(calls) == 1


def test_lane_identifier_is_stable_and_non_secret() -> None:
    first = lane_identifier("super-secret-key")
    second = lane_identifier("super-secret-key")
    other = lane_identifier("another-secret-key")
    assert first == second
    assert first.startswith("lane:")
    assert len(first) == len("lane:") + 16
    assert first != other
    assert "super-secret-key" not in first
    assert lane_identifier("") == ""


def test_reserve_lane_slot_validates_non_secret_lane_id(
    tmp_path: Path,
) -> None:
    cache = S2PersistentCache(tmp_path / "s2.sqlite")
    assert cache.reserve_lane_slot(
        lane_id="lane:0123456789abcdef",
        min_interval_seconds=0,
    ) == 0
    try:
        cache.reserve_lane_slot(
            lane_id="super-secret-key",
            min_interval_seconds=1.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("raw key material must not be accepted as a lane id")


def test_single_authenticated_key_reserves_its_key_lane() -> None:
    reservations: list[str | None] = []
    router = S2KeyRouter(
        keys=["single-key"],
        min_interval_seconds=1.1,
        sleep_fn=lambda _: None,
        reserve_fn=lambda lane_id, _interval: reservations.append(lane_id) or 0.0,
    )
    lane, _ = router.acquire_lane()
    assert lane is not None
    router.release_lane(lane)
    assert reservations == [lane_identifier("single-key")]


def test_failed_cross_process_reservation_releases_lane() -> None:
    router = S2KeyRouter(
        keys=["reservation-key"],
        min_interval_seconds=1.1,
        sleep_fn=lambda _: None,
        reserve_fn=lambda _lane_id, _interval: (_ for _ in ()).throw(
            RuntimeError("reservation failed")
        ),
    )
    with pytest.raises(RuntimeError, match="reservation failed"):
        router.acquire_lane()
    assert router.lanes[0].busy is False


def test_router_round_robin_prefers_available_lanes() -> None:
    router = S2KeyRouter(
        keys=["k1", "k2"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    first, _ = router.acquire_lane()
    second, _ = router.acquire_lane()
    assert first is not None and second is not None
    assert first.key != second.key
    router.release_lane(first)
    router.release_lane(second)


def test_two_transports_share_same_key_lanes_concurrently(
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    keys_seen: list[str] = []
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = str(request.headers.get("X-api-key", ""))
        with lock:
            keys_seen.append(key)
            in_flight[key] = in_flight.get(key, 0) + 1
            max_in_flight[key] = max(max_in_flight.get(key, 0), in_flight[key])
        time.sleep(0.05)
        with lock:
            in_flight[key] -= 1
        return _FakeResponse({"ok": True})

    cache_path = tmp_path / "s2.sqlite"
    first = S2Transport(
        keys=["shared-k1", "shared-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=2,
    )
    second = S2Transport(
        keys=["shared-k1", "shared-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=2,
    )
    transports = [first, second]
    urls = [
        f"https://api.semanticscholar.org/graph/v1/paper/{index}"
        for index in range(8)
    ]

    def run(index: int):
        transport = transports[index % 2]
        return transport.request_json("GET", urls[index])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert all(result.ok for result in results)
    assert set(keys_seen) == {"shared-k1", "shared-k2"}
    assert max(max_in_flight.values()) == 1


def test_shared_lane_cooldown_across_transports(tmp_path: Path) -> None:
    keys_seen: list[str] = []
    calls = 0

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        key = str(request.headers.get("X-api-key", ""))
        keys_seen.append(key)
        if key == "cool-k1" and calls == 1:
            raise _http_error(429, "60")
        return _FakeResponse({"ok": True})

    cache_path = tmp_path / "s2.sqlite"
    first = S2Transport(
        keys=["cool-k1", "cool-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    second = S2Transport(
        keys=["cool-k1", "cool-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    assert first.request_json("GET", "https://example.test/a").ok
    assert keys_seen == ["cool-k1", "cool-k2"]
    keys_seen.clear()
    assert second.request_json("GET", "https://example.test/b").ok
    assert keys_seen == ["cool-k2"]


def test_shared_lane_quarantine_across_transports(tmp_path: Path) -> None:
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        key = str(request.headers.get("X-api-key", ""))
        keys_seen.append(key)
        if key == "bad-k1":
            raise _http_error(401)
        return _FakeResponse({"ok": True})

    cache_path = tmp_path / "s2.sqlite"
    first = S2Transport(
        keys=["bad-k1", "good-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    second = S2Transport(
        keys=["bad-k1", "good-k2"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
        max_attempts=3,
    )
    assert first.request_json("GET", "https://example.test/a").ok
    assert keys_seen == ["bad-k1", "good-k2"]
    keys_seen.clear()
    assert second.request_json("GET", "https://example.test/b").ok
    assert keys_seen == ["good-k2"]
    assert second.router.quarantined_count == 1


def test_repeated_429_escalates_cooldown_and_success_resets() -> None:
    clock = [0.0]
    router = S2KeyRouter(
        keys=["escalate-a", "escalate-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    lane = router.lanes[0]
    router.rate_limit_cool_lane(lane, 2)
    assert lane.consecutive_429_count == 1
    assert lane.cool_until == 2.0
    clock[0] = 2.0
    router.rate_limit_cool_lane(lane, 2)
    assert lane.cool_until == 6.0
    clock[0] = 6.0
    router.rate_limit_cool_lane(lane, 2)
    assert lane.cool_until == 14.0
    router.reset_lane_penalty(lane)
    clock[0] = 14.0
    router.rate_limit_cool_lane(lane, 2)
    assert lane.consecutive_429_count == 1
    assert lane.cool_until == 16.0
    assert lane.quarantined is False
    other = router.lanes[1]
    assert other.consecutive_429_count == 0


def test_single_key_repeated_429_escalates_then_succeeds(
    tmp_path: Path,
) -> None:
    calls = 0
    waits: list[float] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _http_error(429, "1")
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["escalate-single"],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=waits.append,
        min_interval_seconds=0,
        max_attempts=4,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert result.ok
    assert calls == 3
    assert sum(waits) >= 2.9
    assert transport.router.lanes[0].consecutive_429_count == 0


def test_round_robin_continues_after_chosen_lane() -> None:
    router = S2KeyRouter(
        keys=["rr-a", "rr-b", "rr-c"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    selected: list[str] = []
    for _ in range(6):
        lane, _ = router.acquire_lane()
        assert lane is not None
        selected.append(lane.key)
        router.release_lane(lane)
    assert selected == ["rr-a", "rr-b", "rr-c", "rr-a", "rr-b", "rr-c"]


def test_blank_keys_treated_as_public(tmp_path: Path) -> None:
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(str(request.headers.get("X-api-key", "")))
        return _FakeResponse({"ok": True})

    transport = S2Transport(
        keys=["", "   "],
        cache_path=tmp_path / "s2.sqlite",
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
    )
    result = transport.request_json("GET", "https://example.test/a")
    assert result.ok
    assert keys_seen == [""]
    assert transport.router.multi_key is False


def test_shared_lane_local_key_slot_follows_router_key_order() -> None:
    router_a = S2KeyRouter(
        keys=["order-a", "order-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    router_b = S2KeyRouter(
        keys=["order-b", "order-a"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert router_a.local_slot(router_a.lanes[0]) == 0
    assert router_a.local_slot(router_a.lanes[1]) == 1
    assert router_b.local_slot(router_b.lanes[0]) == 0
    assert router_b.local_slot(router_b.lanes[1]) == 1
    # Shared health state is keyed by lane id, not by local position.
    assert router_a.lanes[1] is router_b.lanes[0]
    assert router_a.lanes[0] is router_b.lanes[1]


def test_transport_key_slot_uses_local_key_order(tmp_path: Path) -> None:
    keys_seen: list[str] = []

    def opener(request: Any, **_: Any) -> _FakeResponse:
        keys_seen.append(str(request.headers.get("X-api-key", "")))
        return _FakeResponse({"ok": True})

    cache_path = tmp_path / "s2.sqlite"
    S2Transport(
        keys=["slot-a", "slot-b"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
    )
    second = S2Transport(
        keys=["slot-b", "slot-a"],
        cache_path=cache_path,
        opener=opener,
        sleep_fn=lambda _: None,
        min_interval_seconds=0,
    )
    result = second.request_json("GET", "https://example.test/x")
    assert result.ok
    assert keys_seen == ["slot-b"]
    assert result.key_slot == 0
