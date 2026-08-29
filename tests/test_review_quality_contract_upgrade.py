from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from optomind_research.runtime.literature_discovery_plan import (
    build_discovery_wave_plan,
    evaluate_wave_stop,
    relation_coverage_ledger,
)
from optomind_research.runtime.coverage_atlas import build_coverage_atlas
from optomind_research.runtime.literature_portfolio import (
    build_literature_portfolio_report,
)
from optomind_research.runtime.review_quality_contract import (
    permission_for_content,
    resolve_review_contract,
)
from optomind_research.runtime.synthesis_bundle import build_synthesis_bundle


@pytest.fixture
def tmp_path(request):
    """Workspace-local temporary directory (system temp is sandbox-blocked)."""
    base = Path(__file__).resolve().parents[1] / ".pytest-basetemp-quality-contract"
    base.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)[:40]
    path = base / f"{safe_name}-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    request.addfinalizer(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
from optomind_research.s2_schemas import (
    LiteratureGraphEdge,
    S2PaperRecord,
    UnifiedTextChunk,
)


def test_comprehensive_contract_is_shared_by_article_and_section_targets():
    contract = resolve_review_contract(
        {
            "review_quality_contract": {
                "mode": "comprehensive_review",
                "reference_target_range": [100, 180],
                "word_target_range": [16000, 28000],
                "section_target_range": [7, 12],
                "section_unique_range": [8, 24],
                "section_direct_range": [5, 16],
            },
            "sections": [{"section_id": "S01"}] * 8,
        },
        section={"section_id": "S01"},
    )
    targets = contract.section_targets(section={}, section_count=8)
    assert contract.minimum_references == 100
    assert targets["minimum_unique_sources"] >= 8
    assert targets["minimum_direct_sources"] >= 5


def test_content_permissions_do_not_promote_abstract_or_metadata():
    assert permission_for_content("metadata")["use_permission"] == "discovery_only"
    assert permission_for_content("abstract")["factual_support_allowed"] is False
    assert permission_for_content(
        "structured_snippet", scope_fit="direct", context_complete=True
    )["factual_support_allowed"] is True
    assert permission_for_content(
        "fulltext", scope_fit="adjacent", context_complete=True
    )["factual_support_allowed"] is False


def test_wave_plan_and_stop_rule_are_bounded_and_explicit():
    plan = build_discovery_wave_plan(
        user_question="radiative cooling coating",
        requested_roles=["foundation", "frontier"],
        seed_paper_ids=["CorpusId:1"],
        enable_expensive_waves=False,
    )
    assert plan["order"] == ["W0_direct", "W1_facets", "W2_backward", "W3_forward", "W4_recommendations", "W5_boundary", "W6_review_frontier"]
    disabled = {row["wave_id"] for row in plan["waves"] if not row["enabled"]}
    assert disabled == {"W2_backward", "W3_forward", "W5_boundary"}
    assert evaluate_wave_stop(
        unique_papers=12,
        minimum_papers=10,
        covered_roles=["foundation"],
        required_roles=["foundation"],
        new_information_gain=0,
        no_gain_rounds=0,
        max_rounds=3,
    )["reason"] == "targets_met"


def test_relationship_ledger_does_not_infer_semantic_edges():
    edge = LiteratureGraphEdge(
        edge_id="e1",
        source_paper_id="p1",
        target_paper_id="p2",
        edge_type="cites",
    )
    ledger = relation_coverage_ledger(
        {"nodes": {"p1": {}, "p2": {}}, "edges": [edge.to_dict()]},
        required_relation_roles=["cites", "extends"],
    )
    assert ledger["edge_type_counts"] == {"cites": 1}
    assert ledger["missing_relation_roles"] == ["extends"]
    assert ledger["relationship_evidence_is_observed_not_inferred"] is True


def test_s2_bridge_persists_route_and_permission_columns(tmp_path: Path):
    db = tmp_path / "kb.sqlite"
    paper = S2PaperRecord(
        paper_id="S2:1",
        title="A paper",
        abstract="A short abstract",
        content_depth="abstract",
        use_permission="background_and_candidate_only",
    )
    chunk = UnifiedTextChunk(
        chunk_id="s2chunk:1:0:20:abc",
        paper_id="S2:1",
        title="A paper",
        text="A structured body passage with enough content for a deterministic test.",
        scope_fit="direct",
        content_depth="structured_snippet",
        route_provenance={"discovery_route": "semantic_scholar_snippet_search"},
    )
    result = S2KnowledgeBaseBridge(db).ingest(papers=[paper], chunks=[chunk])
    assert result["chunks_inserted"] == 1
    with sqlite3.connect(db) as conn:
        paper_row = conn.execute(
            "SELECT discovery_route,content_depth,use_permission FROM papers WHERE paper_id='S2:1'"
        ).fetchone()
        chunk_row = conn.execute(
            "SELECT content_depth,use_permission,scope_fit,route_provenance_json FROM text_chunks WHERE chunk_id=?",
            (chunk.chunk_id,),
        ).fetchone()
    assert paper_row[0] == "semantic_scholar_graph"
    assert paper_row[1] == "abstract"
    assert paper_row[2] == "background_and_candidate_only"
    assert chunk_row[0] == "structured_snippet"
    assert chunk_row[2] == "direct"
    assert json.loads(chunk_row[3])["discovery_route"] == "semantic_scholar_snippet_search"


def test_synthesis_bundle_is_compact_and_traceable():
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "chapter_argument": "Explain the mechanism."},
        claims=[
            {
                "statement": "A mechanism is supported by several sources.",
                "saturation_score": 2.0,
                "citation_paper_ids": ["p1"],
                "supporting_text_chunk_ids": ["c1"],
            },
            {
                "statement": "A boundary remains uncertain.",
                "saturation_score": 0.8,
                "status": "open_question",
                "citation_paper_ids": ["p2"],
                "context_text_chunk_ids": ["c2"],
            },
        ],
    )
    assert set(bundle.paper_ids) == {"p1", "p2"}
    assert set(bundle.chunk_ids) == {"c1", "c2"}
    assert len(bundle.established_points) == 1
    assert len(bundle.conditional_points) == 1
    assert bundle.forbidden_overclaims


def test_coverage_atlas_reports_role_gaps_without_calling_an_llm(tmp_path: Path):
    section_dir = tmp_path / "sections" / "S01"
    section_dir.mkdir(parents=True)
    (section_dir / "SECTION_SOURCE_LEDGER.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p1",
                        "literature_role": "mechanism",
                        "scope_fit": "direct",
                        "content_depth": "structured_snippet",
                        "use_permission": "factual_support",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    blueprint = {
        "review_quality_contract": {
            "mode": "comprehensive_review",
            "reference_target_range": [100, 180],
            "section_unique_range": [8, 24],
            "section_direct_range": [5, 16],
        },
        "sections": [
            {
                "section_id": "S01",
                "required_roles": ["foundation", "mechanism"],
                "role_source_targets": {"foundation": 2, "mechanism": 1},
                "relationship_tasks": ["progression"],
            }
        ],
    }
    atlas = build_coverage_atlas(blueprint=blueprint, coverage_root=tmp_path)
    assert atlas["sections_needing_expansion"] == ["S01"]
    row = atlas["sections"][0]
    assert row["role_coverage"]["mechanism"]["covered"] is True
    assert "foundation" in row["missing_literature_roles"]
    report = build_literature_portfolio_report(
        blueprint=blueprint,
        coverage_root=tmp_path,
    )
    assert report["coverage_atlas"]["article_unique_papers"] == 1
