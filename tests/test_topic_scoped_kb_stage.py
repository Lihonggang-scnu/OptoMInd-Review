from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest


# The focused stage tests are also useful in the repository's lightweight
# tooling environment, where the optional AgentScope/ftfy stack may not be
# installed.  This does not affect production imports.
RUNTIME_DIR = Path(__file__).resolve().parents[1] / "optomind_research" / "runtime"
if "optomind_research.runtime" not in sys.modules:
    runtime_package = types.ModuleType("optomind_research.runtime")
    runtime_package.__path__ = [str(RUNTIME_DIR)]
    sys.modules["optomind_research.runtime"] = runtime_package
if "ftfy" not in sys.modules:
    try:
        import ftfy  # noqa: F401
    except ModuleNotFoundError:
        ftfy_module = types.ModuleType("ftfy")
        ftfy_module.fix_text = lambda value: value
        sys.modules["ftfy"] = ftfy_module

from optomind_research.runtime.s2_policy_runtime import (  # noqa: E402
    S2PolicyError,
    load_s2_policy,
)
from optomind_research.runtime.topic_scoped_kb_stage import (  # noqa: E402
    TopicScopedKBStage,
    TopicScopeContract,
    _is_explicit_current_run_paper,
    _score_paper_scope,
    _scope_match,
    build_s2_query_telemetry,
    build_topic_scoped_kb,
    derive_topic_scope_contract,
)
from optomind_research.s2_schemas import S2PaperRecord, UnifiedTextChunk  # noqa: E402
from optomind_research.s2_literature_graph import LiteratureGraph  # noqa: E402


def _query_plan(*, exclusions: list[str] | None = None) -> dict:
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
            "exclusion_boundaries": exclusions or ["acoustic metalens", "radiative cooling"],
            "keyword_decomposition": {
                "keywords": [
                    "broadband achromatic metalens",
                    "metalens group delay dispersion",
                ]
            },
        },
    }


def _write_query_plan(tmp_path: Path, *, exclusions: list[str] | None = None) -> Path:
    path = tmp_path / "query_plan.json"
    path.write_text(
        json.dumps(_query_plan(exclusions=exclusions), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_policy(
    tmp_path: Path,
    *,
    target_papers: tuple[int, int] = (1, 1),
    min_factual_papers: int = 1,
    min_factual_chunks: int = 1,
) -> Path:
    # JSON is valid YAML and avoids making the fixture depend on PyYAML.
    path = tmp_path / "s2_policy.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "standard": {
                    "accepted_s2_text_papers_per_facet": list(target_papers),
                    "graph_depth": 0,
                },
                "graph": {
                    "seed_count": 0,
                    "reference_limit_per_seed": 0,
                    "citation_limit_per_seed": 0,
                    "recommendation_limit": 0,
                },
                "evidence": {
                    "minimum_factual_papers": min_factual_papers,
                    "minimum_factual_chunks": min_factual_chunks,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_base_kb(tmp_path: Path, *, metadata_only: bool = False) -> Path:
    path = tmp_path / "base.sqlite"
    conn = sqlite3.connect(path)
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
        CREATE TABLE concepts(label TEXT);
        """
    )
    p1_raw = {
        "content_depth": "metadata" if metadata_only else "fulltext",
        "use_permission": "discovery_only" if metadata_only else "factual_support",
        "scope_fit": "direct",
        "context_complete": not metadata_only,
        "provenance": {"discovery_route": "legacy_fixture"},
    }
    p2_raw = {
        "content_depth": "fulltext",
        "use_permission": "factual_support",
        "scope_fit": "direct",
        "context_complete": True,
    }
    conn.execute(
        "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "p-metalens",
            "10.1000/metalens",
            "Broadband achromatic metalens imaging",
            2024,
            "Optics Journal",
            "fixture",
            "direct",
            "broadband achromatic metalens group delay dispersion optical imaging",
            json.dumps(p1_raw),
        ),
    )
    conn.execute(
        "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "p-cooling",
            "10.1000/cooling",
            "Passive daytime radiative cooling films",
            2024,
            "Thermal Journal",
            "fixture",
            "unrelated",
            "radiative cooling atmospheric window emissivity",
            json.dumps(p2_raw),
        ),
    )
    if metadata_only:
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c-metadata",
                "p-metalens",
                "10.1000/metalens",
                "Broadband achromatic metalens imaging",
                0,
                "Abstract",
                0,
                20,
                20,
                0,
                "Broadband achromatic metalens group delay metadata record.",
                "broadband achromatic metalens metadata",
                json.dumps(
                    {
                        "content_depth": "metadata",
                        "use_permission": "discovery_only",
                        "scope_fit": "direct",
                    }
                ),
            ),
        )
    else:
        rows = [
            (
                "c-relevant",
                "p-metalens",
                "10.1000/metalens",
                "Broadband achromatic metalens imaging",
                0,
                "Mechanism",
                0,
                900,
                900,
                0,
                "Broadband achromatic metalens group delay dispersion controls optical imaging efficiency and phase compensation.",
                "broadband achromatic metalens group delay dispersion optical imaging",
                json.dumps(
                    {
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "scope_fit": "direct",
                        "context_complete": True,
                        "route_provenance": {"discovery_route": "legacy_fixture"},
                    }
                ),
            ),
            (
                "c-generic-in-scope-paper",
                "p-metalens",
                "10.1000/metalens",
                "Broadband achromatic metalens imaging",
                1,
                "Methods",
                900,
                1200,
                300,
                0,
                "The fabrication workflow used a deterministic process model and measured the samples.",
                "fabrication workflow deterministic process model",
                json.dumps(
                    {
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "scope_fit": "direct",
                        "context_complete": True,
                    }
                ),
            ),
            (
                "c-off-topic",
                "p-cooling",
                "10.1000/cooling",
                "Passive daytime radiative cooling films",
                0,
                "Results",
                0,
                600,
                600,
                0,
                "Radiative cooling films emit through the atmospheric window.",
                "radiative cooling atmospheric window",
                json.dumps(
                    {
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "scope_fit": "direct",
                        "context_complete": True,
                    }
                ),
            ),
        ]
        conn.executemany("INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO concepts(label) VALUES (?)",
        [("metalens group delay",), ("radiative cooling",)],
    )
    conn.commit()
    conn.close()
    return path


def _make_generated_only_ordering_base(tmp_path: Path) -> Path:
    path = tmp_path / "ordering.sqlite"
    conn = sqlite3.connect(path)
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
    rows = [
        (
            "p-direct",
            "10.1000/direct",
            "PINN differentiable electromagnetic solvers for optical simulation",
            "PINN differentiable electromagnetic solvers establish simulation "
            "credibility and predict near-field truncation error and far-field "
            "prediction accuracy in optical simulation.",
        ),
        (
            "p-euv",
            "10.1000/euv",
            "EUV lithography PINN surrogate models for near-field accuracy",
            "EUV lithography PINN surrogate models predict near-field "
            "truncation error and far-field prediction accuracy.",
        ),
        (
            "p-stock",
            "10.1000/stock",
            "Stock market forecasting with simulation solver models",
            "Simulation solver models predict near-field truncation error and "
            "far-field prediction accuracy in stock market forecasting.",
        ),
    ]
    for index, (paper_id, doi, title, abstract) in enumerate(rows):
        conn.execute(
            "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
            (
                paper_id,
                doi,
                title,
                2024,
                "Journal",
                "fixture",
                "direct",
                abstract,
                json.dumps(
                    {
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "scope_fit": "direct",
                        "context_complete": True,
                        "provenance": {"discovery_route": "ordering_fixture"},
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"c-{paper_id}",
                paper_id,
                doi,
                title,
                0,
                "Results",
                0,
                len(abstract),
                len(abstract),
                0,
                abstract,
                abstract,
                json.dumps(
                    {
                        "content_depth": "fulltext",
                        "use_permission": "factual_support",
                        "scope_fit": "direct",
                        "context_complete": True,
                    }
                ),
            ),
        )
    conn.commit()
    conn.close()
    return path


def test_policy_loader_applies_defaults_and_rejects_invalid_config(tmp_path: Path):
    policy = load_s2_policy()
    assert policy.results_per_query == 300
    assert policy.snippet_results_per_query == 300
    assert policy.precise_snippet_results_per_paper == 100
    assert policy.max_precise_snippet_papers == 300
    assert policy.max_abstract_claim_papers == 300
    assert policy.graph_depth == 2
    assert policy.graph_seed_count == 5
    assert policy.maximum_accepted_papers == 300
    assert policy.maximum_oa_downloads == 200

    invalid = tmp_path / "invalid_policy.yaml"
    invalid.write_text(
        "version: 2\nstandard:\n  results_per_query: 0\n", encoding="utf-8"
    )
    with pytest.raises(S2PolicyError):
        load_s2_policy(invalid)


def test_scope_contract_derives_allowlist_and_exclusion_boundary():
    contract = derive_topic_scope_contract(_query_plan())
    assert contract.valid
    assert contract.canonical_question.startswith("Review broadband")
    assert "broadband achromatic metalens" in contract.keywords
    assert "mechanism" in contract.lenses
    assert "optical metalens design" in contract.inclusion_boundaries
    assert "radiative cooling" in contract.exclusion_boundaries
    assert "metalens" in " ".join(contract.allowlist_terms).casefold()


def test_generated_only_plan_returns_only_discovery_queries():
    plan = _query_plan()
    plan["supplementary_retrieval"] = {
        "discovery_mode": "generated_only",
        "discovery_queries": [
            "measured cooling power multilayer inverse design",
            "fabrication tolerance radiative cooling multilayer",
            "",
            "measured cooling power multilayer inverse design",
        ],
    }
    contract = derive_topic_scope_contract(plan)
    assert contract.valid
    assert contract.discovery_mode == "generated_only"
    assert contract.search_queries() == [
        "measured cooling power multilayer inverse design",
        "fabrication tolerance radiative cooling multilayer",
    ]
    assert contract.search_queries(max_items=1) == [
        "measured cooling power multilayer inverse design"
    ]
    assert contract.canonical_question == "Review broadband achromatic metalens design."
    assert "acoustic metalens" in contract.exclusion_boundaries
    assert "radiative cooling" in contract.exclusion_boundaries
    search_queries = contract.search_queries()
    assert contract.canonical_question not in search_queries
    assert not any(
        broad in search_queries
        for broad in (
            "mechanism",
            "manufacturing",
            "imaging performance",
            "Group delay engineering",
            "optical metalens design",
        )
    )
    assert contract.to_dict().get("discovery_mode") == "generated_only"


def test_ordinary_plan_still_appends_canonical_question_and_keeps_hash_shape():
    contract = derive_topic_scope_contract(_query_plan())
    assert contract.discovery_mode == ""
    assert contract.discovery_queries == ()
    queries = contract.search_queries()
    assert contract.canonical_question in queries
    assert queries[-1] == contract.canonical_question
    assert queries == list(dict.fromkeys(queries))
    assert "discovery_mode" not in contract.to_dict()
    assert "discovery_queries" not in contract.to_dict()


def test_topic_scope_contract_direct_construction_with_marker():
    generated_only = TopicScopeContract(
        canonical_question="Original question",
        lenses=("mechanism",),
        inclusion_boundaries=("optical multilayer films",),
        exclusion_boundaries=("acoustic metalens",),
        keywords=("broad keyword",),
        discovery_mode="generated_only",
        discovery_queries=("gap query a", "gap query b"),
    )
    assert generated_only.search_queries() == ["gap query a", "gap query b"]
    plain = TopicScopeContract(
        canonical_question="Original question",
        lenses=("mechanism",),
        inclusion_boundaries=("optical multilayer films",),
        exclusion_boundaries=("acoustic metalens",),
        keywords=("broad keyword",),
    )
    assert plain.search_queries() == [
        "broad keyword",
        "mechanism",
        "optical multilayer films",
        "Original question",
    ]


def _generated_only_plan(*, discovery_queries: list[str]) -> dict:
    plan = _query_plan()
    plan["supplementary_retrieval"] = {
        "discovery_mode": "generated_only",
        "discovery_queries": discovery_queries,
    }
    return plan


def test_generated_only_rejects_gap_query_match_without_topic_identity():
    contract = derive_topic_scope_contract(
        _generated_only_plan(
            discovery_queries=["measured cooling power multilayer inverse design"]
        )
    )
    assert contract.valid
    text = (
        "We measure the cooling power of inverse-designed multilayer films; "
        "the measured cooling power reaches 82 W/m2."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is False
    assert match["reason"] == "generated_only_usefulness_below_threshold"
    assert match["generated_only_discovery_match"] is True
    assert match["generated_only_discovery_hits"]
    assert match["usefulness_score"] < match["usefulness_threshold"]
    assert match["usefulness_features"]["generic_singleton_penalty"] > 0


def test_generated_only_accepts_gap_query_match_with_topic_identity():
    contract = derive_topic_scope_contract(
        _generated_only_plan(
            discovery_queries=["measured cooling power multilayer inverse design"]
        )
    )
    assert contract.valid
    text = (
        "Broadband achromatic metalens multilayer films with measured cooling "
        "power from inverse-designed stacks reach 82 W/m2 in the imaging "
        "performance test."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"
    assert match["scope_fit"] == "direct"
    assert match["generated_only_discovery_match"] is True
    assert match["generated_only_discovery_hits"]
    assert match["usefulness_score"] >= match["usefulness_threshold"]
    assert match["matched_precise_query"] == (
        "measured cooling power multilayer inverse design"
    )


def _pinn_generated_only_plan(*, discovery_queries: list[str]) -> dict:
    question = (
        "Compare PINN methods with differentiable electromagnetic solvers, "
        "including simulation credibility and the path from simulation to "
        "experiment."
    )
    return {
        "input": {"user_query": question},
        "output": {
            "canonical_question": question,
            "problem_understanding": question,
            "scope_definition": {
                "main_scope": (
                    "PINN and differentiable electromagnetic solvers for "
                    "optical research"
                ),
                "scope_items": [
                    "near-field fidelity",
                    "far-field error",
                    "alignment tolerance",
                ],
                "inclusion_boundaries": [
                    "optical and electromagnetic simulation",
                    "near-field to far-field prediction",
                    "experimental validation",
                ],
                "exclusion_boundaries": [
                    "unrelated fluid-only PINN studies",
                    "purely biological imaging",
                ],
            },
            "lenses": [
                "PINN",
                "differentiable electromagnetic solver",
                "simulation credibility",
                "simulation to experiment",
            ],
            "inclusion_boundaries": [
                "optical and electromagnetic simulation",
                "near-field to far-field prediction",
                "experimental validation",
            ],
            "exclusion_boundaries": [
                "unrelated fluid-only PINN studies",
                "purely biological imaging",
            ],
            "keyword_decomposition": {"keywords": list(discovery_queries)},
        },
        "supplementary_retrieval": {
            "discovery_mode": "generated_only",
            "discovery_queries": discovery_queries,
        },
    }


def test_generated_only_rejects_off_domain_gap_query_overlap():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.valid
    text = (
        "We simulate near-field and far-field error propagation in stock "
        "market forecasting, using gradient-based prediction accuracy models."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is False
    assert match["generated_only_discovery_match"] is True
    assert match["object_anchor_hits"] == []
    assert contract.object_anchor_mode == "method_fallback"
    assert contract.scientific_object_anchor_required is False


def test_generated_only_accepts_on_topic_gap_query_with_original_identity():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.valid
    text = (
        "PINN differentiable electromagnetic solvers establish simulation "
        "credibility and the path from simulation to experiment, predicting "
        "near-field truncation error and far-field prediction accuracy in "
        "optical simulation."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"
    assert match["generated_only_discovery_match"] is True
    assert match["object_anchor_hits"]
    assert match["method_family_identity_present"] is True
    assert match["generated_only_discovery_hits"][0]["field"] == "abstract"
    assert match["generated_only_discovery_hits"][0]["ratio"] == 1.0
    assert contract.object_anchor_mode == "method_fallback"
    assert contract.scientific_object_anchor_required is False
    assert set(contract.topic_object_anchors) <= set(contract.method_anchors)


def test_generated_only_query_match_requires_local_token_window():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.valid
    scattered = (
        "near "
        + " ".join(["unrelated"] * 60)
        + " field "
        + " ".join(["unrelated"] * 60)
        + " error "
        + " ".join(["unrelated"] * 60)
        + " far "
        + " ".join(["unrelated"] * 60)
        + " prediction "
        + " ".join(["unrelated"] * 60)
        + " accuracy "
        + " ".join(["unrelated"] * 60)
        + " truncation"
    )
    match = _scope_match(contract, scattered)
    assert match["accepted"] is False
    assert match["reason"] == "generated_only_usefulness_below_threshold"
    assert match["generated_only_discovery_match"] is False
    assert match["generated_only_discovery_hits"] == []


def test_generated_only_query_miss_is_not_terminal_when_identity_is_strong():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    strong_identity_no_gap = (
        "PINN differentiable electromagnetic solvers establish simulation "
        "credibility and the path from simulation to experiment in optical "
        "design."
    )
    match = _score_paper_scope(
        contract,
        {"search_text": strong_identity_no_gap, "raw_json": "{}"},
        explicit_current_run=True,
    )
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_usefulness_related"
    assert match["method_family_identity_present"] is True
    assert match["generated_only_discovery_match"] is False
    assert match["generated_only_provenance_match"] is True
    assert match["usefulness_score"] >= match["usefulness_threshold"]


def test_generated_only_method_family_rejects_generic_singletons():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    generic_singletons = (
        "Simulation solver models predict near-field truncation error and "
        "far-field prediction accuracy."
    )
    match = _scope_match(contract, generic_singletons)
    assert match["accepted"] is False
    assert match["reason"] == "generated_only_usefulness_below_threshold"
    assert match["method_family_identity_present"] is False
    assert match["generated_only_discovery_match"] is True
    assert match["usefulness_features"]["generic_singleton_penalty"] > 0


def test_generated_only_scientific_object_requires_combined_usefulness():
    contract = derive_topic_scope_contract(
        _metasurface_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.object_anchor_mode == "scientific_object"
    identity_only = (
        "Nanophotonic metasurface films achieve measured cooling power of "
        "82 W/m2 with imaging performance across the atmospheric window."
    )
    identity_only_match = _scope_match(contract, identity_only)
    assert identity_only_match["accepted"] is False
    assert identity_only_match["reason"] == (
        "generated_only_usefulness_below_threshold"
    )
    assert identity_only_match["usefulness_features"][
        "provenance_gap_penalty"
    ] > 0

    gap_only = (
        "Near-field truncation error propagates into far-field prediction "
        "accuracy in a completely different engineering setting."
    )
    gap_only_match = _scope_match(contract, gap_only)
    assert gap_only_match["accepted"] is False
    assert gap_only_match["generated_only_discovery_match"] is True
    assert gap_only_match["reason"] == (
        "generated_only_usefulness_below_threshold"
    )


def test_generated_only_current_route_on_topic_passes_without_query_words():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.object_anchor_mode == "method_fallback"
    paper = S2PaperRecord(
        paper_id="p-current",
        title="PINN differentiable electromagnetic solvers in optical simulation",
        abstract=(
            "We compare PINN differentiable electromagnetic solvers for "
            "simulation credibility and the path from simulation to "
            "experiment in optical design."
        ),
        discovery_route="s2_reference",
    )
    assert _is_explicit_current_run_paper(paper) is True
    match = _score_paper_scope(
        contract,
        {"search_text": paper.abstract, "raw_json": "{}"},
        explicit_current_run=True,
    )
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_usefulness_related"
    assert match["generated_only_discovery_match"] is False
    assert match["generated_only_provenance_match"] is True
    assert match["generated_only_precise_side"] is True
    assert match["method_family_identity_present"] is True
    assert match["usefulness_score"] >= match["usefulness_threshold"]


def test_generated_only_current_route_off_domain_rejects():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    paper = S2PaperRecord(
        paper_id="p-off",
        title="Simulation solver models for financial forecasting",
        abstract=(
            "Simulation solver models predict near-field truncation error "
            "and far-field prediction accuracy in a financial setting."
        ),
        discovery_route="s2_recommendation",
    )
    assert _is_explicit_current_run_paper(paper) is True
    match = _score_paper_scope(
        contract,
        {"search_text": paper.abstract, "raw_json": "{}"},
        explicit_current_run=True,
    )
    assert match["accepted"] is False
    assert match["reason"] == "generated_only_usefulness_below_threshold"
    assert match["method_family_identity_present"] is False


def test_generated_only_stale_graph_row_without_marker_stays_rejected():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    stale = S2PaperRecord(
        paper_id="p-stale",
        title="PINN differentiable electromagnetic solvers in optical design",
        abstract=(
            "PINN differentiable electromagnetic solvers establish simulation "
            "credibility and the path from simulation to experiment in "
            "optical design."
        ),
        discovery_route="semantic_scholar_graph",
    )
    assert _is_explicit_current_run_paper(stale) is False
    match = _score_paper_scope(
        contract,
        {"search_text": stale.abstract, "raw_json": "{}"},
        explicit_current_run=False,
    )
    assert match["accepted"] is False
    assert match["reason"] == "generated_only_usefulness_below_threshold"
    assert match["generated_only_precise_side"] is False


def test_generated_only_rejects_unrelated_domains_even_with_gap_overlap():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.object_anchor_mode == "method_fallback"
    off_domain_texts = {
        "stocks": (
            "We simulate near-field and far-field error propagation in stock "
            "market forecasting, using gradient-based prediction accuracy "
            "models."
        ),
        "underwater": (
            "Simulation solver models predict near-field truncation error "
            "and far-field prediction accuracy in underwater acoustic "
            "waveguides."
        ),
        "epidemiology": (
            "Simulation solver models predict near-field truncation error "
            "and far-field prediction accuracy in epidemic spread models."
        ),
    }
    for label, text in off_domain_texts.items():
        match = _scope_match(contract, text)
        assert match["accepted"] is False, label
        assert match["reason"] == (
            "generated_only_usefulness_below_threshold"
        ), label
        assert match["usefulness_score"] < match["usefulness_threshold"], label
        assert match["generated_only_discovery_match"] is True, label


def test_generated_only_retains_indirect_neighbors_and_ranks_direct_first():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    direct = _scope_match(
        contract,
        "PINN differentiable electromagnetic solvers establish simulation "
        "credibility and the path from simulation to experiment, predicting "
        "near-field truncation error and far-field prediction accuracy in "
        "optical simulation.",
    )
    euv_neighbor = _scope_match(
        contract,
        "EUV lithography PINN surrogate models predict near-field "
        "truncation error and far-field prediction accuracy in optical "
        "simulation.",
    )
    em_solver_neighbor = _score_paper_scope(
        contract,
        {
            "search_text": (
                "Differentiable electromagnetic solvers establish simulation "
                "credibility for propagation modeling in optical research."
            ),
            "raw_json": "{}",
        },
        explicit_current_run=True,
    )
    for match, label in (
        (direct, "direct"),
        (euv_neighbor, "euv"),
        (em_solver_neighbor, "em-solver"),
    ):
        assert match["accepted"] is True, label
        assert match["usefulness_score"] >= match["usefulness_threshold"], label
        assert match["usefulness_reason"] == "usefulness_score_accepted", label
        assert match["usefulness_features"], label
    # Direct gap matches rank first; indirect neighbors still survive.
    assert direct["usefulness_score"] > euv_neighbor["usefulness_score"]
    assert euv_neighbor["usefulness_score"] > em_solver_neighbor[
        "usefulness_score"
    ]
    assert direct["generated_only_discovery_match"] is True
    assert em_solver_neighbor["generated_only_discovery_match"] is False
    assert em_solver_neighbor["generated_only_provenance_match"] is True


def test_generated_only_accepted_papers_are_ranked_by_usefulness(
    tmp_path: Path,
) -> None:
    base = _make_generated_only_ordering_base(tmp_path)
    plan = _pinn_generated_only_plan(
        discovery_queries=[
            "near-field truncation error far-field prediction accuracy"
        ]
    )
    plan_path = tmp_path / "ordering_plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    policy = _write_policy(tmp_path)
    result = build_topic_scoped_kb(
        query_plan_path=plan_path,
        base_kb_sqlite=base,
        work_dir=tmp_path / "ordering-run",
        policy_path=policy,
    )
    assert result["status"] == "completed"
    decisions = result["selection"]["papers"]["paper_decisions"]
    accepted = [item for item in decisions if item.get("accepted")]
    assert [item["paper_id"] for item in accepted] == [
        "p-direct",
        "p-euv",
    ]
    assert [item["paper_id"] for item in decisions if not item.get("accepted")] == [
        "p-stock"
    ]
    scores = [item["usefulness_score"] for item in accepted]
    assert scores == sorted(scores, reverse=True)
    conn = sqlite3.connect(result["runtime_kb_sqlite"])
    try:
        rows = [row[0] for row in conn.execute("SELECT paper_id FROM papers")]
    finally:
        conn.close()
    assert rows == ["p-direct", "p-euv"]


def test_generated_only_relevance_context_is_reachable_from_decisions():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    # Legacy plan without a durable relevance_context gets a bounded fallback
    # that is still auditable and deterministic.
    assert contract.relevance_context
    assert contract.relevance_context["search_background_cue"] == (
        contract.search_background_cue
    )
    assert contract.relevance_context_sha256
    match = _scope_match(
        contract,
        "PINN differentiable electromagnetic solvers establish simulation "
        "credibility and predict near-field truncation error and far-field "
        "prediction accuracy in optical simulation.",
    )
    assert match["relevance_context_sha256"] == (
        contract.relevance_context_sha256
    )
    assert match["relevance_context_field_count"] == len(
        contract.relevance_context
    )
    assert contract.to_dict()["relevance_context"] == (
        contract.relevance_context
    )


def test_evaluate_s2_paper_is_side_effect_safe_and_returns_usefulness():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    stage = TopicScopedKBStage(
        query_plan_path="unused-query-plan.json",
        base_kb_sqlite="unused-base.sqlite",
        work_dir="unused-work-dir",
        policy=load_s2_policy(),
        scope_contract=contract,
    )
    on_topic = S2PaperRecord(
        paper_id="p-eval-on",
        title=(
            "PINN differentiable electromagnetic solvers in optical simulation"
        ),
        abstract=(
            "PINN differentiable electromagnetic solvers establish simulation "
            "credibility and predict near-field truncation error and far-field "
            "prediction accuracy in optical simulation."
        ),
        discovery_route="s2_search",
    )
    first = stage.evaluate_s2_paper(on_topic)
    second = stage.evaluate_s2_paper(on_topic)
    assert first == second
    assert first["accepted"] is True
    assert first["usefulness_score"] >= first["usefulness_threshold"]
    assert first["relevance_context_sha256"] == contract.relevance_context_sha256
    # Side-effect safe: no stage state is written by evaluation.
    assert stage._paper_decisions == {}
    assert stage._allowed_paper_ids == set()

    off_domain = S2PaperRecord(
        paper_id="p-eval-off",
        title="Simulation solver models for stock market forecasting",
        abstract=(
            "Simulation solver models predict near-field truncation error "
            "and far-field prediction accuracy in stock market forecasting."
        ),
        discovery_route="s2_search",
    )
    rejected = stage.evaluate_s2_paper(off_domain)
    assert rejected["accepted"] is False
    assert rejected["reason"] == "generated_only_usefulness_below_threshold"
    assert rejected["usefulness_score"] < rejected["usefulness_threshold"]
    assert stage._paper_decisions == {}
    assert stage._allowed_paper_ids == set()


def test_semantic_features_drive_generated_only_usefulness():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    text = (
        "PINN differentiable electromagnetic solvers establish simulation "
        "credibility and predict near-field truncation error and far-field "
        "prediction accuracy in optical simulation."
    )
    row = {"search_text": text, "raw_json": "{}"}
    high = _score_paper_scope(
        contract,
        row,
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.9,
            "max_precise_similarity": 0.9,
            "matched_query": (
                "near-field truncation error far-field prediction accuracy"
            ),
        },
    )
    assert high["accepted"] is True
    assert high["semantic_mode"] == "semantic"
    assert high["background_similarity"] == 0.9
    assert high["max_precise_similarity"] == 0.9
    assert high["matched_precise_query"] == (
        "near-field truncation error far-field prediction accuracy"
    )
    assert high["usefulness_score"] >= high["usefulness_threshold"]

    low = _score_paper_scope(
        contract,
        row,
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.1,
            "max_precise_similarity": 0.1,
            "matched_query": "",
        },
    )
    # Semantic scores rank/audit only; low similarity is not an admission
    # gate in semantic mode.
    assert low["accepted"] is True
    assert low["reason"] == "generated_only_discovery_match"
    assert low["usefulness_score"] < low["usefulness_threshold"]
    assert low["usefulness_features"]["domain_gap_penalty"] > 0

    fallback = _score_paper_scope(
        contract,
        row,
        semantic_features={
            "mode": "lexical_fallback",
            "fallback_error_code": "embedding_failed:RuntimeError",
        },
    )
    assert fallback["accepted"] is True
    assert fallback["semantic_mode"] == "lexical_fallback"
    assert fallback["semantic_fallback_error_code"] == (
        "embedding_failed:RuntimeError"
    )
    assert fallback["usefulness_features"]["semantic_mode"] == (
        "lexical_fallback"
    )


def test_lexical_token_collision_is_audit_only_in_semantic_mode():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    text = (
        "PINN near-field truncation error far-field prediction accuracy."
    )
    row = {"search_text": text, "raw_json": "{}"}
    semantic_zero = _score_paper_scope(
        contract,
        row,
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.0,
            "max_precise_similarity": 0.0,
            "matched_query": "",
        },
    )
    # The paper has a genuine lexical query collision (>=2 distinctive gap
    # tokens), but embeddings are available and say it is unrelated.
    assert semantic_zero["generated_only_discovery_match"] is True
    assert semantic_zero["usefulness_features"]["query_match_score"] > 0
    assert semantic_zero["accepted"] is True
    assert semantic_zero["reason"] == "generated_only_discovery_match"
    assert semantic_zero["semantic_mode"] == "semantic"
    assert semantic_zero["usefulness_score"] < (
        semantic_zero["usefulness_threshold"]
    )
    # The lexical collision adds no score and does not waive the precise-side
    # deficit penalty; it is purely audit while semantic mode is active.
    assert semantic_zero["usefulness_features"]["provenance_gap_penalty"] > 0
    assert semantic_zero["usefulness_features"]["domain_gap_penalty"] > 0

    semantic_high = _score_paper_scope(
        contract,
        row,
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.9,
            "max_precise_similarity": 0.9,
            "matched_query": (
                "near-field truncation error far-field prediction accuracy"
            ),
        },
    )
    assert semantic_high["accepted"] is True
    assert semantic_high["usefulness_score"] >= (
        semantic_high["usefulness_threshold"]
    )
    assert semantic_high["usefulness_features"]["provenance_gap_penalty"] == 0


def test_semantic_mode_precise_relevance_admits_support_paper_without_method_labels():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    # Electromagnetic/optical support paper that never mentions the top-level
    # method labels (PINN / differentiable solver).
    text = (
        "Electromagnetic propagation modeling predicts near-field truncation "
        "error and far-field prediction accuracy in optical simulation."
    )
    match = _score_paper_scope(
        contract,
        {"search_text": text, "raw_json": "{}"},
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.39,
            "max_precise_similarity": 0.67,
            "matched_query": (
                "near-field truncation error far-field prediction accuracy"
            ),
        },
    )
    assert contract.object_anchor_mode == "method_fallback"
    assert match["method_family_identity_present"] is False
    assert match["usefulness_features"]["identity_score"] == 0
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"
    assert match["usefulness_score"] >= match["usefulness_threshold"]
    assert match["usefulness_threshold"] == 40.0
    assert match["matched_precise_query"] == (
        "near-field truncation error far-field prediction accuracy"
    )


def test_semantic_mode_low_precise_retained_with_low_score_for_review():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    text = (
        "PINN models for electromagnetic propagation with near-field "
        "truncation error and far-field prediction accuracy."
    )
    match = _score_paper_scope(
        contract,
        {"search_text": text, "raw_json": "{}"},
        explicit_current_run=True,
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.6,
            "max_precise_similarity": 0.2,
            "matched_query": "",
        },
    )
    assert match["method_family_identity_present"] is True
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"
    assert match["usefulness_score"] < match["usefulness_threshold"]
    # PINN-like overlap is only a small secondary bonus in semantic mode.
    assert match["usefulness_features"]["identity_score"] == 10.0
    assert match["usefulness_features"]["provenance_gap_penalty"] > 0


def test_semantic_mode_retains_unrelated_candidate_and_keeps_hard_exclusions():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    unrelated = (
        "Underwater acoustic propagation with stock market forecasting models."
    )
    retained = _score_paper_scope(
        contract,
        {"search_text": unrelated, "raw_json": "{}"},
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.1,
            "max_precise_similarity": 0.05,
            "matched_query": "",
        },
    )
    assert retained["accepted"] is True
    assert retained["reason"] == "generated_only_usefulness_related"
    assert retained["semantic_mode"] == "semantic"
    assert retained["usefulness_score"] < retained["usefulness_threshold"]
    assert retained["usefulness_features"]["max_precise_similarity"] == 0.05

    excluded = _score_paper_scope(
        contract,
        {
            "search_text": (
                "PINN models for unrelated fluid-only PINN studies with "
                "near-field truncation error and far-field prediction "
                "accuracy."
            ),
            "raw_json": "{}",
        },
        semantic_features={
            "mode": "semantic",
            "background_similarity": 0.9,
            "max_precise_similarity": 0.9,
            "matched_query": (
                "near-field truncation error far-field prediction accuracy"
            ),
        },
    )
    # Explicit exclusion boundaries remain hard rejects in semantic mode.
    assert excluded["accepted"] is False
    assert excluded["reason"] == "exclusion_boundary_match"


def test_stage_semantic_scores_persist_across_evaluations():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    paper = S2PaperRecord(
        paper_id="p-sem",
        title=(
            "PINN differentiable electromagnetic solvers in optical simulation"
        ),
        abstract=(
            "PINN differentiable electromagnetic solvers establish simulation "
            "credibility and predict near-field truncation error and far-field "
            "prediction accuracy in optical simulation."
        ),
        discovery_route="s2_search",
    )

    def make_stage() -> TopicScopedKBStage:
        return TopicScopedKBStage(
            query_plan_path="unused-query-plan.json",
            base_kb_sqlite="unused-base.sqlite",
            work_dir="unused-work-dir",
            policy=load_s2_policy(),
            scope_contract=contract,
        )

    semantic_stage = make_stage()
    semantic_stage.register_semantic_scores(
        {
            "p-sem": {
                "mode": "semantic",
                "background_similarity": 0.1,
                "max_precise_similarity": 0.1,
                "matched_query": "",
            }
        }
    )
    first = semantic_stage.evaluate_s2_paper(paper)
    second = semantic_stage.evaluate_s2_paper(paper)
    assert first == second
    assert first["accepted"] is True
    assert first["semantic_mode"] == "semantic"
    assert first["max_precise_similarity"] == 0.1
    assert first["usefulness_score"] < first["usefulness_threshold"]

    # Without registered semantic scores the same paper passes lexically with
    # a higher score; both paths accept, and the registered features are what
    # the stage keeps using across evaluations.
    lexical_stage = make_stage()
    lexical = lexical_stage.evaluate_s2_paper(paper)
    assert lexical["accepted"] is True
    assert lexical["usefulness_score"] > first["usefulness_score"]
    # No semantic features supplied: plain lexical scoring, no mode claim.
    assert lexical["semantic_mode"] == ""


def test_method_centric_plan_uses_method_fallback_not_workflow_nouns():
    contract = derive_topic_scope_contract(
        _pinn_generated_only_plan(
            discovery_queries=[
                "near-field truncation error far-field prediction accuracy"
            ]
        )
    )
    assert contract.valid
    assert contract.object_anchor_mode == "method_fallback"
    assert contract.scientific_object_anchor_required is False
    assert contract.topic_object_anchors
    assert contract.topic_object_anchors == contract.method_anchors
    assert not any(
        term in {"credibility", "path"}
        for term in contract.topic_object_anchors
    )
    # Broad topic background stays in the scope contract but is never
    # expanded into generated-only search queries.
    assert contract.lenses
    assert contract.inclusion_boundaries
    assert contract.search_queries() == [
        "near-field truncation error far-field prediction accuracy"
    ]
    assert ("pinn",) in contract.method_family_phrases
    assert any(
        "differentiable" in family and "solver" in family
        for family in contract.method_family_phrases
    )
    assert ("simulation", "credibility") not in contract.method_family_phrases


def _metasurface_generated_only_plan(
    discovery_queries: list[str] | None = None,
) -> dict:
    question = (
        "Review measured cooling power of nanophotonic metasurfaces for "
        "radiative cooling."
    )
    queries = discovery_queries or [
        "measured cooling power nanophotonic metasurface"
    ]
    return {
        "input": {"user_query": question},
        "output": {
            "canonical_question": question,
            "problem_understanding": question,
            "scope_definition": {
                "main_scope": (
                    "nanophotonic metasurfaces for radiative cooling"
                ),
                "scope_items": ["measured cooling power"],
                "inclusion_boundaries": [
                    "optical multilayer films",
                    "nanophotonic metasurfaces",
                ],
                "exclusion_boundaries": ["acoustic metalens"],
            },
            "lenses": ["mechanism", "fabrication"],
            "inclusion_boundaries": [
                "optical multilayer films",
                "nanophotonic metasurfaces",
            ],
            "exclusion_boundaries": ["acoustic metalens"],
            "keyword_decomposition": {
                "keywords": list(queries)
            },
        },
        "supplementary_retrieval": {
            "discovery_mode": "generated_only",
            "discovery_queries": queries,
        },
    }


def test_concrete_object_plan_stays_scientific_object():
    contract = derive_topic_scope_contract(_metasurface_generated_only_plan())
    assert contract.valid
    assert contract.object_anchor_mode == "scientific_object"
    assert contract.scientific_object_anchor_required is True
    assert any("metasurface" in term.casefold() for term in contract.topic_object_anchors)
    assert any(
        "metasurface" in term.casefold() for term in contract.object_head_anchors
    )
    text = (
        "Nanophotonic metasurface films achieve measured cooling power of "
        "82 W/m2 with imaging performance across the atmospheric window."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"
    assert any("metasurface" in term.casefold() for term in match["object_anchor_hits"])
    assert contract.search_queries() == [
        "measured cooling power nanophotonic metasurface"
    ]


def _unknown_object_generated_only_plan() -> dict:
    question = (
        "Review measured cooling power of radiative cooling multilayer stacks."
    )
    return {
        "input": {"user_query": question},
        "output": {
            "canonical_question": question,
            "problem_understanding": question,
            "scope_definition": {
                "main_scope": "radiative cooling multilayer stacks",
                "scope_items": ["measured cooling power"],
                "inclusion_boundaries": ["optical multilayer films"],
                "exclusion_boundaries": ["acoustic metalens"],
            },
            "lenses": ["mechanism", "fabrication"],
            "inclusion_boundaries": ["optical multilayer films"],
            "exclusion_boundaries": ["acoustic metalens"],
            "keyword_decomposition": {
                "keywords": ["measured cooling power multilayer"]
            },
        },
        "supplementary_retrieval": {
            "discovery_mode": "generated_only",
            "discovery_queries": ["measured cooling power multilayer"],
        },
    }


def test_unknown_object_without_method_stays_scientific_object():
    contract = derive_topic_scope_contract(_unknown_object_generated_only_plan())
    assert contract.valid
    assert contract.object_anchor_mode == "scientific_object"
    assert contract.scientific_object_anchor_required is True
    assert "multilayer" in contract.topic_object_anchors
    assert "stack" in contract.topic_object_anchors
    assert not any(
        term in {"credibility", "path", "pinn", "solver"}
        for term in contract.topic_object_anchors
    )
    assert contract.lenses
    assert contract.inclusion_boundaries
    assert contract.search_queries() == ["measured cooling power multilayer"]
    text = (
        "Radiative cooling multilayer stacks achieve measured cooling power "
        "of 82 W/m2."
    )
    match = _scope_match(contract, text)
    assert match["accepted"] is True
    assert match["reason"] == "generated_only_discovery_match"


def test_generated_only_rejects_unrelated_and_exclusion_hits():
    contract = derive_topic_scope_contract(
        _generated_only_plan(
            discovery_queries=["measured cooling power multilayer inverse design"]
        )
    )
    unrelated = (
        "Quantum dot displays achieve high color conversion efficiency "
        "for perovskite emitters."
    )
    assert _scope_match(contract, unrelated)["accepted"] is False
    excluded = (
        "Inverse-designed multilayer films with measured cooling power "
        "for acoustic metalens applications."
    )
    match = _scope_match(contract, excluded)
    assert match["accepted"] is False
    assert match["reason"] == "exclusion_boundary_match"


def test_ordinary_first_round_filtering_is_unchanged():
    ordinary = derive_topic_scope_contract(_query_plan())
    gap_only_text = (
        "We measure the cooling power of inverse-designed multilayer films; "
        "the measured cooling power reaches 82 W/m2."
    )
    match = _scope_match(ordinary, gap_only_text)
    assert match["accepted"] is False
    assert match["reason"] in {"topic_object_anchor_miss", "generic_shared_term_only"}
    assert match["discovery_mode"] == ""
    # Ordinary first-round decisions carry no generated-only usefulness keys.
    assert "usefulness_score" not in match
    assert "matched_precise_query" not in match


def test_overlay_migrates_legacy_schema_and_isolates_scope(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    result = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tmp_path / "run",
        policy_path=policy,
    )
    assert result["status"] == "completed"
    overlay = Path(result["runtime_kb_sqlite"])
    conn = sqlite3.connect(overlay)
    assert {
        row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()
    } >= {"scope_fit", "use_permission", "content_depth", "route_provenance_json"}
    assert [row[0] for row in conn.execute("SELECT paper_id FROM papers ORDER BY paper_id")] == [
        "p-metalens"
    ]
    assert {
        row[0]
        for row in conn.execute("SELECT chunk_id FROM text_chunks ORDER BY chunk_id")
    } == {"c-generic-in-scope-paper", "c-relevant"}
    assert conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
    conn.close()

    manifest = json.loads(
        (tmp_path / "run" / "KB_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "optomind.topic_scoped_kb_manifest.v1"
    # Permission counts cover both the scoped paper row and its two chunks;
    # factual evidence counts are chunk-specific below.
    assert manifest["provenance_counts"]["permission"]["factual_support"] == 3
    assert manifest["evidence"]["factual_support_chunk_count"] == 2


def test_metadata_and_discovery_only_rows_never_become_factual_evidence(tmp_path: Path):
    base = _make_base_kb(tmp_path, metadata_only=True)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    result = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tmp_path / "metadata-run",
        policy_path=policy,
    )
    assert result["status"] == "needs_more_literature"
    evidence = result["evidence"]
    assert evidence["evidence_eligible_chunk_count"] == 0
    assert evidence["factual_support_chunk_count"] == 0
    permissions = result["provenance_counts"]["permission"]
    assert permissions["discovery_only"] >= 1


def test_failed_status_is_reserved_for_invalid_stage_inputs(tmp_path: Path):
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    result = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=tmp_path / "missing.sqlite",
        work_dir=tmp_path / "failed-run",
        policy_path=policy,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "topic_scoped_kb_stage_failed"


def test_extra_notes_populate_forbidden_and_allowed_regimes():
    """output.extra_notes with explicit boundary language must populate regime constraints."""
    plan = {
        "input": {"user_query": "Review broadband achromatic metalens design."},
        "output": {
            **_query_plan()["output"],
            "extra_notes": (
                "This review is limited to optical/near-IR metasurfaces. "
                "Do not include microwave metamaterials."
            ),
        },
    }
    contract = derive_topic_scope_contract(plan)
    assert contract.valid
    assert "microwave_rf" in contract.forbidden_regimes
    assert "optical_near_ir" in contract.allowed_regimes


def test_extra_notes_without_boundary_language_do_not_add_regime_constraints():
    """Descriptive prose in extra_notes must not silently create regime deny-list entries."""
    plan = {
        "input": {"user_query": "Review broadband achromatic metalens design."},
        "output": {
            **_query_plan()["output"],
            "extra_notes": (
                "Nanophotonics and integrated photonics are active research areas "
                "adjacent to metalens technology."
            ),
        },
    }
    contract = derive_topic_scope_contract(plan)
    assert contract.valid
    assert not contract.forbidden_regimes
    assert not contract.allowed_regimes


def test_real_planner_contrast_separates_objects_methods_and_regimes():
    plan = {
        "input": {
            "user_query": (
                "How do physics-informed neural networks and differentiable "
                "electromagnetic solvers compare for inverse design of "
                "nanophotonic metasurfaces?"
            )
        },
        "output": {
            "problem_understanding": "Compare two inverse-design method families.",
            "scope_definition": {
                "main_scope": "Nanophotonic metasurface inverse design.",
                "scope_items": [
                    "Physics-informed neural networks for nanophotonic metasurfaces",
                    "Differentiable solvers for metasurface inverse design",
                ],
            },
            "keyword_decomposition": {
                "keywords": [
                    "physics-informed neural network nanophotonic inverse design",
                    "differentiable electromagnetic solver metasurface design",
                ]
            },
            "extra_notes": (
                "The scope is limited to nanophotonic metasurfaces rather than "
                "broader photonic or microwave metamaterial domains."
            ),
        },
    }

    contract = derive_topic_scope_contract(plan)

    assert contract.valid
    assert contract.allowed_regimes == ("optical_near_ir",)
    assert contract.forbidden_regimes == ("microwave_rf",)
    assert contract.object_anchor_mode == "scientific_object"
    assert contract.scientific_object_anchor_required is True
    assert {"nanophotonic", "metasurface"} <= set(contract.topic_object_anchors)
    assert "metasurface" in contract.object_head_anchors
    assert "nanophotonic" in contract.object_modifier_anchors
    assert "nanophotonic metasurface" in contract.compound_object_phrases
    assert not {
        "differentiable",
        "solver",
        "informed",
        "network",
    } & set(contract.topic_object_anchors)
    assert {"differentiable", "solver", "informed", "network"} <= set(
        contract.method_anchors
    )
    assert contract.explicit_boundary_notes == (plan["output"]["extra_notes"],)


def test_method_centric_question_uses_auditable_method_fallback():
    plan = {
        "input": {
            "user_query": (
                "How do physics-informed neural networks compare with "
                "differentiable solvers?"
            )
        },
        "output": {
            "scope_definition": {
                "main_scope": "Compare physics-informed neural networks and differentiable solvers.",
                "scope_items": ["Training", "Automatic differentiation"],
            },
            "keyword_decomposition": {
                "keywords": [
                    "physics-informed neural network",
                    "differentiable solver",
                ]
            },
            "extra_notes": "",
        },
    }

    contract = derive_topic_scope_contract(plan)

    assert contract.valid
    assert contract.object_anchor_mode == "method_fallback"
    assert contract.scientific_object_anchor_required is False
    assert contract.topic_object_anchors
    assert set(contract.topic_object_anchors) <= set(contract.method_anchors)


def test_negated_positive_boundary_does_not_create_allowed_only_regime():
    plan = {
        "input": {"user_query": "Review metasurface operation across spectral regimes."},
        "output": {
            "scope_definition": {
                "main_scope": "Metasurface operation across spectral regimes.",
                "scope_items": ["Metasurface spectral response"],
            },
            "keyword_decomposition": {"keywords": ["metasurface spectral response"]},
            "extra_notes": "The review is not limited to optical wavelengths.",
        },
    }

    contract = derive_topic_scope_contract(plan)

    assert contract.valid
    assert not contract.allowed_regimes
    assert not contract.forbidden_regimes


def test_status_is_partial_when_evidence_exists_but_target_is_not_met(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path, target_papers=(2, 2))
    result = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tmp_path / "partial-run",
        policy_path=policy,
    )
    assert result["status"] == "partial"
    assert result["evidence"]["factual_support_chunk_count"] == 2
    assert result["evidence"]["target_reached"] is False


def test_s2_query_telemetry_includes_graph_categories_and_zeroes():
    telemetry = build_s2_query_telemetry(
        discovery_runs=[{"query": "metalens", "status_category": "ok", "result_count": 3}],
        snippet_runs=[{"query": "group delay", "status_category": "ok", "result_count": 1}],
        enrichment_runs=[{"status_category": "cached", "result_count": 2}],
        graph_runs=[
            {"channel": "references", "status_category": "ok", "result_count": 4},
            {"channel": "citations", "status_category": "empty", "result_count": 0},
            {"channel": "recommendations", "status_category": "ok", "result_count": 2},
            {"channel": "multi_seed_recommendations", "status_category": "ok", "result_count": 1},
        ],
    )
    assert telemetry["total_query_count"] == 7
    assert telemetry["graph_query_count"] == 4
    assert telemetry["category_counts"]["references"] == 1
    assert telemetry["category_counts"]["citations"] == 1
    assert telemetry["category_counts"]["title_match"] == 0
    assert {event["query_category"] for event in telemetry["events"]} >= {
        "discovery_search",
        "snippet_search",
        "batch_enrichment",
        "references",
        "citations",
        "recommendations",
        "multi_seed_recommendations",
    }


def test_manifest_is_immutable_and_second_build_is_reused(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / "immutable-run"
    telemetry = build_s2_query_telemetry(
        discovery_runs=[{"query": "metalens", "status_category": "ok"}]
    )
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        query_telemetry=telemetry,
    )
    manifest_path = work_dir / "KB_MANIFEST.json"
    before = manifest_path.read_bytes()
    second = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        query_telemetry=telemetry,
    )
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert second["reused"] is True
    assert manifest_path.read_bytes() == before


def test_changed_telemetry_rejects_reuse_without_overwriting(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / "telemetry-change"
    first_telemetry = build_s2_query_telemetry(
        discovery_runs=[{"query": "metalens", "status_category": "ok"}]
    )
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        query_telemetry=first_telemetry,
    )
    assert first["status"] == "completed"
    before = {
        path.name: path.read_bytes()
        for path in (
            work_dir / "KB_MANIFEST.json",
            work_dir / "review_knowledge_base.s2.sqlite",
            work_dir / "S2_QUERY_TELEMETRY.json",
        )
    }
    rejected = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        query_telemetry=build_s2_query_telemetry(
            discovery_runs=[{"query": "different", "status_category": "ok"}]
        ),
    )
    assert rejected["status"] == "isolated_rebuild_available"
    assert rejected["reused"] is False
    assert "current build request" in rejected["error"]
    assert list(work_dir.glob("_stale_scoped_kb_*"))


def test_contract_mismatch_relocates_sqlite_sidecars_to_stale_archive(
    tmp_path: Path,
):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / "sidecar-run"
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
    )
    assert first["status"] == "completed"

    runtime = work_dir / "review_knowledge_base.s2.sqlite"
    wal = work_dir / (runtime.name + "-wal")
    shm = work_dir / (runtime.name + "-shm")
    wal.write_bytes(b"stale-wal")
    shm.write_bytes(b"stale-shm")

    rejected = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        query_telemetry=build_s2_query_telemetry(
            discovery_runs=[{"query": "different", "status_category": "ok"}]
        ),
    )

    assert rejected["status"] == "isolated_rebuild_available"
    assert rejected["reused"] is False
    assert not wal.exists()
    assert not shm.exists()
    assert not (work_dir / "KB_MANIFEST.json").exists()
    assert not runtime.exists()
    assert not (work_dir / "S2_QUERY_TELEMETRY.json").exists()

    stale_dirs = list(work_dir.glob("_stale_scoped_kb_*"))
    assert len(stale_dirs) == 1
    archive = stale_dirs[0]
    assert (archive / runtime.name).is_file()
    assert (archive / wal.name).is_file()
    assert (archive / shm.name).is_file()
    assert (archive / "KB_MANIFEST.json").is_file()
    assert (archive / "S2_QUERY_TELEMETRY.json").is_file()


def test_scoped_stage_accepts_s2_records_without_promoting_metadata(tmp_path: Path):
    base = _make_base_kb(tmp_path, metadata_only=True)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    s2_paper = S2PaperRecord(
        paper_id="s2-metadata",
        title="Broadband achromatic metalens imaging",
        abstract="A metadata record for a metalens paper.",
        content_depth="metadata",
        use_permission="discovery_only",
    )
    s2_chunk = UnifiedTextChunk(
        chunk_id="s2-metadata-chunk",
        paper_id="s2-metadata",
        title=s2_paper.title,
        text="Broadband achromatic metalens metadata only.",
        content_depth="metadata",
        use_permission="discovery_only",
        allowed_claim_kinds=["discovery"],
    )
    result = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tmp_path / "s2-metadata-run",
        policy_path=policy,
        papers=[s2_paper],
        chunks=[s2_chunk],
    )
    assert result["status"] == "needs_more_literature"
    assert result["evidence"]["factual_support_chunk_count"] == 0
    assert result["provenance_counts"]["permission"]["discovery_only"] >= 2


def _incoming_scope_records(*, marker: str = "baseline"):
    paper = S2PaperRecord(
        paper_id="s2-reuse-paper",
        title="Broadband achromatic metalens imaging",
        abstract=(
            "Broadband achromatic metalens group delay dispersion for optical "
            f"imaging. {marker}"
        ),
    )
    chunk = UnifiedTextChunk(
        chunk_id="s2-reuse-chunk",
        paper_id=paper.paper_id,
        title=paper.title,
        text=(
            "Broadband achromatic metalens group delay dispersion controls "
            f"optical imaging. {marker}"
        ),
        content_depth="structured_snippet",
        context_complete=True,
    )
    graph = LiteratureGraph(
        nodes={paper.paper_id: paper},
        node_annotations={paper.paper_id: {"fixture_marker": marker}},
    )
    return paper, chunk, graph


def test_generator_inputs_are_materialized_once_and_can_be_reused(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / "generator-reuse"
    paper, chunk, graph = _incoming_scope_records()
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        papers=(item for item in [paper]),
        chunks=(item for item in [chunk]),
        graph=graph,
    )
    assert first["status"] in {"completed", "partial", "needs_more_literature"}
    paper, chunk, graph = _incoming_scope_records()
    second = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        papers=(item for item in [paper]),
        chunks=(item for item in [chunk]),
        graph=graph,
    )
    assert second["reused"] is True


@pytest.mark.parametrize("changed_input", ["paper", "chunk", "graph"])
def test_incoming_scientific_input_change_rejects_reuse(
    tmp_path: Path,
    changed_input: str,
):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / changed_input
    paper, chunk, graph = _incoming_scope_records()
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        papers=[paper],
        chunks=[chunk],
        graph=graph,
    )
    assert first["status"] != "failed"
    before = (work_dir / "KB_MANIFEST.json").read_bytes()
    changed_marker = "changed" if changed_input in {"paper", "chunk"} else "baseline"
    paper, chunk, graph = _incoming_scope_records(marker=changed_marker)
    if changed_input == "paper":
        chunk = _incoming_scope_records()[1]
        graph = LiteratureGraph(nodes={paper.paper_id: paper})
    elif changed_input == "chunk":
        baseline_paper, _, baseline_graph = _incoming_scope_records()
        paper = baseline_paper
        graph = baseline_graph
    else:
        graph.node_annotations[paper.paper_id]["new_scientific_role"] = "frontier"
    rejected = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
        papers=[paper],
        chunks=[chunk],
        graph=graph,
    )
    assert rejected["status"] == "isolated_rebuild_available"
    assert rejected["reused"] is False
    assert list(work_dir.glob("_stale_scoped_kb_*"))


def test_static_input_and_rule_changes_reject_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import optomind_research.runtime.topic_scoped_kb_stage as scoped_module

    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)
    work_dir = tmp_path / "static-change"
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
    )
    assert first["status"] == "completed"
    manifest_path = work_dir / "KB_MANIFEST.json"
    manifest_before = manifest_path.read_bytes()
    query_before = query.read_bytes()
    base_before = base.read_bytes()
    policy_before = policy.read_bytes()

    def assert_isolated(label: str, mutate) -> None:
        local_work = tmp_path / f"static-change-{label}"
        first_local = build_topic_scoped_kb(
            query_plan_path=query,
            base_kb_sqlite=base,
            work_dir=local_work,
            policy_path=policy,
        )
        assert first_local["status"] == "completed"
        mutate()
        try:
            result = build_topic_scoped_kb(
                query_plan_path=query,
                base_kb_sqlite=base,
                work_dir=local_work,
                policy_path=policy,
            )
            assert result["status"] == "isolated_rebuild_available"
            assert list(local_work.glob("_stale_scoped_kb_*"))
        finally:
            query.write_bytes(query_before)
            base.write_bytes(base_before)
            policy.write_bytes(policy_before)

    assert_isolated(
        "query",
        lambda: query.write_text(
            json.dumps(_query_plan(exclusions=["microwave metalens"])),
            encoding="utf-8",
        ),
    )
    assert_isolated(
        "base",
        lambda: base.write_bytes(base_before + b"input-change"),
    )

    def mutate_policy() -> None:
        payload = json.loads(policy.read_text(encoding="utf-8"))
        payload["evidence"]["minimum_factual_chunks"] = 2
        policy.write_text(json.dumps(payload), encoding="utf-8")

    assert_isolated("policy", mutate_policy)

    original_rule = scoped_module.SCOPE_DECISION_RULE_VERSION
    assert_isolated(
        "rule",
        lambda: monkeypatch.setattr(
            scoped_module,
            "SCOPE_DECISION_RULE_VERSION",
            original_rule + ".changed",
        ),
    )
    monkeypatch.setattr(
        scoped_module,
        "SCOPE_DECISION_RULE_VERSION",
        original_rule,
    )


def test_runtime_tamper_and_legacy_manifest_are_not_reused(tmp_path: Path):
    base = _make_base_kb(tmp_path)
    query = _write_query_plan(tmp_path)
    policy = _write_policy(tmp_path)

    tamper_dir = tmp_path / "runtime-tamper"
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tamper_dir,
        policy_path=policy,
    )
    assert first["status"] == "completed"
    runtime = tamper_dir / "review_knowledge_base.s2.sqlite"
    tampered = runtime.read_bytes() + b"tampered"
    runtime.write_bytes(tampered)
    rejected = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=tamper_dir,
        policy_path=policy,
    )
    assert rejected["status"] == "failed"
    assert runtime.read_bytes() == tampered

    legacy_dir = tmp_path / "legacy-manifest"
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=legacy_dir,
        policy_path=policy,
    )
    assert first["status"] == "completed"
    manifest_path = legacy_dir / "KB_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("reuse_contract", None)
    manifest.pop("scope_decision_rule_version", None)
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=legacy_dir,
        policy_path=policy,
    )
    assert rejected["status"] == "isolated_rebuild_available"
    assert rejected["reused"] is False


def test_path_only_migration_reuses_identical_content(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    base = _make_base_kb(source)
    query = _write_query_plan(source)
    policy = _write_policy(source)
    work_dir = source / "work"
    first = build_topic_scoped_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work_dir,
        policy_path=policy,
    )
    assert first["status"] == "completed"

    migrated = tmp_path / "migrated"
    migrated.mkdir()
    migrated_base = migrated / base.name
    migrated_query = migrated / query.name
    migrated_policy = migrated / policy.name
    shutil.copy2(base, migrated_base)
    shutil.copy2(query, migrated_query)
    shutil.copy2(policy, migrated_policy)
    migrated_work = migrated / "work"
    shutil.copytree(work_dir, migrated_work)

    reused = build_topic_scoped_kb(
        query_plan_path=migrated_query,
        base_kb_sqlite=migrated_base,
        work_dir=migrated_work,
        policy_path=migrated_policy,
    )
    assert reused["reused"] is True
    assert Path(reused["runtime_kb_sqlite"]) == (
        migrated_work / "review_knowledge_base.s2.sqlite"
    )
