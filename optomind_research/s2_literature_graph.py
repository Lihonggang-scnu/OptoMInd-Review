"""Typed literature relationship graph built from S2 citations and recommendations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from optomind_research.s2_candidate_ranker import S2CandidateRanker
from optomind_research.s2_intelligence_gateway import (
    S2GatewayResponse,
    S2IntelligenceGateway,
)
from optomind_research.s2_schemas import (
    LiteratureGraphEdge,
    S2PaperRecord,
    UnifiedTextChunk,
    parse_paper_record,
)


def _edge_id(source: str, target: str, edge_type: str) -> str:
    digest = hashlib.sha1(f"{source}|{target}|{edge_type}".encode("utf-8")).hexdigest()[
        :16
    ]
    return f"s2edge:{edge_type}:{digest}"


_OPTICAL_MARKERS = {
    "optic",
    "optical",
    "photon",
    "photonic",
    "metasurface",
    "metamaterial",
    "multilayer",
    "coating",
    "film",
    "films",
    "resonance",
    "laser",
    "spectral",
    "emission",
    "reflectance",
    "transmittance",
}
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {item.casefold() for item in _TOKEN_RE.findall(text or "")}


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text or ""))


def _candidate_annotations(
    ranker: S2CandidateRanker,
    paper: S2PaperRecord,
    *,
    topic_queries: list[str],
    channel: str,
) -> dict[str, Any]:
    vector = ranker.score(
        paper,
        queries=topic_queries,
        requested_roles=["foundation", "method", "frontier"],
    )
    topical_fit = max(vector.direct_relevance, vector.semantic_seed_similarity)
    query_optical = bool(_tokens(" ".join(topic_queries)) & _OPTICAL_MARKERS)
    optical_fit = bool(
        _tokens(" ".join([paper.title, paper.abstract, paper.venue]))
        & _OPTICAL_MARKERS
    )
    needs_translation = _contains_cjk(paper.title)
    active = topical_fit >= 0.06
    if query_optical:
        active = active and optical_fit
    if needs_translation:
        active = False
    return {
        "topic_fit": round(topical_fit, 4),
        "optical_domain_fit": optical_fit,
        "active_for_lineage": active,
        "needs_english_translation": needs_translation,
        "source_channel": channel,
        "risk_flags": vector.risk_flags,
    }


@dataclass(slots=True)
class LiteratureGraph:
    nodes: dict[str, S2PaperRecord] = field(default_factory=dict)
    node_annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[LiteratureGraphEdge] = field(default_factory=list)
    excluded_candidates: list[dict[str, Any]] = field(default_factory=list)
    query_runs: list[dict[str, Any]] = field(default_factory=list)

    def add_node(
        self, paper: S2PaperRecord, annotations: dict[str, Any] | None = None
    ) -> None:
        if paper.paper_id:
            self.nodes[paper.paper_id] = paper
            if annotations:
                self.node_annotations.setdefault(paper.paper_id, {}).update(annotations)

    def add_edge(self, edge: LiteratureGraphEdge) -> None:
        key = (edge.source_paper_id, edge.target_paper_id, edge.edge_type)
        if any(
            (item.source_paper_id, item.target_paper_id, item.edge_type) == key
            for item in self.edges
        ):
            return
        self.edges.append(edge)

    def apply_semantic_classifier(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        real_llm: bool = False,
        model_tier: str = "standard_model",
        max_items: int = 4,
    ) -> list[dict[str, Any]]:
        """Apply bounded semantic decisions only to existing graph edges."""

        from optomind_research.runtime.semantic_relation_classifier import (
            SemanticRelationClassifier,
        )

        decisions = SemanticRelationClassifier(model_tier=model_tier).classify_batch(
            candidates,
            real_llm=real_llm,
            max_items=max_items,
        )
        by_id = {decision.edge_id: decision for decision in decisions}
        for edge in self.edges:
            decision = by_id.get(edge.edge_id)
            if decision is None or not decision.semantic_relation:
                continue
            edge.semantic_relation = decision.semantic_relation
            edge.relation_basis_chunk_ids = list(decision.relation_basis_chunk_ids)
            edge.confidence = decision.confidence
            edge.status = decision.status
        return [decision.to_dict() for decision in decisions]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {key: value.to_dict() for key, value in self.nodes.items()},
            "node_annotations": self.node_annotations,
            "edges": [edge.to_dict() for edge in self.edges],
            "excluded_candidates": self.excluded_candidates,
            "query_runs": self.query_runs,
            "summary": self.summary(),
            "historical_lineage": self.historical_lineage(),
            "research_branches": self.research_branches(),
        }

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for edge in self.edges:
            counts[edge.edge_type] = counts.get(edge.edge_type, 0) + 1
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "edge_type_counts": dict(sorted(counts.items())),
        }

    def historical_lineage(self) -> dict[str, Any]:
        dated = [
            paper
            for paper in self.nodes.values()
            if paper.year
            and self.node_annotations.get(paper.paper_id, {}).get(
                "active_for_lineage", True
            )
        ]
        dated.sort(key=lambda paper: (paper.year or 9999, -paper.citation_count))
        if not dated:
            return {
                "foundational_roots": [],
                "turning_points": [],
                "frontier_papers": [],
                "timeline": [],
            }
        latest = max(paper.year or 0 for paper in dated)
        roots = sorted(
            dated,
            key=lambda paper: (
                paper.year or 9999,
                -paper.influential_citation_count,
                -paper.citation_count,
            ),
        )[: min(10, len(dated))]
        turning = sorted(
            dated,
            key=lambda paper: (
                paper.influential_citation_count,
                paper.citation_count,
            ),
            reverse=True,
        )[: min(10, len(dated))]
        frontier = [
            paper for paper in dated if paper.year and paper.year >= latest - 2
        ][:20]
        return {
            "foundational_roots": [paper.paper_id for paper in roots],
            "turning_points": [paper.paper_id for paper in turning],
            "frontier_papers": [paper.paper_id for paper in frontier],
            "timeline": [
                {
                    "paper_id": paper.paper_id,
                    "year": paper.year,
                    "title": paper.title,
                    "citation_count": paper.citation_count,
                }
                for paper in dated
            ],
        }

    def research_branches(self) -> list[dict[str, Any]]:
        adjacency: dict[str, set[str]] = {node: set() for node in self.nodes}
        for edge in self.edges:
            if edge.edge_type not in {
                "semantic_recommendation",
                "co_cited_with",
                "bibliographic_coupling",
                "same_research_branch",
            }:
                continue
            adjacency.setdefault(edge.source_paper_id, set()).add(edge.target_paper_id)
            adjacency.setdefault(edge.target_paper_id, set()).add(edge.source_paper_id)
        branches: list[dict[str, Any]] = []
        visited: set[str] = set()
        for node in adjacency:
            if node in visited:
                continue
            stack = [node]
            component: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.append(current)
                stack.extend(adjacency.get(current, set()) - visited)
            if len(component) < 2:
                continue
            titles = [self.nodes[item].title for item in component if item in self.nodes]
            branches.append(
                {
                    "branch_id": f"branch_{len(branches)+1:03d}",
                    "paper_ids": component,
                    "representative_titles": titles[:5],
                }
            )
        return branches


class S2LiteratureGraphBuilder:
    def __init__(
        self,
        gateway: S2IntelligenceGateway | None = None,
        ranker: S2CandidateRanker | None = None,
    ) -> None:
        self.gateway = gateway or S2IntelligenceGateway()
        self.ranker = ranker or S2CandidateRanker()

    @staticmethod
    def _paper_from_edge(item: dict[str, Any], relation: str) -> S2PaperRecord | None:
        key = "citedPaper" if relation == "references" else "citingPaper"
        payload = item.get(key)
        return parse_paper_record(payload) if isinstance(payload, dict) else None

    def expand_seed(
        self,
        seed: S2PaperRecord,
        *,
        reference_limit: int = 30,
        citation_limit: int = 30,
        recommendation_limit: int = 30,
        recommendation_pool: str = "recent",
        topic_queries: list[str] | None = None,
    ) -> LiteratureGraph:
        graph = LiteratureGraph()
        effective_queries = topic_queries or [seed.title]
        graph.add_node(
            seed,
            {
                "topic_fit": 1.0,
                "active_for_lineage": True,
                "source_channel": "seed",
            },
        )
        if reference_limit > 0:
            references, ref_response = self.gateway.references(
                seed.paper_id, limit=reference_limit
            )
        else:
            references, ref_response = [], S2GatewayResponse(
                ok=True, status_category="skipped"
            )
        if citation_limit > 0:
            citations, cit_response = self.gateway.citations(
                seed.paper_id, limit=citation_limit
            )
        else:
            citations, cit_response = [], S2GatewayResponse(
                ok=True, status_category="skipped"
            )
        if recommendation_limit > 0:
            recommendations, rec_response = self.gateway.recommendations_for_paper(
                seed.paper_id,
                limit=recommendation_limit,
                pool=recommendation_pool,
            )
        else:
            recommendations, rec_response = [], S2GatewayResponse(
                ok=True, status_category="skipped"
            )
        graph.query_runs.extend(
            [
                {
                    "channel": "references",
                    "status_category": ref_response.status_category,
                    "result_count": len(references),
                },
                {
                    "channel": "citations",
                    "status_category": cit_response.status_category,
                    "result_count": len(citations),
                },
                {
                    "channel": "recommendations",
                    "status_category": rec_response.status_category,
                    "result_count": len(recommendations),
                },
            ]
        )

        for item in references:
            paper = self._paper_from_edge(item, "references")
            if not paper or not paper.paper_id:
                continue
            graph.add_node(
                paper,
                _candidate_annotations(
                    self.ranker,
                    paper,
                    topic_queries=effective_queries,
                    channel="references",
                ),
            )
            graph.add_edge(
                LiteratureGraphEdge(
                    edge_id=_edge_id(seed.paper_id, paper.paper_id, "cites"),
                    source_paper_id=seed.paper_id,
                    target_paper_id=paper.paper_id,
                    edge_type="cites",
                    edge_origin="s2_api",
                    context=" ".join(item.get("contexts") or [])[:4000],
                    intents=list(item.get("intents") or []),
                    is_influential=item.get("isInfluential"),
                    historical_role="predecessor",
                )
            )
        for item in citations:
            paper = self._paper_from_edge(item, "citations")
            if not paper or not paper.paper_id:
                continue
            graph.add_node(
                paper,
                _candidate_annotations(
                    self.ranker,
                    paper,
                    topic_queries=effective_queries,
                    channel="citations",
                ),
            )
            graph.add_edge(
                LiteratureGraphEdge(
                    edge_id=_edge_id(seed.paper_id, paper.paper_id, "cited_by"),
                    source_paper_id=seed.paper_id,
                    target_paper_id=paper.paper_id,
                    edge_type="cited_by",
                    edge_origin="s2_api",
                    context=" ".join(item.get("contexts") or [])[:4000],
                    intents=list(item.get("intents") or []),
                    is_influential=item.get("isInfluential"),
                    historical_role="follow_up",
                )
            )
        for paper in recommendations:
            if not paper.paper_id or paper.paper_id == seed.paper_id:
                continue
            annotations = _candidate_annotations(
                self.ranker,
                paper,
                topic_queries=effective_queries,
                channel="recommendations",
            )
            if not annotations["active_for_lineage"]:
                graph.excluded_candidates.append(
                    {
                        "paper_id": paper.paper_id,
                        "title": paper.title,
                        "channel": "recommendations",
                        "reason": (
                            "needs_english_translation"
                            if annotations["needs_english_translation"]
                            else "low_topic_or_domain_fit"
                        ),
                        "topic_fit": annotations["topic_fit"],
                    }
                )
                continue
            graph.add_node(paper, annotations)
            graph.add_edge(
                LiteratureGraphEdge(
                    edge_id=_edge_id(
                        seed.paper_id, paper.paper_id, "semantic_recommendation"
                    ),
                    source_paper_id=seed.paper_id,
                    target_paper_id=paper.paper_id,
                    edge_type="semantic_recommendation",
                    edge_origin="s2_recommendations",
                    edge_direction="undirected",
                    confidence=0.7,
                    historical_role="parallel_branch",
                )
            )
        return graph

    def expand_from_seeds(
        self,
        seeds: list[S2PaperRecord],
        *,
        topic_queries: list[str],
        reference_limit_per_seed: int = 15,
        citation_limit_per_seed: int = 15,
        recommendation_limit: int = 30,
    ) -> LiteratureGraph:
        """Build one graph from multiple topic seeds and multi-seed recommendations."""

        merged = LiteratureGraph()
        for seed in seeds:
            partial = self.expand_seed(
                seed,
                reference_limit=reference_limit_per_seed,
                citation_limit=citation_limit_per_seed,
                recommendation_limit=0,
                topic_queries=topic_queries,
            )
            for paper_id, paper in partial.nodes.items():
                merged.add_node(paper, partial.node_annotations.get(paper_id))
            for edge in partial.edges:
                merged.add_edge(edge)
            merged.excluded_candidates.extend(partial.excluded_candidates)
            merged.query_runs.extend(partial.query_runs)

        seed_ids = [seed.paper_id for seed in seeds if seed.paper_id]
        recommendations, response = self.gateway.recommendations_from_seeds(
            seed_ids, limit=recommendation_limit
        )
        merged.query_runs.append(
            {
                "channel": "multi_seed_recommendations",
                "status_category": response.status_category,
                "result_count": len(recommendations),
                "seed_count": len(seed_ids),
            }
        )
        source_seed = seed_ids[0] if seed_ids else ""
        for paper in recommendations:
            if not paper.paper_id or paper.paper_id in seed_ids:
                continue
            annotations = _candidate_annotations(
                self.ranker,
                paper,
                topic_queries=topic_queries,
                channel="multi_seed_recommendations",
            )
            if not annotations["active_for_lineage"]:
                merged.excluded_candidates.append(
                    {
                        "paper_id": paper.paper_id,
                        "title": paper.title,
                        "channel": "multi_seed_recommendations",
                        "reason": (
                            "needs_english_translation"
                            if annotations["needs_english_translation"]
                            else "low_topic_or_domain_fit"
                        ),
                        "topic_fit": annotations["topic_fit"],
                    }
                )
                continue
            merged.add_node(paper, annotations)
            if source_seed:
                merged.add_edge(
                    LiteratureGraphEdge(
                        edge_id=_edge_id(
                            source_seed,
                            paper.paper_id,
                            "semantic_recommendation",
                        ),
                        source_paper_id=source_seed,
                        target_paper_id=paper.paper_id,
                        edge_type="semantic_recommendation",
                        edge_origin="s2_multi_seed_recommendations",
                        edge_direction="undirected",
                        context="seed_set=" + ",".join(seed_ids),
                        confidence=0.75,
                        historical_role="parallel_branch",
                    )
                )
        return merged

    @staticmethod
    def add_snippet_reference_mentions(
        graph: LiteratureGraph, chunks: Iterable[UnifiedTextChunk]
    ) -> None:
        for chunk in chunks:
            for mention in chunk.reference_mentions:
                corpus_id = mention.get("matchedPaperCorpusId")
                if corpus_id in (None, ""):
                    continue
                target = f"CorpusId:{corpus_id}"
                graph.add_edge(
                    LiteratureGraphEdge(
                        edge_id=_edge_id(
                            chunk.paper_id, target, "snippet_ref_mention"
                        ),
                        source_paper_id=chunk.paper_id,
                        target_paper_id=target,
                        edge_type="snippet_ref_mention",
                        edge_origin="s2_snippet",
                        source_chunk_id=chunk.chunk_id,
                        confidence=1.0,
                        historical_role="predecessor",
                    )
                )
