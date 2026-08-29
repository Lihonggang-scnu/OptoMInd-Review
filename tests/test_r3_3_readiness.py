"""Adversarial, offline tests for R3.3 quality readiness."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.evidence_portfolio_selector import select_evidence_portfolio
from optomind_research.runtime.legacy_asset_migration import migrate_shared_legacy_assets
from optomind_research.runtime.semantic_relation_classifier import SemanticRelationClassifier
from optomind_research.runtime.section_asset_overlay import build_section_asset_overlay
from optomind_research.runtime.synthesis_bundle import build_synthesis_bundle


def _records() -> list[dict]:
    rows = []
    for paper_id in ("p1", "p2", "p3"):
        for index in range(4):
            rows.append(
                {
                    "chunk_id": f"{paper_id}-chunk-{index}",
                    "paper_id": paper_id,
                    "normalized_text": f"Direct mechanism evidence for the optical method from {paper_id}.",
                    "scope_fit": "direct",
                    "use_permission": "factual_support",
                    "content_depth": "fulltext",
                    "literature_role": "mechanism" if index < 2 else "method",
                }
            )
    return rows


def test_shared_selector_is_multi_paper_and_not_chunk_id_prefix_ordered() -> None:
    result = select_evidence_portfolio(
        section={"section_id": "S01", "title": "Optical mechanism", "required_roles": ["mechanism", "method"]},
        candidates=_records(),
        claims=[{"statement": "The mechanism is compared across multiple papers."}],
        max_core_chunks=6,
        max_core_chunks_per_paper=2,
    )
    assert result.status == "material_ready"
    assert len(result.core_paper_ids) == 3
    assert max(result.paper_core_counts.values()) <= 3
    assert result.diagnostics["core_selection_is_not_chunk_id_sorted"] is True


def test_discovery_only_stays_out_of_core_material() -> None:
    rows = _records()
    rows[0]["use_permission"] = "discovery_only"
    result = select_evidence_portfolio(
        section={"section_id": "S01", "title": "Optical mechanism"},
        candidates=rows,
        claims=[{"statement": "A real mechanism claim that needs evidence."}],
        max_core_chunks=3,
    )
    assert rows[0]["chunk_id"] not in result.core_chunk_ids
    assert rows[0]["chunk_id"] in result.candidate_chunk_ids


def test_no_claim_or_relation_is_inventory_only_and_never_material_ready() -> None:
    rows = _records()
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "title": "Optical mechanism"},
        claims=[],
        relation_edges=[],
        allowed_paper_ids=["p1", "p2", "p3"],
        allowed_chunk_ids=[item["chunk_id"] for item in rows],
        chunk_to_paper={item["chunk_id"]: item["paper_id"] for item in rows},
        chunk_permissions={item["chunk_id"]: "factual_support" for item in rows},
        chunk_records=rows,
        max_core_chunks=6,
    )
    assert bundle.material_status == "inventory_only"
    assert bundle.status == "needs_more_literature"
    assert bundle.established_points == []
    assert not any("formulate the supported points" in item for item in bundle.established_points)


def test_synthesis_bundle_uses_shared_selector_and_tracks_core_diversity() -> None:
    rows = _records()
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "title": "Optical mechanism", "required_roles": ["mechanism"]},
        claims=[{"statement": "The mechanism is a real cross-paper claim."}],
        allowed_paper_ids=["p1", "p2", "p3"],
        allowed_chunk_ids=[item["chunk_id"] for item in rows],
        chunk_to_paper={item["chunk_id"]: item["paper_id"] for item in rows},
        chunk_permissions={item["chunk_id"]: "factual_support" for item in rows},
        source_permissions={f"p{i}": "factual_support" for i in (1, 2, 3)},
        chunk_records=rows,
        max_core_chunks=6,
    )
    assert bundle.status == "material_ready"
    assert len(bundle.paper_ids) == 3
    assert max(bundle.selection_diagnostics.get("max_core_chunks_per_paper", 0), 0) >= 2
    assert bundle.selection_diagnostics["core_selection_is_not_chunk_id_sorted"] is True


def test_semantic_classifier_rejects_inactive_target_and_shared_task_only() -> None:
    classifier = SemanticRelationClassifier()
    decisions = classifier.classify_batch(
        [
            {
                "edge_id": "lead",
                "source_paper_id": "p1",
                "target_paper_id": "p2",
                "active_paper_ids": ["p1"],
                "observed_relation": "cited_by",
                "semantic_relation": "extends",
                "relation_basis_chunk_ids": ["c1"],
                "relation_context": "The later method extends the earlier mechanism.",
            },
            {
                "edge_id": "task",
                "source_paper_id": "p1",
                "target_paper_id": "p2",
                "active_paper_ids": ["p1", "p2"],
                "observed_relation": "cites",
                "semantic_relation": "foundation_of",
                "historical_role": "foundation",
                "shared_argument_task": "progression",
                "relation_basis_chunk_ids": ["c2"],
                "relation_context": "Both papers address the same review task.",
            },
        ],
        max_items=2,
    )
    assert decisions[0].status == "discovery_lead"
    assert decisions[0].semantic_relation == ""
    assert decisions[1].semantic_relation == ""


def test_relation_target_outside_bundle_allowlist_is_not_chapter_evidence() -> None:
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "title": "Mechanism"},
        claims=[{"statement": "A real mechanism claim."}],
        relation_edges=[
            {
                "edge_id": "e1",
                "source_paper_id": "p1",
                "target_paper_id": "not-active",
                "observed_relation": "cited_by",
                "semantic_relation": "extends",
                "status": "inferred",
                "relation_basis_chunk_ids": ["c1"],
            }
        ],
        allowed_paper_ids=["p1"],
        allowed_chunk_ids=["c1"],
        chunk_to_paper={"c1": "p1"},
        chunk_permissions={"c1": "factual_support"},
        source_permissions={"p1": "factual_support"},
        chunk_records=[
            {
                "chunk_id": "c1", "paper_id": "p1", "normalized_text": "A mechanism passage.",
                "scope_fit": "direct", "use_permission": "factual_support",
            }
        ],
    )
    assert bundle.relation_evidence == []


def test_shared_migration_creates_one_database_set_and_lightweight_overlays(tmp_path: Path) -> None:
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute("INSERT INTO text_chunks VALUES ('c1','p1','A passage','fulltext','fulltext')")
        conn.commit()
    ledgers = []
    for section_id in ("S01", "S02"):
        path = tmp_path / section_id / "SECTION_SOURCE_LEDGER.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps({"section_id": section_id, "sources": [{"paper_id": "p1", "canonical_chunk_ids": ["c1"], "scope_fit": "direct"}]}),
            encoding="utf-8",
        )
        ledgers.append(path)
    _, report, overlays, stats = migrate_shared_legacy_assets(
        ledgers, kb_paths=[kb], output_dir=tmp_path / "shared", overlay_dir=tmp_path / "overlays"
    )
    assert stats["shared_database_copy_count"] == 1
    assert stats["section_database_copy_count"] == 0
    assert len(overlays) == 2
    assert not list((tmp_path / "overlays").rglob("*.sqlite"))
    assert len(report.migrated_kb_paths) == 1


def test_overlay_contains_policy_not_text_copy(tmp_path: Path) -> None:
    path = tmp_path / "S01.json"
    payload = build_section_asset_overlay(
        section_id="S01",
        sources=[{"paper_id": "p1", "scope_fit": "direct", "use_permission": "factual_support", "canonical_chunk_ids": ["c1"]}],
        shared_kb_paths=[tmp_path / "shared.sqlite"],
        output_path=path,
    )
    assert payload["chunk_ids"] == ["c1"]
    assert "text" not in json.dumps(payload).lower()
    assert payload["database_copy_count"] == 0
