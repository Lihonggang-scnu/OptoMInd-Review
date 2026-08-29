"""Offline tests for supplementary policy consumption at the S2 boundary."""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import optomind_research.s2_harness_bootstrap as bootstrap_module
from optomind_research.runtime.supplementary_retrieval_contract import (
    DEFAULT_PORTFOLIO_LIMITS,
    GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    ContextRegistry,
    SupplementaryRetrievalTask,
)
from optomind_research.runtime.supplementary_retrieval_pipeline import (
    build_supplementary_query_plan,
)
from optomind_research.s2_harness_bootstrap import prepare_s2_harness_kb
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk
from optomind_research.s2_text_chunk_retriever import TextChunkRetrievalResult


@pytest.fixture
def tmp_path(request):
    """Sandbox-safe temporary directory (pytest's default creates ACL-blocked dirs)."""
    base = Path(tempfile.gettempdir()) / "optomind-supplementary-tmp"
    base.mkdir(exist_ok=True)
    path = base / f"{request.node.name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path


def _registry() -> ContextRegistry:
    registry = ContextRegistry()
    registry.set("user_question", "How do radiative cooling multilayers compare?")
    registry.set(
        "dynamic_axes",
        [{"axis_id": "Q01", "description": "multilayer mechanism"}],
    )
    registry.set(
        "section_task",
        {"section_id": "S01", "title": "Mechanism", "task": "Explain physics."},
    )
    registry.set(
        "target_claim_or_sentence",
        {"claim_id": "C1", "statement": "Cooling power exceeds 60 W/m2."},
    )
    registry.set("argument_role", "mechanism_explanation")
    registry.set(
        "bound_papers_and_quotes",
        [{"paper_id": "p1", "quote": "emissivity 0.95"}],
    )
    registry.set("reviewer_feedback", {"mentor": "needs direct measurement support"})
    registry.set("author_revision_history", [{"revision": 1, "outcome": "still_open"}])
    registry.set("missing_fact_units", ["cooling_power_measured"])
    registry.set(
        "required_material_strength",
        {"minimum": "factual_support", "abstract_ceiling": "background_only"},
    )
    registry.set("retrieval_success_criteria", ["has_measured_cooling_power"])
    registry.set("existing_paper_identities", ["doi:10.1/example"])
    registry.set("historical_queries", [])
    registry.set("concurrent_queries", [])
    registry.set(
        "current_review_structure",
        {
            "existing_sections": [{"section_id": "S01"}],
            "new_sections": [],
            "new_subsections_per_existing_section": {},
        },
    )
    registry.set(
        "paper_introduction_conclusion_excerpts",
        {
            "current_paper_introduction_excerpt": "Radiative cooling is emerging.",
            "current_paper_conclusion_excerpt": "Fabrication challenges remain.",
        },
    )
    registry.set(
        "whole_review_feedback",
        {"section_count": 8, "uncovered_roles": ["boundary"]},
    )
    registry.set(
        "visual_slots",
        [{"slot_id": "V01", "role": "mechanism_anchor", "section_id": "S01"}],
    )
    registry.set("visual_gaps", ["mechanism_anchor_figure_missing"])
    registry.set(
        "topic_scope",
        {
            "topic": "radiative cooling multilayers",
            "main_scope": "optical multilayer radiative cooling",
            "lenses": ["mechanism", "fabrication"],
            "inclusion_boundaries": ["optical multilayer films"],
            "exclusion_boundaries": ["acoustic metalens"],
            "scope_items": ["group delay engineering"],
        },
    )
    registry.set(
        "materialization_policy",
        {
            "priority": ["s2_structured_body", "public_oa_fulltext", "abstract_claim"],
            "abstract_background_only": True,
        },
    )
    registry.set("portfolio_limits", dict(DEFAULT_PORTFOLIO_LIMITS))
    return registry.freeze()


def _task(
    gap_type: str = "claim_evidence_gap",
    *,
    task_id: str = "task-1",
    metadata: dict | None = None,
) -> SupplementaryRetrievalTask:
    return SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=GAP_TYPE_REQUIRED_CONTEXT_FIELDS[gap_type],
        source_provenance={"producer": "test", "stage": "s2-boundary"},
        success_criteria=("has_adequate_evidence",),
        material_requirements=("s2_structured_body",),
        retrieval_queries=("bounded supplementary gap query",),
        visual_route=(gap_type == "visual_material_gap"),
        metadata=metadata or {},
    )


def _ordinary_plan() -> dict:
    return {
        "input": {"user_query": "Review broadband achromatic metalens design."},
        "output": {
            "problem_understanding": (
                "Review broadband achromatic metalenses and dispersion compensation."
            ),
            "scope_definition": {
                "main_scope": "Achromatic optical metalens physics and imaging.",
                "scope_items": ["Group delay engineering"],
            },
            "lenses": ["mechanism", "imaging performance"],
            "inclusion_boundaries": ["optical metalens design"],
            "exclusion_boundaries": ["acoustic metalens"],
            "keyword_decomposition": {
                "keywords": ["broadband achromatic metalens"]
            },
        },
    }


def _supplementary_plan(*, gap_type: str, metadata: dict | None = None) -> dict:
    registry = _registry()
    task = _task(gap_type, task_id=f"plan-{gap_type}", metadata=metadata)
    resolved = registry.resolve(task.context_refs)
    records = [{
        "query_id": "q1",
        "text": "measured cooling power multilayer inverse design",
        "decision": "keep",
    }]
    return build_supplementary_query_plan(task, resolved, records)


def _write_policy(
    path: Path,
    *,
    graph_depth: int = 0,
    roles: tuple[str, ...] = ("review", "foundation"),
    oa_enabled: bool = True,
    use_snippet_search: bool = True,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "s2_first": {
                    "enabled": True,
                    "requested_roles": list(roles),
                    "download_high_value_oa_without_llm_gate": oa_enabled,
                    "use_snippet_search": use_snippet_search,
                },
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 3],
                    "graph_depth": graph_depth,
                    "max_search_queries": 12,
                    "max_snippet_queries": 12,
                    "precise_snippet_results_per_paper": 10,
                    "max_precise_snippet_papers": 3,
                    "max_abstract_claim_papers": 2,
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


def _write_empty_base(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
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
                raw_json TEXT NOT NULL
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
                raw_json TEXT NOT NULL
            );
            """
        )
    return path


def _paper(
    index: int = 1,
    title: str | None = None,
    *,
    current_run: bool = False,
    abstract: str | None = None,
) -> S2PaperRecord:
    effective_title = title or (
        "Measured cooling power of inverse-designed multilayer structures"
    )
    if abstract is not None:
        effective_abstract = abstract
    elif title is None:
        effective_abstract = (
            "We compare measured cooling power for multilayer inverse-designed "
            "films and report broadband achromatic metalens imaging with "
            "group delay dispersion across the visible spectrum."
        )
    else:
        effective_abstract = (
            "We analyze daily equity index returns, volatility clustering, and "
            "forecast errors for stock market time series."
        )
    route_events = (
        [{"route": "s2_reference", "current_run": True}]
        if current_run
        else []
    )
    return S2PaperRecord(
        paper_id=f"p{index}",
        corpus_id=1000 + index,
        doi=f"10.1000/metalens-{index:03d}",
        title=effective_title,
        abstract=effective_abstract,
        year=2024,
        is_oa=True,
        s2_open_access_candidate_url=f"https://example.test/p{index}.pdf",
        route_events=route_events,
    )


def _chunk() -> UnifiedTextChunk:
    return UnifiedTextChunk(
        chunk_id="s2chunk:test:0:100:body",
        paper_id="p1",
        corpus_id=1001,
        doi="10.1000/metalens-001",
        title="Measured cooling power of inverse-designed multilayer structures",
        text=(
            "measured cooling power multilayer inverse design body evidence "
            "near-field fidelity far-field prediction"
        ),
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


class _FakeSemanticUsage:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.request_count = 0
        self.embed_calls = 0
        self.vector_count = 0
        self.failure_count = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "request_count": self.request_count,
            "embed_calls": self.embed_calls,
            "vector_count": self.vector_count,
            "failure_count": self.failure_count,
        }


class _FakeSemanticEngine:
    """Term-unit vectors: optical/EM/EUV texts beat acoustic/stock texts."""

    TOKENS = {
        "optical": (1, 0, 0, 0, 0, 0, 0, 0),
        "electromagnetic": (0, 1, 0, 0, 0, 0, 0, 0),
        "pinn": (0, 0, 1, 0, 0, 0, 0, 0),
        "euv": (0, 0, 0, 1, 0, 0, 0, 0),
        "lithography": (0, 0, 0, 1, 0, 0, 0, 0),
        "near": (0, 0, 0, 0, 1, 0, 0, 0),
        "field": (0, 0, 0, 0, 1, 0, 0, 0),
        "error": (0, 0, 0, 0, 1, 0, 0, 0),
        "multilayer": (1, 0, 0, 0, 0, 1, 0, 0),
        "radiative": (0, 0, 0, 0, 0, 1, 0, 0),
        "cooling": (0, 0, 0, 0, 0, 1, 0, 0),
        "underwater": (0, 0, 0, 0, 0, 0, 1, 0),
        "acoustic": (0, 0, 0, 0, 0, 0, 1, 0),
        "stock": (0, 0, 0, 0, 0, 0, 0, 1),
        "market": (0, 0, 0, 0, 0, 0, 0, 1),
    }

    def __init__(self) -> None:
        self.usage = _FakeSemanticUsage()
        self._cache: dict[str, list[float]] = {}
        self._calls: list[list[str]] = []

    @property
    def calls(self) -> list[list[str]]:
        return self._calls

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 8
        words = re.findall(r"[a-z0-9]{3,}", str(text or "").casefold())
        for word in words:
            if word in self.TOKENS:
                for index, value in enumerate(self.TOKENS[word]):
                    vector[index] += value
        # Contrastive domain behavior: off-domain vocabulary suppresses the
        # optical/electromagnetic background dimensions, so underwater/stock
        # PINN papers are semantically far even when they share domain words.
        for word in ("underwater", "acoustic", "stock", "market"):
            if word in words:
                vector[0] -= 3.0
                vector[5] -= 3.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_texts(
        self, texts: list[str]
    ) -> dict[str, list[float]]:
        unseen: list[str] = []
        for text in texts:
            norm = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
            if norm and norm not in self._cache and norm not in unseen:
                unseen.append(str(text))
        if unseen:
            self._calls.append(list(unseen))
            self.usage.embed_calls += 1
            self.usage.request_count += 1
            self.usage.input_tokens += sum(
                len(str(text).split()) for text in unseen
            )
            for text in unseen:
                norm = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
                self._cache[norm] = self._vector(text)
                self.usage.vector_count += 1
        return {
            re.sub(r"\s+", " ", str(text or "")).strip().casefold():
            self._cache[
                re.sub(r"\s+", " ", str(text or "")).strip().casefold()
            ]
            for text in texts
            if re.sub(r"\s+", " ", str(text or "")).strip().casefold()
        }

    def cosine(self, left: str, right: str) -> float:
        vectors = self.embed_texts([left, right])
        left_vec = vectors.get(
            re.sub(r"\s+", " ", str(left or "")).strip().casefold(), []
        )
        right_vec = vectors.get(
            re.sub(r"\s+", " ", str(right or "")).strip().casefold(), []
        )
        if not left_vec or not right_vec or len(left_vec) != len(right_vec):
            return 0.0
        dot = sum(a * b for a, b in zip(left_vec, right_vec))
        left_norm = math.sqrt(sum(a * a for a in left_vec))
        right_norm = math.sqrt(sum(b * b for b in right_vec))
        if not left_norm or not right_norm:
            return 0.0
        return round(max(0.0, dot / (left_norm * right_norm)), 6)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict,
    *,
    graph_depth: int,
    with_chunk: bool = False,
    paper_count: int = 1,
    paper_title: str | None = None,
    current_run_paper_ids: set[int] | None = None,
    paper_specs: dict[int, tuple[str, str]] | None = None,
) -> None:
    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, facets, **kwargs):
            captured["facets"] = facets
            expanded_queries: list[str] = []
            for facet in facets:
                for query in facet.queries:
                    expanded_queries.append(query)
                    roles_lower = {
                        role.casefold() for role in facet.requested_roles
                    }
                    if not facet.direct_only and "review" in roles_lower:
                        expanded_queries.append(
                            f"{query} review perspective roadmap"
                        )
                    if not facet.direct_only and "foundation" in roles_lower:
                        expanded_queries.append(
                            f"{query} fundamental theory origin"
                        )
            captured["expanded_queries"] = expanded_queries
            return bootstrap_module.DiscoveryPortfolio(
                candidates=[
                    SimpleNamespace(
                        decision="retain",
                        paper=_paper(
                            index,
                            title=(
                                paper_specs[index][0]
                                if paper_specs and index in paper_specs
                                else paper_title
                            ),
                            current_run=(
                                bool(current_run_paper_ids)
                                and index in current_run_paper_ids
                            ),
                            abstract=(
                                paper_specs[index][1]
                                if paper_specs and index in paper_specs
                                else None
                            ),
                        ),
                    )
                    for index in range(1, paper_count + 1)
                ],
                query_runs=[{"query": "bounded gap query", "status_code": 200}],
                pool_counts={},
            )

    class FakeRetriever:
        def __init__(self, **kwargs):
            captured["retriever_kwargs"] = dict(kwargs)
            self.gateway = SimpleNamespace(batch_papers=self._batch_papers)

        def _batch_papers(self, batch_ids):
            captured["batch_calls"] = captured.get("batch_calls", 0) + 1
            captured["batch_ids"] = list(batch_ids)
            return [], SimpleNamespace(
                endpoint="batch",
                status_code=200,
                status_category="ok",
                cache_hit=False,
                wait_seconds=0,
            )

        def retrieve(self, *args, **kwargs):
            captured["snippet_kwargs"] = dict(kwargs)
            if with_chunk:
                return TextChunkRetrievalResult(
                    accepted_chunks=[_chunk()],
                    rejected_items=[],
                    query_runs=[],
                    paper_ids=["p1"],
                )
            return bootstrap_module._empty_chunks()

        def retrieve_precise_missing_papers(self, *args, **kwargs):
            captured["precise_kwargs"] = dict(kwargs)
            captured["precise_paper_ids"] = [
                str(getattr(paper, "paper_id", ""))
                for paper in (args[0] if args else [])
            ]
            return bootstrap_module._empty_chunks()

    class FakeGraphBuilder:
        def __init__(self):
            captured["graph_builder_calls"] = (
                captured.get("graph_builder_calls", 0) + 1
            )

        def expand_from_seeds(self, frontier, **kwargs):
            captured["expand_calls"] = captured.get("expand_calls", 0) + 1
            captured["expand_frontier"] = list(frontier)
            return LiteratureGraph(
                query_runs=[{"query_category": "graph_references"}]
            )

        @staticmethod
        def add_snippet_reference_mentions(graph, chunks):
            captured["ref_mentions"] = True

    class FakeFulltextAcquirer:
        def __init__(self, **kwargs):
            captured["fulltext_constructed"] = (
                captured.get("fulltext_constructed", 0) + 1
            )

        def acquire(self, selections, **kwargs):
            captured["fulltext_selections"] = selections
            captured["fulltext_max_successes"] = kwargs.get("max_successes")
            return SimpleNamespace(
                to_dict=lambda: {
                    "selected_paper_ids": [],
                    "skipped": [],
                    "new_chunk_ids": [],
                    "reused_chunk_ids": [],
                    "new_paper_ids": [],
                    "stats": {
                        "attempted": 0,
                        "downloaded": 0,
                        "parse_failed": 0,
                        "paper_outcomes": [],
                    },
                }
            )

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", FakeDiscovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", FakeRetriever)
    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", FakeGraphBuilder)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", FakeFulltextAcquirer)
    if graph_depth > 0:
        def fake_policy_graph(
            *, seeds, topic_queries, policy, relation_controls=None
        ):
            captured["graph_seeds"] = list(seeds)
            captured["graph_relation_controls"] = relation_controls
            return LiteratureGraph(
                query_runs=[{"query_category": "graph_references"}]
            )

        monkeypatch.setattr(bootstrap_module, "_build_policy_graph", fake_policy_graph)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: dict,
    captured: dict,
    *,
    graph_depth: int = 0,
    results_limit: int | None = 3,
    snippet_limit: int | None = 3,
    with_chunk: bool = False,
    paper_count: int = 1,
    use_snippet_search: bool = True,
    paper_title: str | None = None,
    current_run_paper_ids: set[int] | None = None,
    paper_specs: dict[int, tuple[str, str]] | None = None,
    semantic_relevance: Any | None = None,
) -> dict:
    _install_fakes(
        monkeypatch,
        captured,
        graph_depth=graph_depth,
        with_chunk=with_chunk,
        paper_count=paper_count,
        paper_title=paper_title,
        current_run_paper_ids=current_run_paper_ids,
        paper_specs=paper_specs,
    )
    run_dir = tmp_path / f"run-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    query_path = run_dir / "query.json"
    query_path.write_text(json.dumps(plan), encoding="utf-8")
    policy_path = _write_policy(
        run_dir / "policy.json",
        graph_depth=graph_depth,
        roles=("review", "foundation"),
        use_snippet_search=use_snippet_search,
    )
    base_path = _write_empty_base(run_dir / "base.sqlite")
    work_dir = run_dir / "work"
    return prepare_s2_harness_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base_path,
        work_dir=work_dir,
        policy_path=policy_path,
        results_limit=results_limit,
        snippet_limit=snippet_limit,
        semantic_relevance=semantic_relevance,
    )


def test_supplementary_role_expansion_switch_changes_requested_roles(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
    )
    assert report["status"] != "failed"
    assert report["supplementary_policy"]["allow_role_expansion"] is False
    assert report["discovery_direct_only"] is True
    assert report["discovery_generated_only"] is True
    assert captured["facets"][0].direct_only is True
    assert captured["facets"][0].requested_roles == []
    assert captured["expanded_queries"] == [
        "measured cooling power multilayer inverse design"
    ]
    assert captured["snippet_kwargs"]["requested_roles"] == []
    assert captured["precise_kwargs"]["requested_roles"] == []

    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"allow_role_expansion": True}},
        ),
        captured,
        graph_depth=0,
    )
    assert report["supplementary_policy"]["allow_role_expansion"] is True
    assert report["discovery_direct_only"] is False
    assert report["discovery_generated_only"] is True
    assert captured["facets"][0].direct_only is False
    assert captured["facets"][0].requested_roles == ["review", "foundation"]
    assert "measured cooling power multilayer inverse design review perspective roadmap" in (
        captured["expanded_queries"]
    )
    assert "measured cooling power multilayer inverse design fundamental theory origin" in (
        captured["expanded_queries"]
    )
    assert captured["snippet_kwargs"]["requested_roles"] == ["review", "foundation"]
    assert captured["precise_kwargs"]["requested_roles"] == ["review", "foundation"]


def test_supplementary_exact_paper_followup_false_skips_precise(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"allow_exact_paper_followup": False}},
        ),
        captured,
    )
    assert report["status"] != "failed"
    assert "precise_kwargs" not in captured
    assert any(
        run.get("query_category")
        == "precise_lookup_disabled_supplementary_policy"
        for run in report["external_query_runs"]
    )
    assert report["supplementary_policy"]["route_usage"]["precise"][
        "attempted"
    ] == 0

    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
    )
    assert report["status"] != "failed"
    assert "precise_kwargs" in captured


def test_supplementary_oa_fallback_false_skips_oa(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"allow_oa_fulltext_fallback": False}},
        ),
        captured,
    )
    assert report["status"] != "failed"
    assert "fulltext_constructed" not in captured
    assert any(
        item.get("reason", "").startswith(
            "supplementary_policy_disabled:allow_oa_fulltext_fallback=false"
        )
        for item in report["fulltext_fallback"]["skipped"]
    )
    assert (
        report["fulltext_fallback"]["supplementary_policy_disabled"]
        == "allow_oa_fulltext_fallback=false"
    )

    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
    )
    assert report["status"] != "failed"
    assert captured.get("fulltext_constructed", 0) >= 1


def test_route_caps_are_independent_and_ceiling_is_audit_only(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
        paper_count=7,
    )
    assert report["status"] != "failed"
    policy = report["supplementary_policy"]
    usage = policy["route_usage"]
    assert policy["result_cap"] == 16
    assert policy["extra_request_cap"] == 8
    assert "budget" not in policy
    precise = usage["precise"]
    oa = usage["oa_fulltext"]
    assert precise["configured_cap"] == 8
    assert precise["eligible"] == 3
    assert precise["attempted"] == 3
    assert oa["configured_cap"] == 16
    assert oa["eligible"] == 3
    assert oa["attempted"] == 3
    assert captured.get("fulltext_max_successes") == 3
    assert "graph" not in usage
    assert "batch_enrichment" not in usage


def test_section_default_route_caps_leave_precise_batch_graph_oa_nonzero(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="section_argument_gap"),
        captured,
        graph_depth=1,
        paper_count=10,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["precise"]["configured_cap"] == 12
    assert usage["precise"]["attempted"] == 3
    assert usage["batch_enrichment"]["configured_cap"] == 24
    assert usage["batch_enrichment"]["attempted"] == 1
    # Policy allows 2 graph seeds, but multi-seed is off for section tasks,
    # so the effective execution cap is at most 1 seed.
    assert report["supplementary_policy"]["graph_seed_cap"] == 2
    assert usage["graph"]["configured_cap"] == 1
    assert usage["graph"]["attempted"] == 1
    assert usage["oa_fulltext"]["configured_cap"] == 16
    assert usage["oa_fulltext"]["attempted"] == 2
    assert captured.get("batch_calls", 0) >= 1
    assert captured.get("fulltext_constructed", 0) >= 1


def test_whole_review_default_route_caps_leave_all_nonzero(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="whole_review_gap"),
        captured,
        graph_depth=1,
        paper_count=14,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["precise"]["configured_cap"] == 16
    assert usage["precise"]["attempted"] == 3
    assert usage["batch_enrichment"]["attempted"] == 1
    assert usage["graph"]["attempted"] == 1
    assert usage["oa_fulltext"]["configured_cap"] == 24
    assert usage["oa_fulltext"]["attempted"] == 2


def test_review_structure_multi_seed_graph_cap_bounded_and_oa_nonzero(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="review_structure_gap"),
        captured,
        graph_depth=1,
        paper_count=10,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["graph"]["configured_cap"] == 3
    assert usage["graph"]["attempted"] == 3
    assert len(captured.get("graph_seeds", [])) == 3
    assert usage["oa_fulltext"]["attempted"] == 2
    assert usage["precise"]["attempted"] == 3
    assert usage["batch_enrichment"]["attempted"] == 1


def test_route_cap_zero_disables_only_its_route(tmp_path, monkeypatch) -> None:
    # Precise cap 0: precise skipped, OA still runs at its own cap.
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"s2_precise_paper_cap": 0}},
        ),
        captured,
        paper_count=3,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["precise"]["attempted"] == 0
    assert usage["oa_fulltext"]["attempted"] == 3
    assert "precise_kwargs" not in captured
    assert captured.get("fulltext_constructed", 0) >= 1

    # OA cap 0: OA skipped, precise still runs.
    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"oa_fulltext_paper_cap": 0}},
        ),
        captured,
        paper_count=3,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["precise"]["attempted"] == 3
    assert usage["oa_fulltext"]["attempted"] == 0
    assert "fulltext_constructed" not in captured

    # Batch cap 0: batch skipped, graph and OA still run.
    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="section_argument_gap",
            metadata={"expansion_policy": {"batch_enrichment_paper_cap": 0}},
        ),
        captured,
        graph_depth=1,
        paper_count=4,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["batch_enrichment"]["attempted"] == 0
    assert usage["graph"]["attempted"] == 1
    assert usage["oa_fulltext"]["attempted"] == 2
    assert captured.get("batch_calls", 0) == 0

    # Graph seed cap 0: graph skipped, batch and OA still run.
    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="section_argument_gap",
            metadata={"expansion_policy": {"graph_seed_cap": 0}},
        ),
        captured,
        graph_depth=1,
        paper_count=4,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["graph"]["attempted"] == 0
    assert usage["batch_enrichment"]["attempted"] == 1
    assert usage["oa_fulltext"]["attempted"] == 2
    assert "graph_seeds" not in captured


def test_abstract_claim_cap_enforced_after_misses(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={
                "expansion_policy": {
                    "oa_fulltext_paper_cap": 0,
                    "abstract_claim_paper_cap": 1,
                }
            },
        ),
        captured,
        paper_count=3,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["abstract_claim"]["configured_cap"] == 1
    assert usage["abstract_claim"]["eligible"] == 3
    assert usage["abstract_claim"]["attempted"] == 1
    statuses = {
        value.get("status")
        for value in (usage["abstract_claim"]["outcomes"][0].values())
    }
    assert "materialized" in statuses
    assert "abstract_claim_budget_reached" in statuses


def test_zero_graph_seed_cap_disables_graph_even_with_multi_seed(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="review_structure_gap",
            metadata={"expansion_policy": {"graph_seed_cap": 0}},
        ),
        captured,
        graph_depth=1,
        paper_count=3,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["graph"]["configured_cap"] == 0
    assert usage["graph"]["attempted"] == 0
    assert "graph_seeds" not in captured
    # Other routes remain independent.
    assert usage["precise"]["attempted"] == 3
    assert usage["batch_enrichment"]["attempted"] == 1
    assert usage["oa_fulltext"]["attempted"] == 2


def test_zero_snippet_cap_disables_only_broad_snippet_and_keeps_precise(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"s2_snippet_results_per_query_cap": 0}},
        ),
        captured,
        paper_count=3,
        snippet_limit=None,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert usage["snippet"]["configured_cap"] == 0
    assert usage["snippet"]["attempted"] == 0
    assert usage["snippet"]["outcomes"][0]["reason"] == (
        "s2_snippet_results_per_query_cap=0"
    )
    assert "snippet_kwargs" not in captured
    assert any(
        run.get("query_category")
        == "snippet_search_disabled_supplementary_policy"
        for run in report["external_query_runs"]
    )
    # Precise per-paper lookup is independent of broad snippet search.
    assert usage["precise"]["attempted"] == 3
    assert "precise_kwargs" in captured
    assert usage["oa_fulltext"]["attempted"] == 3


def test_supplementary_global_snippet_gate_disables_broad_and_precise(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
        paper_count=3,
        snippet_limit=None,
        use_snippet_search=False,
    )
    assert report["status"] != "failed"
    usage = report["supplementary_policy"]["route_usage"]
    assert "snippet" not in usage
    assert "precise" not in usage
    assert "snippet_kwargs" not in captured
    assert "precise_kwargs" not in captured


def test_generated_only_precap_ranking_selects_usefulness_first(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"s2_precise_paper_cap": 1}},
        ),
        captured,
        paper_count=2,
        current_run_paper_ids={2},
    )
    assert report["status"] != "failed"
    assert report["discovery_generated_only"] is True
    # p2 carries current-run provenance and therefore a higher usefulness
    # score than p1; the cap=1 precise slot goes to p2 even though p1 was
    # first in the discovery order.
    assert captured["precise_paper_ids"] == ["p2", "p1"]
    assert captured["precise_kwargs"]["max_papers"] == 1


def test_ordinary_plan_preserves_historical_order(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _ordinary_plan(),
        captured,
        paper_count=2,
        current_run_paper_ids={2},
    )
    assert report["status"] != "failed"
    assert report["discovery_generated_only"] is False
    # Ordinary first-round plans keep the discovery order even though p2 has
    # higher generated-only usefulness; no supplementary ranking applies.
    assert captured["precise_paper_ids"] == ["p1", "p2"]


def test_semantic_generated_only_ranking_changes_cap_winner_and_persists(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    engine = _FakeSemanticEngine()
    underwater_pinn = (
        "Radiative cooling multilayer underwater acoustic PINN",
        "Radiative cooling multilayer films use PINN models for underwater "
        "acoustic propagation with near-field truncation error and far-field "
        "prediction accuracy and measured cooling power from inverse design, "
        "and how we compare imaging performance across group delay dispersion.",
    )
    optical_pinn = (
        "PINN optical electromagnetic multilayer inverse design",
        "PINN differentiable electromagnetic solvers for optical multilayer "
        "radiative cooling with near-field truncation error, far-field "
        "prediction accuracy, and measured cooling power from inverse design.",
    )
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"s2_precise_paper_cap": 1}},
        ),
        captured,
        paper_count=2,
        paper_specs={1: underwater_pinn, 2: optical_pinn},
        semantic_relevance=engine,
    )
    assert report["status"] != "failed"
    assert report["semantic_candidate_score_count"] == 2
    assert report["semantic_relevance_usage"]["embed_calls"] == 1
    # Semantic scores rank/audit only: both candidates are retained, and the
    # higher-scoring on-topic paper is ranked first so it receives the cap=1
    # precise slot.  The low-scoring underwater PINN paper is kept for
    # downstream review rather than rejected.
    assert captured["precise_paper_ids"] == ["p2", "p1"]
    assert captured["precise_kwargs"]["max_papers"] == 1
    # The same semantic-aware decisions persist through stage ingestion with
    # the audit fields intact.
    ingest = report["kb_ingest"]
    assert ingest["papers_accepted"] == 2
    decisions = ingest["paper_decisions"]
    accepted = [item for item in decisions if item.get("accepted")]
    assert [item["paper_id"] for item in accepted] == ["p2", "p1"]
    assert all(item["semantic_mode"] == "semantic" for item in accepted)
    assert accepted[0]["usefulness_score"] > accepted[1]["usefulness_score"]
    assert accepted[0]["max_precise_similarity"] > 0.5
    assert accepted[1]["max_precise_similarity"] < 0.5
    assert accepted[1]["usefulness_score"] < accepted[1][
        "usefulness_threshold"
    ]
    conn = sqlite3.connect(report["runtime_kb_sqlite"])
    try:
        rows = [row[0] for row in conn.execute("SELECT paper_id FROM papers")]
    finally:
        conn.close()
    assert set(rows) == {"p1", "p2"}

    # Without the semantic engine the lexical fallback also admits both papers
    # but in discovery order, proving semantic scoring is what changed the
    # pre-cap winner while keeping every accepted candidate.
    captured.clear()
    report_lexical = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(
            gap_type="claim_evidence_gap",
            metadata={"expansion_policy": {"s2_precise_paper_cap": 1}},
        ),
        captured,
        paper_count=2,
        paper_specs={1: underwater_pinn, 2: optical_pinn},
    )
    assert report_lexical["status"] != "failed"
    assert captured["precise_paper_ids"] == ["p1", "p2"]
    assert report_lexical["kb_ingest"]["papers_accepted"] == 2


def test_ordinary_plan_ignores_semantic_engine(tmp_path, monkeypatch) -> None:
    captured: dict = {}
    engine = _FakeSemanticEngine()
    report = _run(
        tmp_path,
        monkeypatch,
        _ordinary_plan(),
        captured,
        paper_count=2,
        semantic_relevance=engine,
    )
    assert report["status"] != "failed"
    assert report["discovery_generated_only"] is False
    assert report["semantic_candidate_score_count"] == 0
    assert engine.usage.embed_calls == 0
    assert captured["precise_paper_ids"] == ["p1", "p2"]


def test_preserved_topic_scope_rejects_unrelated_title(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
        paper_count=1,
        snippet_limit=None,
        paper_title="Stock market volatility forecasting with deep learning",
    )
    assert report["status"] != "failed"
    summary = report["material_flow_summary"]
    assert summary["paper_count"] == 0
    assert summary["admitted_paper_count"] == 0


def test_supplementary_graph_switch_and_modes_are_reported(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="section_argument_gap"),
        captured,
        graph_depth=1,
        paper_count=2,
    )
    assert report["status"] != "failed"
    assert report["graph_expansion_allowed"] is True
    assert report["supplementary_graph_modes"] == [
        "citations",
        "cited_by",
        "references",
    ]
    assert captured["graph_relation_controls"] == {
        "references": True,
        "citations": True,
        "recommendations": False,
        "multi_seed": False,
    }
    # Multi-seed is off for section gaps: at most one seed.
    assert len(captured.get("graph_seeds", [])) == 1
    assert any(
        run.get("query_category") == "graph_expansion_policy"
        and run.get("effective_graph_modes") == [
            "citations",
            "cited_by",
            "references",
        ]
        for run in report["external_query_runs"]
    )


def test_review_structure_enables_all_approved_routes(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="review_structure_gap"),
        captured,
        graph_depth=1,
        paper_count=2,
    )
    assert report["status"] != "failed"
    policy = report["supplementary_policy"]
    for key in (
        "allow_role_expansion",
        "allow_exact_paper_followup",
        "allow_batch_enrichment",
        "allow_oa_fulltext_fallback",
        "allow_reference_expansion",
        "allow_citation_expansion",
        "allow_recommendation_expansion",
        "allow_multi_seed_graph",
    ):
        assert policy[key] is True
    assert policy["allow_visual_processing"] is False
    assert report["supplementary_graph_modes"] == [
        "citations",
        "cited_by",
        "multi_seed",
        "recommendations",
        "references",
    ]
    assert captured["graph_relation_controls"]["multi_seed"] is True
    assert len(captured.get("graph_seeds", [])) == 2


def test_supplementary_batch_enrichment_switch_gates_gateway_call(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        captured,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    assert captured.get("batch_calls", 0) == 0
    assert report["supplementary_policy"]["allow_batch_enrichment"] is False
    assert report["supplementary_policy"]["allow_reference_expansion"] is False
    assert report["supplementary_policy"]["allow_citation_expansion"] is False
    assert report["supplementary_policy"]["allow_recommendation_expansion"] is False
    assert report["supplementary_policy"]["allow_multi_seed_graph"] is False
    assert any(
        run.get("query_category")
        == "batch_enrichment_disabled_supplementary_policy"
        for run in report["external_query_runs"]
    )

    captured.clear()
    report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="section_argument_gap"),
        captured,
        with_chunk=True,
    )
    assert report["status"] != "failed"
    assert captured.get("batch_calls", 0) >= 1
    assert any(
        run.get("query_category") == "batch_enrichment"
        for run in report["external_query_runs"]
    )

def test_build_policy_graph_respects_per_mode_controls(monkeypatch) -> None:
    captured: dict = {}

    class FakeGraphBuilder:
        def __init__(self):
            captured["constructed"] = True

        def expand_from_seeds(self, frontier, **kwargs):
            captured["frontier"] = list(frontier)
            captured["limits"] = {
                key: kwargs[key]
                for key in (
                    "reference_limit_per_seed",
                    "citation_limit_per_seed",
                    "recommendation_limit",
                )
            }
            return LiteratureGraph()

        @staticmethod
        def add_snippet_reference_mentions(graph, chunks):
            pass

    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", FakeGraphBuilder)
    policy = SimpleNamespace(
        graph_depth=1,
        graph_seed_count=2,
        graph_reference_limit_per_seed=5,
        graph_citation_limit_per_seed=6,
        graph_recommendation_limit=7,
        feature_enabled=lambda name, default=True: True,
    )
    seeds = [
        SimpleNamespace(paper_id="p1"),
        SimpleNamespace(paper_id="p2"),
    ]
    bootstrap_module._build_policy_graph(
        seeds=seeds,
        topic_queries=["q"],
        policy=policy,
        relation_controls={
            "references": True,
            "citations": False,
            "recommendations": False,
            "multi_seed": False,
        },
    )
    assert len(captured["frontier"]) == 1
    assert captured["limits"] == {
        "reference_limit_per_seed": 5,
        "citation_limit_per_seed": 0,
        "recommendation_limit": 0,
    }

    captured.clear()
    bootstrap_module._build_policy_graph(
        seeds=seeds,
        topic_queries=["q"],
        policy=policy,
        relation_controls=None,
    )
    assert len(captured["frontier"]) == 2
    assert captured["limits"] == {
        "reference_limit_per_seed": 5,
        "citation_limit_per_seed": 6,
        "recommendation_limit": 7,
    }


def test_ordinary_plan_keeps_historical_role_and_graph_behavior(
    tmp_path, monkeypatch
) -> None:
    captured: dict = {}
    report = _run(
        tmp_path,
        monkeypatch,
        _ordinary_plan(),
        captured,
        graph_depth=1,
    )
    assert report["status"] != "failed"
    assert report["discovery_direct_only"] is False
    assert report["discovery_generated_only"] is False
    assert report["supplementary_policy"] is None
    assert captured["facets"][0].direct_only is False
    assert captured["facets"][0].requested_roles == ["review", "foundation"]
    assert "broadband achromatic metalens review perspective roadmap" in (
        captured["expanded_queries"]
    )
    assert "broadband achromatic metalens fundamental theory origin" in (
        captured["expanded_queries"]
    )
    assert report["graph_expansion_allowed"] is True
    assert captured.get("graph_seeds", []) != []
    assert captured.get("fulltext_constructed", 0) >= 1


def test_visual_task_enters_oa_fulltext_selection_with_s2_primary_chunk(
    tmp_path,
    monkeypatch,
) -> None:
    """Visual tasks must request OA/fulltext even when S2 text already exists."""

    visual_captured: dict = {}
    visual_report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="visual_material_gap"),
        visual_captured,
        with_chunk=True,
    )
    assert visual_report["status"] != "failed"
    visual_selected = {
        str(getattr(paper, "paper_id", "") or "")
        for paper, _decision in (
            visual_captured.get("fulltext_selections") or []
        )
    }
    assert "p1" in visual_selected
    visual_fulltext = visual_report["fulltext_fallback"]
    visual_skipped = {
        row["paper_id"]: row["reason"]
        for row in visual_fulltext.get("skipped") or []
    }
    assert visual_skipped.get("p1") != "s2_material_sufficient"
    assert visual_fulltext["visual_intent"]["enabled"] is True
    assert "p1" in visual_fulltext["visual_intent"][
        "s2_sufficient_papers_selected_for_fulltext"
    ]

    # Identical inventory through a text task still skips before OA.
    text_captured: dict = {}
    text_report = _run(
        tmp_path,
        monkeypatch,
        _supplementary_plan(gap_type="claim_evidence_gap"),
        text_captured,
        with_chunk=True,
    )
    assert text_report["status"] != "failed"
    text_selected = {
        str(getattr(paper, "paper_id", "") or "")
        for paper, _decision in (
            text_captured.get("fulltext_selections") or []
        )
    }
    assert "p1" not in text_selected
    text_skipped = {
        row["paper_id"]: row["reason"]
        for row in (text_report["fulltext_fallback"].get("skipped") or [])
    }
    assert text_skipped.get("p1") == "s2_material_sufficient"
