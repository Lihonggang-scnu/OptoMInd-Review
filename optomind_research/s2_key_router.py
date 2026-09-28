"""Semantic Scholar compatibility wrapper over the provider lane router.

Public imports, constructor signature, ``key_slot`` semantics, and
``lane_identifier`` output are preserved for existing S2 transports and
tests.  The implementation delegates to the provider-neutral
:class:`optomind_research.provider_key_router.ProviderKeyRouter` under the
``s2`` namespace so S2 lanes never share health state with other providers.
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Sequence

from optomind_research.provider_key_router import (
    MAX_429_COOLDOWN_SECONDS,
    MAX_429_PENALTY_STEPS,
    MIN_POLL_SECONDS,
    ProviderKeyLane,
    ProviderKeyRouter,
)


def lane_identifier(key: str) -> str:
    """Stable non-secret lane id (SHA-256 prefix); never the key itself."""

    if not key:
        return ""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return "lane:" + digest[:16]


S2KeyLane = ProviderKeyLane


class S2KeyRouter(ProviderKeyRouter):
    """S2-namespaced router preserving the original S2 public API."""

    def __init__(
        self,
        keys: Sequence[str],
        *,
        min_interval_seconds: float = 1.1,
        max_attempts: int | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.monotonic,
        reserve_fn: Callable[[str | None, float], float] | None = None,
    ) -> None:
        super().__init__(
            "s2",
            keys,
            min_interval_seconds=min_interval_seconds,
            max_attempts=max_attempts,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            reserve_fn=reserve_fn,
            lane_id_fn=lane_identifier,
        )

    @classmethod
    def reset_process_lanes(cls) -> None:
        ProviderKeyRouter.reset_process_lanes(provider="s2")


__all__ = [
    "MIN_POLL_SECONDS",
    "MAX_429_COOLDOWN_SECONDS",
    "MAX_429_PENALTY_STEPS",
    "S2KeyLane",
    "S2KeyRouter",
    "lane_identifier",
]
