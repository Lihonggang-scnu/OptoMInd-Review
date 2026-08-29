"""Local-first S2 bootstrap coverage tests.

These tests verify that a fresh harness run inspects the persistent,
topic-scoped base KB before any external S2 traffic and seals the normal
runtime artifacts from local records when coverage is demonstrated.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import optomind_research.s2_harness_bootstrap as bootstrap_module
from optomind_research.s2_harness_bootstrap import prepare_s2_harness_kb


def _query_plan() -> dict:
    return {
        "input": {"user_query": "Review broadband achromatic metalens design."},
        "output": {
            "problem_understanding": (
                "Review broadband achromatic metalenses and dispersion "
                "compensation."
            ),
            "scope_definition": {
                "main_scope": "Achromatic optical metalens physics.",
                "scope_items": [],
            },
            "lenses": ["mechanism", "imaging performance"],
            "inclusion_boundaries": ["optical metalens design"],
            "exclusion_boundaries": ["acoustic metalens"],
            "keyword_decomposition": {
                "keywords": [
                    "broadband achromatic metalens",
                    "metalens group delay dispersion",
                ]
            },
        },
    }


def _write_query(path: Path) -> Path:
    path.write_text(json.dumps(_query_plan()), encoding="utf-8")
    return path


def _write_policy(
    path: Path,
    *,
    roles: tuple[str, ...] = ("foundation", "review"),
    target_papers: tuple[int, int] = (2, 6),
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "s2_first": {
                    "enabled": True,
                    "requested_roles": list(roles),
                    "use_relevance_search": True,
                    "use_snippet_search": True,
                    "use_batch_enrichment": True,
                    "build_literature_graph": True,
                    "download_high_value_oa_without_llm_gate": True,
                },
                "standard": {
                    "accepted_s2_text_papers_per_facet": list(target_papers),
                    "graph_depth": 2,
                    "max_search_queries": 12,
                    "max_snippet_queries": 12,
                    "results_per_query": 20,
                    "snippet_results_per_query": 20,
                    "precise_snippet_results_per_paper": 5,
                    "max_precise_snippet_papers": 6,
                    "max_abstract_claim_papers": 2,
                    "max_batch_papers": 6,
                    "oa_fulltext_downloads_per_facet": [1, 3],
                },
                "graph": {
                    "seed_count": 2,
                    "reference_limit_per_seed": 1,
                    "citation_limit_per_seed": 1,
                    "recommendation_limit": 1,
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


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE papers(
            paper_id TEXT PRIMARY KEY,
            doi TEXT,
            title TEXT,
            year INTEGER,
            venue TEXT,
            quality_tier TEXT,
            query_relevance TEXT,
            search_text TEXT,
            raw_json TEXT NOT NULL,
            discovery_route TEXT NOT NULL DEFAULT 'unknown',
            materialization_route TEXT NOT NULL DEFAULT 'not_materialized',
            content_depth TEXT NOT NULL DEFAULT 'metadata',
            use_permission TEXT NOT NULL DEFAULT 'discovery_only',
            scope_fit TEXT NOT NULL DEFAULT 'unreviewed',
            route_provenance_json TEXT NOT NULL DEFAULT '{}',
            literature_roles_json TEXT NOT NULL DEFAULT '[]',
            relation_roles_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE text_chunks(
            chunk_id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            doi TEXT,
            title TEXT,
            ordinal INTEGER,
            section_path TEXT,
            char_start INTEGER,
            char_end INTEGER,
            char_count INTEGER,
            boilerplate_score REAL,
            text TEXT,
            search_text TEXT,
            raw_json TEXT NOT NULL,
            route_provenance_json TEXT NOT NULL DEFAULT '{}',
            content_depth TEXT NOT NULL DEFAULT 'fulltext',
            use_permission TEXT NOT NULL DEFAULT 'contextual_or_qualified_support',
            context_complete INTEGER NOT NULL DEFAULT 1,
            allowed_claim_kinds_json TEXT NOT NULL DEFAULT '[]',
            scope_fit TEXT NOT NULL DEFAULT 'unreviewed',
            relation_roles_json TEXT NOT NULL DEFAULT '[]'
        );
        """
    )


def _insert_paper(
    conn: sqlite3.Connection,
    *,
    paper_id: str,
    title: str,
    search_text: str,
    roles: list[str] | None = None,
    raw: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO papers(
            paper_id, doi, title, year, venue, quality_tier,
            query_relevance, search_text, raw_json, literature_roles_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            paper_id,
            f"10.1000/{paper_id}",
            title,
            2024,
            "Optics Journal",
            "fixture",
            "direct",
            search_text,
            json.dumps(raw or {}, ensure_ascii=False),
            json.dumps(roles or [], ensure_ascii=False),
        ),
    )


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    paper_id: str,
    text: str,
    content_depth: str = "fulltext",
    use_permission: str = "factual_support",
    source_kind: str = "s2_body_snippet",
    roles: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO text_chunks(
            chunk_id, paper_id, doi, title, ordinal, section_path,
            char_start, char_end, char_count, boilerplate_score,
            text, search_text, raw_json, route_provenance_json,
            content_depth, use_permission, context_complete,
            allowed_claim_kinds_json, scope_fit, relation_roles_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chunk_id,
            paper_id,
            f"10.1000/{paper_id}",
            "Broadband achromatic metalens",
            0,
            "Results",
            0,
            max(1, len(text)),
            len(text),
            0.0,
            text,
            text,
            json.dumps(
                {
                    "content_depth": content_depth,
                    "use_permission": use_permission,
                    "scope_fit": "direct",
                    "context_complete": True,
                    "source_kind": source_kind,
                    "route_provenance": {
                        "discovery_route": "semantic_scholar_snippet_search",
                        "materialization_route": (
                            "s2_structured_body_snippet"
                            if source_kind == "s2_body_snippet"
                            else "public_oa_fulltext"
                        ),
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "route_events": [
                        {
                            "event": "local_fixture",
                            "discovery_route": "semantic_scholar_snippet_search",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            content_depth,
            use_permission,
            1,
            json.dumps(["claim", "comparison"], ensure_ascii=False),
            "direct",
            json.dumps(roles or [], ensure_ascii=False),
        ),
    )


def _rich_base_kb(
    path: Path,
    *,
    include_group_delay: bool = True,
    roles: list[str] | None = None,
) -> Path:
    conn = sqlite3.connect(path)
    try:
        _schema(conn)
        with conn:
            text = (
                "Review broadband achromatic metalens design; the mechanism "
                "and imaging performance of optical metalens designs are "
                "analyzed."
            )
            if include_group_delay:
                text = text.replace(
                    "analyzed.",
                    "analyzed with group delay dispersion control.",
                )
            for index in range(1, 4):
                _insert_paper(
                    conn,
                    paper_id=f"p{index}",
                    title=(
                        f"Broadband achromatic metalens {index}: "
                        "group delay dispersion and imaging"
                    ),
                    search_text=text,
                    roles=roles,
                    raw={
                        "abstract": (
                            "Broadband achromatic metalens mechanism, "
                            "imaging performance, and optical design."
                        )
                    },
                )
            for index, source_kind in enumerate(
                ("s2_body_snippet", "oa_fulltext", "local_pdf"),
                start=1,
            ):
                _insert_chunk(
                    conn,
                    chunk_id=f"c{index}-a",
                    paper_id=f"p{index}",
                    text=text,
                    source_kind=source_kind,
                )
                _insert_chunk(
                    conn,
                    chunk_id=f"c{index}-b",
                    paper_id=f"p{index}",
                    text=text,
                    source_kind=source_kind,
                )
    finally:
        conn.close()
    return path


def _empty_base_kb(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        _schema(conn)
    finally:
        conn.close()
    return path


def _monkeypatch_network_bombs(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    def bomb(*args, **kwargs):
        calls.append("network_constructor")
        raise AssertionError("network constructor must not be called")

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", bomb)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", bomb)
    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", bomb)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", bomb)


def _artifact_paths(work: Path) -> dict[str, Path]:
    return {
        "report": work / "S2_BOOTSTRAP_REPORT.json",
        "manifest": work / "KB_MANIFEST.json",
        "runtime": work / "review_knowledge_base.s2.sqlite",
        "graph": work / "S2_LITERATURE_GRAPH.json",
        "telemetry": work / "S2_QUERY_TELEMETRY.json",
        "material_flow": work / "S2_MATERIAL_FLOW_LEDGER.json",
    }


def test_zero_network_sufficient_local_kb_seals_normal_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "zero-network"
    root.mkdir()
    query = _write_query(root / "query.json")
    policy = _write_policy(root / "policy.json")
    base = _rich_base_kb(root / "base.sqlite")
    work = root / "work"
    calls: list[str] = []
    _monkeypatch_network_bombs(monkeypatch, calls)

    report = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
    )

    assert report["status"] == "completed"
    assert report["local_coverage_decision"] == "sufficient"
    assert report["local_first"] is True
    assert report["local_cache_reuse"] is True
    assert report["executed_search_queries"] == []
    assert report["external_query_runs"] == []
    assert report["network_requests_avoided"] == [
        "broad_discovery",
        "broad_snippet_search",
        "precise_followup",
        "batch_enrichment",
        "graph_expansion",
        "oa_acquisition",
    ]
    assert report["reused_local_paper_count"] == 3
    assert report["reused_local_chunk_count"] == 6
    assert calls == []

    paths = _artifact_paths(work)
    for path in paths.values():
        assert path.is_file(), path
    assert bootstrap_module._report_hash_is_valid(report)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert bootstrap_module._manifest_hash_is_valid(manifest)
    assert report["runtime_kb_sha256"] == manifest["runtime_kb_sha256"]
    assert report["telemetry_sha256"] == manifest["telemetry_sha256"]
    assert bootstrap_module._sha256_file(paths["graph"]) == report["graph_sha256"]
    assert (
        bootstrap_module._sha256_file(paths["material_flow"])
        == report["material_flow_ledger_sha256"]
    )
    assert report["kb_manifest_sha256"] == manifest["manifest_sha256"]
    telemetry = json.loads(paths["telemetry"].read_text(encoding="utf-8"))
    assert telemetry["total_query_count"] == 6
    assert all(event["status_category"] == "skipped" for event in telemetry["events"])
    assert all(event["ok"] for event in telemetry["events"])

    conn = sqlite3.connect(paths["runtime"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 6
    finally:
        conn.close()

    material_flow = json.loads(
        paths["material_flow"].read_text(encoding="utf-8")
    )
    assert material_flow["summary"]["admitted_paper_count"] == 3
    assert all(
        row["material_status"] in {"s2_body", "oa_fulltext"}
        for row in material_flow["papers"]
    )


def test_partial_local_coverage_only_uncovered_query_and_role_go_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "partial"
    root.mkdir()
    query = _write_query(root / "query.json")
    policy = _write_policy(
        root / "policy.json",
        roles=("foundation", "review"),
        target_papers=(1, 3),
    )
    base = _rich_base_kb(
        root / "base.sqlite",
        include_group_delay=False,
        roles=["foundation"],
    )
    work = root / "work"

    captured: dict = {
        "discovery_queries": [],
        "roles": [],
        "snippet_queries": [],
        "snippet_roles": [],
        "precise_roles": [],
    }

    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, facets, **kwargs):
            captured["discovery_queries"] = list(facets[0].queries)
            captured["roles"] = list(facets[0].requested_roles)
            return bootstrap_module.DiscoveryPortfolio(
                candidates=[],
                query_runs=[
                    {
                        "query": facets[0].queries[0],
                        "status_code": 200,
                        "status_category": "ok",
                    }
                ],
                pool_counts={},
                rejected_count=0,
            )

    class FakeRetriever:
        def __init__(self, **kwargs):
            pass

        def retrieve(self, queries, **kwargs):
            captured["snippet_queries"] = list(queries)
            captured["snippet_roles"] = list(
                kwargs.get("requested_roles") or []
            )
            return bootstrap_module._empty_chunks()

        def retrieve_precise_missing_papers(self, papers, **kwargs):
            captured["precise_papers"] = [p.paper_id for p in papers]
            captured["precise_roles"] = list(
                kwargs.get("requested_roles") or []
            )
            return bootstrap_module._empty_chunks()

    class BombGraph:
        def __init__(self, *args, **kwargs):
            raise AssertionError("graph must not run without network papers")

    class BombFulltext:
        def __init__(self, *args, **kwargs):
            raise AssertionError("OA must not run without network papers")

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", FakeDiscovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", FakeRetriever)
    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", BombGraph)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", BombFulltext)

    report = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
    )

    assert report["local_coverage_decision"] == "partial"
    assert report["local_cache_reuse"] is True
    assert report["executed_search_queries"] == [
        "metalens group delay dispersion"
    ]
    assert captured["discovery_queries"] == [
        "metalens group delay dispersion"
    ]
    assert captured["roles"] == ["review"]
    assert captured["snippet_queries"] == ["metalens group delay dispersion"]
    assert captured["snippet_roles"] == ["review"]
    assert "precise_papers" not in captured
    assert "broadband achromatic metalens" not in " ".join(
        [*captured["discovery_queries"], *captured["snippet_queries"]]
    )
    assert report["network_requests_avoided"] == [
        "broad_discovery_covered_queries",
        "broad_snippet_covered_queries",
    ]
    executed = " ".join(
        str(run.get("query") or "") for run in report["external_query_runs"]
    )
    assert "broadband achromatic metalens" not in executed
    assert "metalens group delay dispersion" in executed


def test_empty_or_insufficient_base_preserves_first_run_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "insufficient"
    root.mkdir()
    query = _write_query(root / "query.json")
    policy = _write_policy(root / "policy.json", target_papers=(1, 3))
    base = _empty_base_kb(root / "base.sqlite")
    work = root / "work"

    captured: dict = {"discovery_queries": [], "roles": [], "snippet_queries": []}

    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, facets, **kwargs):
            captured["discovery_queries"] = list(facets[0].queries)
            captured["roles"] = list(facets[0].requested_roles)
            return bootstrap_module.DiscoveryPortfolio(
                candidates=[],
                query_runs=[
                    {
                        "query": facet_query,
                        "status_code": 200,
                        "status_category": "ok",
                    }
                    for facet_query in facets[0].queries
                ],
                pool_counts={},
                rejected_count=0,
            )

    class FakeRetriever:
        def __init__(self, **kwargs):
            pass

        def retrieve(self, queries, **kwargs):
            captured["snippet_queries"] = list(queries)
            return bootstrap_module._empty_chunks()

        def retrieve_precise_missing_papers(self, papers, **kwargs):
            return bootstrap_module._empty_chunks()

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", FakeDiscovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", FakeRetriever)

    report = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
    )

    assert report["local_coverage_decision"] == "insufficient"
    assert report["local_first"] is False
    assert report["local_cache_reuse"] is False
    assert report["executed_search_queries"] == report["search_queries"]
    assert captured["discovery_queries"] == report["search_queries"]
    assert captured["roles"] == ["foundation", "review"]
    assert captured["snippet_queries"] == report["search_queries"][:12]
    assert report["status"] == "needs_more_literature"
    assert report["external_query_runs"]


def test_local_reuse_preserves_provenance_and_permission_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "provenance"
    root.mkdir()
    query = _write_query(root / "query.json")
    policy = _write_policy(root / "policy.json", target_papers=(1, 3))
    base = root / "base.sqlite"

    conn = sqlite3.connect(base)
    try:
        _schema(conn)
        with conn:
            text = (
                "Review broadband achromatic metalens design; the mechanism "
                "and imaging performance of optical metalens designs are "
                "analyzed with group delay dispersion control."
            )
            _insert_paper(
                conn,
                paper_id="p-factual",
                title="Broadband achromatic metalens factual study",
                search_text=text,
            )
            _insert_paper(
                conn,
                paper_id="p-qualified",
                title="Broadband achromatic metalens qualified study",
                search_text=text,
            )
            _insert_paper(
                conn,
                paper_id="p-weak",
                title="Broadband achromatic metalens abstract record",
                search_text=text,
            )
            _insert_chunk(
                conn,
                chunk_id="c-factual",
                paper_id="p-factual",
                text=text,
                content_depth="fulltext",
                use_permission="factual_support",
                source_kind="s2_body_snippet",
            )
            _insert_chunk(
                conn,
                chunk_id="c-qualified",
                paper_id="p-qualified",
                text=text,
                content_depth="structured_snippet",
                use_permission="contextual_or_qualified_support",
                source_kind="s2_body_snippet",
            )
            _insert_chunk(
                conn,
                chunk_id="c-weak",
                paper_id="p-weak",
                text="Broadband achromatic metalens abstract only.",
                content_depth="metadata",
                use_permission="discovery_only",
                source_kind="s2_abstract_snippet",
            )
    finally:
        conn.close()

    work = root / "work"
    _monkeypatch_network_bombs(monkeypatch, [])
    report = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
    )

    assert report["local_coverage_decision"] == "sufficient"
    assert report["local_coverage"]["counts"]["total_usable_chunks"] == 2
    assert report["evidence"]["factual_support_chunk_count"] == 1
    assert report["evidence"]["qualified_support_chunk_count"] == 1

    runtime = work / "review_knowledge_base.s2.sqlite"
    conn = sqlite3.connect(runtime)
    conn.row_factory = sqlite3.Row
    try:
        chunks = {
            str(row["chunk_id"]): dict(row)
            for row in conn.execute("SELECT * FROM text_chunks").fetchall()
        }
    finally:
        conn.close()

    assert chunks["c-factual"]["content_depth"] == "fulltext"
    assert chunks["c-factual"]["use_permission"] == "factual_support"
    assert chunks["c-factual"]["scope_fit"] == "direct"
    assert "semantic_scholar_snippet_search" in chunks["c-factual"][
        "route_provenance_json"
    ]
    assert chunks["c-qualified"]["content_depth"] == "structured_snippet"
    assert (
        chunks["c-qualified"]["use_permission"]
        == "contextual_or_qualified_support"
    )
    assert chunks["c-weak"]["content_depth"] == "metadata"
    assert chunks["c-weak"]["use_permission"] == "discovery_only"
    raw_weak = json.loads(chunks["c-weak"]["raw_json"])
    assert raw_weak["topic_scope_audit"]["use_permission"] != "factual_support"
