from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import optomind_research.s2_harness_bootstrap as bootstrap_module
from optomind_research.s2_harness_bootstrap import prepare_s2_harness_kb
from optomind_research.s2_literature_graph import LiteratureGraph
from optomind_research.s2_schemas import S2PaperRecord


def _query_plan() -> dict:
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


def _write_query(path: Path) -> Path:
    path.write_text(json.dumps(_query_plan()), encoding="utf-8")
    return path


def _write_policy(path: Path, *, enabled: bool = False) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "s2_first": {"enabled": enabled},
                "standard": {
                    "accepted_s2_text_papers_per_facet": [1, 1],
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


def _build_fixture(root: Path) -> tuple[Path, Path, Path, Path, dict]:
    root.mkdir(parents=True, exist_ok=True)
    query = _write_query(root / "query.json")
    policy = _write_policy(root / "policy.json")
    base = _write_empty_base(root / "base.sqlite")
    work = root / "work"
    report = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
        results_limit=2,
        snippet_limit=3,
    )
    assert report["status"] == "needs_more_literature"
    assert report["reused"] is False
    assert report["report_sha256"]
    return query, policy, base, work, report


def _artifact_bytes(work: Path) -> dict[str, bytes]:
    return {
        name: (work / name).read_bytes()
        for name in (
            "S2_BOOTSTRAP_REPORT.json",
            "KB_MANIFEST.json",
            "review_knowledge_base.s2.sqlite",
            "S2_LITERATURE_GRAPH.json",
            "S2_QUERY_TELEMETRY.json",
        )
        if (work / name).exists()
    }


def test_identical_bootstrap_inputs_reuse_without_external_calls(tmp_path: Path):
    query, policy, base, work, first = _build_fixture(tmp_path)
    before = _artifact_bytes(work)
    reused = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
        results_limit=2,
        snippet_limit=3,
    )
    assert reused["reused"] is True
    assert reused["persisted_report_sha256"] == first["report_sha256"]
    assert _artifact_bytes(work) == before


@pytest.mark.parametrize(
    "changed_input",
    ["query", "base", "policy", "results_limit", "snippet_limit", "rule"],
)
def test_changed_bootstrap_input_rejects_before_external_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_input: str,
):
    query, policy, base, work, _ = _build_fixture(tmp_path)
    before = _artifact_bytes(work)
    results_limit = 2
    snippet_limit = 3

    if changed_input == "query":
        changed = _query_plan()
        changed["output"]["scope_definition"]["scope_items"].append(
            "Large aperture fabrication"
        )
        query.write_text(json.dumps(changed), encoding="utf-8")
    elif changed_input == "base":
        base.write_bytes(base.read_bytes() + b"changed-input")
    elif changed_input == "policy":
        _write_policy(policy, enabled=True)
    elif changed_input == "results_limit":
        results_limit = 4
    elif changed_input == "snippet_limit":
        snippet_limit = 5
    else:
        monkeypatch.setattr(
            bootstrap_module,
            "SCOPE_DECISION_RULE_VERSION",
            bootstrap_module.SCOPE_DECISION_RULE_VERSION + ".changed",
        )

    external_constructions: list[str] = []

    def bomb(*args, **kwargs):
        external_constructions.append("called")
        raise AssertionError("external constructor must not run during reuse preflight")

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", bomb)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", bomb)
    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", bomb)
    monkeypatch.setattr(bootstrap_module, "S2FulltextAcquirer", bomb)

    rejected = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
        results_limit=results_limit,
        snippet_limit=snippet_limit,
    )
    assert rejected["status"] in {"failed", "isolated_rebuild_available"}
    if rejected["status"] == "isolated_rebuild_available":
        assert rejected["error_code"] == "s2_bootstrap_reuse_contract_mismatch"
        assert rejected["isolated_rebuild_available"] is True
        # Contract mismatches relocate the old artifacts into a
        # _stale_bootstrap_* directory; the original work-dir bytes no longer
        # exist at their old paths.
        assert list(work.glob("_stale_bootstrap_*"))
        assert _artifact_bytes(work) == {}
    else:
        # Contract-validity failures (for example a scope-decision rule
        # version change makes the stored reuse contract invalid) remain
        # failed and leave the occupied artifacts untouched in place.
        assert rejected["reuse_rejected"] is True
        assert _artifact_bytes(work) == before
    assert external_constructions == []


@pytest.mark.parametrize(
    "artifact",
    ["report", "manifest", "runtime", "graph", "telemetry", "incomplete"],
)
def test_bootstrap_artifact_tamper_or_incomplete_set_is_rejected(
    tmp_path: Path,
    artifact: str,
):
    query, policy, base, work, _ = _build_fixture(tmp_path)
    if artifact == "report":
        path = work / "S2_BOOTSTRAP_REPORT.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["query"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif artifact == "manifest":
        path = work / "KB_MANIFEST.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = "completed"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif artifact == "runtime":
        path = work / "review_knowledge_base.s2.sqlite"
        path.write_bytes(path.read_bytes() + b"tampered")
    elif artifact == "graph":
        path = work / "S2_LITERATURE_GRAPH.json"
        path.write_text('{"tampered": true}', encoding="utf-8")
    elif artifact == "telemetry":
        path = work / "S2_QUERY_TELEMETRY.json"
        path.write_text('{"tampered": true}', encoding="utf-8")
    else:
        (work / "S2_LITERATURE_GRAPH.json").unlink()
    before = _artifact_bytes(work)

    rejected = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
        results_limit=2,
        snippet_limit=3,
    )
    assert rejected["status"] == "failed"
    assert rejected["reuse_rejected"] is True
    assert _artifact_bytes(work) == before


def test_old_report_without_reuse_contract_is_rejected(tmp_path: Path):
    query, policy, base, work, _ = _build_fixture(tmp_path)
    report_path = work / "S2_BOOTSTRAP_REPORT.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("reuse_contract", None)
    report.pop("report_sha256", None)
    report["report_sha256"] = bootstrap_module._canonical_sha256(report)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    before = report_path.read_bytes()

    rejected = prepare_s2_harness_kb(
        query_plan_path=query,
        base_kb_sqlite=base,
        work_dir=work,
        policy_path=policy,
        results_limit=2,
        snippet_limit=3,
    )
    assert rejected["status"] == "failed"
    assert rejected["reuse_rejected"] is True
    assert report_path.read_bytes() == before


def test_bootstrap_path_only_migration_reuses_current_stage_files(tmp_path: Path):
    query, policy, base, work, _ = _build_fixture(tmp_path / "source")
    migrated = tmp_path / "migrated"
    migrated.mkdir()
    migrated_query = migrated / query.name
    migrated_policy = migrated / policy.name
    migrated_base = migrated / base.name
    shutil.copy2(query, migrated_query)
    shutil.copy2(policy, migrated_policy)
    shutil.copy2(base, migrated_base)
    migrated_work = migrated / "work"
    shutil.copytree(work, migrated_work)

    reused = prepare_s2_harness_kb(
        query_plan_path=migrated_query,
        base_kb_sqlite=migrated_base,
        work_dir=migrated_work,
        policy_path=migrated_policy,
        results_limit=2,
        snippet_limit=3,
    )
    assert reused["reused"] is True
    assert Path(reused["runtime_kb_sqlite"]) == (
        migrated_work / "review_knowledge_base.s2.sqlite"
    )
    assert Path(reused["kb_manifest_path"]) == migrated_work / "KB_MANIFEST.json"


def test_generated_only_plan_forwards_direct_only_to_discovery(
    tmp_path: Path, monkeypatch
):
    def _write_enabled_policy(path: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "s2_first": {"enabled": True},
                    "standard": {
                        "accepted_s2_text_papers_per_facet": [1, 3],
                        "graph_depth": 0,
                        "max_search_queries": 12,
                        "max_snippet_queries": 12,
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

    query_path = _write_query(tmp_path / "query.json")
    plan = json.loads(query_path.read_text(encoding="utf-8"))
    plan["supplementary_retrieval"] = {
        "discovery_mode": "generated_only",
        "discovery_queries": [
            "measured cooling power multilayer inverse design",
            "fabrication tolerance radiative cooling multilayer",
        ],
    }
    query_path.write_text(json.dumps(plan), encoding="utf-8")
    policy_path = _write_enabled_policy(tmp_path / "policy.json")
    base_path = _write_empty_base(tmp_path / "base.sqlite")
    work = tmp_path / "work"
    captured: dict = {}

    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, facets, **kwargs):
            captured["facets"] = facets
            return bootstrap_module._empty_portfolio()

    class FakeRetriever:
        def __init__(self, **kwargs):
            pass

        def retrieve(self, *args, **kwargs):
            return bootstrap_module._empty_chunks()

        def retrieve_precise_missing_papers(self, *args, **kwargs):
            return bootstrap_module._empty_chunks()

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", FakeDiscovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", FakeRetriever)
    report = prepare_s2_harness_kb(
        query_plan_path=query_path,
        base_kb_sqlite=base_path,
        work_dir=work,
        policy_path=policy_path,
        results_limit=3,
        snippet_limit=3,
    )
    assert report["status"] != "failed"
    facet = captured["facets"][0]
    assert facet.direct_only is True
    assert list(facet.queries) == [
        "measured cooling power multilayer inverse design",
        "fabrication tolerance radiative cooling multilayer",
    ]
    assert report["discovery_direct_only"] is True

    # Ordinary plans keep direct_only=False and existing discovery behavior.
    ordinary_query = _write_query(tmp_path / "ordinary_query.json")
    ordinary_policy = _write_enabled_policy(tmp_path / "ordinary_policy.json")
    ordinary_work = tmp_path / "ordinary_work"
    captured.clear()
    report_ordinary = prepare_s2_harness_kb(
        query_plan_path=ordinary_query,
        base_kb_sqlite=base_path,
        work_dir=ordinary_work,
        policy_path=ordinary_policy,
        results_limit=3,
        snippet_limit=3,
    )
    assert report_ordinary["status"] != "failed"
    assert captured["facets"][0].direct_only is False
    assert report_ordinary["discovery_direct_only"] is False


def test_generated_only_suppresses_graph_expansion_unless_opted_in(
    tmp_path: Path, monkeypatch
):
    def _write_policy(path: Path, graph_depth: int) -> Path:
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "s2_first": {
                        "enabled": True,
                        "download_high_value_oa_without_llm_gate": False,
                    },
                    "standard": {
                        "accepted_s2_text_papers_per_facet": [1, 3],
                        "graph_depth": graph_depth,
                        "max_search_queries": 12,
                        "max_snippet_queries": 12,
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

    captured: dict = {}

    class FakeDiscovery:
        def __init__(self, **kwargs):
            pass

        def discover(self, facets, **kwargs):
            captured["facets"] = facets
            paper = S2PaperRecord(
                paper_id="p1",
                title="Measured cooling power of inverse-designed multilayer structures",
                abstract=(
                    "We measure cooling power for multilayer inverse designs "
                    "and report broadband achromatic metalens imaging with "
                    "group delay dispersion across the visible spectrum."
                ),
                year=2024,
            )
            return bootstrap_module.DiscoveryPortfolio(
                candidates=[SimpleNamespace(decision="retain", paper=paper)],
                query_runs=[{"query": "explicit gap query", "status_code": 200}],
                pool_counts={},
            )

    class FakeRetriever:
        def __init__(self, **kwargs):
            pass

        def retrieve(self, *args, **kwargs):
            return bootstrap_module._empty_chunks()

        def retrieve_precise_missing_papers(self, *args, **kwargs):
            return bootstrap_module._empty_chunks()

    class FakeGraphBuilder:
        def __init__(self):
            captured["graph_builder_calls"] = (
                captured.get("graph_builder_calls", 0) + 1
            )

        def expand_from_seeds(self, frontier, **kwargs):
            captured["expand_calls"] = captured.get("expand_calls", 0) + 1
            return LiteratureGraph(
                query_runs=[{"query_category": "graph_references"}]
            )

        @staticmethod
        def add_snippet_reference_mentions(graph, chunks):
            captured["ref_mentions"] = True

    monkeypatch.setattr(bootstrap_module, "S2DiscoveryPortfolioBuilder", FakeDiscovery)
    monkeypatch.setattr(bootstrap_module, "S2TextChunkRetriever", FakeRetriever)
    monkeypatch.setattr(bootstrap_module, "S2LiteratureGraphBuilder", FakeGraphBuilder)

    def _run(plan: dict, work_dir: Path) -> dict:
        captured.clear()
        query_path = tmp_path / f"{work_dir.name}_query.json"
        query_path.write_text(json.dumps(plan), encoding="utf-8")
        policy_path = _write_policy(tmp_path / f"{work_dir.name}_policy.json", 2)
        base_path = _write_empty_base(tmp_path / f"{work_dir.name}_base.sqlite")
        report = prepare_s2_harness_kb(
            query_plan_path=query_path,
            base_kb_sqlite=base_path,
            work_dir=work_dir,
            policy_path=policy_path,
            results_limit=3,
            snippet_limit=3,
        )
        return report

    base_plan = _query_plan()
    base_plan["supplementary_retrieval"] = {
        "discovery_mode": "generated_only",
        "discovery_queries": ["measured cooling power multilayer inverse design"],
    }

    suppressed = _run(base_plan, tmp_path / "suppressed")
    assert suppressed["status"] != "failed"
    assert suppressed["discovery_direct_only"] is True
    assert suppressed["graph_expansion_suppressed"] is True
    assert suppressed["graph_expansion_allowed"] is False
    assert captured.get("graph_builder_calls", 0) == 0
    assert captured.get("expand_calls", 0) == 0
    assert not any(
        run.get("query_category") == "graph_references"
        for run in suppressed["external_query_runs"]
    )

    opted_in_plan = json.loads(json.dumps(base_plan))
    opted_in_plan["supplementary_retrieval"]["allow_graph_expansion"] = True
    opted_in = _run(opted_in_plan, tmp_path / "opted_in")
    assert opted_in["status"] != "failed"
    assert opted_in["graph_expansion_suppressed"] is False
    assert opted_in["graph_expansion_allowed"] is True
    assert captured.get("graph_builder_calls", 0) >= 1
    assert captured.get("expand_calls", 0) >= 1
    assert any(
        run.get("query_category") == "graph_references"
        for run in opted_in["external_query_runs"]
    )

    ordinary = _run(_query_plan(), tmp_path / "ordinary")
    assert ordinary["status"] != "failed"
    assert ordinary["discovery_direct_only"] is False
    assert ordinary["graph_expansion_suppressed"] is False
    assert ordinary["graph_expansion_allowed"] is True
    assert captured.get("graph_builder_calls", 0) >= 1
