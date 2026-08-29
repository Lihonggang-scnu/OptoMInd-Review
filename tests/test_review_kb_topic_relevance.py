"""Topic-role preservation tests for ReviewKnowledgeBase ingestion."""

from __future__ import annotations

from optomind_research.review_knowledge_base import ReviewKnowledgeBaseBuilder


def test_core_topic_fit_and_downstream_policy_survive_kb_ingestion():
    builder = ReviewKnowledgeBaseBuilder.__new__(ReviewKnowledgeBaseBuilder)
    builder._paper_aliases = {}
    builder._doi_aliases = {}
    builder._title_aliases = {}
    builder._identity_merges = []
    loaded = {
        "core": {
            "core_fulltexts": [
                {
                    "paper_id": "doi:10.1000/example",
                    "doi": "10.1000/example",
                    "title": "Transfer method from another optical domain",
                    "quality_check": {
                        "quality_tier": "review_core",
                        "query_relevance": "medium",
                    },
                    "current_topic_fit": {
                        "relevance_class": "method_transfer",
                        "directness_score": 2,
                        "reason": "Useful algorithm, different device target.",
                    },
                    "downstream_use_policy": "method_only",
                }
            ]
        },
        "paper_cards": [],
        "text_chunks": [],
        "visual_assets": [],
        "visual_chunks": [],
    }

    builder._prepare_identity_resolver(loaded)
    papers = builder._build_papers(loaded)

    assert len(papers) == 1
    paper = papers[0]
    assert paper["fulltext"]["quality_tier"] == "review_core"
    assert paper["fulltext"]["query_relevance"] == "medium"
    assert paper["fulltext"]["topic_relevance_class"] == "method_transfer"
    assert paper["topic_relevance"]["directness_score"] == 2
    assert paper["downstream_use_policy"] == "method_only"
    assert "method_transfer" in paper["search_text"]


def test_exact_title_aliases_merge_into_the_single_doi_identity():
    builder = ReviewKnowledgeBaseBuilder.__new__(ReviewKnowledgeBaseBuilder)
    builder._paper_aliases = {}
    builder._doi_aliases = {}
    builder._title_aliases = {}
    builder._identity_merges = []
    title = "A Reinforcement Learning Method for Optical Thin-Film Design"
    loaded = {
        "core": {
            "core_fulltexts": [
                {"paper_id": "title:local-hash", "doi": "", "title": title},
                {"paper_id": "doi:10.1000/thin-film", "doi": "10.1000/thin-film", "title": title},
            ]
        },
        "paper_cards": [
            {"paper_identity": {"paper_id": "local:thin-film", "doi": "", "title": title}}
        ],
        "text_chunks": [],
        "visual_assets": [],
        "visual_chunks": [
            {"paper_id": "local:thin-film", "paper_title": title, "chunk_id": "visual-1"}
        ],
    }

    builder._prepare_identity_resolver(loaded)
    papers = builder._build_papers(loaded)

    assert [paper["paper_id"] for paper in papers] == ["doi:10.1000/thin-film"]
    assert builder._resolve_paper_id(paper_id="local:thin-film", title=title) == "doi:10.1000/thin-film"
    assert len(builder._identity_merges) == 1


def test_conflicting_dois_with_same_title_are_not_merged():
    builder = ReviewKnowledgeBaseBuilder.__new__(ReviewKnowledgeBaseBuilder)
    builder._paper_aliases = {}
    builder._doi_aliases = {}
    builder._title_aliases = {}
    builder._identity_merges = []
    loaded = {
        "core": {
            "core_fulltexts": [
                {"paper_id": "doi:10.1000/a", "doi": "10.1000/a", "title": "Shared title"},
                {"paper_id": "doi:10.1000/b", "doi": "10.1000/b", "title": "Shared title"},
            ]
        },
        "paper_cards": [],
        "text_chunks": [],
        "visual_assets": [],
        "visual_chunks": [],
    }

    builder._prepare_identity_resolver(loaded)
    papers = builder._build_papers(loaded)

    assert {paper["paper_id"] for paper in papers} == {"doi:10.1000/a", "doi:10.1000/b"}
    assert builder._identity_merges == []
