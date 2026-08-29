from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import optomind_research.s2_harness_bootstrap as bootstrap_module
from optomind_research.runtime.argument_quality_policy import (
    QUALIFIED,
    evidence_ceiling,
)
from optomind_research.runtime.review_quality_contract import permission_for_content
from optomind_research.s2_candidate_ranker import S2CandidateRanker
from optomind_research.s2_discovery import DiscoveryPortfolio
from optomind_research.s2_fulltext_acquisition import (
    S2FulltextAcquisitionResult,
)
from optomind_research.s2_harness_bootstrap import (
    _build_material_flow_ledger,
    prepare_s2_harness_kb,
)
from optomind_research.s2_intelligence_gateway import (
    S2GatewayResponse,
    S2IntelligenceGateway,
)
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk
from optomind_research.s2_text_chunk_retriever import (
    S2TextChunkRetriever,
    TextChunkRetrievalResult,
    materialize_abstract_claim,
)


def _body(label: str) -> str:
    return (
        f"{label} broadband achromatic metalens group delay dispersion engineering "
        "controls optical phase and imaging response across wavelength. "
        "The study compares physical mechanisms, device methods, and measured performance. "
    ) * 6


def _paper(index: int, *, abstract: bool = True) -> S2PaperRecord:
    return S2PaperRecord(
        paper_id=f"p{index}",
        corpus_id=1000 + index,
        doi=f"10.1000/metalens-{index}",
        title=f"Broadband achromatic metalens dispersion study {index}",
        abstract=(
            "This paper reports broadband achromatic metalens imaging through "
            "group-delay and dispersion engineering, and identifies the measured "
            "bandwidth and focusing response as the main outcomes."
            if abstract
            else ""
        ),
        year=2024,
        is_oa=True,
        s2_open_access_candidate_url=f"https://example.test/p{index}.pdf",
    )


def _chunk(paper: S2PaperRecord, label: str) -> UnifiedTextChunk:
    return UnifiedTextChunk(
        chunk_id=f"s2chunk:{paper.corpus_id}:0:900:{label}",
        paper_id=paper.paper_id,
        corpus_id=paper.corpus_id,
        doi=paper.doi,
        title=paper.title,
        text=_body(label),
        section="Results",
        content_depth="structured_snippet",
        context_complete=True,
        scope_fit="direct",
        use_permission="factual_support",
        allowed_claim_kinds=["mechanism", "measurement"],
        route_provenance={
            "discovery_route": "semantic_scholar_snippet_search",
            "materialization_route": "s2_structured_body_snippet",
        },
    )


def test_paper_search_collects_all_configured_pages() -> None:
    offsets: list[int] = []

    class StubTransport:
        def request_json(self, _method: str, _url: str, **kwargs: Any):
            params = kwargs["params"]
            offset = int(params["offset"])
            limit = int(params["limit"])
            offsets.append(offset)
            data = [
                {
                    "paperId": f"paper-{idx}",
                    "title": f"Metalens paper {idx}",
                    "abstract": "Broadband achromatic metalens abstract.",
                    "year": 2024,
                }
                for idx in range(offset, min(130, offset + limit))
            ]
            return S2GatewayResponse(
                ok=True,
                status_code=200,
                status_category="ok",
                payload={"total": 130, "data": data},
            )

    papers, response = S2IntelligenceGateway(
        transport=StubTransport()  # type: ignore[arg-type]
    ).search_papers("achromatic metalens", limit=130)

    assert len(papers) == 130
    assert offsets == [0, 100]
    assert response.audit["page_count"] == 2
    assert response.audit["returned_unique_papers"] == 130


def test_precise_lookup_only_targets_papers_missing_body_material() -> None:
    first, second = _paper(1), _paper(2)
    calls: list[tuple[str, list[str]]] = []

    class StubGateway:
        def search_snippets(self, query: str, **kwargs: Any):
            calls.append((query, list(kwargs.get("paper_ids") or [])))
            return [
                {
                    "score": 0.9,
                    "snippet": {
                        "text": _body("precise"),
                        "snippetKind": "body",
                        "section": "Results",
                        "snippetOffset": {"start": 0, "end": 900},
                        "annotations": {},
                    },
                    "paper": {
                        "paperId": second.paper_id,
                        "corpusId": second.corpus_id,
                        "title": second.title,
                    },
                }
            ], S2GatewayResponse(ok=True, status_code=200, status_category="ok")

    result = S2TextChunkRetriever(
        gateway=StubGateway(), min_chars=100  # type: ignore[arg-type]
    ).retrieve_precise_missing_papers(
        [first, second],
        existing_chunks=[_chunk(first, "broad")],
        limit_per_paper=100,
    )

    assert calls == [(second.title, [second.paper_id])]
    assert len(result.accepted_chunks) == 1
    assert result.query_runs[0]["query_category"] == "precise_missing_paper"
    assert result.query_runs[0]["target_paper_id"] == second.paper_id


def test_materialized_abstract_claim_is_qualified_without_promoting_old_abstracts(
    tmp_path: Path,
) -> None:
    paper = _paper(1)
    chunk, reason = materialize_abstract_claim(paper)
    duplicate, _ = materialize_abstract_claim(paper)

    assert reason == "materialized"
    assert chunk is not None and duplicate is not None
    assert chunk.chunk_id == duplicate.chunk_id
    assert chunk.content_depth == "abstract_claim"
    assert chunk.use_permission == QUALIFIED
    assert "do_not_infer_unstated_numbers_mechanisms_or_causality" in chunk.context_limitations
    assert permission_for_content("abstract")["use_permission"] == "background_and_candidate_only"
    assert evidence_ceiling(chunk.to_dict())[0] == QUALIFIED

    db = tmp_path / "kb.sqlite"
    S2KnowledgeBaseBridge(db).ingest(papers=[paper], chunks=[chunk])
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT content_depth,use_permission,evidence_level,source_kind "
            "FROM text_chunks WHERE chunk_id=?",
            (chunk.chunk_id,),
        ).fetchone()
    assert row == (
        "abstract_claim",
        "contextual_or_qualified_support",
        "abstract",
        "abstract",
    )


def test_materialized_resolver_abstract_preserves_provider_provenance() -> None:
    paper = _paper(1)
    paper.route_events.append(
        {
            "event": "abstract_enriched_from_verified_public_metadata",
            "provider": "openalex",
        }
    )

    chunk, reason = materialize_abstract_claim(paper)

    assert reason == "materialized"
    assert chunk is not None
    assert chunk.source_locator["provider"] == "openalex"
    assert chunk.route_provenance["abstract_provider"] == "openalex"
    assert chunk.route_provenance["materialization_route"] == (
        "verified_public_metadata_abstract_claim_after_body_and_oa_miss"
    )


def test_verified_supplement_reuses_prior_scope_decision_without_orphaning_chunk(
    tmp_path: Path,
) -> None:
    paper = _paper(1, abstract=False)
    policy_path = _write_policy(tmp_path / "policy.json", 1)
    policy = bootstrap_module.load_s2_policy(policy_path)
    query_plan_path = _write_plan(tmp_path / "query.json")
    scope_contract = bootstrap_module.derive_topic_scope_contract(
        json.loads(query_plan_path.read_text(encoding="utf-8"))
    )
    stage = bootstrap_module.TopicScopedKBStage(
        query_plan_path=query_plan_path,
        base_kb_sqlite=_write_base(tmp_path / "base.sqlite"),
        work_dir=tmp_path / "work",
        policy=policy,
        scope_contract=scope_contract,
    )
    stage.create_overlay()
    stage.ingest_s2(papers=[paper])

    paper.abstract = (
        "This broadband achromatic metalens study reports group-delay control "
        "and briefly lists radiative cooling as an unrelated application boundary."
    )
    chunk, reason = materialize_abstract_claim(paper)
    assert reason == "materialized" and chunk is not None

    supplement = stage.ingest_s2_supplement(
        papers=[paper],
        chunks=[chunk],
        label="verified_abstract_after_primary_acceptance",
    )

    assert supplement["papers_accepted"] == 1
    assert supplement["chunks_accepted"] == 1
    assert supplement["paper_decisions"][0]["reason"] == (
        "prior_scope_acceptance_reused_for_verified_supplement"
    )
    with sqlite3.connect(stage.runtime_kb) as conn:
        assert conn.execute(
            "SELECT paper_id FROM text_chunks WHERE chunk_id=?", (chunk.chunk_id,)
        ).fetchone() == (paper.paper_id,)


def test_material_flow_admission_is_or_not_and() -> None:
    papers = [_paper(index) for index in range(1, 5)]
    inventory = {
        "p1": {
            "s2_body_chunk_ids": ["s2-1"],
            "oa_fulltext_chunk_ids": [],
            "abstract_claim_chunk_ids": [],
        },
        "p2": {
            "s2_body_chunk_ids": [],
            "oa_fulltext_chunk_ids": ["oa-2"],
            "abstract_claim_chunk_ids": [],
        },
        "p3": {
            "s2_body_chunk_ids": [],
            "oa_fulltext_chunk_ids": [],
            "abstract_claim_chunk_ids": ["abstract-3"],
        },
        "p4": {
            "s2_body_chunk_ids": [],
            "oa_fulltext_chunk_ids": [],
            "abstract_claim_chunk_ids": [],
        },
    }

    ledger = _build_material_flow_ledger(
        papers=papers,
        inventory=inventory,
        precise_runs=[],
        fulltext_report={},
        abstract_outcomes={},
    )

    assert ledger["summary"] == {
        "paper_count": 4,
        "admitted_paper_count": 3,
        "s2_body_paper_count": 1,
        "oa_fulltext_paper_count": 1,
        "abstract_claim_paper_count": 1,
        "discovery_only_paper_count": 1,
    }
    assert [row["admitted_to_downstream"] for row in ledger["papers"]] == [
        True,
        True,
        True,
        False,
    ]


def _write_plan(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "input": {"user_query": "Review broadband achromatic metalenses."},
                "output": {
                    "problem_understanding": (
                        "Review broadband achromatic metalens group-delay and "
                        "dispersion engineering for optical imaging."
                    ),
                    "scope_definition": {
                        "main_scope": "Optical broadband achromatic metalenses.",
                        "scope_items": ["group delay", "dispersion", "imaging"],
                    },
                    "lenses": ["mechanism", "method", "measurement"],
                    "inclusion_boundaries": ["optical metalens"],
                    "exclusion_boundaries": ["acoustic metalens"],
                    "keyword_decomposition": {
                        "keywords": ["broadband achromatic metalens dispersion"]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_base(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE papers(
                paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER,
                venue TEXT, quality_tier TEXT, query_relevance TEXT,
                search_text TEXT, raw_json TEXT NOT NULL
            );
            CREATE TABLE text_chunks(
                chunk_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, doi TEXT,
                title TEXT, ordinal INTEGER, section_path TEXT, char_start INTEGER,
                char_end INTEGER, char_count INTEGER, boilerplate_score REAL,
                text TEXT, search_text TEXT, raw_json TEXT NOT NULL
            );
            """
        )
    return path


def _write_policy(path: Path, paper_count: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "s2_first": {
                    "enabled": True,
                    "use_batch_enrichment": False,
                    "build_literature_graph": False,
                },
                "standard": {
                    "results_per_query": paper_count,
                    "snippet_results_per_query": 20,
                    "precise_snippet_results_per_paper": 20,
                    "max_precise_snippet_papers": paper_count,
                    "max_abstract_claim_papers": paper_count,
                    "accepted_s2_text_papers_per_facet": [1, paper_count],
                    "oa_fulltext_downloads_per_facet": [0, paper_count],
                    "graph_depth": 0,
                    "max_search_queries": 1,
                    "max_snippet_queries": 1,
                },
                "graph": {
                    "seed_count": 0,
                    "reference_limit_per_seed": 0,
                    "citation_limit_per_seed": 0,
                    "recommendation_limit": 0,
                },
                "evidence": {
                    "minimum_factual_papers": 1,
                    "minimum_factual_chunks": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _portfolio(papers: list[S2PaperRecord]) -> DiscoveryPortfolio:
    ranker = S2CandidateRanker(current_year=2026)
    candidates = [
        ranker.build_candidate(
            paper,
            facet_id="bootstrap",
            queries=["broadband achromatic metalens dispersion"],
            requested_roles=["mechanism", "method"],
            discovery_channel="s2_relevance_search",
        )
        for paper in papers
    ]
    return DiscoveryPortfolio(
        candidates=candidates,
        query_runs=[{"query": "broadband achromatic metalens", "status_code": 200}],
        pool_counts={},
    )


def test_bootstrap_stops_fallback_after_sufficient_s2_or_oa(
    tmp_path: Path,
    monkeypatch,
) -> None:
    papers = [_paper(index) for index in range(1, 4)]
    portfolio = _portfolio(papers)
    fulltext_targets: list[str] = []

    class Discovery:
        def discover(self, *_args: Any, **_kwargs: Any):
            return portfolio

    class Retriever:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        def retrieve(self, *_args: Any, **_kwargs: Any):
            return TextChunkRetrievalResult([_chunk(papers[0], "broad")], [], [], ["p1"])

        def retrieve_precise_missing_papers(self, *_args: Any, **_kwargs: Any):
            return TextChunkRetrievalResult(
                [_chunk(papers[1], "precise")],
                [],
                [
                    {
                        "query_category": "precise_missing_paper",
                        "target_paper_id": "p2",
                    },
                    {
                        "query_category": "precise_missing_paper",
                        "target_paper_id": "p3",
                    },
                ],
                ["p2"],
            )

    class Fulltext:
        def __init__(self, *, kb_sqlite: Path, download_dir: Path):
            self.kb_sqlite = kb_sqlite

        def acquire(self, selections: list[Any], **_kwargs: Any):
            fulltext_targets.extend(paper.paper_id for paper, _ in selections)
            paper = selections[0][0]
            chunk = UnifiedTextChunk(
                chunk_id="oa:p3:fulltext",
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                text=_body("oa-fulltext"),
                text_provenance="local_pdf_parse",
                content_depth="fulltext",
                context_complete=True,
                scope_fit="direct",
                use_permission="factual_support",
                allowed_claim_kinds=["mechanism", "measurement"],
                route_provenance={
                    "discovery_route": "semantic_scholar_graph",
                    "materialization_route": "m3_oa_fulltext_parse",
                },
            )
            S2KnowledgeBaseBridge(self.kb_sqlite).ingest(
                papers=[paper], chunks=[chunk]
            )
            return S2FulltextAcquisitionResult(
                selected_paper_ids=[paper.paper_id],
                new_chunk_ids=[chunk.chunk_id],
                new_paper_ids=[paper.paper_id],
                stats={
                    "attempted": 1,
                    "downloaded": 1,
                    "parse_failed": 0,
                    "paper_outcomes": [
                        {
                            "paper_id": paper.paper_id,
                            "status": "oa_fulltext_success",
                        }
                    ],
                },
            )

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", Discovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", Retriever)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", Fulltext)
    monkeypatch.setattr(
        bootstrap_module,
        "materialize_abstract_claim",
        lambda _paper: (_ for _ in ()).throw(
            AssertionError("abstract fallback must stop after OA succeeds")
        ),
    )

    report = prepare_s2_harness_kb(
        query_plan_path=_write_plan(tmp_path / "query.json"),
        base_kb_sqlite=_write_base(tmp_path / "base.sqlite"),
        work_dir=tmp_path / "work",
        policy_path=_write_policy(tmp_path / "policy.json", 3),
    )

    assert report["status"] == "completed"
    assert fulltext_targets == ["p3"]
    assert report["material_flow_summary"] == {
        "paper_count": 3,
        "admitted_paper_count": 3,
        "s2_body_paper_count": 2,
        "oa_fulltext_paper_count": 1,
        "abstract_claim_paper_count": 0,
        "discovery_only_paper_count": 0,
    }


def test_bootstrap_materializes_abstract_only_after_s2_and_oa_are_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paper = _paper(1)
    portfolio = _portfolio([paper])

    class Discovery:
        def discover(self, *_args: Any, **_kwargs: Any):
            return portfolio

    class Retriever:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        def retrieve(self, *_args: Any, **_kwargs: Any):
            return TextChunkRetrievalResult([], [], [], [])

        def retrieve_precise_missing_papers(self, *_args: Any, **_kwargs: Any):
            return TextChunkRetrievalResult(
                [],
                [],
                [
                    {
                        "query_category": "precise_missing_paper",
                        "target_paper_id": paper.paper_id,
                    }
                ],
                [],
            )

    class Fulltext:
        def __init__(self, **_kwargs: Any):
            pass

        def acquire(self, selections: list[Any], **_kwargs: Any):
            return S2FulltextAcquisitionResult(
                selected_paper_ids=[item[0].paper_id for item in selections],
                stats={
                    "attempted": len(selections),
                    "downloaded": 0,
                    "parse_failed": len(selections),
                    "paper_outcomes": [
                        {
                            "paper_id": item[0].paper_id,
                            "status": "oa_download_or_parse_failed",
                        }
                        for item in selections
                    ],
                },
            )

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", Discovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", Retriever)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", Fulltext)

    report = prepare_s2_harness_kb(
        query_plan_path=_write_plan(tmp_path / "query.json"),
        base_kb_sqlite=_write_base(tmp_path / "base.sqlite"),
        work_dir=tmp_path / "work",
        policy_path=_write_policy(tmp_path / "policy.json", 1),
    )

    assert report["status"] == "partial"
    assert report["material_flow_summary"]["abstract_claim_paper_count"] == 1
    assert report["material_flow_summary"]["admitted_paper_count"] == 1
    assert report["evidence"]["evidence_eligible_chunk_count"] == 1
    with sqlite3.connect(report["runtime_kb_sqlite"]) as conn:
        row = conn.execute(
            "SELECT content_depth,use_permission FROM text_chunks"
        ).fetchone()
    assert row == ("abstract_claim", "contextual_or_qualified_support")
