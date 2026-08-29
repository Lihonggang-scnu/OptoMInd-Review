"""Focused offline tests for the provider-neutral lane router."""

from __future__ import annotations

import threading

import pytest

from optomind_research.provider_key_router import (
    ProviderKeyRouter,
    provider_lane_identifier,
)


@pytest.fixture(autouse=True)
def _reset_process_wide_lanes() -> None:
    ProviderKeyRouter.reset_process_lanes()
    yield


def test_cross_provider_lanes_do_not_share_health() -> None:
    openalex = ProviderKeyRouter(
        "openalex",
        ["same-key"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    core = ProviderKeyRouter(
        "core",
        ["same-key"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert openalex.lanes[0] is not core.lanes[0]
    openalex.rate_limit_cool_lane(openalex.lanes[0], 60)
    assert core.lanes[0].cool_until == 0.0
    core.quarantine_lane(core.lanes[0])
    assert openalex.lanes[0].quarantined is False


def test_same_provider_routers_share_health() -> None:
    first = ProviderKeyRouter(
        "openalex",
        ["shared-a", "shared-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    second = ProviderKeyRouter(
        "openalex",
        ["shared-a", "shared-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert first.lanes[0] is second.lanes[0]
    first.rate_limit_cool_lane(first.lanes[0], 30)
    assert second.lanes[0].cool_until > 0
    second.quarantine_lane(second.lanes[1])
    assert first.quarantined_count == 1


def test_reversed_local_key_order_preserves_local_slots() -> None:
    first = ProviderKeyRouter(
        "openalex",
        ["order-a", "order-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    second = ProviderKeyRouter(
        "openalex",
        ["order-b", "order-a"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert first.local_slot(first.lanes[0]) == 0
    assert first.local_slot(first.lanes[1]) == 1
    assert second.local_slot(second.lanes[0]) == 0
    assert second.local_slot(second.lanes[1]) == 1
    assert first.lanes[0] is second.lanes[1]
    assert first.lanes[1] is second.lanes[0]


def test_blank_keys_treated_as_public() -> None:
    router = ProviderKeyRouter(
        "openalex",
        ["", "   "],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    assert router.multi_key is False
    assert router.lanes[0].key == ""
    assert router.lanes[0].lane_id == ""
    assert router.local_slot(router.lanes[0]) is None


def test_429_escalates_and_success_resets() -> None:
    clock = [0.0]
    router = ProviderKeyRouter(
        "openalex",
        ["escalate-a", "escalate-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
        now_fn=lambda: clock[0],
    )
    lane = router.lanes[0]
    router.rate_limit_cool_lane(lane, 2)
    assert lane.cool_until == 2.0
    clock[0] = 2.0
    router.rate_limit_cool_lane(lane, 2)
    assert lane.cool_until == 6.0
    router.reset_lane_penalty(lane)
    clock[0] = 6.0
    router.rate_limit_cool_lane(lane, 2)
    assert lane.consecutive_429_count == 1
    assert lane.cool_until == 8.0
    assert lane.quarantined is False


def test_5xx_transient_cooldown_does_not_quarantine() -> None:
    router = ProviderKeyRouter(
        "core",
        ["fivexx-key"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    lane = router.lanes[0]
    router.cool_lane(lane, 5)
    assert lane.cool_until > 0
    assert lane.quarantined is False


def test_release_on_reservation_error() -> None:
    def failing_reserve(_lane_id, _interval):
        raise RuntimeError("reservation failed")

    router = ProviderKeyRouter(
        "openalex",
        ["reserve-key"],
        min_interval_seconds=1.0,
        sleep_fn=lambda _: None,
        reserve_fn=failing_reserve,
    )
    with pytest.raises(RuntimeError, match="reservation failed"):
        router.acquire_lane()
    assert router.lanes[0].busy is False


def test_concurrent_same_key_never_overlaps_within_provider() -> None:
    lock = threading.Lock()
    in_flight: dict[str, int] = {}
    max_in_flight: dict[str, int] = {}

    router = ProviderKeyRouter(
        "openalex",
        ["conc-a", "conc-b"],
        min_interval_seconds=0,
        sleep_fn=lambda _: None,
    )
    errors: list[str] = []

    def worker():
        try:
            lane, _ = router.acquire_lane()
            assert lane is not None
            key = lane.key
            with lock:
                in_flight[key] = in_flight.get(key, 0) + 1
                max_in_flight[key] = max(
                    max_in_flight.get(key, 0), in_flight[key]
                )
            # Simulated request time; busy flag keeps the key exclusive.
            import time

            time.sleep(0.02)
            with lock:
                in_flight[key] -= 1
            router.release_lane(lane)
        except Exception as exc:  # pragma: no cover - failure surfacing
            errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert max(max_in_flight.values()) == 1


def test_provider_lane_identifier_format() -> None:
    openalex = provider_lane_identifier("openalex", "secret-key")
    core = provider_lane_identifier("core", "secret-key")
    assert openalex.startswith("openalex:lane:")
    assert core.startswith("core:lane:")
    assert openalex != core
    assert len(openalex) == len("openalex:lane:") + 16
    assert provider_lane_identifier("openalex", "") == ""
