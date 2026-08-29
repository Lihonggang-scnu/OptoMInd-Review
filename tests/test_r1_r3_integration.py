from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from optomind_research.runtime.coverage_atlas import build_coverage_atlas
from optomind_research.runtime.legacy_asset_migration import migrate_source_ledger
from optomind_research.runtime.multi_wave_discovery_controller import (
    MultiWaveDiscoveryController,
)
from optomind_research.runtime.review_quality_contract import (
    assess_structured_snippet,
    evaluate_discovery_stop,
)
from optomind_research.runtime.review_scope_map import build_review_scope_map
from optomind_research.runtime.section_authoring_assets import build_canonical_asset_graph
from optomind_research.runtime.synthesis_bundle import build_synthesis_bundle
from optomind_research.runtime.semantic_relation_classifier import SemanticRelationClassifier
from optomind_research.s2_missing_chunk_recovery import recover_missing_chunks
from optomind_research.s2_candidate_ranker import S2CandidateRanker
from optomind_research.s2_intelligence_gateway import S2GatewayResponse
from optomind_research.s2_schemas import LiteratureGraphEdge, S2PaperRecord, UnifiedTextChunk
from optomind_research.s2_text_chunk_retriever import TextChunkRetrievalResult


def _response() -> S2GatewayResponse:
    return S2GatewayResponse(ok=True, status_code=200, status_category="ok")


def test_scope_map_reconciles_existing_upstream_artifacts() -> None:
    scope = build_review_scope_map(
        user_question="How can optical multilayers be inverse designed?",
        query_plan={
            "output": {
                "problem_understanding": "The design space is large and constrained.",
                "scope_definition": {"inclusions": ["thin films"], "exclusions": ["biology"]},
                "keyword_decomposition": {"keywords": ["inverse design", "multilayer"]},
            }
        },
        review_charter={
            "target_article_type": "critical narrative review",
            "scope_statement": "Focus on optical multilayer design and validation.",
        },
        mentor_advice={"usable_intellectual_moves": [{"move": "Separate mechanism from deployment."}]},
        blueprint={
            "review_thesis": "The useful comparison is mechanism-to-constraint.",
            "sections": [
                {
                    "section_id": "S01",
                    "title": "Mechanisms",
                    "chapter_argument": "Explain the governing mechanism.",
                    "required_roles": ["mechanism"],
                    "relationship_tasks": ["progression"],
                }
            ],
        },
    )
    assert scope.core_question.startswith("How can optical")
    assert scope.research_dimensions[0]["dimension_id"] == "S01"
    assert "mechanism" in scope.literature_roles
    assert "progression" in scope.relation_tasks
    assert scope.m1_architecture_guidance == ["Separate mechanism from deployment."]
    assert scope.provenance["m1_is_architecture_only"] is True


def test_scope_map_records_missing_query_planner_contract_fields() -> None:
    scope = build_review_scope_map(
        user_question="How does an optical multilayer work?",
        query_plan={"output": {"problem_understanding": "A partial problem."}},
        blueprint={"sections": [{"section_id": "S01", "title": "Mechanism"}]},
    )
    assert "query_planner_scope_definition_missing" in scope.unresolved_items
    assert "query_planner_search_anchors_missing" in scope.unresolved_items
    assert "scope_exclusion_boundary_not_declared" in scope.unresolved_items


def test_stop_condition_uses_current_wave_and_all_marginal_deltas() -> None:
    early = evaluate_discovery_stop(
        current_wave_index=1,
        max_wave_index=7,
        unique_papers=0,
        minimum_papers=10,
        covered_roles=[],
        required_roles=[],
        covered_dimensions=[],
        required_dimensions=[],
        observed_relation_count=0,
        new_papers=0,
        new_roles=0,
        new_dimensions=0,
        new_relations=0,
        new_information_gain=0,
        no_gain_rounds=2,
        max_rounds=7,
    )
    assert early["stop"] is False
    late = evaluate_discovery_stop(
        current_wave_index=2,
        max_wave_index=7,
        unique_papers=0,
        minimum_papers=10,
        covered_roles=[],
        required_roles=[],
        covered_dimensions=[],
        required_dimensions=[],
        observed_relation_count=3,
        new_papers=0,
        new_roles=0,
        new_dimensions=0,
        new_relations=0,
        new_information_gain=0,
        no_gain_rounds=2,
        max_rounds=7,
        required_relation_tasks=["progression"],
    )
    assert late["stop"] is True
    assert late["reason"] == "marginal_gain_exhausted"
    assert late["missing_relation_tasks"] == ["progression"]


class _WaveRetriever:
    def __init__(self) -> None:
        self.calls = 0

    def retrieve(self, queries: list[str], *, paper_ids=None, **_: Any):
        self.calls += 1
        paper_id = (paper_ids or [f"snippet-paper-{self.calls}"])[0]
        chunk = UnifiedTextChunk(
            chunk_id=f"snippet-chunk-{self.calls}",
            paper_id=paper_id,
            title=f"Snippet source {self.calls}",
            text=(
                "The measured mechanism and its limitation are described in detail. "
                * 20
            ),
            scope_fit="direct",
            content_depth="structured_snippet",
            context_complete=True,
            route_provenance={
                "discovery_route": "semantic_scholar_snippet_search",
                "materialization_route": "s2_structured_body_snippet",
            },
        )
        return TextChunkRetrievalResult(
            accepted_chunks=[chunk],
            rejected_items=[],
            query_runs=[{"channel": "s2_snippet_search", "status_code": 200}],
            paper_ids=[paper_id],
        )


class _WaveGateway:
    def __init__(self) -> None:
        self.search_count = 0

    def _paper(self, paper_id: str, title: str) -> S2PaperRecord:
        return S2PaperRecord(
            paper_id=paper_id,
            title=title,
            abstract=(
                "A mechanism method and frontier validation study for optical multilayers."
            ),
            year=2024,
            publication_types=["JournalArticle"],
        )

    @staticmethod
    def _payload(paper: S2PaperRecord) -> dict[str, Any]:
        return {
            "paperId": paper.paper_id,
            "corpusId": paper.corpus_id,
            "title": paper.title,
            "abstract": paper.abstract,
            "year": paper.year,
            "publicationTypes": paper.publication_types,
            "authors": [],
            "externalIds": {},
        }

    def search_papers(self, query: str, *, limit: int = 20):
        self.search_count += 1
        return [
            self._paper(
                f"search-{self.search_count}",
                f"Mechanism method frontier study {self.search_count}",
            )
        ], _response()

    def search_snippets(self, *args: Any, **kwargs: Any):
        return [], _response()

    def references(self, paper_id: str, *, limit: int = 20):
        paper = self._paper(
            f"reference-{paper_id}", "Foundation principle of multilayers"
        )
        return [
            {
                "citedPaper": self._payload(paper),
                "contexts": ["The earlier principle motivates this method."],
            }
        ], _response()

    def citations(self, paper_id: str, *, limit: int = 20):
        paper = self._paper(
            f"citation-{paper_id}", "Recent frontier validation of multilayers"
        )
        return [
            {
                "citingPaper": self._payload(paper),
                "contexts": ["The later study extends the method."],
            }
        ], _response()

    def recommendations_from_seeds(self, paper_ids: list[str], *, limit: int = 20):
        return [
            self._paper("recommended-1", "Adjacent application comparison"),
        ], _response()


def test_multi_wave_controller_executes_real_wave_branches_offline() -> None:
    retriever = _WaveRetriever()
    controller = MultiWaveDiscoveryController(
        gateway=_WaveGateway(),  # type: ignore[arg-type]
        retriever=retriever,  # type: ignore[arg-type]
    )
    result = controller.run(
        facets=[],
        scope_map={
            "core_question": "optical multilayer inverse design",
            "search_anchors": ["optical multilayer inverse design"],
            "research_dimensions": [{"dimension_id": "D1", "title": "mechanism"}],
            "relation_tasks": [],
        },
        minimum_papers=999,
        max_waves=7,
        max_results_per_query=1,
        max_snippets_per_query=1,
    )
    wave_ids = [record.wave_id for record in result.wave_records]
    assert wave_ids == [
        "W0_direct",
        "W1_facets",
        "W2_backward",
        "W3_forward",
        "W4_recommendations",
        "W5_boundary",
        "W6_review_frontier",
    ]
    channels = {
        call["channel"]
        for record in result.wave_records
        for call in record.api_calls
    }
    assert "s2_snippet_search" in channels
    assert "s2_references" in channels
    assert "s2_citations" in channels
    assert "s2_recommendations" in channels
    assert result.wave_records[5].new_chunk_ids
    assert result.wave_records[-1].stop_decision["current_wave_index"] == 7


def test_legacy_migration_recovers_routes_and_marks_unresolved(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {"paper_id": "p-s2", "title": "S2", "source_kind": "semantic_scholar"},
                    {
                        "paper_id": "p-full",
                        "title": "Full",
                        "acquisition_status": "fulltext",
                        "source_kind": "publisher_html",
                    },
                    {"paper_id": "p-unknown", "title": "Unknown"},
                ]
            }
        ),
        encoding="utf-8",
    )
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, source_kind TEXT, content_depth TEXT, provenance_json TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c-s2','p-s2','s2_body_snippet','structured_snippet','{}')"
        )
        conn.commit()
    output, report = migrate_source_ledger(ledger, kb_paths=[kb], output_dir=tmp_path / "out")
    payload = json.loads(output.read_text(encoding="utf-8"))
    by_id = {item["paper_id"]: item for item in payload["sources"]}
    assert by_id["p-s2"]["discovery_route"] == "s2_legacy_recovered"
    assert by_id["p-full"]["content_depth"] == "fulltext"
    assert by_id["p-unknown"]["discovery_route"] == "legacy_unresolved"
    assert by_id["p-unknown"]["use_permission"] == "discovery_only"
    assert report.coverage["route_unresolved"] == 1
    assert report.chunks_updated == 1
    assert report.migrated_kb_paths
    migrated_kb = Path(report.migrated_kb_paths[0])
    with sqlite3.connect(migrated_kb) as conn:
        row = conn.execute(
            "SELECT discovery_route, materialization_route, content_depth, use_permission "
            "FROM text_chunks WHERE chunk_id='c-s2'"
        ).fetchone()
    assert row[0] == "s2_legacy_recovered"
    assert row[1] == "s2_structured_snippet"
    assert row[2] == "structured_snippet"


def test_migration_keeps_s2_discovery_separate_from_local_fulltext(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.json"
    ledger.write_text(
        json.dumps({"sources": [{
            "paper_id": "p1", "source_kind": "semantic_scholar",
            "scope_fit": "direct", "canonical_chunk_ids": ["c1"],
        }]}),
        encoding="utf-8",
    )
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "source_kind TEXT, content_depth TEXT, provenance_json TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?)",
            ("c1", "p1", "complete local PDF passage", "fulltext", "fulltext", "{}"),
        )
        conn.commit()
    output, report = migrate_source_ledger(ledger, kb_paths=[kb], output_dir=tmp_path / "out")
    payload = json.loads(output.read_text(encoding="utf-8"))
    source = payload["sources"][0]
    assert source["discovery_route"] == "s2_legacy_recovered"
    assert source["materialization_route"] == "local_cached_fulltext"
    assert source["content_depth"] == "fulltext"
    with sqlite3.connect(report.migrated_kb_paths[0]) as conn:
        assert conn.execute(
            "SELECT materialization_route FROM text_chunks WHERE chunk_id='c1'"
        ).fetchone()[0] == "local_cached_fulltext"


def test_generic_chunk_does_not_promote_abstract_source(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.json"
    ledger.write_text(
        json.dumps({"sources": [{
            "paper_id": "p-abstract", "abstract": "A short abstract.",
            "content_depth": "abstract", "canonical_chunk_ids": ["c1"],
        }]}),
        encoding="utf-8",
    )
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "source_kind TEXT, content_depth TEXT, provenance_json TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?)",
            ("c1", "p-abstract", "generic indexed text", "text_chunk", "", "{}"),
        )
        conn.commit()
    output, _ = migrate_source_ledger(ledger, kb_paths=[kb], output_dir=tmp_path / "out")
    source = json.loads(output.read_text(encoding="utf-8"))["sources"][0]
    assert source["content_depth"] == "abstract"
    assert source["use_permission"] == "background_and_candidate_only"
    with sqlite3.connect(tmp_path / "out" / "MIGRATED_00_kb.sqlite") as conn:
        row = conn.execute(
            "SELECT content_depth, use_permission FROM text_chunks WHERE chunk_id='c1'"
        ).fetchone()
    assert row == ("abstract", "background_and_candidate_only")


def test_migration_conflict_downgrades_method_transfer_scope(tmp_path: Path) -> None:
    ledger = tmp_path / "legacy.json"
    ledger.write_text(
        json.dumps({"sources": [{
            "paper_id": "p1", "scope_fit": "direct",
            "canonical_chunk_ids": ["c1"],
        }]}),
        encoding="utf-8",
    )
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, "
            "source_kind TEXT, content_depth TEXT, provenance_json TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES (?,?,?,?,?,?)",
            ("c1", "p1", "method transfer passage", "method_transfer", "fulltext",
             json.dumps({"scope_fit": "cross_domain_analogy"})),
        )
        conn.commit()
    output, report = migrate_source_ledger(ledger, kb_paths=[kb], output_dir=tmp_path / "out")
    source = json.loads(output.read_text(encoding="utf-8"))["sources"][0]
    assert source["scope_fit"] == "adjacent"
    assert source["use_permission"] != "factual_support"
    assert source["metadata_conflicts"]
    with sqlite3.connect(report.migrated_kb_paths[0]) as conn:
        assert conn.execute(
            "SELECT scope_fit, use_permission FROM text_chunks WHERE chunk_id='c1'"
        ).fetchone() == ("adjacent", "contextual_or_qualified_support")


def test_snippet_permission_is_downgraded_for_truncation_or_missing_context() -> None:
    text = (
        "The multilayer inverse design mechanism controls resonance and thermal emission "
        "under constrained fabrication conditions. "
    ) * 12
    good = assess_structured_snippet(
        text,
        query="multilayer inverse design resonance thermal emission",
    )
    bad = assess_structured_snippet(
        text + "...",
        query="multilayer inverse design resonance thermal emission",
    )
    assert good["scope_fit"] == "direct"
    assert good["context_complete"] is True
    assert bad["scope_fit"] == "direct"
    assert bad["context_complete"] is False
    assert "possible_truncation" in bad["context_limitations"]


def test_semantic_relation_requires_basis_and_inference_status() -> None:
    invalid = LiteratureGraphEdge(
        edge_id="e-invalid",
        source_paper_id="p1",
        target_paper_id="p2",
        edge_type="cites",
        observed_relation="cites",
        semantic_relation="extends",
        status="observed",
    )
    valid = LiteratureGraphEdge(
        edge_id="e-valid",
        source_paper_id="p1",
        target_paper_id="p2",
        edge_type="cites",
        observed_relation="cites",
        semantic_relation="extends",
        relation_basis_chunk_ids=["chunk-1"],
        status="inferred",
        confidence=0.8,
    )
    assert invalid.semantic_relation == ""
    assert invalid.validation_errors
    assert valid.semantic_relation == "extends"
    assert valid.relation_basis_chunk_ids == ["chunk-1"]


def test_synthesis_bundle_filters_section_ids_and_relations() -> None:
    edge = {
        "edge_id": "e1",
        "source_paper_id": "p1",
        "target_paper_id": "p2",
        "edge_type": "cites",
        "observed_relation": "cites",
        "status": "observed",
        "relation_basis_chunk_ids": ["c1"],
    }
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "chapter_argument": "Explain mechanism."},
        claims=[
            {
                "statement": "A supported point.",
                "citation_paper_ids": ["p1", "p2"],
                "supporting_text_chunk_ids": ["c1", "c2"],
                "saturation_score": 2.0,
            }
        ],
        relation_edges=[edge],
        allowed_paper_ids=["p1"],
        allowed_chunk_ids=["c1"],
        chunk_to_paper={"c1": "p1", "c2": "p2"},
        source_permissions={"p1": "factual_support"},
        chunk_permissions={"c1": "factual_support"},
    )
    assert bundle.paper_ids == ["p1"]
    assert bundle.chunk_ids == ["c1"]
    assert "p2" in bundle.invalid_paper_ids
    assert "c2" in bundle.invalid_chunk_ids
    assert bundle.relation_evidence == []
    assert bundle.source_permission_summary["factual_support"] == 2


def test_synthesis_bundle_has_compact_core_and_candidate_pool() -> None:
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "chapter_argument": "Explain mechanism."},
        claims=[],
        allowed_paper_ids=[f"p{i}" for i in range(20)],
        allowed_chunk_ids=[f"c{i}" for i in range(40)],
        chunk_to_paper={f"c{i}": f"p{i % 20}" for i in range(40)},
        source_permissions={f"p{i}": "factual_support" for i in range(20)},
        chunk_permissions={f"c{i}": "factual_support" for i in range(40)},
        max_core_chunks=8,
    )
    payload = bundle.to_dict()
    assert len(bundle.chunk_ids) <= 8
    assert bundle.candidate_pool_count >= 32
    assert len(payload["chunk_ids"]) <= 8
    assert "candidate_chunk_ids" not in payload
    # An ID inventory without a real claim or semantic relation is not writing
    # material.  R3.3 must report this honestly instead of inserting a generic
    # established-point placeholder.
    assert payload["established_points"] == []
    assert payload["status"] == "needs_more_literature"
    assert payload["material_status"] == "inventory_only"
    assert payload["conditional_points"] == []
    assert payload["conflicts_or_boundaries"]
    assert payload["author_synthesis_space"]


def test_canonical_graph_conservatively_downgrades_unresolved_chunk_route(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "SECTION_SOURCE_LEDGER.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p-legacy",
                        "title": "Legacy full text",
                        "acquisition_status": "fulltext",
                        "canonical_chunk_ids": ["c-legacy"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    kb = tmp_path / "legacy.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c-legacy','p-legacy','A full text passage','fulltext','fulltext')"
        )
        conn.commit()
    graph = build_canonical_asset_graph(
        material_package_path=None,
        source_ledger_path=ledger,
        work_dir=tmp_path,
        kb_paths=[kb],
    )
    assert graph.papers["p-legacy"].use_permission == "discovery_only"
    assert graph.chunks["c-legacy"].use_permission == "discovery_only"
    assert any(
        item.get("asset_type") == "text_chunk"
        for item in graph.unresolved_asset_audit
    )


def test_coverage_atlas_counts_actual_edges_not_declared_tasks(tmp_path: Path) -> None:
    section_dir = tmp_path / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(
            {
                "sources": [
                    {"paper_id": "p1", "literature_role": "mechanism", "scope_fit": "direct"},
                    {"paper_id": "p2", "literature_role": "frontier", "scope_fit": "direct"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "RELATION_GRAPH.json").write_text(
        json.dumps(
            {
                "edges": [
                    {
                        "edge_id": "observed",
                        "source_paper_id": "p1",
                        "target_paper_id": "p2",
                        "edge_type": "cites",
                        "observed_relation": "cites",
                        "status": "observed",
                    },
                    {
                        "edge_id": "semantic",
                        "source_paper_id": "p1",
                        "target_paper_id": "p2",
                        "edge_type": "cites",
                        "observed_relation": "cites",
                        "semantic_relation": "extends",
                        "relation_basis_chunk_ids": ["c1"],
                        "status": "inferred",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    atlas = build_coverage_atlas(
        blueprint={
            "review_mode": "critical_narrative_review",
            "sections": [
                {
                    "section_id": "S01",
                    "relationship_tasks": ["progression", "controversy"],
                    "required_roles": [],
                    "optional_roles": [],
                }
            ],
        },
        coverage_root=tmp_path,
    )
    relationship = atlas["sections"][0]["relationship_coverage"]
    assert relationship["observed_edge_count"] == 2
    assert relationship["actual_semantic_relation_tasks"] == ["progression"]
    assert relationship["missing_semantic_relation_tasks"] == ["controversy"]


def test_coverage_atlas_uses_scope_map_without_global_relation_obligations(tmp_path: Path) -> None:
    section_dir = tmp_path / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps({"sources": [{"paper_id": "p1", "literature_role": "mechanism", "scope_fit": "direct"}]}),
        encoding="utf-8",
    )
    atlas = build_coverage_atlas(
        blueprint={"review_mode": "critical_narrative_review", "sections": [{"section_id": "S01"}]},
        coverage_root=tmp_path,
        scope_map={"literature_roles": ["mechanism"], "relation_tasks": []},
    )
    row = atlas["sections"][0]
    assert row["relationship_tasks"] == []
    assert row["relation_completion_status"]["complete"] is True


def test_stop_condition_can_satisfy_semantic_relation_task() -> None:
    result = evaluate_discovery_stop(
        current_wave_index=1,
        max_wave_index=7,
        unique_papers=10,
        minimum_papers=10,
        covered_roles=[],
        required_roles=[],
        new_information_gain=1,
        no_gain_rounds=0,
        max_rounds=7,
        required_relation_tasks=["progression"],
        satisfied_relation_tasks=["progression"],
    )
    assert result["stop"] is True
    assert result["reason"] == "targets_met"
    assert result["relation_tasks_satisfied"] == 1


def test_semantic_relation_classifier_is_candidate_restricted_and_traceable() -> None:
    classifier = SemanticRelationClassifier()
    decisions = classifier.classify_batch([
        {
            "edge_id": "e1", "source_paper_id": "p1", "target_paper_id": "p2",
            "observed_relation": "cited_by", "semantic_relation": "extends",
            "relation_basis_chunk_ids": ["c1"], "shared_argument_task": "progression",
            "relation_context": "The later method extends the earlier mechanism.",
        },
        {
            "edge_id": "e2", "source_paper_id": "p3", "target_paper_id": "p4",
            "observed_relation": "semantic_recommendation", "semantic_relation": "complements",
            "relation_basis_chunk_ids": ["c2"],
        },
    ])
    assert decisions[0].semantic_relation == "extends"
    assert decisions[0].status == "inferred"
    assert decisions[0].relation_basis_chunk_ids == ["c1"]
    assert decisions[1].semantic_relation == ""
    assert "recommendation" in decisions[1].reason


def test_authoring_permission_guard_blocks_only_overclaiming() -> None:
    from optomind_research.runtime.section_authoring_tool_registry import (
        _permission_guard_error,
    )

    assert _permission_guard_error(
        use_permission="discovery_only", writing_permission="factual_assertion",
        claim_text="The system reaches 99% efficiency.", asset_id="p1",
    )
    assert _permission_guard_error(
        use_permission="background_and_candidate_only", writing_permission="factual_assertion",
        claim_text="The paper reports 99% efficiency.", asset_id="p2",
    )
    assert _permission_guard_error(
        use_permission="contextual_or_qualified_support", writing_permission="factual_assertion",
        claim_text="The method always causes improvement.", asset_id="p3",
    )
    assert _permission_guard_error(
        use_permission="contextual_or_qualified_support", writing_permission="author_synthesis",
        claim_text="Across studies, this suggests a possible trend.", asset_id="p4",
    ) is None


def test_missing_chunk_recovery_fails_closed_without_fabrication(tmp_path: Path) -> None:
    result = recover_missing_chunks(
        ["s2chunk:123:1:2:missing"],
        cache=None,
        gateway=None,
    )
    assert result.recovered_ids == []
    assert result.unavailable_ids == ["s2chunk:123:1:2:missing"]
    assert result.recovered_chunks == []


def test_deterministic_120_paper_merge_preserves_role_breadth() -> None:
    ranker = S2CandidateRanker(current_year=2026)
    candidates = []
    for index in range(120):
        role = ["foundation", "mechanism", "method", "frontier", "application"][index % 5]
        paper = S2PaperRecord(
            paper_id=f"p-{index:03d}",
            title=f"{role} optical multilayer study {index}",
            abstract=f"A {role} study with mechanism and optical multilayer evidence.",
            year=2000 + index % 26,
        )
        candidates.append(
            ranker.build_candidate(
                paper,
                facet_id=role,
                queries=[f"optical multilayer {role}"],
                requested_roles=[role],
                discovery_channel="fixture",
            )
        )
    merged = S2CandidateRanker.merge_candidates(candidates + candidates[:20])
    assert len(merged) == 120
    roles = {candidate.facet_id for candidate in merged}
    assert roles == {"foundation", "mechanism", "method", "frontier", "application"}
