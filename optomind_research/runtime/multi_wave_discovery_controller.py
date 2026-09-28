"""Executable W0-W6 Semantic Scholar discovery controller.

The controller owns orchestration, not scientific truth.  It executes the
existing S2 gateway methods, records per-wave deltas, and keeps observed graph
edges separate from any later semantic interpretation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from optomind_research.runtime.literature_discovery_plan import (
    WAVE_DEFINITIONS,
    build_discovery_wave_plan,
    evaluate_wave_stop,
)
from optomind_research.runtime.review_quality_contract import (
    assess_structured_snippet,
)
from optomind_research.s2_candidate_ranker import S2CandidateRanker
from optomind_research.s2_intelligence_gateway import S2GatewayResponse, S2IntelligenceGateway
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_schemas import LiteratureGraphEdge, S2PaperRecord, UnifiedTextChunk, parse_paper_record
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}", re.IGNORECASE)
_STOP = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "using",
        "into", "study", "based", "review", "perspective", "paper",
    }
)
_ROLE_MARKERS: dict[str, tuple[str, ...]] = {
    "foundation": ("principle", "theory", "fundamental", "origin", "first", "early"),
    "definition": ("definition", "concept", "framework", "taxonomy"),
    "mechanism": ("mechanism", "physics", "model", "resonance", "scattering", "coupling"),
    "method": ("method", "design", "fabrication", "optimization", "simulation", "measurement"),
    "validation": ("experiment", "experimental", "validation", "characterization", "demonstration"),
    "comparison": ("comparison", "benchmark", "versus", "performance", "tradeoff"),
    "review": ("review", "perspective", "roadmap", "survey", "overview"),
    "controversy": ("controversy", "debate", "discrepancy", "contradiction", "limitation"),
    "boundary": ("limit", "limitation", "boundary", "failure", "uncertainty"),
    "frontier": ("recent", "emerging", "advanced", "programmable", "inverse", "dynamic"),
    "application": ("application", "device", "sensor", "imaging", "communication", "energy"),
}

_SEMANTIC_TO_TASK = {
    "foundation_of": "progression",
    "extends": "progression",
    "progression": "progression",
    "complements": "complementarity",
    "complementarity": "complementarity",
    "contradicts": "controversy",
    "controversy": "controversy",
    "compares_with": "tradeoff",
    "tradeoff": "tradeoff",
    "sets_boundary_for": "boundary",
    "boundary": "boundary",
}


def _tokens(text: Any) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(str(text or ""))
        if token.casefold() not in _STOP
    }


def _edge_id(source: str, target: str, relation: str) -> str:
    digest = hashlib.sha1(f"{source}|{target}|{relation}".encode("utf-8")).hexdigest()[:16]
    return f"controller:{relation}:{digest}"


def _response_record(response: S2GatewayResponse, *, channel: str, count: int) -> dict[str, Any]:
    return {
        "channel": channel,
        "status_code": response.status_code,
        "status_category": response.status_category,
        "ok": response.ok,
        "cache_hit": response.cache_hit,
        "result_count": count,
        "wait_seconds": response.wait_seconds,
        "retry_count": response.retry_count,
        "key_slot": response.key_slot,
    }


def _paper_key(paper: S2PaperRecord) -> str:
    return (
        paper.paper_id
        or paper.doi.casefold()
        or re.sub(r"[^a-z0-9]+", " ", paper.title.casefold()).strip()
    )


def _assign_roles(
    paper: S2PaperRecord,
    *,
    requested_roles: Iterable[str],
    wave_id: str,
) -> list[str]:
    text = " ".join((paper.title, paper.abstract, paper.tldr, paper.venue)).casefold()
    requested = [str(item).casefold() for item in requested_roles if str(item).strip()]
    roles: list[str] = []
    publication_types = " ".join(paper.publication_types).casefold()
    for role, markers in _ROLE_MARKERS.items():
        if role not in requested and requested:
            continue
        if role == "review" and (
            "review" in publication_types or any(marker in paper.title.casefold() for marker in markers)
        ):
            roles.append(role)
        elif any(marker in text for marker in markers):
            roles.append(role)
    if wave_id == "W2_backward" and "foundation" in requested and "foundation" not in roles:
        roles.append("foundation")
    if wave_id == "W3_forward" and "frontier" in requested and "frontier" not in roles:
        roles.append("frontier")
    if wave_id in {"W4_recommendations", "W1_facets"} and not roles:
        roles.append("adjacent_candidate")
    return list(dict.fromkeys(roles))


@dataclass(slots=True)
class WaveExecutionRecord:
    wave_id: str
    enabled: bool
    channels: list[str] = field(default_factory=list)
    query_count: int = 0
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    new_paper_ids: list[str] = field(default_factory=list)
    new_chunk_ids: list[str] = field(default_factory=list)
    roles_added: list[str] = field(default_factory=list)
    dimensions_added: list[str] = field(default_factory=list)
    observed_relation_edge_ids: list[str] = field(default_factory=list)
    new_information_gain: float = 0.0
    stop_decision: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MultiWaveDiscoveryResult:
    graph: LiteratureGraph
    candidates: list[S2PaperRecord] = field(default_factory=list)
    chunks: list[UnifiedTextChunk] = field(default_factory=list)
    wave_records: list[WaveExecutionRecord] = field(default_factory=list)
    wave_plan: dict[str, Any] = field(default_factory=dict)
    stop_decision: dict[str, Any] = field(default_factory=dict)
    query_runs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "research_harness.multi_wave_discovery.v1",
            "graph": self.graph.to_dict(),
            "candidates": [paper.to_dict() for paper in self.candidates],
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "wave_plan": self.wave_plan,
            "wave_records": [asdict(record) for record in self.wave_records],
            "stop_decision": self.stop_decision,
            "query_runs": self.query_runs,
        }


class MultiWaveDiscoveryController:
    """Execute the planned discovery waves with deterministic audit metrics."""

    def __init__(
        self,
        *,
        gateway: S2IntelligenceGateway | None = None,
        retriever: S2TextChunkRetriever | None = None,
        ranker: S2CandidateRanker | None = None,
    ) -> None:
        self.gateway = gateway or S2IntelligenceGateway()
        self.retriever = retriever or S2TextChunkRetriever(self.gateway)
        self.ranker = ranker or S2CandidateRanker()

    @staticmethod
    def _query_list(
        facets: Iterable[Any],
        scope_map: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        queries: list[str] = []
        roles: list[str] = []
        for facet in facets:
            queries.extend(list(getattr(facet, "queries", []) or []))
            roles.extend(list(getattr(facet, "requested_roles", []) or []))
        queries.extend(scope_map.get("search_anchors") or [])
        queries.extend(
            [
                scope_map.get("core_question", ""),
                scope_map.get("problem_understanding", ""),
            ]
        )
        roles.extend(scope_map.get("required_literature_roles") or [])
        roles.extend(["foundation", "mechanism", "method", "frontier"])
        return (
            list(dict.fromkeys(str(item).strip()[:1200] for item in queries if str(item).strip())),
            list(dict.fromkeys(str(item).strip() for item in roles if str(item).strip())),
        )

    @staticmethod
    def _dimension_for_paper(
        paper: S2PaperRecord,
        dimensions: list[dict[str, Any]],
    ) -> list[str]:
        paper_tokens = _tokens(" ".join((paper.title, paper.abstract, paper.tldr)))
        result: list[str] = []
        for dimension in dimensions:
            dimension_id = str(dimension.get("dimension_id") or "").strip()
            dimension_tokens = _tokens(
                " ".join(
                    (
                        str(dimension.get("title") or ""),
                        str(dimension.get("argument_task") or ""),
                    )
                )
            )
            if dimension_id and dimension_tokens & paper_tokens:
                result.append(dimension_id)
        return result

    @staticmethod
    def _add_paper(
        graph: LiteratureGraph,
        paper: S2PaperRecord,
        *,
        channel: str,
        requested_roles: list[str],
        wave_id: str,
        dimensions: list[dict[str, Any]],
    ) -> tuple[bool, list[str], list[str]]:
        key = _paper_key(paper)
        existing_keys = {_paper_key(item) for item in graph.nodes.values()}
        is_new = bool(key) and key not in existing_keys
        roles = _assign_roles(paper, requested_roles=requested_roles, wave_id=wave_id)
        paper.discovery_route = {
            "W0_direct": "s2_search",
            "W1_facets": "s2_search",
            "W2_backward": "s2_reference",
            "W3_forward": "s2_citation",
            "W4_recommendations": "s2_recommendation",
            "W5_boundary": "s2_snippet_search",
            "W6_review_frontier": "s2_review_frontier_search",
        }.get(wave_id, "semantic_scholar_graph")
        paper.literature_roles = list(dict.fromkeys(list(paper.literature_roles) + roles))
        graph.add_node(
            paper,
            {
                "source_channel": channel,
                "wave_id": wave_id,
                "literature_roles": roles,
                "dimension_ids": MultiWaveDiscoveryController._dimension_for_paper(
                    paper, dimensions
                ),
            },
        )
        return is_new, roles, MultiWaveDiscoveryController._dimension_for_paper(paper, dimensions)

    @staticmethod
    def _add_observed_edge(
        graph: LiteratureGraph,
        *,
        source_id: str,
        target_id: str,
        relation: str,
        origin: str,
        context: str = "",
        source_chunk_id: str = "",
    ) -> bool:
        if not source_id or not target_id or source_id == target_id:
            return False
        before = len(graph.edges)
        graph.add_edge(
            LiteratureGraphEdge(
                edge_id=_edge_id(source_id, target_id, relation),
                source_paper_id=source_id,
                target_paper_id=target_id,
                edge_type=relation,  # observed relation only
                edge_origin=origin,
                context=context[:4000],
                source_chunk_id=source_chunk_id,
                observed_relation=relation,
                semantic_relation="",
                relation_basis_chunk_ids=[],
                status="observed",
            )
        )
        return len(graph.edges) > before

    def _search(
        self,
        record: WaveExecutionRecord,
        graph: LiteratureGraph,
        *,
        query: str,
        wave_id: str,
        limit: int,
        requested_roles: list[str],
        dimensions: list[dict[str, Any]],
    ) -> list[S2PaperRecord]:
        papers, response = self.gateway.search_papers(query, limit=limit)
        record.api_calls.append(_response_record(response, channel="s2_graph_search", count=len(papers)))
        for paper in papers:
            is_new, roles, dimension_ids = self._add_paper(
                graph,
                paper,
                channel="s2_graph_search",
                requested_roles=requested_roles,
                wave_id=wave_id,
                dimensions=dimensions,
            )
            if is_new:
                record.new_paper_ids.append(paper.paper_id)
            record.roles_added.extend(roles)
            record.dimensions_added.extend(dimension_ids)
        return papers

    def _snippet(
        self,
        record: WaveExecutionRecord,
        *,
        query: str,
        paper_ids: list[str],
        section_context: str,
        requested_roles: list[str],
        limit: int,
    ) -> list[UnifiedTextChunk]:
        result = self.retriever.retrieve(
            [query],
            paper_ids=paper_ids or None,
            limit_per_query=limit,
            requested_roles=requested_roles,
            scope_context={"section_context": section_context},
        )
        record.api_calls.extend(
            {
                "channel": "s2_snippet_search",
                "status_code": item.get("status_code", 0),
                "status_category": item.get("status_category", ""),
                "cache_hit": item.get("cache_hit", False),
                "result_count": item.get("result_count", 0),
                "wait_seconds": item.get("wait_seconds", 0.0),
            }
            for item in result.query_runs
        )
        record.new_chunk_ids.extend(chunk.chunk_id for chunk in result.accepted_chunks)
        return result.accepted_chunks

    def run(
        self,
        *,
        facets: Iterable[Any] = (),
        scope_map: dict[str, Any] | None = None,
        seed_papers: Iterable[S2PaperRecord] = (),
        max_waves: int = 7,
        minimum_papers: int = 10,
        max_results_per_query: int = 20,
        max_snippets_per_query: int = 10,
        required_roles: Iterable[str] = (),
    ) -> MultiWaveDiscoveryResult:
        scope = scope_map if isinstance(scope_map, dict) else {}
        facets = list(facets)
        queries, facet_roles = self._query_list(facets, scope)
        requested_roles = list(
            dict.fromkeys(
                list(required_roles)
                + facet_roles
                + list(scope.get("required_literature_roles") or [])
            )
        )
        dimensions = [
            item
            for item in scope.get("research_dimensions") or []
            if isinstance(item, dict)
        ]
        plan = build_discovery_wave_plan(
            user_question=scope.get("core_question") or scope.get("user_question", ""),
            section_title=str(scope.get("section_title") or ""),
            section_argument=scope.get("central_judgment") or "",
            requested_roles=requested_roles,
            seed_paper_ids=[paper.paper_id for paper in seed_papers if paper.paper_id],
            base_queries=queries,
            enable_expensive_waves=True,
        )
        graph = LiteratureGraph()
        all_chunks: list[UnifiedTextChunk] = []
        all_papers: dict[str, S2PaperRecord] = {}
        for paper in seed_papers:
            if paper.paper_id:
                graph.add_node(paper, {"source_channel": "seed", "wave_id": "seed"})
                all_papers[_paper_key(paper)] = paper
        wave_records: list[WaveExecutionRecord] = []
        covered_roles: set[str] = set()
        covered_dimensions: set[str] = set()
        new_papers_total = 0
        no_gain_waves = 0
        max_wave_index = min(max(0, int(max_waves)), len(WAVE_DEFINITIONS))
        for index, definition in enumerate(WAVE_DEFINITIONS[:max_wave_index], start=1):
            wave_id = str(definition["wave_id"])
            wave = next(item for item in plan["waves"] if item["wave_id"] == wave_id)
            record = WaveExecutionRecord(
                wave_id=wave_id,
                enabled=bool(wave.get("enabled", True)),
                channels=list(wave.get("channels") or []),
            )
            if not record.enabled:
                record.stop_decision = {"stop": False, "reason": "wave_disabled"}
                wave_records.append(record)
                continue
            wave_queries = list(wave.get("query_templates") or queries[:2])[:4]
            record.query_count = len(wave_queries)
            wave_new_before = len(all_papers)
            if wave_id in {"W0_direct", "W1_facets", "W6_review_frontier"}:
                for query in wave_queries:
                    found = self._search(
                        record,
                        graph,
                        query=query,
                        wave_id=wave_id,
                        limit=max_results_per_query,
                        requested_roles=requested_roles,
                        dimensions=dimensions,
                    )
                    for paper in found:
                        all_papers[_paper_key(paper)] = paper
                if wave_id == "W0_direct" and wave_queries:
                    all_chunks.extend(
                        self._snippet(
                            record,
                            query=wave_queries[0],
                            paper_ids=list(graph.nodes)[:100],
                            section_context=str(scope.get("section_context") or scope.get("core_question") or ""),
                            requested_roles=requested_roles,
                            limit=max_snippets_per_query,
                        )
                    )
            elif wave_id == "W5_boundary":
                # Boundary work is a real snippet-search wave, not a label on
                # the static plan.  Keep its role vocabulary explicit so that
                # a limitation/controversy hit is not silently treated as a
                # generic direct hit.
                boundary_roles = list(
                    dict.fromkeys(
                        requested_roles + ["controversy", "boundary", "comparison"]
                    )
                )
                for query in wave_queries[:2]:
                    all_chunks.extend(
                        self._snippet(
                            record,
                            query=query,
                            paper_ids=list(graph.nodes)[:100],
                            section_context=str(
                                scope.get("section_context")
                                or scope.get("core_question")
                                or ""
                            ),
                            requested_roles=boundary_roles,
                            limit=max_snippets_per_query,
                        )
                    )
            elif wave_id in {"W2_backward", "W3_forward"}:
                relation = "references" if wave_id == "W2_backward" else "citations"
                edge_type = "cites" if wave_id == "W2_backward" else "cited_by"
                seeds = list(graph.nodes.values())[:3]
                for seed in seeds:
                    items, response = getattr(self.gateway, relation)(
                        seed.paper_id, limit=max_results_per_query
                    )
                    record.api_calls.append(
                        _response_record(response, channel=f"s2_{relation}", count=len(items))
                    )
                    for item in items:
                        payload_key = "citedPaper" if relation == "references" else "citingPaper"
                        payload = item.get(payload_key) if isinstance(item, dict) else None
                        paper = parse_paper_record(payload) if isinstance(payload, dict) else None
                        if not paper or not paper.paper_id:
                            continue
                        is_new, roles, dimension_ids = self._add_paper(
                            graph,
                            paper,
                            channel=f"s2_{relation}",
                            requested_roles=requested_roles,
                            wave_id=wave_id,
                            dimensions=dimensions,
                        )
                        all_papers[_paper_key(paper)] = paper
                        if is_new:
                            record.new_paper_ids.append(paper.paper_id)
                        record.roles_added.extend(roles)
                        record.dimensions_added.extend(dimension_ids)
                        if self._add_observed_edge(
                            graph,
                            source_id=seed.paper_id,
                            target_id=paper.paper_id,
                            relation=edge_type,
                            origin="s2_api",
                            context=" ".join(item.get("contexts") or []) if isinstance(item, dict) else "",
                        ):
                            record.observed_relation_edge_ids.append(
                                _edge_id(seed.paper_id, paper.paper_id, edge_type)
                            )
            elif wave_id == "W4_recommendations":
                seeds = list(graph.nodes)[:5]
                recommendations, response = self.gateway.recommendations_from_seeds(
                    seeds, limit=max_results_per_query
                )
                record.api_calls.append(
                    _response_record(response, channel="s2_recommendations", count=len(recommendations))
                )
                for paper in recommendations:
                    is_new, roles, dimension_ids = self._add_paper(
                        graph,
                        paper,
                        channel="s2_recommendations",
                        requested_roles=requested_roles,
                        wave_id=wave_id,
                        dimensions=dimensions,
                    )
                    all_papers[_paper_key(paper)] = paper
                    if is_new:
                        record.new_paper_ids.append(paper.paper_id)
                    record.roles_added.extend(roles)
                    record.dimensions_added.extend(dimension_ids)
                    if seeds and self._add_observed_edge(
                        graph,
                        source_id=seeds[0],
                        target_id=paper.paper_id,
                        relation="semantic_recommendation",
                        origin="s2_recommendations",
                    ):
                        record.observed_relation_edge_ids.append(
                            _edge_id(seeds[0], paper.paper_id, "semantic_recommendation")
                        )
            record.new_paper_ids = list(dict.fromkeys(record.new_paper_ids))
            record.new_chunk_ids = list(dict.fromkeys(record.new_chunk_ids))
            record.roles_added = list(dict.fromkeys(record.roles_added))
            record.dimensions_added = list(dict.fromkeys(record.dimensions_added))
            record.observed_relation_edge_ids = list(dict.fromkeys(record.observed_relation_edge_ids))
            covered_roles.update(record.roles_added)
            covered_dimensions.update(record.dimensions_added)
            new_papers_total += len(record.new_paper_ids)
            gain = (
                len(record.new_paper_ids)
                + 0.35 * len(record.new_chunk_ids)
                + 0.5 * len(record.roles_added)
                + 0.5 * len(record.dimensions_added)
                + 0.4 * len(record.observed_relation_edge_ids)
            )
            record.new_information_gain = round(gain, 3)
            if gain <= 0 and len(record.observed_relation_edge_ids) == 0:
                no_gain_waves += 1
            else:
                no_gain_waves = 0
            required = list(dict.fromkeys(str(item) for item in requested_roles if str(item)))
            required_dimensions = [str(item.get("dimension_id")) for item in dimensions if item.get("dimension_id")]
            required_relations = list(scope.get("relation_tasks") or [])
            satisfied_relations = sorted(
                {
                    _SEMANTIC_TO_TASK.get(str(edge.semantic_relation or ""))
                    for edge in graph.edges
                    if str(edge.semantic_relation or "") in _SEMANTIC_TO_TASK
                }
                - {None}
            )
            record.stop_decision = evaluate_wave_stop(
                current_wave_index=index,
                max_wave_index=max_wave_index,
                unique_papers=len(all_papers),
                minimum_papers=minimum_papers,
                covered_roles=covered_roles,
                required_roles=required,
                covered_dimensions=covered_dimensions,
                required_dimensions=required_dimensions,
                observed_relation_count=len(graph.edges),
                new_papers=len(record.new_paper_ids),
                new_roles=len(record.roles_added),
                new_dimensions=len(record.dimensions_added),
                new_relations=len(record.observed_relation_edge_ids),
                new_information_gain=gain,
                no_gain_rounds=no_gain_waves,
                max_rounds=max_wave_index,
                required_relation_tasks=required_relations,
                satisfied_relation_tasks=satisfied_relations,
            )
            wave_records.append(record)
            if record.stop_decision.get("stop"):
                break
        stop = wave_records[-1].stop_decision if wave_records else {
            "stop": False,
            "reason": "no_wave_executed",
        }
        return MultiWaveDiscoveryResult(
            graph=graph,
            candidates=list(all_papers.values()),
            chunks=all_chunks,
            wave_records=wave_records,
            wave_plan=plan,
            stop_decision=stop,
            query_runs=[
                call
                for record in wave_records
                for call in record.api_calls
            ],
        )
