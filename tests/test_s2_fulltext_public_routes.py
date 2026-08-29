from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import optomind_research.m3_kb_ingest as m3
import optomind_research.s2_fulltext_acquisition as fulltext
from optomind_research.m3_kb_ingest import KBIngester, sanitize_audit_url
from optomind_research.s2_fulltext_acquisition import (
    FulltextEscalationDecision,
    S2FulltextAcquirer,
)
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import S2PaperRecord


def _paper(index: int = 1, **overrides: object) -> S2PaperRecord:
    values = {
        "paper_id": f"p{index}",
        "title": f"Public optical paper {index}",
        "year": 2025,
        "abstract": "A real abstract that must not be written by the OA stage.",
    }
    values.update(overrides)
    return S2PaperRecord(**values)


def _decision(paper: S2PaperRecord) -> FulltextEscalationDecision:
    return FulltextEscalationDecision(
        paper_id=paper.paper_id,
        should_download=True,
        reason="missing_primary_material",
        priority=1.0,
    )


def _fake_result(*, success: bool, route: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        new_chunk_ids=[f"chunk:{route}"] if success else [],
        reused_chunk_ids=[],
        new_paper_ids=[route] if success else [],
        stats={
            "candidates_tried": 1,
            "downloaded": int(success),
            "parse_failed": int(not success),
            "download_attempts": [
                {
                    "url": f"https://example.org/{route}.pdf",
                    "status": "success" if success else "download_failed",
                }
            ],
        },
    )


def _install_fake_ingester(monkeypatch, handler):
    class FakeIngester:
        def __init__(self, **_kwargs):
            pass

        def ingest_oa_candidates(self, candidates, **_kwargs):
            return handler(candidates[0])

    monkeypatch.setattr(fulltext, "KBIngester", FakeIngester)
    monkeypatch.setattr(
        fulltext, "_quarantine_non_body_chunks", lambda _db, ids: (ids, [])
    )


def test_direct_arxiv_success_stops_all_later_resolvers(monkeypatch, tmp_path):
    paper = _paper(external_ids={"ArXiv": "2501.01234"})
    calls: list[str] = []

    def handler(candidate):
        calls.append(candidate["materialization_route"])
        assert candidate["pdf_url"] == "https://arxiv.org/pdf/2501.01234.pdf"
        return _fake_result(success=True, route=paper.paper_id)

    _install_fake_ingester(monkeypatch, handler)
    for name in (
        "_resolve_unpaywall_wave",
        "_resolve_openalex_wave",
        "_resolve_crossref_wave",
        "_resolve_arxiv_wave",
    ):
        monkeypatch.setattr(
            fulltext, name, lambda *_args, _name=name, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run after direct success")
            )
        )

    result = S2FulltextAcquirer(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).acquire([(paper, _decision(paper))], max_successes=10)

    assert calls == ["public_oa:s2_direct"]
    assert result.stats["paper_outcomes"][0]["success_wave"] == "s2_direct"


def test_direct_arxiv_route_normalizes_prefix_and_spacing():
    paper = _paper(external_ids={"ArXiv": "arXiv: 2501.01234"})
    assert fulltext._direct_public_routes(paper) == [
        "https://arxiv.org/pdf/2501.01234.pdf"
    ]


def test_openalex_runs_after_unpaywall_no_route_and_then_stops(monkeypatch, tmp_path):
    paper = _paper(doi="10.1000/test")
    calls: list[str] = []
    monkeypatch.setattr(fulltext, "_direct_public_routes", lambda _paper: [])
    monkeypatch.setattr(
        fulltext, "_resolve_unpaywall_wave", lambda _doi: ([], "resolver_no_route")
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_openalex_wave",
        lambda _paper, doi="": (
            ["https://content.openalex.org/works/W1.pdf"],
            "resolved",
            {"doi": doi, "title": paper.title, "year": paper.year},
        ),
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_crossref_wave",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Crossref must not run after OpenAlex success")
        ),
    )

    def handler(candidate):
        calls.append(candidate["materialization_route"])
        return _fake_result(success=True, route=paper.paper_id)

    _install_fake_ingester(monkeypatch, handler)
    outcome = S2FulltextAcquirer(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).acquire([(paper, _decision(paper))], max_successes=10).stats[
        "paper_outcomes"
    ][0]

    assert calls == ["public_oa:openalex"]
    assert outcome["success_wave"] == "openalex"
    assert [row["wave"] for row in outcome["waves"]] == [
        "s2_direct",
        "unpaywall",
        "openalex",
    ]


def test_verified_openalex_abstract_is_recovered_after_fulltext_miss(
    monkeypatch, tmp_path
):
    paper = _paper(abstract="", doi="10.1000/abstract")
    monkeypatch.setattr(fulltext, "_direct_public_routes", lambda _paper: [])
    monkeypatch.setattr(
        fulltext, "_resolve_unpaywall_wave", lambda _doi: ([], "resolver_no_route")
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_openalex_wave",
        lambda *_args, **_kwargs: (
            [],
            "resolver_no_route",
            {
                "doi": paper.doi,
                "title": paper.title,
                "year": paper.year,
                "abstract_or_snippet": (
                    "The authors report a verified optical inverse-design "
                    "method and experimentally compare its predicted response."
                ),
            },
        ),
    )
    monkeypatch.setattr(
        fulltext, "_resolve_crossref_wave", lambda _paper: ([], "resolver_no_route", {})
    )
    monkeypatch.setattr(
        fulltext, "_resolve_arxiv_wave", lambda _paper: ([], "resolver_no_route", {})
    )
    _install_fake_ingester(
        monkeypatch,
        lambda _candidate: (_ for _ in ()).throw(
            AssertionError("no full-text URL means no download attempt")
        ),
    )

    result = S2FulltextAcquirer(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).acquire([(paper, _decision(paper))], max_successes=10)

    assert paper.abstract.startswith("The authors report")
    assert result.stats["abstracts_enriched"] == 1
    assert result.stats["abstract_enrichment_sources"] == {"openalex": 1}
    assert result.stats["paper_outcomes"][0]["abstract_enriched_source"] == "openalex"


def test_crossref_abstract_html_is_cleaned_and_existing_s2_abstract_is_not_overwritten():
    missing = _paper(abstract="")
    existing = _paper(index=2, abstract="Original S2 abstract remains authoritative.")
    record = {
        "abstract_or_snippet": (
            "<jats:p>The authors report an optical method with measured "
            "spectral agreement and explicitly state the study boundary.</jats:p>"
        )
    }

    assert fulltext._enrich_verified_abstract(missing, record, provider="crossref")
    assert "<jats" not in missing.abstract
    assert missing.route_events[-1]["provider"] == "crossref"
    assert not fulltext._enrich_verified_abstract(existing, record, provider="crossref")
    assert existing.abstract == "Original S2 abstract remains authoritative."


def test_all_resolver_failures_remain_visible_per_wave(monkeypatch, tmp_path):
    paper = _paper()
    monkeypatch.setattr(fulltext, "_direct_public_routes", lambda _paper: [])
    monkeypatch.setattr(
        fulltext, "_resolve_unpaywall_wave", lambda _doi: ([], "resolver_no_route")
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_openalex_wave",
        lambda *_args, **_kwargs: ([], "resolver_error", {}),
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_crossref_wave",
        lambda _paper: ([], "identity_mismatch", {}),
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_arxiv_wave",
        lambda _paper: ([], "resolver_no_route", {}),
    )
    _install_fake_ingester(
        monkeypatch,
        lambda _candidate: (_ for _ in ()).throw(
            AssertionError("no URL means no download attempt")
        ),
    )

    outcome = S2FulltextAcquirer(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).acquire([(paper, _decision(paper))], max_successes=10).stats[
        "paper_outcomes"
    ][0]

    assert outcome["status"] == "no_public_oa_route_after_resolvers"
    assert {row["resolver_status"] for row in outcome["waves"]} >= {
        "resolver_no_route",
        "resolver_error",
        "identity_mismatch",
    }


def test_success_budget_counts_materialized_papers_not_failed_attempts(
    monkeypatch, tmp_path
):
    papers = [
        _paper(index, s2_open_access_candidate_url=f"https://example.org/{index}.pdf")
        for index in range(1, 4)
    ]
    attempted: list[str] = []

    def handler(candidate):
        paper_id = candidate["paper_id"]
        attempted.append(paper_id)
        return _fake_result(success=paper_id == "p2", route=paper_id)

    _install_fake_ingester(monkeypatch, handler)
    monkeypatch.setattr(
        fulltext, "_resolve_unpaywall_wave", lambda _doi: ([], "resolver_no_route")
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_openalex_wave",
        lambda *_args, **_kwargs: ([], "resolver_no_route", {}),
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_crossref_wave",
        lambda _paper: ([], "resolver_no_route", {}),
    )
    monkeypatch.setattr(
        fulltext,
        "_resolve_arxiv_wave",
        lambda _paper: ([], "resolver_no_route", {}),
    )

    result = S2FulltextAcquirer(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).acquire(
        [(paper, _decision(paper)) for paper in papers],
        max_successes=1,
        # The exact attempt-order contract belongs to the historical serial
        # path; bounded concurrent semantics are covered by the dedicated
        # parallel acquisition tests.
        max_workers=1,
    )

    assert attempted == ["p1", "p2"]
    assert result.stats["successful_papers"] == 1
    assert {row["paper_id"] for row in result.skipped} == {"p3"}


def test_failed_oa_attempt_never_materializes_the_candidate_abstract(
    monkeypatch, tmp_path
):
    db = tmp_path / "kb.sqlite"
    paper = _paper(doi="10.1000/no-fulltext")
    S2KnowledgeBaseBridge(db).ingest(papers=[paper])
    monkeypatch.setattr(m3, "download_and_extract", lambda *_args, **_kwargs: ("", ""))
    candidate = fulltext._candidate_from_s2(
        paper, route_urls=["https://example.org/failure.pdf"]
    )

    result = KBIngester(
        kb_sqlite=db, download_dir=tmp_path / "downloads", require_scope_audit=False
    ).ingest_oa_candidates(
        [candidate],
        claim={
            "claim_id": "test",
            "section_id": "S1",
            "supporting_text_chunk_ids": [],
        },
        max_successes=1,
    )

    assert result.new_chunk_ids == []
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0


def test_audit_url_redacts_secret_query_values():
    safe = sanitize_audit_url(
        "https://example.org/paper.pdf?api_key=secret&token=hidden&page=2"
    )
    assert "secret" not in safe
    assert "hidden" not in safe
    assert "page=2" in safe
    assert safe.count("%5BREDACTED%5D") == 2


def test_download_failure_records_sanitized_per_url_reason(monkeypatch):
    attempts: list[dict[str, str]] = []
    monkeypatch.setattr(m3, "_try_download_bytes", lambda _url: None)

    text, source = m3.download_and_extract(
        {
            "pdf_url": (
                "https://example.org/paper.pdf?access_token=do-not-store&page=3"
            )
        },
        attempt_audit=attempts,
    )

    assert (text, source) == ("", "")
    assert attempts == [
        {
            "url": (
                "https://example.org/paper.pdf?access_token=%5BREDACTED%5D&page=3"
            ),
            "status": "download_failed",
        }
    ]


def test_downloaded_landing_page_is_distinct_from_parse_failure(monkeypatch):
    attempts: list[dict[str, str]] = []
    monkeypatch.setattr(
        m3,
        "_try_download_bytes",
        lambda _url: b"<!doctype html><html><body>Article abstract. Sign in.</body></html>",
    )

    text, source = m3.download_and_extract(
        {"pdf_url": "https://example.org/article"},
        attempt_audit=attempts,
    )

    assert (text, source) == ("", "")
    assert attempts == [{
        "url": "https://example.org/article",
        "status": "not_fulltext_page",
    }]


def test_m3_success_budget_ignores_download_that_yields_no_chunks(
    monkeypatch, tmp_path
):
    calls: list[str] = []

    def fake_download(candidate, _download_dir=None):
        calls.append(candidate["paper_id"])
        if candidate["paper_id"] == "empty":
            return "too short", "https://example.org/empty.pdf"
        return (
            "Introduction. " + ("Substantial optical full text. " * 160),
            "https://example.org/good.pdf",
        )

    monkeypatch.setattr(m3, "download_and_extract", fake_download)
    candidates = [
        {
            "candidate_id": paper_id,
            "paper_id": paper_id,
            "title": f"Paper {paper_id}",
            "doi": f"10.1000/{paper_id}",
            "pdf_url": f"https://example.org/{paper_id}.pdf",
            "llm_scope_fit": "in_domain",
            "llm_retrieval_role": "evidence_candidate",
            "llm_relevance_grade": "direct",
            "skip_abstract_fallback": True,
        }
        for paper_id in ("empty", "good", "over-budget")
    ]

    result = KBIngester(
        kb_sqlite=tmp_path / "kb.sqlite", download_dir=tmp_path / "downloads"
    ).ingest_oa_candidates(
        candidates,
        claim={
            "claim_id": "budget",
            "section_id": "S1",
            "supporting_text_chunk_ids": [],
        },
        max_successes=1,
    )

    assert calls == ["empty", "good"]
    assert result.stats["fulltext_papers_materialized"] == 1
    assert result.stats["fulltext_chunks_written"] > 0


def test_m3_sanitizes_claim_id_before_building_download_directory(
    monkeypatch, tmp_path
):
    observed: list[object] = []

    def fake_download(_candidate, download_dir=None):
        observed.append(download_dir)
        return "", ""

    monkeypatch.setattr(m3, "download_and_extract", fake_download)
    KBIngester(
        kb_sqlite=tmp_path / "kb.sqlite",
        download_dir=tmp_path / "downloads",
        require_scope_audit=False,
    ).ingest_oa_candidates(
        [{
            "candidate_id": "p1",
            "paper_id": "p1",
            "title": "Paper one",
            "skip_abstract_fallback": True,
        }],
        {
            "claim_id": "review_harness_bootstrap:paper/with*unsafe?chars",
            "section_id": "s2_first",
            "supporting_text_chunk_ids": [],
        },
        max_successes=1,
    )

    assert len(observed) == 1
    leaf = observed[0].name
    assert leaf.startswith("review_harness_bootstrap-paper-with-unsafe-chars-")
    assert not any(char in leaf for char in '<>:"/\\|?*')


# ---------------------------------------------------------------------------
# Repair 1 — Topic-scoped KB recovery: isolated_rebuild_available
# ---------------------------------------------------------------------------

def test_isolated_rebuild_report_returns_correct_status(tmp_path):
    """_isolated_rebuild_report returns status=isolated_rebuild_available, not 'failed'."""
    from optomind_research.s2_harness_bootstrap import (
        _isolated_rebuild_report,
        _relocate_stale_artifacts,
        BOOTSTRAP_SCHEMA_VERSION,
    )
    stale = _relocate_stale_artifacts(tmp_path, [], started=1.0)
    report = _isolated_rebuild_report(
        stale_artifact_dir=stale,
        runtime_kb=tmp_path / "kb.sqlite",
        base_kb=None,
        policy_path=None,
        started=1.0,
        reason="schema_version mismatch",
    )
    assert report["status"] == "isolated_rebuild_available"
    assert report["isolated_rebuild_available"] is True
    assert report["error_code"] == "s2_bootstrap_reuse_contract_mismatch"
    assert report["schema_version"] == BOOTSTRAP_SCHEMA_VERSION


def test_relocate_stale_artifacts_moves_files(tmp_path):
    """_relocate_stale_artifacts moves existing files into a timestamped subdir."""
    from optomind_research.s2_harness_bootstrap import (
        _relocate_stale_artifacts,
        _STALE_ARTIFACTS_DIRNAME,
    )
    artifact = tmp_path / "S2_BOOTSTRAP_REPORT.json"
    artifact.write_text('{"status":"ok"}', encoding="utf-8")
    stale_dir = _relocate_stale_artifacts(tmp_path, [artifact], started=1234.5)
    assert stale_dir.name.startswith(_STALE_ARTIFACTS_DIRNAME)
    assert (stale_dir / artifact.name).is_file()
    assert not artifact.is_file()


def test_relocate_stale_artifacts_ignores_missing_files(tmp_path):
    """_relocate_stale_artifacts is a no-op for paths that don't exist."""
    from optomind_research.s2_harness_bootstrap import _relocate_stale_artifacts
    missing = tmp_path / "nonexistent.json"
    stale_dir = _relocate_stale_artifacts(tmp_path, [missing], started=999.0)
    assert stale_dir.is_dir()
    assert list(stale_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Fix 1 — Replace the invalid test that imported the non-existent symbol
# build_s2_bootstrap_report.  The real isolated-rebuild path lives in
# topic_scoped_kb_stage.build_topic_scoped_kb; test that instead.
# ---------------------------------------------------------------------------

def test_build_topic_scoped_kb_isolated_rebuild_on_schema_mismatch(tmp_path):
    """build_topic_scoped_kb returns isolated_rebuild_available on schema mismatch."""
    import json
    import sqlite3
    from optomind_research.runtime.topic_scoped_kb_stage import (
        build_topic_scoped_kb,
        MANIFEST_SCHEMA_VERSION,
        _STALE_KB_DIRNAME,
    )

    # Minimal base KB (empty SQLite so the function can open it).
    base_kb = tmp_path / "base.sqlite"
    sqlite3.connect(str(base_kb)).close()

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    query_plan = tmp_path / "query_plan.json"
    query_plan.write_text(
        json.dumps({"topic": "test", "sections": []}), encoding="utf-8"
    )

    # Plant the three reserved artefacts with a wrong (stale) schema version so
    # the contract-mismatch branch fires instead of an integrity raise.
    (work_dir / "review_knowledge_base.s2.sqlite").write_text(
        "", encoding="utf-8"
    )
    (work_dir / "S2_QUERY_TELEMETRY.json").write_text("{}", encoding="utf-8")
    (work_dir / "KB_MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": "optomind.STALE",
                "reuse_contract": {},
            }
        ),
        encoding="utf-8",
    )

    result = build_topic_scoped_kb(
        query_plan_path=query_plan,
        base_kb_sqlite=base_kb,
        work_dir=work_dir,
    )

    # Contract mismatch must NOT raise — instead it returns the isolated-rebuild
    # status so the caller can decide whether to rebuild in a separate directory.
    assert result.get("status") == "isolated_rebuild_available"
    assert result.get("isolated_rebuild_available") is True
    assert result.get("error_code") == "topic_scoped_kb_reuse_contract_mismatch"

    # Stale artefacts must have been moved into a timestamped subdirectory.
    stale_dirs = [
        d for d in work_dir.iterdir() if d.name.startswith(_STALE_KB_DIRNAME)
    ]
    assert len(stale_dirs) == 1, f"Expected one stale dir, found: {list(work_dir.iterdir())}"
