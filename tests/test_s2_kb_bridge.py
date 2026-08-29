from __future__ import annotations

import json
import sqlite3

from optomind_research.s2_fulltext_acquisition import (
    _candidate_from_s2,
    _looks_like_reference_or_footer,
    _non_body_reason,
    _quarantine_non_body_chunks,
    decide_fulltext_escalation,
)
from optomind_research.s2_candidate_ranker import S2Candidate
from optomind_research.s2_discovery import DiscoveryPortfolio
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_m3_gap_loop import S2M3GapLoop
from optomind_research.s2_text_chunk_retriever import TextChunkRetrievalResult
from optomind_research.s2_schemas import (
    LiteratureGraphEdge,
    S2PaperRecord,
    UnifiedTextChunk,
)


def _paper() -> S2PaperRecord:
    return S2PaperRecord(
        paper_id="s2-paper-1",
        corpus_id=123,
        doi="10.1000/test",
        title="Inverse design of optical multilayers",
        abstract="An optical multilayer inverse-design study.",
        year=2025,
        is_oa=True,
        s2_open_access_candidate_url="https://example.org/paper.pdf",
    )


def test_s2_body_snippet_is_first_class_and_fts_searchable(tmp_path):
    db = tmp_path / "kb.sqlite"
    chunk = UnifiedTextChunk(
        chunk_id="s2chunk:123:10:900:test",
        paper_id="s2-paper-1",
        corpus_id=123,
        doi="10.1000/test",
        title="Inverse design of optical multilayers",
        section="Results",
        text=(
            "The inverse-designed multilayer achieves broadband spectral control "
            "through a constrained neural optimization procedure. " * 8
        ),
        citation_roles=["method_example", "direct_support"],
        source_locator={"provider": "semantic_scholar", "offset_start": 10, "offset_end": 900},
    )
    bridge = S2KnowledgeBaseBridge(db)
    first = bridge.ingest(papers=[_paper()], chunks=[chunk])
    second = bridge.ingest(papers=[_paper()], chunks=[chunk])
    assert first["chunks_inserted"] == 1
    assert second["chunks_reused"] == 1
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT evidence_level,source_kind,provenance_json FROM text_chunks"
        ).fetchone()
        assert row[0] == "text_chunk"
        assert row[1] == "s2_body_snippet"
        provenance = json.loads(row[2])
        assert provenance["provider"] == "semantic_scholar"
        hits = conn.execute(
            "SELECT chunk_id FROM text_chunk_fts WHERE text_chunk_fts MATCH ?",
            ("multilayer",),
        ).fetchall()
        assert hits == [(chunk.chunk_id,)]
    finally:
        conn.close()


def test_graph_edges_persist_with_typed_provenance(tmp_path):
    db = tmp_path / "kb.sqlite"
    graph = LiteratureGraph()
    graph.add_node(_paper(), {"active_for_lineage": True})
    graph.add_node(S2PaperRecord(paper_id="s2-paper-2", title="Earlier method", year=2020))
    graph.add_edge(
        LiteratureGraphEdge(
            edge_id="edge-1",
            source_paper_id="s2-paper-1",
            target_paper_id="s2-paper-2",
            edge_type="cites",
        )
    )
    result = S2KnowledgeBaseBridge(db).ingest_graph(graph)
    assert result["nodes_upserted"] == 2
    assert result["edges_upserted"] == 1
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT edge_type,edge_origin FROM s2_literature_graph_edges"
        ).fetchone()
        assert row == ("cites", "s2_api")
    finally:
        conn.close()


def test_fulltext_escalation_is_deterministic_and_not_an_llm_gate():
    decision = decide_fulltext_escalation(
        _paper(),
        role_labels=["mechanism"],
        need_visual_assets=True,
    )
    assert decision.should_download
    assert "visual_assets" in decision.desired_assets
    title_only = S2PaperRecord(
        paper_id="metadata-only",
        title="A metadata-only paper",
        is_oa=False,
    )
    resolved = decide_fulltext_escalation(
        title_only, role_labels=["frontier"], need_complete_context=True
    )
    assert resolved.should_download
    assert resolved.reason == "metadata_available_for_public_oa_resolution"


def test_s2_fulltext_candidate_does_not_duplicate_abstract_and_upgrades_arxiv():
    paper = _paper()
    paper.s2_open_access_candidate_url = "http://arxiv.org/pdf/2304.10294"
    candidate = _candidate_from_s2(paper, enrich_oa_routes=False)
    assert candidate["pdf_url"].startswith("https://arxiv.org/")
    assert candidate["skip_abstract_fallback"] is True


def test_reference_and_footer_detection_is_conservative():
    body = (
        "The transfer matrix method computes reflection and transmission for "
        "each wavelength and angle. The resulting spectra agree with the "
        "reference implementation and preserve differentiability. " * 3
    )
    references = (
        "References Smith et al. (2020). Journal of Optics. doi:10.1/a. "
        "Jones et al. (2021). Proceedings. https://example.org. "
        "Lee et al. (2022). Journal of Photonics. doi:10.1/b."
    )
    assert not _looks_like_reference_or_footer(body)
    assert _looks_like_reference_or_footer(references)
    assert _looks_like_reference_or_footer("Journal footer, 4")


def test_reference_detection_catches_numbered_tail_without_heading():
    references = (
        "(94) Wang, Y.; Zhang, S. Multi-receptive-field physics-informed "
        "network. Optical Materials Express 2024, 14, 2740-2754. "
        "(95) Sui, X.; Wu, Q. A review of optical neural networks. IEEE "
        "Access 2020, 8, 70773-70783. (96) Chen, M.; Fan, J. High speed "
        "simulation of nanophotonic devices. ACS Photonics 2022, 9, 1-10."
    )
    body = (
        "The model uses measurements collected in 2020, 2022, and 2024. "
        "Equations (1), (2), and (3) define the forward operator, loss, and "
        "regularizer. The resulting optimization remains differentiable "
        "and preserves the measured spectral constraints."
    )
    assert _looks_like_reference_or_footer(references)
    assert not _looks_like_reference_or_footer(body)


def test_reference_detection_catches_repository_navigation():
    navigation = (
        "Deep optical paper - PMC Skip to main content An official website "
        "of the United States government. Official websites use .gov. "
        "Secure .gov websites use HTTPS. Search Log in."
    )
    assert _looks_like_reference_or_footer(navigation)


def test_publisher_header_is_removed_without_starting_a_bibliography_tail():
    header = (
        "View Online Export Citation Research Article March 10 2025. "
        "Articles You May Be Interested In: a geotechnical perspective, "
        "AIP Conference Proceedings November 2021; rainfall prediction with "
        "a neural network, AIP Conference Proceedings November 2021; and "
        "groundwater estimation using geophysical methods November 2021. "
        "https://doi.org/10.1063/example"
    )
    assert _non_body_reason(header) == "publisher_header"


def test_quarantine_removes_entire_reference_tail_but_keeps_earlier_body(tmp_path):
    db = tmp_path / "kb.sqlite"
    S2KnowledgeBaseBridge(db).ingest(papers=[_paper()])
    rows = [
        (
            "body",
            7,
            "The measured spectrum agrees with the differentiable forward "
            "model and supports the reported inverse-design result. " * 3,
        ),
        (
            "references-start",
            8,
            "References (1) Smith et al. Journal of Optics 2020. doi:10.1/a. "
            "(2) Jones et al. Proceedings 2021. doi:10.1/b. "
            "(3) Lee et al. Photonics 2022. doi:10.1/c.",
        ),
        (
            "references-end",
            9,
            "(4) Chen, A.; Wu, B. Optical study. Photonics 2023, 4, 1-10.",
        ),
    ]
    with sqlite3.connect(db) as conn:
        for chunk_id, ordinal, text in rows:
            conn.execute(
                "INSERT INTO text_chunks(chunk_id,paper_id,ordinal,text) VALUES(?,?,?,?)",
                (chunk_id, _paper().paper_id, ordinal, text),
            )
            conn.execute(
                "INSERT INTO text_chunk_fts(chunk_id,paper_id,text) VALUES(?,?,?)",
                (chunk_id, _paper().paper_id, text),
            )
        conn.commit()

    kept, removed = _quarantine_non_body_chunks(
        db, ["body", "references-start", "references-end"]
    )

    assert kept == ["body"]
    assert removed == ["references-start", "references-end"]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT chunk_id FROM text_chunks ORDER BY ordinal"
        ).fetchall() == [("body",)]


def test_s2_m3_attaches_direct_and_contextual_material_without_exact_match_gate(
    tmp_path,
):
    paper = _paper()
    direct = UnifiedTextChunk(
        chunk_id="s2chunk:123:0:800:direct",
        paper_id=paper.paper_id,
        text="Mechanistic evidence for inverse-designed multilayer optics. " * 12,
        title=paper.title,
        citation_roles=["partial_support", "method_example"],
    )
    context = UnifiedTextChunk(
        chunk_id="s2chunk:123:900:1700:context",
        paper_id=paper.paper_id,
        text="Historical and application context for optical multilayers. " * 12,
        title=paper.title,
        citation_roles=["background_context", "historical_origin"],
    )

    class FakeDiscovery:
        def discover(self, *args, **kwargs):
            return DiscoveryPortfolio(
                candidates=[
                    S2Candidate(
                        paper=paper,
                        facet_id="f1",
                        decision="retain",
                    )
                ],
                query_runs=[{"status_category": "fixture"}],
                pool_counts={"direct_relevance": 1},
            )

    class FakeRetriever:
        def retrieve(self, *args, **kwargs):
            return TextChunkRetrievalResult(
                accepted_chunks=[direct, context],
                rejected_items=[],
                query_runs=[{"status_category": "fixture"}],
                paper_ids=[paper.paper_id],
            )

    class FakeGraphBuilder:
        def expand_from_seeds(self, seeds, **kwargs):
            graph = LiteratureGraph()
            for seed in seeds:
                graph.add_node(seed, {"active_for_lineage": True})
            return graph

        @staticmethod
        def add_snippet_reference_mentions(graph, chunks):
            return None

    blueprint = {
        "sections": [
            {
                "section_id": "S01",
                "title": "Mechanisms",
                "argument_role": "Explain the mechanism.",
                "claims": [
                    {
                        "claim_id": "S01-C01",
                        "statement": "Inverse design changes multilayer optimization.",
                        "evidence_type": "mechanism",
                        "saturation_score": 0.5,
                        "supporting_text_chunk_ids": [],
                    }
                ],
            }
        ]
    }
    updated, report, _ = S2M3GapLoop(
        kb_sqlite=tmp_path / "kb.sqlite",
        discovery=FakeDiscovery(),
        retriever=FakeRetriever(),
        graph_builder=FakeGraphBuilder(),
    ).run(blueprint, max_claims=1, max_rounds=1)
    claim = updated["sections"][0]["claims"][0]
    assert direct.chunk_id in claim["supporting_text_chunk_ids"]
    assert context.chunk_id in claim["context_text_chunk_ids"]
    assert paper.paper_id in claim["citation_paper_ids"]
    assert report["summary"]["accepted_chunks"] == 2
