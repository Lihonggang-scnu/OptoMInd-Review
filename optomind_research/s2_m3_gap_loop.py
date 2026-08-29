"""S2-first recursive evidence expansion for blueprint claims."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.s2_discovery import (
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_literature_graph import (
    LiteratureGraph,
    S2LiteratureGraphBuilder,
)
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever
from optomind_research.runtime.literature_discovery_plan import (
    evaluate_wave_stop,
    relation_coverage_ledger,
)
from optomind_research.runtime.synthesis_bundle import build_bundles_for_blueprint


@dataclass(slots=True)
class S2GapRound:
    claim_id: str
    section_id: str
    query: str
    discovered_papers: int = 0
    accepted_chunks: int = 0
    direct_or_partial_chunks: int = 0
    contextual_chunks: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    new_information_gain: float = 0.0
    stop_reason: str = ""
    query_runs: list[dict[str, Any]] = field(default_factory=list)
    wave_ids: list[str] = field(default_factory=list)
    stop_decision: dict[str, Any] = field(default_factory=dict)


def _iter_claims(blueprint: dict[str, Any]):
    for section in blueprint.get("sections") or []:
        if isinstance(section, dict):
            for claim in section.get("claims") or []:
                if isinstance(claim, dict):
                    yield section, claim


def _claim_query(section: dict[str, Any], claim: dict[str, Any]) -> str:
    return " ".join(
        part.strip()
        for part in (
            str(claim.get("statement") or ""),
            str(section.get("title") or ""),
            str(section.get("argument_role") or ""),
        )
        if part.strip()
    )[:1200]


def _merge_graph(target: LiteratureGraph, source: LiteratureGraph) -> None:
    for paper_id, paper in source.nodes.items():
        target.add_node(paper, source.node_annotations.get(paper_id))
    for edge in source.edges:
        target.add_edge(edge)
    target.excluded_candidates.extend(source.excluded_candidates)
    target.query_runs.extend(source.query_runs)


class S2M3GapLoop:
    """Retrieve useful evidence without requiring exact sentence equivalence."""

    def __init__(
        self,
        *,
        kb_sqlite: str | Path,
        discovery: S2DiscoveryPortfolioBuilder | None = None,
        retriever: S2TextChunkRetriever | None = None,
        graph_builder: S2LiteratureGraphBuilder | None = None,
    ) -> None:
        self.kb_sqlite = Path(kb_sqlite)
        self.discovery = discovery or S2DiscoveryPortfolioBuilder()
        self.retriever = retriever or S2TextChunkRetriever()
        self.graph_builder = graph_builder or S2LiteratureGraphBuilder()
        self.bridge = S2KnowledgeBaseBridge(self.kb_sqlite)

    def run(
        self,
        blueprint: dict[str, Any],
        *,
        max_claims: int = 3,
        max_rounds: int = 2,
        results_per_query: int = 20,
        snippets_per_query: int = 20,
        saturation_threshold: float = 1.5,
    ) -> tuple[dict[str, Any], dict[str, Any], LiteratureGraph]:
        updated = deepcopy(blueprint)
        targets = [
            (section, claim)
            for section, claim in _iter_claims(updated)
            if float(claim.get("saturation_score") or 0.0) < saturation_threshold
            and str(claim.get("status") or "") not in {"dropped", "closed"}
        ][: max(0, int(max_claims))]
        graph = LiteratureGraph()
        reports: list[S2GapRound] = []
        seen_chunks: set[str] = set()
        wave_plans: list[dict[str, Any]] = []

        for section, claim in targets:
            no_gain_rounds = 0
            for round_index in range(1, max(1, int(max_rounds)) + 1):
                query = _claim_query(section, claim)
                requested_roles = list(
                    dict.fromkeys(
                        [
                            str(claim.get("evidence_type") or "mechanism"),
                            "frontier",
                            "review",
                        ]
                    )
                )
                facet = ScholarFacetRequest(
                    facet_id=f"{claim.get('claim_id','claim')}:r{round_index}",
                    queries=[query],
                    requested_roles=requested_roles,
                    max_results_per_query=results_per_query,
                )
                scope_map = dict(updated.get("review_scope_map") or {})
                if hasattr(self.discovery, "discover_multiwave"):
                    portfolio = self.discovery.discover_multiwave(
                        [facet],
                        scope_map=scope_map,
                        minimum_papers=max(5, min(20, results_per_query)),
                        max_waves=7,
                        max_results_per_query=results_per_query,
                        max_snippets_per_query=snippets_per_query,
                    )
                else:
                    portfolio = self.discovery.discover([facet])
                if portfolio.wave_plan:
                    wave_plans.append(portfolio.wave_plan)
                retained = [
                    candidate
                    for candidate in portfolio.candidates
                    if candidate.decision != "reject"
                ]
                seeds = [candidate.paper for candidate in retained[:5]]
                seed_ids = [paper.paper_id for paper in seeds if paper.paper_id]
                controller_chunks = list(
                    getattr(portfolio, "structured_chunks", []) or []
                )
                if controller_chunks:
                    from optomind_research.s2_text_chunk_retriever import (
                        TextChunkRetrievalResult,
                    )
                    snippets = TextChunkRetrievalResult(
                        accepted_chunks=controller_chunks,
                        rejected_items=[],
                        query_runs=list(getattr(portfolio, "query_runs", []) or []),
                        paper_ids=list(
                            dict.fromkeys(chunk.paper_id for chunk in controller_chunks)
                        ),
                    )
                else:
                    snippets = self.retriever.retrieve(
                        [query],
                        paper_ids=seed_ids or None,
                        limit_per_query=snippets_per_query,
                        requested_roles=requested_roles,
                        scope_context={
                            "section_context": " ".join(
                                [
                                    str(updated.get("review_scope_map", {}).get("core_question") or ""),
                                    str(section.get("title") or ""),
                                    str(section.get("chapter_argument") or ""),
                                ]
                            )
                        },
                    )
                if not snippets.accepted_chunks:
                    fallback_queries = [query]
                    if seeds and seeds[0].title:
                        fallback_queries.append(seeds[0].title)
                    snippets = self.retriever.retrieve(
                        fallback_queries,
                        paper_ids=None,
                        limit_per_query=snippets_per_query,
                        requested_roles=requested_roles,
                        scope_context={
                            "section_context": str(section.get("chapter_argument") or "")
                        },
                    )
                # Snippet search can find excellent papers even when the
                # metadata ranker conservatively rejects every broad-search
                # candidate.  Resolve those paper IDs and use them as graph
                # seeds instead of leaving the citation graph empty.
                if snippets.paper_ids and getattr(self.retriever, "gateway", None):
                    resolved, _ = self.retriever.gateway.batch_papers(
                        snippets.paper_ids[:10]
                    )
                    seed_by_id = {paper.paper_id: paper for paper in seeds}
                    for paper in resolved:
                        seed_by_id.setdefault(paper.paper_id, paper)
                    seeds = list(seed_by_id.values())[:10]
                    seed_ids = [
                        paper.paper_id for paper in seeds if paper.paper_id
                    ]
                new_chunks = [
                    chunk
                    for chunk in snippets.accepted_chunks
                    if chunk.chunk_id not in seen_chunks
                ]
                seen_chunks.update(chunk.chunk_id for chunk in new_chunks)
                self.bridge.ingest(papers=seeds, chunks=new_chunks)

                direct_ids = [
                    chunk.chunk_id
                    for chunk in new_chunks
                    if set(chunk.citation_roles)
                    & {"direct_support", "partial_support"}
                ]
                context_ids = [
                    chunk.chunk_id
                    for chunk in new_chunks
                    if chunk.chunk_id not in direct_ids
                ]
                claim["supporting_text_chunk_ids"] = list(
                    dict.fromkeys(
                        list(claim.get("supporting_text_chunk_ids") or [])
                        + direct_ids
                    )
                )
                claim["context_text_chunk_ids"] = list(
                    dict.fromkeys(
                        list(claim.get("context_text_chunk_ids") or [])
                        + context_ids
                    )
                )
                claim["citation_paper_ids"] = list(
                    dict.fromkeys(
                        list(claim.get("citation_paper_ids") or [])
                        + [chunk.paper_id for chunk in new_chunks]
                        + seed_ids
                    )
                )
                role_map = claim.setdefault("citation_role_map", {})
                for chunk in new_chunks:
                    role_map[chunk.chunk_id] = list(chunk.citation_roles)

                partial_graph = getattr(portfolio, "relation_graph", None)
                if partial_graph is None:
                    partial_graph = LiteratureGraph()
                if seeds and not getattr(portfolio, "relation_graph", None):
                    partial_graph = self.graph_builder.expand_from_seeds(
                        seeds[:3],
                        topic_queries=[query],
                        reference_limit_per_seed=5,
                        citation_limit_per_seed=5,
                        recommendation_limit=10,
                    )
                    self.graph_builder.add_snippet_reference_mentions(
                        partial_graph, new_chunks
                    )
                    _merge_graph(graph, partial_graph)
                    self.bridge.ingest_graph(partial_graph)

                wave_execution = list(getattr(portfolio, "wave_execution", []) or [])
                wave_ids = list(
                    dict.fromkeys(
                        str(item.get("wave_id") or "")
                        for item in wave_execution
                        if str(item.get("wave_id") or "")
                    )
                )
                new_relation_count = len(partial_graph.edges)
                gain = len(new_chunks) + 0.25 * len(partial_graph.nodes)
                wave_stop = evaluate_wave_stop(
                    current_wave_index=round_index,
                    max_wave_index=max_rounds,
                    unique_papers=len({candidate.paper.paper_id for candidate in retained}),
                    minimum_papers=max(5, min(20, results_per_query)),
                    covered_roles=[
                        role
                        for candidate in retained
                        for role in candidate.paper.literature_roles
                    ],
                    required_roles=requested_roles,
                    covered_dimensions=[],
                    required_dimensions=[],
                    observed_relation_count=new_relation_count,
                    new_papers=len(retained),
                    new_roles=0,
                    new_dimensions=0,
                    new_relations=new_relation_count,
                    new_information_gain=gain,
                    no_gain_rounds=no_gain_rounds,
                    max_rounds=max_rounds,
                    required_relation_tasks=list(
                        (updated.get("review_scope_map") or {}).get("relation_tasks")
                        or []
                    ),
                )
                row = S2GapRound(
                    claim_id=str(claim.get("claim_id") or ""),
                    section_id=str(section.get("section_id") or ""),
                    query=query,
                    discovered_papers=len(retained),
                    accepted_chunks=len(new_chunks),
                    direct_or_partial_chunks=len(direct_ids),
                    contextual_chunks=len(context_ids),
                    graph_nodes=len(partial_graph.nodes),
                    graph_edges=len(partial_graph.edges),
                    new_information_gain=round(gain, 3),
                    query_runs=portfolio.query_runs + snippets.query_runs,
                    wave_ids=wave_ids,
                    stop_decision=wave_stop,
                )
                if direct_ids:
                    distinct_papers = {
                        chunk.paper_id
                        for chunk in new_chunks
                        if chunk.chunk_id in direct_ids
                    }
                    claim["saturation_score"] = max(
                        float(claim.get("saturation_score") or 0.0),
                        min(2.7, 1.0 + 0.55 * len(distinct_papers)),
                    )
                no_gain_rounds = no_gain_rounds + 1 if gain <= 0 else 0
                if float(claim.get("saturation_score") or 0.0) >= saturation_threshold:
                    row.stop_reason = "claim_has_sufficient_direct_or_partial_support"
                elif wave_stop.get("stop"):
                    row.stop_reason = str(
                        wave_stop.get("reason") or "controller_stop"
                    )
                elif no_gain_rounds >= 2:
                    row.stop_reason = "two_rounds_without_new_information"
                elif round_index >= max_rounds:
                    row.stop_reason = "round_budget_reached_keep_as_open_question"
                reports.append(row)
                if row.stop_reason:
                    break
            claim["s2_gap_status"] = (
                reports[-1].stop_reason if reports else "not_run"
            )

        report = {
            "schema_version": "s2_m3_gap_loop.v1",
            "mode": "s2_first_with_existing_fallback_available",
            "target_claim_count": len(targets),
            "rounds": [asdict(row) for row in reports],
            "discovery_wave_plans": wave_plans,
            "relation_coverage": relation_coverage_ledger(graph),
            "synthesis_bundles": build_bundles_for_blueprint(
                updated,
                relation_edges=graph.edges,
            ),
            "summary": {
                "accepted_chunks": sum(row.accepted_chunks for row in reports),
                "direct_or_partial_chunks": sum(
                    row.direct_or_partial_chunks for row in reports
                ),
                "contextual_chunks": sum(row.contextual_chunks for row in reports),
                "graph_nodes": len(graph.nodes),
                "graph_edges": len(graph.edges),
            },
        }
        return updated, report, graph
