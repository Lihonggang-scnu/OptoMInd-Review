from __future__ import annotations

from typing import Any

from optomind_research.s2_citation_role_mapper import map_citation_roles
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.s2_literature_graph import (
    LiteratureGraph,
    S2LiteratureGraphBuilder,
)
from optomind_research.s2_schemas import parse_paper_record
from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever


def _long_body() -> str:
    return (
        "The physical mechanism combines radiative coupling, interference, and "
        "symmetry breaking to control the linewidth and angular response. "
        "Experimental measurements compare several structures and show how the "
        "resonant response changes with geometry. "
    ) * 5


def test_s2_body_snippet_becomes_first_class_text_chunk() -> None:
    class StubGateway:
        def search_snippets(self, query: str, **_: Any):
            return [
                {
                    "score": 0.72,
                    "snippet": {
                        "text": _long_body(),
                        "snippetKind": "body",
                        "section": "Physical mechanism",
                        "snippetOffset": {"start": 1200, "end": 2400},
                        "annotations": {
                            "sentences": [{"start": 0, "end": 120}],
                            "refMentions": [
                                {
                                    "start": 80,
                                    "end": 95,
                                    "matchedPaperCorpusId": "42",
                                }
                            ],
                        },
                    },
                    "paper": {
                        "corpusId": "100",
                        "title": "Mechanisms of optical resonances",
                    },
                }
            ], S2GatewayResponse(
                ok=True,
                status_code=200,
                status_category="ok",
                payload={"retrievalVersion": "test-v1"},
            )

    result = S2TextChunkRetriever(
        gateway=StubGateway(), min_chars=500  # type: ignore[arg-type]
    ).retrieve(
        ["symmetry breaking optical resonance mechanism"],
        requested_roles=["mechanism"],
    )
    assert len(result.accepted_chunks) == 1
    chunk = result.accepted_chunks[0]
    assert chunk.content_kind == "text_chunk"
    assert chunk.text_provenance == "s2_body_snippet"
    assert chunk.quality_status == "accepted"
    assert chunk.paper_id == "CorpusId:100"
    assert "direct_support" in chunk.citation_roles
    assert "mechanism_neighbor" in chunk.citation_roles
    assert chunk.reference_mentions[0]["matchedPaperCorpusId"] == "42"


def test_non_body_or_short_snippets_are_rejected_not_mislabeled() -> None:
    class StubGateway:
        def search_snippets(self, query: str, **_: Any):
            return [
                {
                    "snippet": {
                        "text": "An abstract-only result.",
                        "snippetKind": "abstract",
                        "section": "Abstract",
                    },
                    "paper": {"corpusId": "1", "title": "Paper A"},
                },
                {
                    "snippet": {
                        "text": "short body",
                        "snippetKind": "body",
                        "section": "Results",
                    },
                    "paper": {"corpusId": "2", "title": "Paper B"},
                },
            ], S2GatewayResponse(ok=True, status_code=200, status_category="ok")

    result = S2TextChunkRetriever(
        gateway=StubGateway(), min_chars=500  # type: ignore[arg-type]
    ).retrieve(["query"])
    assert not result.accepted_chunks
    assert {item["reason"] for item in result.rejected_items} == {
        "not_body:abstract",
        "too_short",
    }


def test_citation_role_mapper_allows_background_and_multiple_roles() -> None:
    paper = parse_paper_record(
        {
            "paperId": "review",
            "title": "A roadmap for optical metasurfaces",
            "year": 2025,
            "citationCount": 100,
            "publicationTypes": ["Review"],
            "authors": [],
        }
    )
    roles = map_citation_roles(
        query_or_claim="thermal emission control",
        text="This review discusses emerging device applications and limitations.",
        paper=paper,
        requested_roles=["review", "frontier"],
        direct_score=0.05,
    )
    assert "background_context" in roles
    assert "application_example" in roles
    assert "controversy_or_boundary" in roles
    assert "frontier_progress" in roles
    assert "review_pointer" in roles


def test_graph_keeps_citation_and_recommendation_edges_distinct() -> None:
    seed = parse_paper_record(
        {
            "paperId": "seed",
            "title": "Seed optical resonance paper",
            "year": 2018,
            "citationCount": 50,
            "authors": [],
        }
    )

    class StubGateway:
        def references(self, *_: Any, **__: Any):
            return [
                {
                    "contexts": ["The foundational principle was introduced earlier."],
                    "intents": ["background"],
                    "isInfluential": True,
                    "citedPaper": {
                        "paperId": "older",
                        "title": "Older optical resonance foundation",
                        "year": 2005,
                        "citationCount": 800,
                        "authors": [],
                    },
                }
            ], S2GatewayResponse(ok=True, status_category="ok")

        def citations(self, *_: Any, **__: Any):
            return [
                {
                    "contexts": ["We extend the seed method."],
                    "intents": ["methodology"],
                    "isInfluential": False,
                    "citingPaper": {
                        "paperId": "newer",
                        "title": "Newer photonic resonance development",
                        "year": 2025,
                        "citationCount": 20,
                        "authors": [],
                    },
                }
            ], S2GatewayResponse(ok=True, status_category="ok")

        def recommendations_for_paper(self, *_: Any, **__: Any):
            return [
                parse_paper_record(
                    {
                        "paperId": "parallel",
                        "title": "Parallel optical resonance branch",
                        "year": 2024,
                        "citationCount": 5,
                        "authors": [],
                    }
                )
            ], S2GatewayResponse(ok=True, status_category="ok")

    graph = S2LiteratureGraphBuilder(
        gateway=StubGateway()  # type: ignore[arg-type]
    ).expand_seed(
        seed,
        reference_limit=5,
        citation_limit=5,
        recommendation_limit=5,
        topic_queries=["optical resonance mechanism"],
    )
    types = {edge.edge_type for edge in graph.edges}
    assert types == {"cites", "cited_by", "semantic_recommendation"}
    assert graph.summary()["node_count"] == 4
    lineage = graph.historical_lineage()
    assert lineage["timeline"][0]["paper_id"] == "older"
    assert "newer" in lineage["frontier_papers"]
    assert graph.research_branches()


def test_snippet_reference_mentions_become_typed_graph_edges() -> None:
    class StubGateway:
        def search_snippets(self, query: str, **_: Any):
            return [
                {
                    "score": 0.8,
                    "snippet": {
                        "text": _long_body(),
                        "snippetKind": "body",
                        "section": "Discussion",
                        "snippetOffset": {"start": 1, "end": 1000},
                        "annotations": {
                            "refMentions": [{"matchedPaperCorpusId": "777"}],
                            "sentences": [],
                        },
                    },
                    "paper": {"corpusId": "555", "title": "Source paper"},
                }
            ], S2GatewayResponse(ok=True, status_category="ok")

    chunks = S2TextChunkRetriever(
        gateway=StubGateway(), min_chars=500  # type: ignore[arg-type]
    ).retrieve(["mechanism"]).accepted_chunks
    graph = LiteratureGraph()
    S2LiteratureGraphBuilder.add_snippet_reference_mentions(graph, chunks)
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.edge_type == "snippet_ref_mention"
    assert edge.target_paper_id == "CorpusId:777"
    assert edge.source_chunk_id == chunks[0].chunk_id


def test_multi_seed_recommendations_form_active_semantic_branch() -> None:
    seeds = [
        parse_paper_record(
            {
                "paperId": "seed1",
                "title": "Passive daytime radiative cooling",
                "year": 2014,
                "authors": [],
            }
        ),
        parse_paper_record(
            {
                "paperId": "seed2",
                "title": "Scalable radiative cooling coatings",
                "year": 2017,
                "authors": [],
            }
        ),
    ]

    class StubGateway:
        def references(self, *_: Any, **__: Any):
            return [], S2GatewayResponse(ok=True, status_category="ok")

        def citations(self, *_: Any, **__: Any):
            return [], S2GatewayResponse(ok=True, status_category="ok")

        def recommendations_from_seeds(self, paper_ids: list[str], **_: Any):
            assert paper_ids == ["seed1", "seed2"]
            return [
                parse_paper_record(
                    {
                        "paperId": "recommended",
                        "title": "Spectrally selective radiative cooling film",
                        "year": 2025,
                        "authors": [],
                    }
                )
            ], S2GatewayResponse(ok=True, status_category="ok")

    graph = S2LiteratureGraphBuilder(
        gateway=StubGateway()  # type: ignore[arg-type]
    ).expand_from_seeds(
        seeds,
        topic_queries=["passive daytime radiative cooling film"],
        reference_limit_per_seed=0,
        citation_limit_per_seed=0,
        recommendation_limit=5,
    )
    assert "recommended" in graph.nodes
    assert any(
        edge.edge_type == "semantic_recommendation" for edge in graph.edges
    )
    assert graph.query_runs[-1]["seed_count"] == 2
