from __future__ import annotations

import json
import re
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


try:
    import ftfy  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - lightweight test environment
    ftfy_module = types.ModuleType("ftfy")
    ftfy_module.fix_text = lambda value: value
    sys.modules["ftfy"] = ftfy_module

from optomind_research.runtime.coverage_decision_contract import (  # noqa: E402
    build_uncovered_query_targets,
    evaluate_candidate_topic_affinity,
)
from optomind_research.runtime.s2_policy_runtime import load_s2_policy  # noqa: E402
from optomind_research.runtime.topic_scoped_kb_stage import (  # noqa: E402
    TopicScopedKBStage,
    derive_topic_scope_contract,
)
from optomind_research.s2_harness_bootstrap import (  # noqa: E402
    _retain_validated_snippet_parents,
)
from optomind_research.s2_intelligence_gateway import (  # noqa: E402
    DISCOVERY_FIELDS,
    ENRICHMENT_FIELDS,
    S2AvailabilityError,
    S2GatewayResponse,
    S2IntelligenceGateway,
)
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk  # noqa: E402


def _section() -> dict:
    return {
        "section_id": "S01",
        "title": "Metasurface wavefront control",
        "scope_description": "Metasurface beam imaging and metalens physics.",
        "topic_identity": {
            "valid": True,
            "fingerprint": "topic-metasurface-1",
            "scientific_object": "metasurface flat optics and wavefront control",
            "core_anchor_tokens": ["metasurface", "wavefront", "control"],
            "supporting_anchor_tokens": ["metalens", "beam", "imaging", "phase"],
        },
    }


def _query_plan() -> dict:
    return {
        "input": {"user_query": "Review broadband achromatic metalens design."},
        "output": {
            "problem_understanding": (
                "Review broadband achromatic metalenses and their dispersion compensation mechanisms."
            ),
            "scope_definition": {
                "main_scope": "Achromatic optical metalens physics, imaging, and fabrication.",
                "scope_items": ["Group delay engineering", "Large aperture metalens fabrication"],
            },
            "lenses": ["mechanism", "manufacturing", "imaging performance"],
            "inclusion_boundaries": ["optical metalens design"],
            "exclusion_boundaries": ["acoustic metalens", "radiative cooling"],
            "keyword_decomposition": {
                "keywords": [
                    "broadband achromatic metalens",
                    "metalens group delay dispersion",
                ]
            },
        },
    }


def _write_plan(tmp_path: Path) -> Path:
    path = tmp_path / "query_plan.json"
    path.write_text(json.dumps(_query_plan()), encoding="utf-8")
    return path


def _write_base_kb(tmp_path: Path) -> Path:
    path = tmp_path / "base.sqlite"
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
                title TEXT, ordinal INTEGER, section_path TEXT,
                char_start INTEGER, char_end INTEGER, char_count INTEGER,
                boilerplate_score REAL, text TEXT, search_text TEXT,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE concepts(label TEXT);
            """
        )
    return path


def test_query_generation_is_concise_but_keeps_object_and_components() -> None:
    targets = build_uncovered_query_targets(
        _section(),
        roles=["foundation"],
        components=["beam imaging", "metalens"],
        existing_targets=[
            {
                "role": "foundation",
                "query": (
                    "Physical Foundations of Metasurface Flat Optics and Wavefront "
                    "Control beam imaging metalens metasurface conceptual basis principles history"
                ),
                "components": ["beam imaging", "metalens"],
            }
        ],
    )
    query = targets[0]["query"]
    terms = re.findall(r"[a-z0-9][a-z0-9+./-]{2,}", query.casefold())
    assert len(query) <= 120
    assert len(terms) <= 10
    assert {"metasurface", "wavefront", "beam", "imaging", "metalens"} <= set(terms)
    assert "conceptual" not in terms
    assert "history" not in terms


def test_unrelated_same_role_cached_candidate_does_not_suppress_search(tmp_path: Path) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry

    portfolio = tmp_path / "ARTICLE_EVIDENCE_PORTFOLIO.json"
    portfolio.write_text(
        json.dumps(
            {
                "topic_fingerprint": "topic-metasurface-1",
                "candidates": [
                    {
                        "material_identity": "doi:10.1000/tweezers",
                        "roles": ["foundation"],
                        "title": "Roadmap for Optical Tweezers",
                        "abstract": "Optical trapping and particle manipulation in biological systems.",
                        "query_texts": ["metasurface wavefront control principles"],
                        "decision": "approved",
                        "scope_fit": "adjacent",
                        "topic_fingerprint": "topic-metasurface-1",
                    }
                ],
                "audits": {
                    "doi:10.1000/tweezers": {
                        "decision": "approved",
                        "scope_fit": "adjacent",
                        "role_fit": ["foundation"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        section_id="S01",
        section_data=_section(),
        work_dir=tmp_path,
        article_evidence_portfolio_path=portfolio,
        phase3_coverage_request={},
        targeted_missing_roles=[],
        targeted_queries=[],
        min_mode_max_queries=4,
        adaptive_coverage_enabled=False,
    )
    assert registry._article_candidates_for_role(
        ctx,
        "foundation",
        ["metasurface wavefront control principles"],
    ) == []


def test_optical_tweezers_fails_topic_quality_gate() -> None:
    quality = evaluate_candidate_topic_affinity(
        {
            "title": "Roadmap for Optical Tweezers",
            "abstract": "Optical trapping, force calibration, and particle manipulation.",
        },
        _section(),
        queries=["metasurface wavefront control principles"],
    )
    assert quality["accepted"] is False
    assert quality["scope_fit"] == "out_of_scope"


def test_openalex_optics_filler_is_not_registered(tmp_path: Path, monkeypatch) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    (tmp_path / "SECTION_COVERAGE_PLAN.json").write_text(
        json.dumps({"roles": {"foundation": {"priority": "required"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_search_s2_first", lambda *_args: [])
    monkeypatch.setattr(registry, "_search_semantic_scholar", lambda *_args: [])
    monkeypatch.setattr(
        registry,
        "_search_openalex",
        lambda *_args: [
            {
                "title": "Roadmap for Optical Tweezers",
                "abstract": "Optical trapping and particle manipulation with wavefront control.",
                "is_oa": True,
                "openalex_id": "https://openalex.org/W-filler",
                "backends": ["openalex"],
                "query_texts": ["metasurface wavefront control principles"],
            }
        ],
    )
    ctx = SectionCoverageContext(
        section_id="S01",
        section_data={**_section(), "required_roles": ["foundation"]},
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
        s2_first_enabled=True,
    )
    result = json.loads(
        registry._make_search_oa_candidates(ctx)(
            "foundation", json.dumps(["metasurface wavefront control principles"]), 2
        )
    )
    assert result["candidate_count"] == 0
    assert result["backend_stats"]["quality_gate_rejections"] == 1


def test_key_discovery_prefers_project_files_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    import tools.academic_backends.semantic_scholar_backend as backend

    files = [
        tmp_path / "project-new.key",
        tmp_path / "project-pool.key",
        tmp_path / "desktop-new.key",
        tmp_path / "desktop-legacy.key",
    ]
    files[0].write_text("preferred-key\nsame-key", encoding="utf-8")
    files[1].write_text("same-key\nproject-pool-key", encoding="utf-8")
    files[2].write_text("desktop-new-key\nproject-pool-key", encoding="utf-8")
    files[3].write_text("desktop-legacy-key\npreferred-key", encoding="utf-8")
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEYS", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEYS_FILE", raising=False)
    monkeypatch.setattr(backend, "NEW_PROJECT_S2_KEYS_FILE", files[0])
    monkeypatch.setattr(backend, "DEFAULT_S2_KEYS_FILE", files[1])
    monkeypatch.setattr(backend, "NEW_DESKTOP_S2_KEYS_FILE", files[2])
    monkeypatch.setattr(backend, "LEGACY_S2_KEYS_FILE", files[3])
    assert backend._api_keys() == [
        "preferred-key",
        "same-key",
        "project-pool-key",
        "desktop-new-key",
        "desktop-legacy-key",
    ]


def test_search_uses_lightweight_discovery_and_bounded_enrichment_fields() -> None:
    calls: list[tuple[str, dict]] = []

    class StubTransport:
        def request_json(self, method: str, url: str, **kwargs):
            calls.append((method, kwargs))
            if method == "GET":
                return S2GatewayResponse(
                    ok=True,
                    status_code=200,
                    payload={
                        "data": [
                            {
                                "paperId": "p1",
                                "title": "Metasurface wavefront control",
                                "abstract": "Metasurface wavefront control abstract.",
                                "year": 2024,
                                "externalIds": {},
                                "isOpenAccess": False,
                                "openAccessPdf": {},
                            }
                        ]
                    },
                )
            return S2GatewayResponse(ok=True, status_code=200, payload=[])

    gateway = S2IntelligenceGateway(transport=StubTransport())  # type: ignore[arg-type]
    papers, response = gateway.search_papers("metasurface wavefront", limit=3)
    assert response.ok and papers
    assert calls[0][1]["params"]["fields"] == DISCOVERY_FIELDS
    assert "tldr" not in calls[0][1]["params"]["fields"]
    assert "embedding.specter_v2" not in calls[0][1]["params"]["fields"]
    gateway.batch_papers(["p1"])
    assert calls[1][1]["params"]["fields"] == ENRICHMENT_FIELDS


def test_s2_availability_failure_does_not_call_legacy_adapter(tmp_path: Path, monkeypatch) -> None:
    import optomind_research.runtime.section_coverage_tool_registry as registry
    from optomind_research.runtime.tool_provider import SectionCoverageContext

    legacy_calls: list[bool] = []
    monkeypatch.setattr(
        registry,
        "_search_s2_first",
        lambda *_args: (_ for _ in ()).throw(S2AvailabilityError("HTTP 429")),
    )
    monkeypatch.setattr(
        registry,
        "_search_semantic_scholar",
        lambda *_args: legacy_calls.append(True) or [],
    )
    monkeypatch.setattr(registry, "_search_openalex", lambda *_args: [])
    ctx = SectionCoverageContext(
        section_id="S01",
        section_data={"required_roles": ["foundation"], "topic_identity": {"valid": False}},
        kb_sqlite=None,
        temp_kb_sqlite=tmp_path / "stage.sqlite",
        work_dir=tmp_path,
        s2_first_enabled=True,
    )
    result = json.loads(
        registry._make_search_oa_candidates(ctx)(
            "foundation", json.dumps(["metasurface principles"]), 2
        )
    )
    assert legacy_calls == []
    assert result["backend_stats"]["semantic_scholar_result_state"] == "transport_failure"
    assert result["backend_stats"]["semantic_scholar_transport_failure"] == 1


def test_cap_retains_validated_snippet_parent_deterministically() -> None:
    class Stage:
        @staticmethod
        def accepts_s2_paper(_paper, *, related_chunks=()):
            return True

    discovery = [
        S2PaperRecord(paper_id="discovery-1", title="Older relevant paper", citation_count=100),
        S2PaperRecord(paper_id="discovery-2", title="Second relevant paper", citation_count=90),
    ]
    resolved = [
        S2PaperRecord(
            paper_id="canonical-snippet-parent",
            corpus_id=987,
            title="Metasurface wavefront control",
            citation_count=1,
        )
    ]
    chunk = UnifiedTextChunk(
        chunk_id="s2chunk:987:0",
        paper_id="CorpusId:987",
        corpus_id=987,
        title="Metasurface wavefront control",
        text="validated snippet text",
    )
    selected, detail = _retain_validated_snippet_parents(
        discovery_papers=discovery,
        resolved_papers=resolved,
        chunks=[chunk],
        stage=Stage(),  # type: ignore[arg-type]
        maximum_papers=2,
    )
    assert "canonical-snippet-parent" in {paper.paper_id for paper in selected}
    assert detail["validated_parent_ids"] == ["canonical-snippet-parent"]


def test_structured_snippet_alias_is_inserted_with_factual_permission(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 2,
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 1],
                    "graph_depth": 0,
                },
                "evidence": {
                    "minimum_factual_papers": 1,
                    "minimum_factual_chunks": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    base = _write_base_kb(tmp_path)
    plan = _query_plan()
    contract = derive_topic_scope_contract(plan)
    stage = TopicScopedKBStage(
        query_plan_path=plan_path,
        base_kb_sqlite=base,
        work_dir=tmp_path,
        policy=load_s2_policy(policy_path),
        scope_contract=contract,
    )
    stage.create_overlay()
    paper = S2PaperRecord(
        paper_id="s2-paper-987",
        corpus_id=987,
        title="Broadband achromatic metalens imaging",
        doi="10.1000/metalens-987",
        year=2024,
        abstract=(
            "Broadband achromatic metalens imaging uses group delay and dispersion "
            "engineering for optical phase control."
        ),
    )
    chunk = UnifiedTextChunk(
        chunk_id="s2chunk:987:0:600",
        paper_id="CorpusId:987",
        corpus_id=987,
        title=paper.title,
        text=(
            "Broadband achromatic metalens imaging uses group delay dispersion "
            "engineering to control optical phase and focal response. "
        ) * 10,
        section="Mechanism",
        content_depth="structured_snippet",
        text_provenance="s2_body_snippet",
        context_complete=True,
        scope_fit="direct",
        raw_metadata={
            "s2_item": {
                "paper": {
                    "paperId": "CorpusId:987",
                    "corpusId": 987,
                    "title": paper.title,
                }
            }
        },
    )
    report = stage.ingest_s2(papers=[paper], chunks=[chunk])
    assert report["papers_accepted"] == 1
    assert report["chunks_accepted"] == 1
    assert report["identity_rebindings"][0]["canonical_parent_id"] == "s2-paper-987"
    with sqlite3.connect(stage.runtime_kb) as conn:
        row = conn.execute(
            "SELECT paper_id, content_depth, use_permission, scope_fit "
            "FROM text_chunks WHERE chunk_id=?",
            (chunk.chunk_id,),
        ).fetchone()
    assert row == ("s2-paper-987", "structured_snippet", "factual_support", "direct")
