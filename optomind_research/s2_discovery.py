"""S2-first discovery portfolio for topics, scholar facets and section roles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

from optomind_research.s2_candidate_ranker import (
    S2Candidate,
    S2CandidateRanker,
)
from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
from optomind_research.runtime.literature_discovery_plan import (
    build_discovery_wave_plan,
)
from optomind_research.runtime.multi_wave_discovery_controller import (
    MultiWaveDiscoveryController,
)


S2_ADAPTIVE_FANOUT_LEVELS = (2, 4, 6, 8)
S2_ADAPTIVE_FANOUT_DEFAULT_LEVEL = 4
S2_ADAPTIVE_FANOUT_MIN_LEVEL = 2
S2_ADAPTIVE_FANOUT_MAX_LEVEL = 8
S2_ADAPTIVE_FANOUT_ENV = "OPTOMIND_S2_ADAPTIVE_DISCOVERY"


def _adaptive_enabled_from_env() -> bool:
    raw = str(os.environ.get(S2_ADAPTIVE_FANOUT_ENV, "") or "").strip()
    return raw.casefold() in {"1", "true", "yes", "on"}


def _is_rate_limited_response(response: Any) -> bool:
    """Detect a provider 429/rate-limit signal without exposing key data."""

    status = int(getattr(response, "status_code", 0) or 0)
    category = str(
        getattr(response, "status_category", "") or ""
    ).casefold()
    return (
        status == 429
        or "429" in category
        or "rate_limit" in category
    )


def _is_transport_failure_response(response: Any) -> bool:
    """Detect elevated transport/provider failure that is not a 429."""

    if _is_rate_limited_response(response):
        return False
    status = int(getattr(response, "status_code", 0) or 0)
    category = str(
        getattr(response, "status_category", "") or ""
    ).casefold()
    return category in {"availability_delay", "authentication_failure"} or status >= 500


@dataclass(slots=True)
class S2BatchFanoutHealth:
    """Non-secret health summary of one completed discovery batch."""

    request_count: int = 0
    healthy_count: int = 0
    rate_limit_count: int = 0
    transport_failure_count: int = 0
    contract_failure_count: int = 0

    @classmethod
    def from_responses(
        cls, responses: Iterable[Any]
    ) -> "S2BatchFanoutHealth":
        health = cls()
        for response in responses:
            health.request_count += 1
            category = str(
                getattr(response, "status_category", "") or ""
            ).casefold()
            if getattr(response, "cache_hit", False) or category in {
                "ok",
                "cache_hit",
            }:
                health.healthy_count += 1
            elif _is_rate_limited_response(response):
                health.rate_limit_count += 1
            elif _is_transport_failure_response(response):
                health.transport_failure_count += 1
            elif category == "request_contract_failure":
                health.contract_failure_count += 1
        return health

    @property
    def has_rate_limit(self) -> bool:
        return self.rate_limit_count > 0

    @property
    def has_transport_failure(self) -> bool:
        return self.transport_failure_count > 0

    @property
    def is_clean(self) -> bool:
        return (
            self.request_count > 0
            and self.contract_failure_count == 0
            and not self.has_rate_limit
            and not self.has_transport_failure
        )


class S2WorkerFanoutPolicy:
    """Deterministic, bounded, observable adaptive S2 discovery fanout.

    The ladder starts at 4, may promote one step per clean completed batch to
    at most 6 and then 8, and rolls back one step on any 429/rate-limit
    signal with a cooldown.  Transport/provider and request-contract
    failures hold the level and prevent promotion.  The audit history
    contains only counts and levels; key material and request bodies are
    never recorded.
    """

    def __init__(
        self,
        *,
        initial_level: int = S2_ADAPTIVE_FANOUT_DEFAULT_LEVEL,
        min_level: int = S2_ADAPTIVE_FANOUT_MIN_LEVEL,
        max_level: int = S2_ADAPTIVE_FANOUT_MAX_LEVEL,
        cooldown_batches: int = 1,
    ) -> None:
        self.min_level = max(
            S2_ADAPTIVE_FANOUT_MIN_LEVEL,
            min(int(min_level), S2_ADAPTIVE_FANOUT_MAX_LEVEL),
        )
        self.max_level = max(
            self.min_level,
            min(int(max_level), S2_ADAPTIVE_FANOUT_MAX_LEVEL),
        )
        self.level = self._snap_to_ladder(int(initial_level))
        self.cooldown_batches = max(0, int(cooldown_batches))
        self.cooldown_remaining = 0
        self.history: list[dict[str, Any]] = []

    def _ladder(self) -> list[int]:
        return [
            step
            for step in S2_ADAPTIVE_FANOUT_LEVELS
            if self.min_level <= step <= self.max_level
        ]

    def _snap_to_ladder(self, level: int) -> int:
        snapped = self.min_level
        for step in self._ladder():
            if step <= max(level, self.min_level):
                snapped = step
        return snapped

    def _previous_level(self) -> int:
        return max(
            (
                step
                for step in self._ladder()
                if step < self.level
            ),
            default=self.min_level,
        )

    def _next_level(self) -> int:
        return min(
            (
                step
                for step in self._ladder()
                if step > self.level
            ),
            default=self.level,
        )

    def current_level(self) -> int:
        return self.level

    def observe_batch(
        self, health: S2BatchFanoutHealth
    ) -> dict[str, Any]:
        before = self.level
        decision = "hold"
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            if health.has_rate_limit:
                self.cooldown_remaining = self.cooldown_batches
        elif health.has_rate_limit:
            self.level = self._previous_level()
            self.cooldown_remaining = self.cooldown_batches
            decision = "rollback"
        elif health.is_clean:
            promoted = self._next_level()
            if promoted > self.level:
                self.level = promoted
                decision = "promote"
        record = {
            "level_before": before,
            "level_after": self.level,
            "decision": decision,
            "cooldown_batches_remaining": self.cooldown_remaining,
            "request_count": int(health.request_count),
            "healthy_count": int(health.healthy_count),
            "rate_limit_count": int(health.rate_limit_count),
            "transport_failure_count": int(health.transport_failure_count),
            "contract_failure_count": int(health.contract_failure_count),
        }
        self.history.append(record)
        return record


@dataclass(slots=True)
class ScholarFacetRequest:
    facet_id: str
    queries: list[str]
    requested_roles: list[str] = field(default_factory=list)
    max_results_per_query: int = 20
    # Generic policy/plan-driven flag: when True, discovery executes only the
    # explicit facet queries (W0 direct) and never expands review/foundation
    # query strings from requested_roles.
    direct_only: bool = False


@dataclass(slots=True)
class DiscoveryPortfolio:
    candidates: list[S2Candidate]
    query_runs: list[dict[str, Any]]
    pool_counts: dict[str, int]
    rejected_count: int = 0
    wave_plan: dict[str, Any] = field(default_factory=dict)
    wave_execution: list[dict[str, Any]] = field(default_factory=list)
    relation_graph: Any | None = None
    structured_chunks: list[Any] = field(default_factory=list)
    adaptive_fanout_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "query_runs": self.query_runs,
            "pool_counts": self.pool_counts,
            "rejected_count": self.rejected_count,
            "wave_plan": self.wave_plan,
            "wave_execution": self.wave_execution,
            "relation_graph": (
                self.relation_graph.to_dict()
                if hasattr(self.relation_graph, "to_dict")
                else self.relation_graph
            ),
            "structured_chunks": [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in self.structured_chunks
            ],
            "adaptive_fanout_history": self.adaptive_fanout_history,
        }


class S2DiscoveryPortfolioBuilder:
    def __init__(
        self,
        gateway: S2IntelligenceGateway | None = None,
        ranker: S2CandidateRanker | None = None,
        *,
        max_workers: int | None = None,
        adaptive_fanout: bool | None = None,
    ) -> None:
        self.gateway = gateway or S2IntelligenceGateway()
        self.ranker = ranker or S2CandidateRanker()
        configured = max_workers
        if configured is None:
            configured = os.environ.get("OPTOMIND_S2_DISCOVERY_WORKERS", "4")
        try:
            configured_int = int(configured)
        except (TypeError, ValueError):
            configured_int = 4
        self.max_workers = max(1, min(configured_int, 16))
        # Adaptive fanout is deliberately opt-in: fixed discovery batches
        # keep their historical default and semantics unless the caller or
        # the environment explicitly enables the bounded policy.
        self.adaptive_enabled = (
            bool(adaptive_fanout)
            if adaptive_fanout is not None
            else _adaptive_enabled_from_env()
        )
        self._adaptive_policy = S2WorkerFanoutPolicy()

    def discover(
        self,
        facets: list[ScholarFacetRequest],
        *,
        oa_only: bool = False,
        seed_vector: list[float] | None = None,
        direct_only: bool | None = None,
    ) -> DiscoveryPortfolio:
        collected: list[S2Candidate] = []
        runs: list[dict[str, Any]] = []
        global_direct_only = bool(direct_only) if direct_only is not None else None
        all_direct_only = (
            bool(direct_only)
            if direct_only is not None
            else all(bool(facet.direct_only) for facet in facets)
        )
        wave_plan = {} if all_direct_only else build_discovery_wave_plan(
            base_queries=[query for facet in facets for query in facet.queries],
            requested_roles=[
                role for facet in facets for role in facet.requested_roles
            ],
            enable_expensive_waves=True,
        )
        channel_to_wave = {
            "s2_relevance_search": "W0_direct",
            "s2_review_search": "W6_review_frontier",
            "s2_foundation_search": "W2_backward",
        }
        adaptive_history: list[dict[str, Any]] = []
        for facet in facets:
            query_specs: list[tuple[str, str]] = []
            facet_direct_only = (
                global_direct_only
                if global_direct_only is not None
                else bool(facet.direct_only)
            )
            for query in list(dict.fromkeys(facet.queries)):
                query_specs.append((query, "s2_relevance_search"))
                roles = {role.casefold() for role in facet.requested_roles}
                if "review" in roles and not facet_direct_only:
                    query_specs.append(
                        (f"{query} review perspective roadmap", "s2_review_search")
                    )
                if "foundation" in roles and not facet_direct_only:
                    query_specs.append(
                        (f"{query} fundamental theory origin", "s2_foundation_search")
                    )
            unique_specs = list(dict.fromkeys(query_specs))

            def execute(spec: tuple[str, str]) -> tuple[list[Any], Any]:
                query, _channel = spec
                return self.gateway.search_papers(
                    query,
                    limit=facet.max_results_per_query,
                    open_access_pdf=oa_only,
                )

            # Discovery queries are independent network requests.  Execute
            # them concurrently, but consume results in the original order so
            # ranking, tie-breaking, and audit files remain deterministic.
            worker_count = min(self.max_workers, max(1, len(unique_specs)))
            if self.adaptive_enabled:
                worker_count = min(
                    worker_count,
                    self._adaptive_policy.current_level(),
                )
            if worker_count == 1:
                responses = [execute(spec) for spec in unique_specs]
            else:
                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="s2-discovery",
                ) as pool:
                    responses = list(pool.map(execute, unique_specs))
            policy_audit: dict[str, Any] | None = None
            if self.adaptive_enabled:
                health = S2BatchFanoutHealth.from_responses(
                    response for _, response in responses
                )
                policy_audit = self._adaptive_policy.observe_batch(health)
                adaptive_history.append(
                    {
                        "facet_id": facet.facet_id,
                        **dict(policy_audit),
                    }
                )
            for request_index, ((query, channel), (papers, response)) in enumerate(
                zip(unique_specs, responses)
            ):
                run = {
                    "facet_id": facet.facet_id,
                    "query": query,
                    "status_category": response.status_category,
                    "status_code": response.status_code,
                    "cache_hit": response.cache_hit,
                    "result_count": len(papers),
                    "wait_seconds": response.wait_seconds,
                    "wave_id": channel_to_wave.get(channel, "W1_facets"),
                    "request_index": request_index,
                    "concurrency_workers": worker_count,
                }
                if policy_audit is not None:
                    run["adaptive_policy_decision"] = policy_audit["decision"]
                    run["adaptive_level_after"] = policy_audit["level_after"]
                runs.append(run)
                for paper in papers:
                    collected.append(
                        self.ranker.build_candidate(
                            paper,
                            facet_id=facet.facet_id,
                            queries=facet.queries,
                            requested_roles=facet.requested_roles,
                            discovery_channel=channel,
                            seed_vector=seed_vector,
                        )
                    )
        merged = self.ranker.merge_candidates(collected)
        ordered = self.ranker.portfolio_sort(merged)
        pool_counts: dict[str, int] = {}
        for candidate in ordered:
            for pool in candidate.assigned_pools:
                pool_counts[pool] = pool_counts.get(pool, 0) + 1
        return DiscoveryPortfolio(
            candidates=ordered,
            query_runs=runs,
            pool_counts=dict(sorted(pool_counts.items())),
            rejected_count=sum(1 for item in ordered if item.decision == "reject"),
            wave_plan=wave_plan,
            adaptive_fanout_history=adaptive_history,
        )

    def discover_multiwave(
        self,
        facets: list[ScholarFacetRequest],
        *,
        scope_map: dict[str, Any] | None = None,
        minimum_papers: int = 10,
        max_waves: int = 7,
        max_results_per_query: int | None = None,
        max_snippets_per_query: int = 10,
    ) -> DiscoveryPortfolio:
        """Execute the real W0-W6 controller and preserve the old portfolio API."""

        controller = MultiWaveDiscoveryController(
            gateway=self.gateway,
            ranker=self.ranker,
        )
        result = controller.run(
            facets=facets,
            scope_map=scope_map or {},
            max_waves=max_waves,
            minimum_papers=minimum_papers,
            max_results_per_query=max_results_per_query
            or max(
                [int(facet.max_results_per_query) for facet in facets] or [20]
            ),
            max_snippets_per_query=max_snippets_per_query,
            required_roles=[
                role
                for facet in facets
                for role in facet.requested_roles
            ],
        )
        all_queries = [
            query
            for facet in facets
            for query in facet.queries
        ]
        all_roles = [
            role
            for facet in facets
            for role in facet.requested_roles
        ]
        facet_id = facets[0].facet_id if facets else "multiwave"
        candidates: list[S2Candidate] = []
        for paper in result.candidates:
            annotations = result.graph.node_annotations.get(paper.paper_id, {})
            channel = str(annotations.get("source_channel") or "s2_multiwave")
            candidates.append(
                self.ranker.build_candidate(
                    paper,
                    facet_id=facet_id,
                    queries=all_queries,
                    requested_roles=all_roles,
                    discovery_channel=channel,
                )
            )
        merged = self.ranker.portfolio_sort(
            self.ranker.merge_candidates(candidates)
        )
        pool_counts: dict[str, int] = {}
        for candidate in merged:
            for pool in candidate.assigned_pools:
                pool_counts[pool] = pool_counts.get(pool, 0) + 1
        return DiscoveryPortfolio(
            candidates=merged,
            query_runs=result.query_runs,
            pool_counts=dict(sorted(pool_counts.items())),
            rejected_count=sum(
                1 for candidate in merged if candidate.decision == "reject"
            ),
            wave_plan=result.wave_plan,
            wave_execution=[asdict(record) for record in result.wave_records],
            relation_graph=result.graph,
            structured_chunks=list(result.chunks),
        )
