"""Cross-topic quality benchmark for the S2-first literature pipeline."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from optomind_research.s2_discovery import (
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)
from optomind_research.s2_fulltext_acquisition import (
    S2FulltextAcquirer,
    decide_fulltext_escalation,
)
from optomind_research.s2_literature_graph import S2LiteratureGraphBuilder
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever


@dataclass(slots=True)
class BenchmarkTopic:
    topic_id: str
    query: str
    roles: list[str]
    title_markers: list[str]


@dataclass(slots=True)
class BenchmarkTopicResult:
    topic_id: str
    query: str
    status: str
    candidate_count: int
    retained_count: int
    relevant_title_count: int
    accepted_body_chunks: int
    fulltext_fallback_chunks: int
    graph_nodes: int
    graph_edges: int
    active_graph_nodes: int
    citation_role_counts: dict[str, int] = field(default_factory=dict)
    top_titles: list[str] = field(default_factory=list)
    external_statuses: list[str] = field(default_factory=list)
    quality_notes: list[str] = field(default_factory=list)


DEFAULT_TOPICS = [
    BenchmarkTopic(
        topic_id="daytime_radiative_cooling",
        query="passive daytime radiative cooling photonic materials mechanisms",
        roles=["foundation", "mechanism", "frontier", "review"],
        title_markers=["radiative cooling", "daytime cooling", "thermal emitter"],
    ),
    BenchmarkTopic(
        topic_id="bic_metasurfaces",
        query="bound states in the continuum dielectric metasurfaces optical",
        roles=["foundation", "mechanism", "frontier", "review"],
        title_markers=["bound state", "bic", "metasurface"],
    ),
    BenchmarkTopic(
        topic_id="multilayer_inverse_design",
        query="inverse design optical multilayer thin film",
        roles=["method", "comparison", "frontier", "review"],
        title_markers=["inverse design", "multilayer", "thin film"],
    ),
]


def _title_is_relevant(title: str, markers: list[str]) -> bool:
    lower = title.casefold()
    return any(marker.casefold() in lower for marker in markers)


def _external_statuses(*runs: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(item.get("status_category") or "")
            for group in runs
            for item in group
            if str(item.get("status_category") or "")
        )
    )


def run_s2_quality_benchmark(
    output_dir: str | Path,
    *,
    topics: list[BenchmarkTopic] | None = None,
    results_per_topic: int = 10,
    snippets_per_topic: int = 8,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    discovery = S2DiscoveryPortfolioBuilder()
    retriever = S2TextChunkRetriever()
    graph_builder = S2LiteratureGraphBuilder()
    rows: list[BenchmarkTopicResult] = []
    started = time.monotonic()

    for topic in topics or DEFAULT_TOPICS:
        portfolio = discovery.discover(
            [
                ScholarFacetRequest(
                    facet_id=topic.topic_id,
                    queries=[topic.query],
                    requested_roles=topic.roles,
                    max_results_per_query=results_per_topic,
                )
            ]
        )
        retained = [
            candidate
            for candidate in portfolio.candidates
            if candidate.decision != "reject"
        ]
        top_candidates = retained[:8] or portfolio.candidates[:8]
        paper_ids = [
            candidate.paper.paper_id
            for candidate in top_candidates
            if candidate.paper.paper_id
        ]
        chunks = retriever.retrieve(
            [topic.query],
            paper_ids=paper_ids or None,
            limit_per_query=snippets_per_topic,
            requested_roles=topic.roles,
        )
        if not chunks.accepted_chunks:
            fallback_queries = [topic.query]
            if top_candidates and top_candidates[0].paper.title:
                fallback_queries.append(top_candidates[0].paper.title)
            chunks = retriever.retrieve(
                fallback_queries,
                paper_ids=None,
                limit_per_query=snippets_per_topic,
                requested_roles=topic.roles,
            )
        fulltext_fallback_chunks = 0
        fulltext_fallback_report: dict[str, Any] = {}
        if not chunks.accepted_chunks:
            oa_candidates = [
                candidate.paper
                for candidate in (retained or portfolio.candidates)
                if candidate.paper.is_oa
                and candidate.paper.s2_open_access_candidate_url
                and candidate.paper.doi
            ][:5]
            selections = [
                (
                    paper,
                    decide_fulltext_escalation(
                        paper,
                        role_labels=topic.roles,
                        need_complete_context=True,
                    ),
                )
                for paper in oa_candidates
            ]
            if selections:
                acquisition = S2FulltextAcquirer(
                    kb_sqlite=output / topic.topic_id / "fallback_kb.sqlite",
                    download_dir=output / topic.topic_id / "downloads",
                ).acquire(
                    selections,
                    max_successes=1,
                    source_task_id=f"benchmark_{topic.topic_id}",
                )
                fulltext_fallback_chunks = len(acquisition.new_chunk_ids)
                fulltext_fallback_report = acquisition.to_dict()
        seeds = [candidate.paper for candidate in top_candidates[:2]]
        graph = graph_builder.expand_from_seeds(
            seeds,
            topic_queries=[topic.query],
            reference_limit_per_seed=3,
            citation_limit_per_seed=3,
            recommendation_limit=5,
        ) if seeds else None
        if graph:
            graph_builder.add_snippet_reference_mentions(
                graph, chunks.accepted_chunks
            )
        role_counts: dict[str, int] = {}
        for chunk in chunks.accepted_chunks:
            for role in chunk.citation_roles:
                role_counts[role] = role_counts.get(role, 0) + 1
        titles = [candidate.paper.title for candidate in top_candidates]
        relevant_titles = sum(
            _title_is_relevant(title, topic.title_markers) for title in titles
        )
        graph_nodes = len(graph.nodes) if graph else 0
        active_nodes = (
            sum(
                bool(graph.node_annotations.get(pid, {}).get("active_for_lineage", True))
                for pid in graph.nodes
            )
            if graph
            else 0
        )
        statuses = _external_statuses(
            portfolio.query_runs,
            chunks.query_runs,
            graph.query_runs if graph else [],
        )
        notes: list[str] = []
        if relevant_titles < max(2, len(titles) // 2):
            notes.append("top_title_relevance_is_thin")
        if not chunks.accepted_chunks and not fulltext_fallback_chunks:
            notes.append("no_accepted_body_snippet_or_fulltext_fallback")
        elif not chunks.accepted_chunks:
            notes.append("s2_body_snippet_unavailable_fulltext_fallback_succeeded")
        if graph_nodes and not active_nodes:
            notes.append("graph_has_no_active_topic_nodes")
        environmental = any(
            status in {
                "availability_delay",
                "authentication_failure",
                "request_contract_failure",
            }
            for status in statuses
        )
        quality_ok = (
            len(portfolio.candidates) >= 5
            and relevant_titles >= max(2, len(titles) // 2)
            and (
                len(chunks.accepted_chunks) >= 1
                or fulltext_fallback_chunks >= 1
            )
            and active_nodes >= 1
        )
        status = (
            "passed"
            if quality_ok
            else "degraded_external"
            if environmental
            else "quality_failed"
        )
        row = BenchmarkTopicResult(
            topic_id=topic.topic_id,
            query=topic.query,
            status=status,
            candidate_count=len(portfolio.candidates),
            retained_count=len(retained),
            relevant_title_count=relevant_titles,
            accepted_body_chunks=len(chunks.accepted_chunks),
            fulltext_fallback_chunks=fulltext_fallback_chunks,
            graph_nodes=graph_nodes,
            graph_edges=len(graph.edges) if graph else 0,
            active_graph_nodes=active_nodes,
            citation_role_counts=role_counts,
            top_titles=titles,
            external_statuses=statuses,
            quality_notes=notes,
        )
        rows.append(row)
        (output / f"{topic.topic_id}.json").write_text(
            json.dumps(
                {
                    "result": asdict(row),
                    "portfolio": portfolio.to_dict(),
                    "chunks": chunks.to_dict(),
                    "fulltext_fallback": fulltext_fallback_report,
                    "graph": graph.to_dict() if graph else {},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    passed = sum(row.status == "passed" for row in rows)
    report = {
        "schema_version": "s2_quality_benchmark.v1",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "topics": [asdict(row) for row in rows],
        "summary": {
            "topics_total": len(rows),
            "topics_passed": passed,
            "topics_degraded_external": sum(
                row.status == "degraded_external" for row in rows
            ),
            "topics_quality_failed": sum(
                row.status == "quality_failed" for row in rows
            ),
            "passed": passed == len(rows),
        },
    }
    (output / "S2_QUALITY_BENCHMARK.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
