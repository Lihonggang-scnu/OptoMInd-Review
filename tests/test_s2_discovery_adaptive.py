"""Offline tests for the bounded adaptive S2 discovery fanout policy."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from optomind_research.s2_discovery import (
    S2BatchFanoutHealth,
    S2DiscoveryPortfolioBuilder,
    S2WorkerFanoutPolicy,
    ScholarFacetRequest,
)
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.s2_schemas import parse_paper_record


def _response(*, status_code=200, category="ok", cache=False) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        status_category=category,
        cache_hit=cache,
    )


class _ScriptedGateway:
    """Returns scripted responses in strict call order for offline waves."""

    def __init__(self, script: dict[str, tuple[int, str]]) -> None:
        self.script = script
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def search_papers(self, query: str, **_: Any):
        with self._lock:
            self.calls.append(query)
        status_code, category = self.script[query]
        healthy = category in {"ok", "cache_hit"}
        papers = (
            [
                parse_paper_record(
                    {
                        "paperId": f"paper-{query}",
                        "title": f"Paper for {query}",
                        "abstract": (
                            "A sufficiently long abstract for adaptive "
                            "fanout testing."
                        ),
                        "year": 2024,
                    }
                )
            ]
            if healthy
            else []
        )
        return papers, S2GatewayResponse(
            ok=healthy,
            status_code=status_code,
            status_category=category,
            cache_hit=healthy and category == "cache_hit",
        )


def _facet(facet_id: str, query_count: int) -> ScholarFacetRequest:
    return ScholarFacetRequest(
        facet_id=facet_id,
        queries=[f"{facet_id}-q{index}" for index in range(query_count)],
        requested_roles=[],
        direct_only=True,
        max_results_per_query=2,
    )


def test_policy_promotes_only_on_clean_batches_and_caps_at_8() -> None:
    policy = S2WorkerFanoutPolicy()
    clean = S2BatchFanoutHealth.from_responses(
        [_response(), _response()]
    )
    levels = []
    decisions = []
    for _ in range(4):
        audit = policy.observe_batch(clean)
        levels.append(audit["level_after"])
        decisions.append(audit["decision"])

    assert levels == [6, 8, 8, 8]
    assert decisions == ["promote", "promote", "hold", "hold"]
    assert max(record["level_after"] for record in policy.history) <= 8


def test_policy_rolls_back_on_429_with_cooldown_and_recovers() -> None:
    policy = S2WorkerFanoutPolicy()
    clean = S2BatchFanoutHealth.from_responses([_response()])
    rate_limited = S2BatchFanoutHealth.from_responses(
        [
            _response(),
            _response(status_code=429, category="availability_delay"),
        ]
    )

    assert policy.observe_batch(clean)["level_after"] == 6
    assert policy.observe_batch(clean)["level_after"] == 8
    rollback = policy.observe_batch(rate_limited)
    assert rollback["level_after"] == 6
    assert rollback["decision"] == "rollback"
    assert rollback["cooldown_batches_remaining"] == 1
    assert policy.observe_batch(clean)["level_after"] == 6
    assert policy.observe_batch(clean)["level_after"] == 8


def test_transport_failure_holds_level_without_promotion() -> None:
    policy = S2WorkerFanoutPolicy()
    clean = S2BatchFanoutHealth.from_responses([_response()])
    policy.observe_batch(clean)  # 4 -> 6
    transport = S2BatchFanoutHealth.from_responses(
        [
            _response(status_code=0, category="availability_delay"),
            _response(),
        ]
    )
    audit = policy.observe_batch(transport)
    assert audit["level_after"] == 6
    assert audit["decision"] == "hold"
    assert policy.observe_batch(clean)["level_after"] == 8


def test_contract_failure_holds_level_without_promotion() -> None:
    policy = S2WorkerFanoutPolicy()
    clean = S2BatchFanoutHealth.from_responses([_response()])
    policy.observe_batch(clean)  # 4 -> 6
    contract = S2BatchFanoutHealth.from_responses(
        [
            _response(
                status_code=200, category="request_contract_failure"
            ),
            _response(),
        ]
    )
    audit = policy.observe_batch(contract)
    assert audit["level_after"] == 6
    assert audit["decision"] == "hold"
    assert audit["contract_failure_count"] == 1
    assert policy.observe_batch(clean)["level_after"] == 8


def test_policy_never_exposes_keys_or_promotes_past_max() -> None:
    policy = S2WorkerFanoutPolicy(initial_level=8, cooldown_batches=2)
    clean = S2BatchFanoutHealth.from_responses([_response()])
    assert policy.observe_batch(clean)["level_after"] == 8
    rate_limited = S2BatchFanoutHealth.from_responses(
        [_response(status_code=429, category="availability_delay")]
    )
    policy.observe_batch(rate_limited)
    serialized = json.dumps(policy.history, sort_keys=True)
    assert "x-api-key" not in serialized.casefold()
    assert policy.history[-1]["rate_limit_count"] == 1
    assert '"rate_limit_count": 1' in serialized
    assert all(record["level_after"] <= 8 for record in policy.history)


def test_runtime_adaptive_builder_advances_and_preserves_order(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPTOMIND_S2_ADAPTIVE_DISCOVERY", raising=False)
    script: dict[str, tuple[int, str]] = {}
    for batch in range(3):
        facet_id = f"f{batch + 1}"
        script.update(
            {
                f"{facet_id}-q{index}": (
                    200,
                    "cache_hit" if batch == 2 else "ok",
                )
                for index in range(8)
            }
        )
    gateway = _ScriptedGateway(script)
    builder = S2DiscoveryPortfolioBuilder(
        gateway=gateway, adaptive_fanout=True, max_workers=8
    )
    portfolio = builder.discover(
        [
            _facet("f1", 8),
            _facet("f2", 8),
            _facet("f3", 8),
        ]
    )

    assert [run["query"] for run in portfolio.query_runs] == [
        f"{facet}-q{index}"
        for facet in ("f1", "f2", "f3")
        for index in range(8)
    ]
    assert [run["concurrency_workers"] for run in portfolio.query_runs] == [
        *([4] * 8),
        *([6] * 8),
        *([8] * 8),
    ]
    assert [
        record["decision"] for record in portfolio.adaptive_fanout_history
    ] == ["promote", "promote", "hold"]
    serialized = json.dumps(portfolio.to_dict(), sort_keys=True)
    assert "x-api-key" not in serialized.casefold()


def test_runtime_rollback_uses_lower_level_for_next_batch(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPTOMIND_S2_ADAPTIVE_DISCOVERY", raising=False)
    script: dict[str, tuple[int, str]] = {
        **{f"f1-q{index}": (200, "ok") for index in range(8)},
        "f2-q0": (429, "availability_delay"),
        **{f"f2-q{index}": (200, "ok") for index in range(1, 8)},
        **{f"f3-q{index}": (200, "ok") for index in range(8)},
    }
    gateway = _ScriptedGateway(script)
    builder = S2DiscoveryPortfolioBuilder(
        gateway=gateway, adaptive_fanout=True, max_workers=8
    )
    portfolio = builder.discover(
        [_facet("f1", 8), _facet("f2", 8), _facet("f3", 8)]
    )

    assert [run["concurrency_workers"] for run in portfolio.query_runs] == [
        *([4] * 8),
        *([6] * 8),
        *([4] * 8),
    ]
    assert [
        record["decision"] for record in portfolio.adaptive_fanout_history
    ] == ["promote", "rollback", "hold"]
    serialized = json.dumps(portfolio.to_dict(), sort_keys=True)
    assert "x-api-key" not in serialized.casefold()


def test_adaptive_mode_is_opt_in_and_fixed_default_is_unchanged(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("OPTOMIND_S2_ADAPTIVE_DISCOVERY", raising=False)
    script: dict[str, tuple[int, str]] = {
        f"{facet_id}-q{index}": (200, "ok")
        for facet_id in ("f1", "f2", "f3")
        for index in range(2)
    }
    gateway = _ScriptedGateway(script)
    builder = S2DiscoveryPortfolioBuilder(gateway=gateway, max_workers=2)
    portfolio = builder.discover(
        [_facet("f1", 2), _facet("f2", 2), _facet("f3", 2)]
    )

    assert portfolio.adaptive_fanout_history == []
    assert all(
        "adaptive_policy_decision" not in run
        for run in portfolio.query_runs
    )
    assert all(run["concurrency_workers"] == 2 for run in portfolio.query_runs)


def test_env_enables_adaptive_and_explicit_max_workers_caps_runtime(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("OPTOMIND_S2_ADAPTIVE_DISCOVERY", "1")
    script: dict[str, tuple[int, str]] = {
        f"{facet_id}-q{index}": (200, "ok")
        for facet_id in ("f1", "f2")
        for index in range(2)
    }
    gateway = _ScriptedGateway(script)
    builder = S2DiscoveryPortfolioBuilder(gateway=gateway, max_workers=2)
    portfolio = builder.discover([_facet("f1", 2), _facet("f2", 2)])

    assert all(run["concurrency_workers"] <= 2 for run in portfolio.query_runs)
    assert len(portfolio.adaptive_fanout_history) == 2
    assert max(
        record["level_after"]
        for record in portfolio.adaptive_fanout_history
    ) <= 8
