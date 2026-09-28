"""Provider-neutral, thread-safe per-key lane routing foundation.

Internal transport enhancement only.  Public contracts (method signatures,
response shapes, result limits, discovery/retrieval business logic) are
unchanged.  Lanes are keyed by a mandatory provider namespace plus a stable
non-reversible fingerprint of the key, so OpenAlex, CORE, Jina, Firecrawl,
and Semantic Scholar never share lane health accidentally.  Within one
provider, separate router/transport instances share busy/cooldown/quarantine
state, while each router keeps its own local key positions for ``key_slot``
metrics.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

MIN_POLL_SECONDS = 0.05
MAX_ACQUIRE_WAIT = 1.0
MAX_429_COOLDOWN_SECONDS = 300.0
MAX_429_PENALTY_STEPS = 8

_PROVIDER_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_LANE_LOCK = threading.RLock()
_LANE_REGISTRY: dict[tuple[str, str], "ProviderKeyLane"] = {}


def _normalize_provider(provider: str) -> str:
    normalized = str(provider or "").strip().casefold()
    if not _PROVIDER_RE.match(normalized):
        raise ValueError(
            "provider must be a non-empty [a-z0-9_] namespace, "
            f"got {provider!r}"
        )
    return normalized


def provider_lane_identifier(provider: str, key: str) -> str:
    """Stable non-secret lane id: provider namespace + SHA-256 prefix."""

    provider = _normalize_provider(provider)
    if not key:
        return ""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return f"{provider}:lane:{digest[:16]}"


@dataclass(slots=True)
class ProviderKeyLane:
    """One routable key slot with pacing/cooldown/quarantine state."""

    slot: int | None
    lane_id: str
    key: str
    cool_until: float = 0.0
    quarantined: bool = False
    busy: bool = False
    last_request_at: float = 0.0
    consecutive_429_count: int = 0


class ProviderKeyRouter:
    """Round-robin, thread-safe lane selection with per-key pacing."""

    def __init__(
        self,
        provider: str,
        keys: Sequence[str],
        *,
        min_interval_seconds: float = 1.1,
        max_attempts: int | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
        reserve_fn: Callable[[str | None, float], float] | None = None,
        lane_id_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.provider = _normalize_provider(provider)
        self.keys = list(
            dict.fromkeys(
                str(key).strip()
                for key in keys
                if str(key).strip()
            )
        )
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_attempts = max_attempts or max(
            3, len(self.keys) * 2 or 3
        )
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        self.reserve_fn = reserve_fn
        self.lane_id_fn = lane_id_fn or (
            lambda key: provider_lane_identifier(self.provider, key)
        )
        self.lanes: list[ProviderKeyLane] = []
        with _LANE_LOCK:
            if self.keys:
                for index, key in enumerate(self.keys):
                    lane_id = self.lane_id_fn(key)
                    lane = _LANE_REGISTRY.get((self.provider, lane_id))
                    if lane is None:
                        lane = ProviderKeyLane(
                            slot=index,
                            lane_id=lane_id,
                            key=key,
                        )
                        _LANE_REGISTRY[(self.provider, lane_id)] = lane
                    self.lanes.append(lane)
            else:
                # Public/no-key path: one conservative anonymous lane per
                # router; blank/zero keys are never authenticated lanes.
                self.lanes.append(
                    ProviderKeyLane(slot=None, lane_id="", key="")
                )
        self._local_slots = {
            lane.lane_id: index
            for index, lane in enumerate(self.lanes)
            if lane.lane_id
        }
        self._cursor = 0

    @property
    def multi_key(self) -> bool:
        return len(self.keys) > 1

    @property
    def quarantined_count(self) -> int:
        with _LANE_LOCK:
            return sum(1 for lane in self.lanes if lane.quarantined)

    def local_slot(self, lane: ProviderKeyLane) -> int | None:
        """Local key position for this router, independent of shared state."""

        if not lane.lane_id:
            return None
        return self._local_slots.get(lane.lane_id, lane.slot)

    @classmethod
    def reset_process_lanes(cls, provider: str | None = None) -> None:
        """Clear process-wide shared lane state (test isolation helper).

        With ``provider``, only that provider's lanes are cleared; otherwise
        all providers are cleared.
        """

        with _LANE_LOCK:
            if provider is None:
                _LANE_REGISTRY.clear()
                return
            normalized = _normalize_provider(provider)
            for key in [
                key for key in _LANE_REGISTRY if key[0] == normalized
            ]:
                del _LANE_REGISTRY[key]

    def acquire_lane(self) -> tuple[ProviderKeyLane | None, float]:
        """Reserve an available lane; returns ``(lane, wait_seconds)``."""

        total_wait = 0.0
        selected: ProviderKeyLane | None = None
        while selected is None:
            now = self.now_fn()
            with _LANE_LOCK:
                eligible = [
                    lane
                    for lane in self.lanes
                    if not lane.busy
                    and not lane.quarantined
                    and now >= lane.cool_until
                    and now
                    >= lane.last_request_at + self.min_interval_seconds
                ]
                if eligible:
                    lane_count = len(self.lanes)
                    chosen_index: int | None = None
                    for offset in range(lane_count):
                        candidate_index = (
                            self._cursor + offset
                        ) % lane_count
                        candidate = self.lanes[candidate_index]
                        if candidate in eligible:
                            selected = candidate
                            chosen_index = candidate_index
                            break
                    if selected is not None:
                        selected.busy = True
                        selected.last_request_at = now
                        if chosen_index is not None:
                            self._cursor = (
                                chosen_index + 1
                            ) % lane_count
                        break
                usable = [
                    lane for lane in self.lanes if not lane.quarantined
                ]
                if not usable:
                    return None, total_wait
                deadlines: list[float] = []
                for lane in usable:
                    deadline = max(
                        lane.cool_until,
                        lane.last_request_at + self.min_interval_seconds,
                    )
                    if deadline > now:
                        deadlines.append(deadline - now)
                wait = (
                    min(deadlines)
                    if deadlines
                    else MIN_POLL_SECONDS
                )
                wait = min(max(wait, 0.0), MAX_ACQUIRE_WAIT)
            self.sleep_fn(wait)
            total_wait += wait
        if (
            self.reserve_fn is not None
            and self.min_interval_seconds > 0
        ):
            try:
                reserve_wait = self.reserve_fn(
                    selected.lane_id or None,
                    self.min_interval_seconds,
                )
                if reserve_wait > 0:
                    self.sleep_fn(reserve_wait)
                    total_wait += reserve_wait
            except Exception:
                self.release_lane(selected)
                raise
        return selected, total_wait

    def release_lane(self, lane: ProviderKeyLane) -> None:
        with _LANE_LOCK:
            lane.busy = False

    def cool_lane(self, lane: ProviderKeyLane, seconds: float) -> None:
        with _LANE_LOCK:
            lane.cool_until = self.now_fn() + max(0.0, float(seconds))

    def rate_limit_cool_lane(
        self,
        lane: ProviderKeyLane,
        base_seconds: float,
    ) -> None:
        """Escalate a 429 cooldown for one lane without deleting the key."""

        with _LANE_LOCK:
            lane.consecutive_429_count += 1
            step = min(
                lane.consecutive_429_count,
                MAX_429_PENALTY_STEPS,
            )
            cooldown = min(
                float(base_seconds) * (2 ** (step - 1)),
                MAX_429_COOLDOWN_SECONDS,
            )
            lane.cool_until = self.now_fn() + max(0.0, cooldown)

    def reset_lane_penalty(self, lane: ProviderKeyLane) -> None:
        with _LANE_LOCK:
            lane.consecutive_429_count = 0

    def quarantine_lane(self, lane: ProviderKeyLane) -> None:
        with _LANE_LOCK:
            lane.quarantined = True
            lane.busy = False


__all__ = [
    "MAX_429_COOLDOWN_SECONDS",
    "MAX_429_PENALTY_STEPS",
    "MIN_POLL_SECONDS",
    "ProviderKeyLane",
    "ProviderKeyRouter",
    "provider_lane_identifier",
]
