from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.legacy_asset_migration import migrate_shared_legacy_assets
from optomind_research.runtime.phase3_argument_orchestrator import (
    Phase3ArgumentOrchestrator,
)
from optomind_research.runtime.semantic_relation_classifier import (
    revalidate_legacy_relation_edges,
)
from optomind_research.runtime.section_authoring_assets import build_canonical_asset_graph


def _coverage_snapshot(path: Path) -> dict:
    """Return the small coverage summary used by the regression assertion."""

    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("bundles") if isinstance(data, dict) else None
    row = next(
        (
            item
            for item in (rows or [])
            if isinstance(item, dict) and item.get("section_id") == "S01"
        ),
        {},
    )
    return {
        "status": row.get("status", ""),
        "readiness_status": row.get("readiness_status", ""),
        "material_status": row.get("material_status", ""),
        "task_statuses": {
            str(item.get("task_id")): item.get("status")
            for item in row.get("argument_task_coverage") or []
            if isinstance(item, dict)
        },
        "missing_components": [
            component
            for item in row.get("argument_task_coverage") or []
            if isinstance(item, dict)
            for component in item.get("missing_components") or []
        ],
        "claim_categories": {
            str(item.get("claim_id")): item.get("category")
            for item in row.get("claim_category_assignments") or []
            if isinstance(item, dict)
        },
        "unique_papers": len(row.get("paper_ids") or []),
        "chunk_count": len(row.get("chunk_ids") or []),
        "permission_summary": row.get("source_permission_summary") or {},
    }


def _make_kb_and_ledgers(tmp_path: Path):
    kb = tmp_path / "source.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c1','p1','Paper one','A full mechanism passage for the first section.','fulltext','fulltext')"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c2','p1','Paper one','A related passage for the second section.','fulltext','fulltext')"
        )
        conn.commit()
    rows = []
    for sid, scope, permission, chunk in (
        ("S01", "direct", "factual_support", "c1"),
        ("S02", "adjacent", "contextual_or_qualified_support", "c2"),
    ):
        path = tmp_path / sid / "SECTION_SOURCE_LEDGER.json"
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    "section_id": sid,
                    "sources": [
                        {
                            "paper_id": "p1",
                            "title": "Paper one",
                            "canonical_chunk_ids": [chunk],
                            "scope_fit": scope,
                            "use_permission": permission,
                            "content_depth": "fulltext",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rows.append(path)
    return kb, rows


def test_legacy_semantic_edges_are_revalidated_not_trusted() -> None:
    edges, audit = revalidate_legacy_relation_edges(
        [
            {
                "edge_id": "inactive",
                "source_paper_id": "p1",
                "target_paper_id": "missing",
                "edge_type": "snippet_ref_mention",
                "semantic_relation": "foundation_of",
                "historical_role": "predecessor",
                "source_chunk_id": "c1",
            },
            {
                "edge_id": "active-but-unproven",
                "source_paper_id": "p1",
                "target_paper_id": "p2",
                "edge_type": "cited_by",
                "semantic_relation": "extends",
                "source_chunk_id": "c1",
                "context": "",
            },
            {
                "edge_id": "active-and-explicit",
                "source_paper_id": "p1",
                "target_paper_id": "p2",
                "edge_type": "cited_by",
                "semantic_relation": "extends",
                "source_chunk_id": "c1",
                "context": "The later method extends the earlier mechanism under a new boundary.",
            },
        ],
        active_paper_ids=["p1", "p2"],
        active_chunk_ids=["c1"],
    )
    by_id = {item["edge_id"]: item for item in edges}
    assert by_id["inactive"]["status"] == "discovery_lead"
    assert by_id["inactive"]["semantic_relation"] == ""
    assert by_id["active-but-unproven"]["status"] == "unverified_legacy"
    assert by_id["active-but-unproven"]["semantic_relation"] == ""
    assert by_id["active-and-explicit"]["semantic_relation"] == "extends"
    assert audit["semantic_retained"] == 1
    assert audit["downgraded_discovery_lead"] == 1
    assert audit["downgraded_unverified_legacy"] == 1


def test_section_overlay_keeps_conflicting_permissions_independent(tmp_path: Path) -> None:
    kb, ledgers = _make_kb_and_ledgers(tmp_path)
    _, report, overlays, stats = migrate_shared_legacy_assets(
        ledgers,
        kb_paths=[kb],
        output_dir=tmp_path / "shared",
        overlay_dir=tmp_path / "overlays",
    )
    assert stats["shared_database_copy_count"] == 1
    assert stats["section_database_copy_count"] == 0
    s01 = json.loads(overlays["S01"].read_text(encoding="utf-8"))
    s02 = json.loads(overlays["S02"].read_text(encoding="utf-8"))
    assert s01["paper_overrides"]["p1"]["use_permission"] == "factual_support"
    assert s02["paper_overrides"]["p1"]["use_permission"] == "contextual_or_qualified_support"
    g01 = build_canonical_asset_graph(
        material_package_path=None,
        source_ledger_path=Path(report.output_ledger_path),
        work_dir=tmp_path / "g01",
        kb_paths=[Path(item) for item in report.migrated_kb_paths],
        overlay_path=overlays["S01"],
    )
    g02 = build_canonical_asset_graph(
        material_package_path=None,
        source_ledger_path=Path(report.output_ledger_path),
        work_dir=tmp_path / "g02",
        kb_paths=[Path(item) for item in report.migrated_kb_paths],
        overlay_path=overlays["S02"],
    )
    assert g01.chunks["c1"].use_permission == "factual_support"
    assert g02.chunks["c2"].use_permission == "contextual_or_qualified_support"


def _orchestrator_fixture(tmp_path: Path, *, two_sections: bool = False):
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c1','p1','Mechanism paper','The measured mechanism is established in a full text passage.','fulltext','fulltext')"
        )
        conn.commit()
    ledger = tmp_path / "shared.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p1",
                        "title": "Mechanism paper",
                        "canonical_chunk_ids": ["c1"],
                        "literature_role": "mechanism",
                        "scope_fit": "direct",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "S01.json"
    overlay.write_text(
        json.dumps(
            {
                "paper_ids": ["p1"],
                "chunk_ids": ["c1"],
                "paper_overrides": {"p1": {"scope_fit": "direct", "use_permission": "factual_support"}},
                "chunk_overrides": {"c1": {"scope_fit": "direct", "use_permission": "factual_support"}},
            }
        ),
        encoding="utf-8",
    )
    sections = [
        {
            "section_id": "S01",
            "title": "Governing mechanism",
            "chapter_argument": "Explain the governing mechanism and its measurable consequence.",
            "required_roles": ["mechanism"],
            "claims": [
                {
                    "claim_id": "S01-C01",
                    "statement": "The measured mechanism establishes the governing response in this platform.",
                    "evidence_type": "mechanism",
                    "supporting_text_chunk_ids": ["c1"],
                    "citation_paper_ids": ["p1"],
                    "load_bearing": True,
                    "saturation_score": 2.0,
                }
            ],
        }
    ]
    overlays = {"S01": overlay}
    if two_sections:
        sections.append(
            {
                "section_id": "S02",
                "title": "Uncovered comparison",
                "chapter_argument": "Compare the competing regimes.",
                "required_roles": ["comparison"],
                "claims": [],
            }
        )
        s02 = tmp_path / "S02.json"
        s02.write_text(json.dumps({"paper_ids": [], "chunk_ids": []}), encoding="utf-8")
        overlays["S02"] = s02
    return {
        "blueprint": {"sections": sections, "review_mode": "focused_perspective"},
        "scope_map": {"user_question": "How does the mechanism work?", "search_anchors": ["optical mechanism"]},
        "coverage_atlas": {"sections": []},
        "relation_graph": {"edges": []},
        "ledger": ledger,
        "kb": kb,
        "overlays": overlays,
    }


def test_phase3_orchestrator_emits_claim_bindings_and_outputs(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path)
    output = tmp_path / "phase3"
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
    ).run()
    assert result["status"] == "passed"
    assert result["r4_entered"] is False
    claim_graph = json.loads((output / "CLAIM_GRAPH.json").read_text(encoding="utf-8"))
    assert len(claim_graph.get("claims") or claim_graph.get("nodes") or []) == 1
    bundles = json.loads((output / "SYNTHESIS_BUNDLES.json").read_text(encoding="utf-8"))
    assert bundles["bundles"][0]["status"] == "material_ready"
    bindings = json.loads((output / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))
    assert bindings["sections"]["S01"]["claims"]["S01-C01"]["supporting_chunk_ids"] == ["c1"]
    for name in (
        "SECTION_ARGUMENT_CONTRACTS.json", "CANDIDATE_CLAIM_POOLS.json",
        "CLAIM_GRAPH.json", "MATERIAL_BINDINGS.json",
        "COVERAGE_REQUESTS.json", "COVERAGE_ATLAS.json", "PHASE3_RUN.json",
        "PHASE3_ACCEPTANCE.json", "PHASE3_ACCEPTANCE.md",
    ):
        assert (output / name).exists()
    pools = json.loads(
        (output / "CANDIDATE_CLAIM_POOLS.json").read_text(encoding="utf-8")
    )
    assert "S01" in pools["sections"]
    assert "runtime_audit" in pools["sections"]["S01"]
    acceptance = json.loads((output / "PHASE3_ACCEPTANCE.json").read_text(encoding="utf-8"))
    assert acceptance["engineering_safety"]["coverage_atlas_uses_migrated_relation_graph"] is True
    assert acceptance["coverage_atlas"]["semantic_relation_edge_count"] == 0


def test_candidate_portfolio_does_not_fake_claim_support(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path)
    fixture["blueprint"]["sections"][0]["claims"][0]["supporting_text_chunk_ids"] = []
    fixture["blueprint"]["sections"][0]["claims"][0]["context_text_chunk_ids"] = []
    output = tmp_path / "phase3_candidate_only"
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
    ).run()
    bindings = json.loads((output / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))
    binding = bindings["sections"]["S01"]["claims"]["S01-C01"]
    # Fallback portfolio chunks must NOT leak into core_chunk_ids — they are
    # contextual support only (the fix for "does not fake claim support").
    assert binding["core_chunk_ids"] == []
    assert binding["supporting_chunk_ids"] == []
    assert binding["evidence_binding_status"] == "contextual_fallback"
    # The fallback chunks must appear in contextual_support_chunk_ids instead.
    assert binding["contextual_support_chunk_ids"]
    assert binding["write_status"] == "needs_more_literature"
    assert result["material_quality"]["material_ready_sections"] == []
    bundles = json.loads((output / "SYNTHESIS_BUNDLES.json").read_text(encoding="utf-8"))
    assert bundles["bundles"][0]["status"] == "needs_more_literature"
    assert bundles["bundles"][0]["r4_handoff_allowed"] is False


def test_phase3_recomputes_only_affected_sections(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_loop"

    def coverage_executor(requests, iteration):
        if iteration == 1 and any(item["section_id"] == "S02" for item in requests):
            return {
                "S02": {
                    "claims": [
                        {
                            "claim_id": "S02-C01",
                            "statement": "A new comparison material is available for this section.",
                            "evidence_type": "comparison",
                            "supporting_text_chunk_ids": [],
                            "load_bearing": False,
                            "saturation_score": 0.5,
                        }
                    ],
                    "candidate_text_chunks": [
                        {
                            "chunk_id": "candidate-chunk",
                            "paper_id": "candidate-paper",
                            "text": "A newly retrieved comparison passage.",
                            "use_permission": "factual_support",
                            "content_depth": "fulltext",
                            "context_complete": True,
                        }
                    ],
                }
            }
        return {}

    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)
    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert "S02" in run["recomputed_sections"]
    assert run["iterations"][1]["sections_processed"] == ["S02"]
    assert len(run["iterations"]) <= 2
    assert result["r4_entered"] is False


def test_phase2_material_patch_refreshes_only_affected_section(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_material_refresh"
    new_kb = tmp_path / "supplemental.sqlite"
    with sqlite3.connect(new_kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.execute(
            "INSERT INTO text_chunks VALUES ('c3','p3','Comparison paper','A full comparison passage for the affected section.','fulltext','fulltext')"
        )
        conn.commit()
    new_ledger = tmp_path / "supplemental_ledger.json"
    new_ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p3",
                        "title": "Comparison paper",
                        "canonical_chunk_ids": ["c3"],
                        "literature_role": "comparison",
                        "scope_fit": "direct",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                        "acquisition_status": "fulltext",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def coverage_executor(requests, iteration):
        if iteration == 1 and any(item["section_id"] == "S02" for item in requests):
            return {
                "S02": {
                    "source_ledger_path": str(new_ledger),
                    "kb_sqlite": str(new_kb),
                }
            }
        return {}

    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)
    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert run["iterations"][1]["sections_processed"] == ["S02"]
    bundle = json.loads((output / "SYNTHESIS_BUNDLES.json").read_text(encoding="utf-8"))
    s02 = next(item for item in bundle["bundles"] if item["section_id"] == "S02")
    assert "c3" in s02["chunk_ids"] or s02["candidate_pool_count"] >= 1
    assert "SECTION_ASSET_OVERLAY.json" in s02["section_overlay_path"]
    assert result["r4_entered"] is False


def test_no_delta_material_bundle_stops_next_reinforcement_wave(
    tmp_path: Path,
) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_no_delta"
    calls: list[int] = []

    def coverage_executor(requests, iteration):
        calls.append(iteration)
        return {
            "S02": {
                "material_bundles": {"S02": {}},
                "notes": {"advisory": "no new material returned"},
            }
        }

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert calls == [1]
    assert len(run["iterations"]) == 1
    assert run["recomputed_sections"] == []


def test_genuine_candidate_delta_recomputes_section(
    tmp_path: Path,
) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_candidate_delta"

    def coverage_executor(requests, iteration):
        if iteration != 1:
            return {}
        return {
            "S02": {
                "candidate_text_chunks": [
                    {
                        "chunk_id": "candidate-delta-chunk",
                        "paper_id": "candidate-delta-paper",
                        "text": "A newly retrieved comparison passage.",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                        "context_complete": True,
                    }
                ]
            }
        }

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert "S02" in run["recomputed_sections"]
    assert len(run["iterations"]) == 2


def test_duplicate_candidate_chunks_stop_next_reinforcement_wave(
    tmp_path: Path,
) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_duplicate_candidate"
    calls: list[int] = []
    chunk = {
        "chunk_id": "duplicate-chunk",
        "paper_id": "duplicate-paper",
        "text": "A newly retrieved comparison passage.",
        "use_permission": "factual_support",
        "content_depth": "fulltext",
        "context_complete": True,
    }

    def coverage_executor(requests, iteration):
        calls.append(iteration)
        if iteration <= 2:
            return {"S02": {"candidate_text_chunks": [dict(chunk)]}}
        return {}

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    # The duplicate first-wave chunk is available to the second local
    # recomputation, but must not trigger another external acquisition call.
    assert calls == [1]
    assert run["coverage_waves_executed"] == 1
    assert run["recomputed_sections"] == ["S02"]
    assert len(run["iterations"]) == 2


def test_same_id_candidate_evidence_upgrade_counts_as_progress(
    tmp_path: Path,
) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    output = tmp_path / "phase3_same_id_upgrade"

    def coverage_executor(requests, iteration):
        if iteration == 1:
            return {
                "S02": {
                    "candidate_text_chunks": [{
                        "chunk_id": "c1",
                        "paper_id": "p1",
                        "text": "An abstract-level comparison excerpt.",
                        "use_permission": "background_and_candidate_only",
                        "content_depth": "abstract",
                        "context_complete": False,
                    }]
                }
            }
        if iteration == 2:
            return {
                "S02": {
                    "candidate_text_chunks": [{
                        "chunk_id": "c1",
                        "paper_id": "p1",
                        "text": "A full-text comparison passage with provenance.",
                        "use_permission": "factual_support",
                        "content_depth": "fulltext",
                        "context_complete": True,
                    }]
                }
            }
        return {}

    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
    ).run(coverage_executor=coverage_executor)

    run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert run["recomputed_sections"] == ["S02"]
    assert len(run["iterations"]) == 2
    assert run["iterations"][0]["sections_processed"] == ["S01", "S02"]
    assert run["iterations"][1]["sections_processed"] == ["S02"]


def test_evidence_fingerprint_ignores_row_order_and_covers_provenance() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import (
        Phase3ArgumentOrchestrator,
    )

    fingerprint = Phase3ArgumentOrchestrator._state_evidence_fingerprint

    def chunk(chunk_id: str) -> dict:
        return {
            "chunk_id": chunk_id,
            "paper_id": "p1",
            "text": f"text-{chunk_id}",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
            "source_kind": "publisher_html",
            "permission_ceiling": "factual_support",
        }

    def source(paper_id: str, route: str) -> dict:
        return {
            "paper_id": paper_id,
            "canonical_chunk_ids": ["c1", "c2"],
            "scope_fit": "direct",
            "content_depth": "fulltext",
            "use_permission": "factual_support",
            "acquisition_status": "fulltext",
            "materialization_route": route,
        }

    def state(sources, records) -> dict:
        return {
            "validated_section_sources": sources,
            "records": records,
            "fresh_chunk_rebinding": {"scientific_components_closed": []},
        }

    base = state(
        [source("p1", "pdf"), source("p2", "publisher_html")],
        [chunk("c1"), chunk("c2")],
    )
    reordered = state(
        [source("p2", "publisher_html"), source("p1", "pdf")],
        [chunk("c2"), chunk("c1")],
    )
    assert fingerprint(base) == fingerprint(reordered)

    chunk_upgrade = state(
        [source("p1", "pdf"), source("p2", "publisher_html")],
        [{**chunk("c1"), "source_kind": "abstract"}, chunk("c2")],
    )
    assert fingerprint(chunk_upgrade) != fingerprint(base)

    route_change = state(
        [
            {**source("p1", "pdf"), "acquisition_status": "abstract"},
            source("p2", "publisher_html"),
        ],
        [chunk("c1"), chunk("c2")],
    )
    assert fingerprint(route_change) != fingerprint(base)


def test_validated_section_refresh_is_domain_agnostic_and_fail_closed(
    tmp_path: Path,
) -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import (
        _merge_and_validate_section_sources,
    )
    from optomind_research.runtime.section_asset_overlay import (
        build_section_asset_overlay,
    )

    kb = tmp_path / "cross_domain.sqlite"
    rows = [
        (
            "film-bandwidth", "paper-film", "Thin-film spectral bandwidth",
            "The measured passband width is reported from the full spectrum.",
        ),
        (
            "surface-efficiency", "paper-surface", "Metasurface efficiency",
            "The conversion efficiency is compared with a reference device.",
        ),
        (
            "microscopy-metric", "paper-microscopy", "Microscopy reconstruction",
            "The reconstruction metric is evaluated on the measured image stack.",
        ),
        (
            "owner-mismatch", "paper-film", "Thin-film ownership control",
            "This chunk belongs to the thin-film paper only.",
        ),
    ]
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,'fulltext','fulltext')",
            rows,
        )
        conn.commit()

    incoming = [
        {
            "paper_id": "paper-film",
            "canonical_chunk_ids": ["film-bandwidth"],
            "literature_role": "measurement",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "section_id": "S-CROSS",
        },
        {
            "paper_id": "paper-surface",
            "canonical_chunk_ids": ["surface-efficiency"],
            "literature_role": "comparison",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "section_id": "S-CROSS",
        },
        {
            "paper_id": "paper-microscopy",
            "canonical_chunk_ids": ["microscopy-metric", "unknown-metric"],
            "literature_role": "method",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "section_id": "S-CROSS",
        },
        {
            "paper_id": "paper-wrong-owner",
            "canonical_chunk_ids": ["owner-mismatch"],
            "literature_role": "control",
            "scope_fit": "direct",
            "use_permission": "factual_support",
            "section_id": "S-CROSS",
        },
    ]
    ownership = _merge_and_validate_section_sources(
        section_id="S-CROSS",
        previous_sources=[],
        incoming_sources=incoming,
        kb_paths=[kb],
    )
    validated_chunks = {
        chunk_id
        for source in ownership["sources"]
        for chunk_id in source["canonical_chunk_ids"]
    }
    assert validated_chunks == {
        "film-bandwidth", "surface-efficiency", "microscopy-metric"
    }
    rejected = {
        (item.get("id"), item.get("reason"))
        for item in ownership["rejected_ids"]
    }
    assert ("unknown-metric", "unknown_chunk_id") in rejected
    assert ("owner-mismatch", "chunk_owner_mismatch") in rejected

    ledger = tmp_path / "validated_ledger.json"
    ledger.write_text(
        json.dumps({"section_id": "S-CROSS", "sources": ownership["sources"]}),
        encoding="utf-8",
    )
    overlay = tmp_path / "validated_overlay.json"
    overlay_payload = build_section_asset_overlay(
        section_id="S-CROSS",
        sources=ownership["sources"],
        shared_kb_paths=[kb],
        output_path=overlay,
    )
    graph = build_canonical_asset_graph(
        material_package_path=None,
        source_ledger_path=ledger,
        work_dir=tmp_path / "validated_graph",
        kb_paths=[kb],
        overlay_path=overlay,
    )
    assert set(graph.chunks) == validated_chunks
    assert "unknown-metric" not in overlay_payload["chunk_ids"]
    assert "owner-mismatch" not in overlay_payload["chunk_ids"]
    assert "paper-wrong-owner" not in overlay_payload["paper_ids"]


def test_successive_phase2_patches_retain_validated_prior_wave_allowlist(
    tmp_path: Path,
) -> None:
    fixture = _orchestrator_fixture(tmp_path)
    staging = tmp_path / "successive.sqlite"
    with sqlite3.connect(staging) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,'fulltext','fulltext')",
            [
                ("wave-one-chunk", "wave-one-paper", "Wave one", "A first newly verified scientific result."),
                ("wave-two-chunk", "wave-two-paper", "Wave two", "A second newly verified scientific result."),
            ],
        )
        conn.commit()

    ledgers = []
    for wave in ("one", "two"):
        path = tmp_path / f"wave-{wave}.json"
        path.write_text(json.dumps({
            "section_id": "S01",
            "sources": [{
                "paper_id": f"wave-{wave}-paper",
                "title": f"Wave {wave}",
                "canonical_chunk_ids": [f"wave-{wave}-chunk"],
                "literature_role": "mechanism",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "acquisition_status": "fulltext",
                "section_id": "S01",
            }],
        }), encoding="utf-8")
        ledgers.append(path)

    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "successive_output",
        max_iterations=2,
    )
    state = orchestrator._prepare_section(
        fixture["blueprint"]["sections"][0],
        0,
        fixture["blueprint"]["sections"],
    )
    for ledger in ledgers:
        orchestrator._refresh_state_from_coverage_patch(state, {
            "source_ledger_path": str(ledger),
            "staging_kb_sqlite": str(staging),
        })
    assert {"wave-one-chunk", "wave-two-chunk"}.issubset(state["graph"].chunks)
    assert {"wave-one-chunk", "wave-two-chunk"}.issubset(
        set(state["section"]["allowed_chunk_ids"])
    )
    overlay = json.loads(Path(state["overlay_path"]).read_text(encoding="utf-8"))
    assert {"wave-one-chunk", "wave-two-chunk"}.issubset(overlay["chunk_ids"])


def test_domain_agnostic_fresh_evidence_states_across_three_scientific_fields() -> None:
    from optomind_research.runtime import fresh_evidence_reconciliation as fresh

    claims = [
        {
            "claim_id": "film",
            "missing_evidence_components": [
                "The thin-film filter has a 3 dB spectral bandwidth of 12 nm."
            ],
        },
        {
            "claim_id": "surface",
            "missing_evidence_components": [
                "The metasurface efficiency exceeds the reference efficiency by 15%."
            ],
        },
        {
            "claim_id": "microscopy",
            "missing_evidence_components": [
                "Microscopy reconstruction reaches SSIM 0.92 without denoising."
            ],
        },
    ]
    records = {
        "film-chunk": {
            "chunk_id": "film-chunk",
            "paper_id": "film-paper",
            "text": "The thin-film filter has a 3 dB spectral bandwidth of 12 nm.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
        "surface-chunk": {
            "chunk_id": "surface-chunk",
            "paper_id": "surface-paper",
            "text": "The metasurface efficiency exceeds the reference efficiency.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
        "microscopy-chunk": {
            "chunk_id": "microscopy-chunk",
            "paper_id": "microscopy-paper",
            "text": "Microscopy reconstruction reaches PSNR 32 dB with denoising.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
    }
    audits = fresh.audit_fresh_components(claims, records, records)
    by_claim = {item["claim_id"]: item for item in audits}
    assert by_claim["film"]["status"] == "supported"
    assert by_claim["film"]["supported_component"] == records["film-chunk"]["text"]
    assert by_claim["surface"]["status"] == "partially_supported"
    assert any("15%" in item for item in by_claim["surface"]["residual_components"])
    assert by_claim["microscopy"]["status"] == "unsupported"
    assert by_claim["microscopy"]["chunk_ids"] == []
    assert any("SSIM" in item for item in by_claim["microscopy"]["residual_components"])
    assert any("0.92" in item for item in by_claim["microscopy"]["residual_components"])

    source = Path(fresh.__file__).read_text(encoding="utf-8").casefold()
    for forbidden in (
        "jordan", "diabolical", "resolvent", "riemann",
        "algebraic multiplicity", "geometric multiplicity", "complex parameter",
    ):
        assert forbidden not in source


def test_residual_normalization_is_idempotent_and_does_not_rewrap_specific_gaps() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        normalize_residual,
        normalize_residuals,
        proposition_coverage_residual,
    )

    recursive = (
        "Unverified proposition coverage: Unverified proposition coverage: "
        "Complex parameter characterization"
    )
    specific = (
        "Unverified proposition coverage: Unverified named technical entity: "
        "quoted metric"
    )
    assert normalize_residual(recursive) == (
        "Unverified proposition coverage: Complex parameter characterization"
    )
    assert normalize_residual(specific) == (
        "Unverified named technical entity: quoted metric"
    )
    once = normalize_residuals([recursive, normalize_residual(recursive)])
    assert once == [
        "Unverified proposition coverage: Complex parameter characterization"
    ]
    assert proposition_coverage_residual(specific) == (
        "Unverified named technical entity: quoted metric"
    )


def test_precision_constraints_do_not_treat_hyphenated_adjectives_as_entities() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        extract_precision_constraints,
    )

    constraints = extract_precision_constraints(
        "A multi-valued, non-trivial SSIM score of order 3 is not < 0.92 "
        "for the 'reference metric'."
    )
    values = {item["value"] for item in constraints}
    assert "multi-valued" not in values
    assert "non-trivial" not in values
    assert "SSIM" in values
    assert "reference metric" in values
    assert any(item["kind"] == "order_or_size" for item in constraints)
    assert any(item["kind"] == "negation" for item in constraints)
    assert any(
        item["kind"] == "comparison" and item["value"] == "<"
        for item in constraints
    )
    semantic_wording = extract_precision_constraints(
        "Efficiency outperforms and exceeds the reference."
    )
    assert not any(
        item["kind"] in {"comparison", "relation"}
        for item in semantic_wording
    )


def test_semantic_batch_rejects_unknown_candidate_chunk_ids(monkeypatch) -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )
    from optomind_research.runtime.fresh_evidence_semantic_judge import (
        QwenFreshEvidenceSemanticJudge,
    )
    from llm import qwen_chat_client

    claims = [{
        "claim_id": "comparison",
        "missing_evidence_components": [
            "The measured response exceeds the reference by 20%."
        ],
    }]
    records = {
        "known": {
            "chunk_id": "known",
            "paper_id": "paper",
            "text": "The measured response exceeds the reference.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    original = dict(audits[0])
    calls = []

    def fake_qwen_chat(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        calls.append(kwargs)
        return {
            "content": json.dumps({"decisions": [{
                "component_id": payload["components"][0]["component_id"],
                "status": "supported",
                "supported_proposition": "The measured response exceeds the reference.",
                "residual_precision": [],
                "cited_candidate_chunk_ids": ["unknown"],
            }]}),
            "_llm_usage": {
                "success": True,
                "failure": False,
                "model_name": "qwen-flash",
                "model_tier": "cheap_model",
                "estimated_input_tokens": 180,
                "estimated_output_tokens": 40,
                "request_attempt_count": 1,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_qwen_chat)
    judge = QwenFreshEvidenceSemanticJudge()

    updated, telemetry = apply_semantic_judge_batch(
        audits, judge, section_id="cross-domain"
    )
    assert len(calls) == 2
    assert telemetry["batch_count"] == 2
    assert telemetry["callable_call_count"] == 2
    assert telemetry["accepted_decision_count"] == 0
    assert telemetry["rejected_decision_count"] == 1
    assert telemetry["format_failures"][0]["reason"] == "unknown_candidate_id"
    assert telemetry["api_call_count"] == 2
    assert updated[0]["status"] == "unreviewed_format_failure"
    assert updated[0]["decision_source"] == original["decision_source"]
    assert "unknown" not in updated[0]["chunk_ids"]


def test_production_semantic_batch_invalid_json_falls_back_without_improvement(
    monkeypatch,
) -> None:
    from llm import qwen_chat_client
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )
    from optomind_research.runtime.fresh_evidence_semantic_judge import (
        QwenFreshEvidenceSemanticJudge,
    )

    claims = [{
        "claim_id": "metric",
        "missing_evidence_components": [
            "The reconstruction score reaches 0.95."
        ],
    }]
    records = {
        "metric-chunk": {
            "chunk_id": "metric-chunk",
            "paper_id": "metric-paper",
            "text": "The reconstruction score reaches a high value.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    original = json.loads(json.dumps(audits))
    calls = []

    def fake_qwen_chat(**kwargs):
        calls.append(kwargs)
        return {
            "content": "not-json",
            "_llm_usage": {
                "success": True,
                "failure": False,
                "model_name": "qwen-flash",
                "estimated_input_tokens": 120,
                "estimated_output_tokens": 3,
                "request_attempt_count": 1,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_qwen_chat)
    updated, telemetry = apply_semantic_judge_batch(
        audits,
        QwenFreshEvidenceSemanticJudge(),
        section_id="metric-section",
    )
    assert len(calls) == 2
    assert updated[0]["status"] == "unreviewed_format_failure"
    assert updated[0]["support_state"] == "unsupported"
    assert telemetry["accepted_decision_count"] == 0
    assert telemetry["rejected_decision_count"] == 1
    assert telemetry["improvement_attributed_to_semantic_judge"] is False
    assert telemetry["fallback_used"] is True
    assert telemetry["api_call_count"] == 2
    assert "JSONDecodeError" in telemetry["error"]
    assert len(telemetry["format_failure_history"]) == 2
    assert telemetry["format_failures"][0]["reason"].startswith("JSONDecodeError")


def test_production_semantic_batch_provider_failure_is_recorded_and_fail_closed(
    monkeypatch,
) -> None:
    from llm import qwen_chat_client
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )
    from optomind_research.runtime.fresh_evidence_semantic_judge import (
        QwenFreshEvidenceSemanticJudge,
    )

    claims = [{
        "claim_id": "bandwidth",
        "missing_evidence_components": [
            "The measured bandwidth is at least 20 nm."
        ],
    }]
    records = {
        "bandwidth-chunk": {
            "chunk_id": "bandwidth-chunk",
            "paper_id": "bandwidth-paper",
            "text": "The measured bandwidth is broad.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    original = json.loads(json.dumps(audits))
    calls = []

    def fake_qwen_chat(**kwargs):
        calls.append(kwargs)
        return {
            "content": "[fallback] no provider response",
            "_llm_usage": {
                "success": False,
                "failure": True,
                "fallback_used": True,
                "error_type": "AllKeysExhausted",
                "model_name": "qwen-flash",
                "model_tier": "cheap_model",
                "estimated_input_tokens": 150,
                "estimated_output_tokens": 1,
                "request_attempt_count": 1,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_qwen_chat)
    updated, telemetry = apply_semantic_judge_batch(
        audits,
        QwenFreshEvidenceSemanticJudge(),
        section_id="bandwidth-section",
    )
    assert len(calls) == 1
    assert updated == original
    assert telemetry["accepted_decision_count"] == 0
    assert telemetry["fallback_used"] is True
    assert telemetry["api_call_count"] == 1
    assert telemetry["actual_model"] == "qwen-flash"
    assert telemetry["input_tokens"] > 0
    assert telemetry["token_provenance"] == "estimated"
    assert telemetry["cost_provenance"] == "estimated_list_price"
    assert telemetry["estimated_cost_cny"] > 0
    assert "AllKeysExhausted" in telemetry["error"]


def test_semantic_repair_success_clears_prior_failure_and_aggregates_telemetry() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [{
        "claim_id": "repair",
        "missing_evidence_components": [
            "The measured response exceeds the reference baseline."
        ],
    }]
    records = {
        "known": {
            "chunk_id": "known",
            "paper_id": "paper",
            "text": "The measured response is above the reference level.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []

    class Judge:
        last_telemetry = {
            "call_count": 1,
            "api_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 20,
            "estimated_cost_cny": 0.1,
            "cost_provenance": "estimated_list_price",
            "token_provenance": "estimated",
            "actual_model": "qwen-flash",
            "model_provenance": "configured_request",
            "fallback_used": False,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            component = payload["components"][0]
            if len(calls) == 1:
                return {"decisions": [{
                    "slot": component["slot"],
                    "status": "supported",
                    "supported_proposition": "The measured response exceeds the reference.",
                    "residual_precision": [],
                    "cited_candidate_chunk_ids": ["unknown"],
                }]}
            return {"decisions": [{
                "slot": component["slot"],
                "status": "supported",
                "supported_proposition": "The measured response is above the reference level.",
                "residual_precision": [],
                "cited_candidate_chunk_ids": ["known"],
            }]}

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="repair-section"
    )
    assert len(calls) == 2
    assert updated[0]["status"] == "supported"
    assert updated[0]["decision_source"] == "semantic_batch_judge"
    assert telemetry["accepted_decision_count"] == 1
    assert telemetry["rejected_decision_count"] == 0
    assert len(telemetry["format_failure_history"]) == 1
    assert telemetry["format_failure_history"][0]["reason"] == "unknown_candidate_id"
    assert telemetry["format_failures"] == []
    assert telemetry["call_count"] == 2
    assert telemetry["api_call_count"] == 2
    assert telemetry["input_tokens"] == 20
    assert telemetry["output_tokens"] == 40
    assert round(telemetry["estimated_cost_cny"], 6) == 0.2


def test_semantic_omitted_decision_is_retried_and_then_unresolved() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [{
        "claim_id": "omitted",
        "missing_evidence_components": [
            "A missing expected decision with 95% confidence."
        ],
    }]
    records = {
        "known": {
            "chunk_id": "known",
            "paper_id": "paper",
            "text": "A missing expected decision is present here.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []

    class Judge:
        last_telemetry = {
            "call_count": 1,
            "api_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 10,
            "estimated_cost_cny": 0.05,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            return {"decisions": []}

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="omitted-section"
    )
    assert len(calls) == 2
    assert updated[0]["status"] == "unreviewed_format_failure"
    assert telemetry["accepted_decision_count"] == 0
    assert telemetry["rejected_decision_count"] == 1
    assert len(telemetry["format_failure_history"]) == 2
    assert telemetry["format_failures"][0]["reason"] == "missing_decision"


def test_semantic_invalid_whole_response_repairs_and_keeps_valid_rows() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [
        {"claim_id": "one", "missing_evidence_components": [
            "First component establishes the initial condition."
        ]},
        {"claim_id": "two", "missing_evidence_components": [
            "Second component establishes the final condition."
        ]},
    ]
    records = {
        "chunk-one": {
            "chunk_id": "chunk-one",
            "paper_id": "paper-one",
            "text": "First component text.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
        "chunk-two": {
            "chunk_id": "chunk-two",
            "paper_id": "paper-two",
            "text": "Second component text.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []

    class Judge:
        last_telemetry = {
            "call_count": 1,
            "api_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 10,
            "estimated_cost_cny": 0.05,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            if len(calls) == 1:
                return "not-json"
            decisions = []
            for component in payload["components"]:
                candidate = component["candidates"][0]
                decisions.append({
                    "slot": component["slot"],
                    "status": "supported",
                    "supported_proposition": candidate["excerpt"],
                    "residual_precision": [],
                    "cited_candidate_chunk_ids": [candidate["chunk_id"]],
                })
            return {"decisions": decisions}

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="whole-response"
    )
    assert len(calls) == 2
    assert all(item["status"] == "supported" for item in updated)
    assert telemetry["accepted_decision_count"] == 2
    assert telemetry["rejected_decision_count"] == 0
    assert len(telemetry["format_failure_history"]) == 2
    assert all(
        item["reason"].startswith("JSONDecodeError")
        for item in telemetry["format_failure_history"]
    )
    assert telemetry["format_failures"] == []


def test_semantic_merge_telemetry_exactly_once_for_returned_invalid_json() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [{
        "claim_id": "merge-once",
        "missing_evidence_components": [
            "A returned invalid response with 40% confidence."
        ],
    }]
    records = {
        "known": {
            "chunk_id": "known",
            "paper_id": "paper",
            "text": "A returned invalid response is present here.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []

    class Judge:
        last_telemetry = {
            "call_count": 1,
            "api_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 10,
            "estimated_cost_cny": 0.05,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            return "not-json"

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="merge-once-section"
    )
    assert len(calls) == 2
    assert updated[0]["status"] == "unreviewed_format_failure"
    assert telemetry["call_count"] == 2
    assert telemetry["api_call_count"] == 2
    assert telemetry["input_tokens"] == 20
    assert telemetry["output_tokens"] == 20
    assert round(telemetry["estimated_cost_cny"], 6) == 0.1
    assert len(telemetry["format_failure_history"]) == 2


def test_semantic_physical_retries_do_not_break_logical_batch_invariant() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [{
        "claim_id": "physical-retries",
        "missing_evidence_components": [
            "The measured response exceeds the reference baseline."
        ],
    }]
    records = {
        "known": {
            "chunk_id": "known",
            "paper_id": "paper",
            "text": "The measured response is above the reference level.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        }
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []

    class Judge:
        last_telemetry = {
            "call_count": 3,
            "api_call_count": 3,
            "input_tokens": 30,
            "output_tokens": 10,
            "estimated_cost_cny": 0.15,
            "fallback_used": True,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            component = payload["components"][0]
            candidate = component["candidates"][0]
            return {"decisions": [{
                "slot": component["slot"],
                "status": "supported",
                "supported_proposition": candidate["excerpt"],
                "residual_precision": [],
                "cited_candidate_chunk_ids": [candidate["chunk_id"]],
            }]}

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="physical-retries-section"
    )
    assert len(calls) == 1
    assert updated[0]["status"] == "supported"
    assert updated[0]["decision_source"] == "semantic_batch_judge"
    assert telemetry["batch_count"] == 1
    assert telemetry["callable_call_count"] == 1
    assert telemetry["one_batch_invariant"] is True
    assert telemetry["api_call_count"] == 3
    assert telemetry["call_count"] == 3
    assert telemetry["accepted_decision_count"] == 1
    assert telemetry["rejected_decision_count"] == 0


def test_semantic_valid_first_call_rows_survive_repair_of_only_bad_rows() -> None:
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
        audit_fresh_components,
    )

    claims = [
        {"claim_id": "good", "missing_evidence_components": [
            "First component establishes the initial condition."
        ]},
        {"claim_id": "bad", "missing_evidence_components": [
            "Second component establishes the final condition."
        ]},
    ]
    records = {
        "chunk-one": {
            "chunk_id": "chunk-one",
            "paper_id": "paper-one",
            "text": "First component text.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
        "chunk-two": {
            "chunk_id": "chunk-two",
            "paper_id": "paper-two",
            "text": "Second component text.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
    }
    audits = audit_fresh_components(claims, records, records)
    calls: list[int] = []
    repair_payloads: list[list[dict]] = []
    first_call_components: dict[str, dict] = {}

    class Judge:
        last_telemetry = {
            "call_count": 1,
            "api_call_count": 1,
            "input_tokens": 10,
            "output_tokens": 10,
            "estimated_cost_cny": 0.05,
            "success": True,
        }

        def __call__(self, payload):
            calls.append(1)
            components = payload["components"]
            if len(calls) == 1:
                first_call_components.update({
                    component["requested_component"]: component
                    for component in components
                })
                decisions = []
                for component in components:
                    candidate = component["candidates"][0]
                    if component["requested_component"].startswith("First"):
                        decisions.append({
                            "slot": component["slot"],
                            "status": "supported",
                            "supported_proposition": candidate["excerpt"],
                            "residual_precision": [],
                            "cited_candidate_chunk_ids": [candidate["chunk_id"]],
                        })
                    else:
                        decisions.append({
                            "slot": component["slot"],
                            "status": "supported",
                            "supported_proposition": candidate["excerpt"],
                            "residual_precision": [],
                            "cited_candidate_chunk_ids": ["unknown"],
                        })
                return {"decisions": decisions}
            repair_payloads.append(components)
            decisions = []
            for component in components:
                candidate = component["candidates"][0]
                decisions.append({
                    "slot": component["slot"],
                    "status": "supported",
                    "supported_proposition": candidate["excerpt"],
                    "residual_precision": [],
                    "cited_candidate_chunk_ids": [candidate["chunk_id"]],
                })
            return {"decisions": decisions}

    updated, telemetry = apply_semantic_judge_batch(
        audits, Judge(), section_id="mixed-batch"
    )
    assert len(calls) == 2
    bad_component = first_call_components[
        "Second component establishes the final condition."
    ]
    assert len(repair_payloads) == 1
    assert [
        item["component_id"] for item in repair_payloads[0]
    ] == [bad_component["component_id"]]
    assert all(item["status"] == "supported" for item in updated)
    assert all(
        item["decision_source"] == "semantic_batch_judge" for item in updated
    )
    assert telemetry["accepted_decision_count"] == 2
    assert telemetry["rejected_decision_count"] == 0
    assert len(telemetry["format_failure_history"]) == 1
    assert telemetry["format_failure_history"][0]["reason"] == "unknown_candidate_id"
    assert telemetry["format_failures"] == []


def test_fresh_s01_evidence_replaces_stale_rewrite_and_rebuilds_task_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from llm import qwen_chat_client

    fixture = _orchestrator_fixture(tmp_path)
    claim = fixture["blueprint"]["sections"][0]["claims"][0]
    claim["claim_id"] = "S01-C04"
    claim["statement"] = (
        "In the complex parameter plane, an exceptional point manifests as a branch-point "
        "singularity of the resolvent operator, leading to a multi-valued Riemann surface "
        "structure for the eigenvalues and eigenvectors."
    )
    stale_rewrite = (
        "At an nth-order exceptional point, a generic perturbation changes the eigenvalues "
        "in proportion to the nth root of its strength."
    )
    claim["supported_rewrite"] = stale_rewrite
    claim["supporting_text_chunk_ids"] = []
    claim["missing_evidence_components"] = [
        "Branch-point singularity of the resolvent operator",
        "Multi-valued Riemann surface structure for eigenvalues and eigenvectors",
        "Complex parameter plane characterization",
    ]
    fixture["blueprint"]["sections"][0]["argument_tasks"] = [{
        "task_id": "S01:task:03",
        "description": "What is the role of the resolvent pole and branch-point singularity in the complex parameter plane?",
        "required": True,
    }]
    new_kb = tmp_path / "fresh.sqlite"
    with sqlite3.connect(new_kb) as conn:
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)"
        )
        conn.executemany(
            "INSERT INTO text_chunks VALUES (?,?,?,?,'fulltext','fulltext')",
            [
                (
                    "fresh-0007", "fresh-paper", "Fresh EP paper",
                    "The EP is the branch-point singularity of a resonance pole in a scattering problem.",
                ),
                (
                    "fresh-0010", "fresh-paper", "Fresh EP paper",
                    "The branch point in the lambda plane defines a two-sheeted branched cover.",
                ),
                (
                    "fresh-0019", "fresh-paper", "Fresh EP paper",
                    "Changing lambda deforms the energy Riemann sheet.",
                ),
                (
                    "fresh-0022", "fresh-paper", "Fresh EP paper",
                    "The branch labels the Riemann sheet of the analytically continued resonance energy.",
                ),
            ],
        )
        conn.commit()
    new_ledger = tmp_path / "fresh_ledger.json"
    new_ledger.write_text(
        json.dumps({
            "sources": [{
                "paper_id": "fresh-paper",
                "title": "Fresh EP paper",
                "canonical_chunk_ids": [
                    "fresh-0007", "fresh-0010", "fresh-0019", "fresh-0022",
                ],
                "literature_role": "mechanism",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "acquisition_status": "fulltext",
            }]
        }),
        encoding="utf-8",
    )

    def coverage_executor(requests, iteration):
        if iteration == 1:
            return {
                "S01": {
                    "source_ledger_path": str(new_ledger),
                    "kb_sqlite": str(new_kb),
                }
            }
        return {}

    judge_calls = []

    def semantic_decisions(payload):
        decisions = []
        for component in payload["components"]:
            requested = component["requested_component"]
            candidates = {
                item["chunk_id"]: item["excerpt"]
                for item in component["candidates"]
            }
            if requested.startswith("Branch-point"):
                cited = ["fresh-0007"]
                status = "partially_supported"
                residual = ["The exact resolvent operator is not established."]
            elif requested.startswith("Multi-valued"):
                cited = ["fresh-0019", "fresh-0022"]
                status = "partially_supported"
                residual = ["The cited excerpts establish sheets but not the stronger entity-specific surface wording."]
            else:
                cited = ["fresh-0010"]
                status = "supported"
                residual = []
            decisions.append({
                "component_id": component["component_id"],
                "status": status,
                "supported_proposition": candidates[cited[0]],
                "residual_precision": residual,
                "cited_candidate_chunk_ids": cited,
            })
        return {"decisions": decisions}

    def fake_qwen_chat(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        judge_calls.append(kwargs)
        return {
            "content": json.dumps(semantic_decisions(payload)),
            "_llm_usage": {
                "success": True,
                "failure": False,
                "model_name": "qwen-flash",
                "model_tier": "cheap_model",
                "input_tokens": 640,
                "output_tokens": 180,
                "request_attempt_count": 1,
            },
        }

    monkeypatch.setattr(qwen_chat_client, "call_qwen_chat", fake_qwen_chat)

    output = tmp_path / "phase3_fresh_rebinding"
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        max_iterations=2,
        enable_fresh_evidence_semantic_judge=True,
    ).run(coverage_executor=coverage_executor)

    bindings = json.loads((output / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))
    section = bindings["sections"]["S01"]
    rebinding = section["fresh_chunk_rebinding"]
    assert set(rebinding["fresh_chunk_ids"]) >= {
        "fresh-0007", "fresh-0010", "fresh-0019", "fresh-0022",
    }
    audits = {
        item["requested_component"]: item
        for item in rebinding["component_audit"]
    }
    assert len(judge_calls) == 1
    call = judge_calls[0]
    assert call["model_tier"] == "cheap_model"
    assert call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    assert 512 <= call["max_tokens"] <= 1800
    assert call["max_retries"] == 0
    # One semantic batch may still rotate an unusable key or model through
    # the shared client. Physical retries are captured by request telemetry.
    assert "max_key_candidates" not in call
    assert "max_transport_key_candidates" not in call
    assert call["allow_model_fallback"] is True
    assert call["enable_thinking"] is False
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "Phase 3 Fresh Evidence Semantic Judge.txt"
    )
    assert call["messages"][0]["content"] == prompt_path.read_text(
        encoding="utf-8"
    ).strip()
    call["messages"][1]["content"].encode("ascii")
    assert rebinding["semantic_judge"]["batch_count"] == 1
    assert rebinding["semantic_judge"]["api_call_count"] == 1
    assert rebinding["semantic_judge"]["provider"] == "qwen"
    assert rebinding["semantic_judge"]["actual_model"] == "qwen-flash"
    assert rebinding["semantic_judge"]["token_provenance"] == "provider_reported"
    assert rebinding["semantic_judge"]["estimated_cost_cny"] > 0
    assert rebinding["semantic_judge"]["one_batch_invariant"] is True
    assert rebinding["semantic_judge"]["accepted_decision_count"] == 3
    assert audits["Branch-point singularity of the resolvent operator"]["status"] == "partially_supported"
    assert any(
        "resolvent operator" in item.casefold()
        for item in audits["Branch-point singularity of the resolvent operator"]["residual_components"]
    )
    assert audits["Multi-valued Riemann surface structure for eigenvalues and eigenvectors"]["status"] == "partially_supported"
    assert audits["Complex parameter plane characterization"]["status"] == "supported"
    claim_binding = section["claims"]["S01-C04"]
    assert set(claim_binding["supporting_chunk_ids"]) >= {
        "fresh-0007", "fresh-0010", "fresh-0019", "fresh-0022",
    }
    assert claim_binding["effective_statement"] != stale_rewrite
    assert claim_binding["superseded_supported_rewrite"] == stale_rewrite
    assert "branch-point singularity of a resonance pole" in claim_binding["effective_statement"]
    assert "energy Riemann sheet" in claim_binding["effective_statement"]
    assert "lambda plane" in claim_binding["effective_statement"]
    missing = set(claim_binding["missing_evidence_components"])
    assert "Branch-point singularity of the resolvent operator" not in missing
    assert "Complex parameter plane characterization" not in missing
    assert any("resolvent operator" in item.casefold() for item in missing)
    assert any("multi-valued" in item.casefold() or "entity-specific" in item.casefold() for item in missing)
    task = next(
        item for item in section["argument_task_coverage"]
        if item["task_id"] == "S01:task:03"
    )
    assert task["effective_claim_ids"] == ["S01-C04"]
    assert task["status"] == "partially_supported"
    assert task["support_state"] == "partially_supported"
    assert set(task["missing_components"]) == missing
    assert not any("No effective claim" in item for item in task["missing_components"])
    bundles = json.loads((output / "SYNTHESIS_BUNDLES.json").read_text(encoding="utf-8"))
    bundle_task = next(
        item for item in bundles["bundles"][0]["argument_task_coverage"]
        if item["task_id"] == "S01:task:03"
    )
    assert bundle_task == task
    final_snapshot = _coverage_snapshot(output / "SYNTHESIS_BUNDLES.json")
    assert final_snapshot["task_statuses"]["S01:task:03"] == "partially_supported"
    assert set(final_snapshot["missing_components"]) == missing
    phase3_run = json.loads((output / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    semantic_summary = phase3_run["fresh_evidence_semantic_judge"]
    assert phase3_run["phase"] == "Phase 3 - Argument and Material Orchestration"
    assert semantic_summary["api_call_count"] == 1
    assert semantic_summary["one_batch_invariant"] is True
    assert semantic_summary["estimated_cost_cny"] > 0
    assert phase3_run["llm"]["calls_observed_or_estimated"] == 1
    assert phase3_run["llm"]["estimated_cost_cny"] == semantic_summary["estimated_cost_cny"]
    assert result["r4_entered"] is False


def test_fresh_component_states_keep_stronger_jordan_and_multiplicity_precision_open() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import (
        _fresh_component_audit,
        _reconcile_fresh_claim_evidence,
    )
    from optomind_research.runtime.fresh_evidence_reconciliation import (
        apply_semantic_judge_batch,
    )

    claims = [
        {
            "claim_id": "S01-C02",
            "statement": "The Hamiltonian has a non-trivial Jordan block of size n at an exceptional point.",
            "missing_evidence_components": [
                "Jordan canonical form contains at least one non-trivial Jordan block of size n",
            ],
        },
        {
            "claim_id": "S01-C03",
            "statement": "The defective operator has algebraic multiplicity exceeding geometric multiplicity.",
            "missing_evidence_components": [
                "Explicit attribution to the defective nature of the non-Hermitian Hamiltonian matrix",
                "Explicit statement that algebraic multiplicity exceeds geometric multiplicity",
            ],
        },
    ]
    records = {
        "fresh-0010": {
            "chunk_id": "fresh-0010",
            "paper_id": "fresh-paper",
            "text": "At the exceptional point the eigenvalues and eigenvectors are degenerate and the operator becomes defective.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
        "fresh-0022": {
            "chunk_id": "fresh-0022",
            "paper_id": "fresh-paper",
            "text": "The Hamiltonian is nondiagonalizable but has a Jordan block.",
            "use_permission": "factual_support",
            "content_depth": "fulltext",
            "context_complete": True,
        },
    }
    audits = _fresh_component_audit(claims, records, records)

    def judge(payload):
        component = next(
            item for item in payload["components"]
            if "defective nature" in item["requested_component"]
        )
        candidates = {
            item["chunk_id"]: item["excerpt"]
            for item in component["candidates"]
        }
        decisions = []
        for item in payload["components"]:
            if item["component_id"] == component["component_id"]:
                decisions.append({
                    "component_id": item["component_id"],
                    "status": "partially_supported",
                    "supported_proposition": candidates["fresh-0010"],
                    "residual_precision": [
                        "Exact non-Hermitian Hamiltonian-matrix attribution is not established."
                    ],
                    "cited_candidate_chunk_ids": ["fresh-0010", "fresh-0022"],
                })
            elif "Jordan canonical form" in item["requested_component"]:
                item_candidates = {
                    row["chunk_id"]: row["excerpt"]
                    for row in item["candidates"]
                }
                decisions.append({
                    "component_id": item["component_id"],
                    "status": "partially_supported",
                    "supported_proposition": item_candidates["fresh-0022"],
                    "residual_precision": [
                        "The exact canonical form and block size are not established."
                    ],
                    "cited_candidate_chunk_ids": ["fresh-0022"],
                })
            else:
                decisions.append({
                    "component_id": item["component_id"],
                    "status": "unsupported",
                    "supported_proposition": "",
                    "residual_precision": [item["requested_component"]],
                    "cited_candidate_chunk_ids": [],
                })
        return {"decisions": decisions}

    audits, telemetry = apply_semantic_judge_batch(
        audits, judge, section_id="S01"
    )
    assert telemetry["batch_count"] == 1
    assert telemetry["accepted_decision_count"] == len(
        [item for item in audits if item.get("ranked_candidates")]
    )
    by_requested = {item["requested_component"]: item for item in audits}
    jordan = by_requested[
        "Jordan canonical form contains at least one non-trivial Jordan block of size n"
    ]
    defective = by_requested[
        "Explicit attribution to the defective nature of the non-Hermitian Hamiltonian matrix"
    ]
    multiplicity = by_requested[
        "Explicit statement that algebraic multiplicity exceeds geometric multiplicity"
    ]
    assert jordan["status"] == "partially_supported"
    assert any("size n" in item for item in jordan["residual_components"])
    assert defective["status"] == "partially_supported"
    assert any("non-Hermitian" in item for item in defective["residual_components"])
    assert multiplicity["status"] == "unsupported"
    assert multiplicity["chunk_ids"] == []

    for claim in claims:
        _reconcile_fresh_claim_evidence(
            claim,
            [item for item in audits if item["claim_id"] == claim["claim_id"]],
        )
    assert any("size n" in item for item in claims[0]["missing_evidence_components"])
    assert any(
        "non-Hermitian" in item
        for item in claims[1]["missing_evidence_components"]
    )
    assert any(
        "multiplicity" in item.casefold() or "exceeds" in item.casefold()
        for item in claims[1]["missing_evidence_components"]
    )


def test_unified_contract_reaches_m2a_without_sentence_cutting(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path)
    section = fixture["blueprint"]["sections"][0]
    section.update(
        {
            "key_questions": [
                "Which physical mechanism governs the measured response?",
                "Which boundary separates this mechanism from the competing regime?",
            ],
            "argument_sequence": [
                {"task_id": "t1", "description": "Establish the governing mechanism and its measured consequence."},
                {"task_id": "t2", "description": "Compare the boundary condition against the competing regime."},
            ],
            "scope_guardrails": ["Do not generalize beyond the stated platform."],
            "transitions": {"to_next": "Use the boundary to motivate the comparison section."},
        }
    )
    orchestrator = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "contract",
    )
    state = orchestrator._prepare_section(section, 0, [section])
    contract = state["section"]["section_contract"]
    assert contract == state["section"]["section_argument_contract"]
    assert len(contract["argument_tasks"]) >= 4
    assert contract["scope_guardrails"]
    assert contract["transitions"]["to_next"]

    from optomind_research.claim_decomposer import ClaimDecomposer

    payload = ClaimDecomposer(real_llm=False)._build_input_payload(state["section"])
    assert payload["section_contract"]["central_thesis"]
    assert len(payload["section_contract"]["argument_tasks"]) >= 4

    from optomind_research.claim_decomposer import _parse_llm_claims

    long_statement = "A complete claim explains the mechanism and remains semantically intact " + "under measured conditions " * 80 + "."
    claims = _parse_llm_claims(
        {"claims": [{"statement": long_statement, "evidence_type": "mechanism", "supporting_text_chunk_ids": ["c1"]}]},
        "S01",
        {"c1"},
    )
    assert claims[0].statement == " ".join(long_statement.split())
    assert "isolated_incomplete_statement" not in claims[0].critic_flags


def test_method_transfer_cannot_become_direct_evidence(monkeypatch) -> None:
    import optomind_research.claim_evidence_verifier as verifier_module
    from optomind_research.claim_schema import Claim

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps({
                "bindings": [{
                    "claim_id": "S01-C01",
                    "verdict": "direct",
                    "confidence": "high",
                    "supporting_text_refs": ["T01"],
                    "evidence_spans": [{
                        "text_ref": "T01",
                        "scope_fit": "cross_domain_analogy",
                        "retrieval_role": "evidence_candidate",
                        "quote": "The transferred method is described.",
                    }],
                    "reason": "The source reports the method.",
                }]
            }),
            "_llm_usage": {"input_tokens": 20, "output_tokens": 10},
        }

    monkeypatch.setattr(verifier_module, "call_qwen_chat", fake_chat)
    claim = Claim(
        claim_id="S01-C01",
        statement="The transferred method explains the target optical response.",
        evidence_type="mechanism",
    )
    section = {
        "section_id": "S01",
        "title": "Mechanism",
        "candidate_text_chunks": [{
            "chunk_id": "c-transfer",
            "paper_id": "p-transfer",
            "text_preview": "The transferred method is described.",
            "use_permission": "factual_support",
            "scope_fit": "cross_domain_analogy",
            "source_kind": "fulltext",
            "content_depth": "fulltext",
            "retrieval_role": "method_transfer",
            "context_complete": True,
        }],
    }
    verifier = verifier_module.ClaimEvidenceVerifier(model_tier="cheap_model")
    result = verifier.verify_and_bind([claim], section)[0]
    assert result.evidence_binding_status == "partial"
    assert "permission_ceiling_downgraded_verdict" in result.critic_flags
    assert verifier.last_audit["permission_rejected_count"] == 0
    assert verifier.last_audit["attempts"][0]["usage_recorded"] is True


def test_boundary_claim_is_never_load_bearing_and_optional_gap_does_not_request_search(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path)
    claim = fixture["blueprint"]["sections"][0]["claims"][0]
    claim["section_fit"] = "boundary"
    claim["load_bearing"] = True
    claim["importance"] = "load_bearing"
    claim["supporting_text_chunk_ids"] = []
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "boundary",
    ).run()
    binding = json.loads((tmp_path / "boundary" / "MATERIAL_BINDINGS.json").read_text(encoding="utf-8"))["sections"]["S01"]["claims"]["S01-C01"]
    assert binding["importance"] == "supporting"
    assert binding["load_bearing"] is False
    assert result["coverage_request_quality_passed"] is True


def test_phase2_receives_full_targeted_request_not_only_section_id(tmp_path: Path, monkeypatch) -> None:
    from optomind_research.runtime.section_coverage_orchestrator import (
        SectionCoverageOrchestrator,
        SectionCoverageOrchestratorConfig,
    )

    blueprint = tmp_path / "blueprint.json"
    blueprint.write_text(json.dumps({"sections": [{"section_id": "S01", "title": "Mechanism"}]}), encoding="utf-8")
    request = {
        "section_id": "S01",
        "queries": ["scientific evidence for optical mechanism frontier peer reviewed literature"],
        "missing_claim_ids": ["S01-C01"],
        "missing_roles": ["frontier"],
        "expected_new_papers": 2,
        "stop_condition": {"max_iterations": 2},
    }
    config = SectionCoverageOrchestratorConfig(
        blueprint_path=blueprint,
        base_kb_sqlite=None,
        output_root=tmp_path / "coverage",
        coverage_requests_by_section={"S01": request},
    )
    orchestrator = SectionCoverageOrchestrator(config)
    captured = {}

    def fake_run_one(section, *, remaining_stage_budget):
        captured.update(section.get("phase3_coverage_request") or {})
        return {"section_id": "S01", "status": "needs_more_literature", "input_tokens": 0, "output_tokens": 0, "cost_cny": 0}, None

    monkeypatch.setattr(orchestrator, "_run_one", fake_run_one)
    orchestrator.run()
    assert captured["queries"] == request["queries"]
    assert captured["missing_claim_ids"] == ["S01-C01"]
    assert captured["missing_roles"] == ["frontier"]
    assert captured["stop_condition"]["max_iterations"] == 2


def test_evidence_arbiter_is_one_batch_and_records_usage(monkeypatch) -> None:
    import llm.qwen_chat_client as client
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter
    from optomind_research.claim_schema import Claim

    calls = []

    def fake_chat(agent_name, messages, **kwargs):
        calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        return {
            "content": json.dumps({
                "claims": [
                    {
                        "claim_id": item["claim_id"],
                        "primary_evidence_type": item["current_evidence_type"],
                        "secondary_evidence_types": [],
                        "confidence": "high",
                        "reason": "The claim function is clear.",
                    }
                    for item in payload["claims"]
                ]
            }),
            "_llm_usage": {"input_tokens": 100, "output_tokens": 40},
        }

    monkeypatch.setattr(client, "call_qwen_chat", fake_chat)
    claims = [
        Claim("S01-C01", "A mechanism claim is stated clearly.", "mechanism"),
        Claim("S01-C02", "A measurement claim is reported clearly.", "measurement"),
    ]
    arbiter = EvidenceTypeArbiter(model_tier="cheap_model")
    arbiter.arbitrate_section(claims, {"section_id": "S01", "title": "Mechanism"})
    assert len(calls) == 1
    assert arbiter.last_audit["call_count"] == 1
    assert len(arbiter.last_audit["attempts"]) == 1
    assert arbiter.last_audit["attempts"][0]["usage_recorded"] is True
    assert arbiter.last_audit["attempts"][0]["model"] == "cheap_model"
    assert "retries" in arbiter.last_audit["attempts"][0]


def test_m2a_portfolio_context_does_not_reintroduce_single_paper_dominance(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from optomind_research.runtime.phase3_argument_orchestrator import SectionArgumentContract

    records = []
    chunks = {}
    papers = {}
    for paper_index in range(5):
        paper_id = f"p{paper_index}"
        papers[paper_id] = object()
        for chunk_index in range(8):
            chunk_id = f"{paper_id}:c{chunk_index}"
            chunks[chunk_id] = object()
            records.append({
                "chunk_id": chunk_id,
                "paper_id": paper_id,
                "paper_title": f"Mechanism paper {paper_index}",
                "normalized_text": (
                    "A complete full-text mechanism passage explains optical response "
                    "under the stated section conditions and comparison boundary."
                ),
                "text": "A complete full-text mechanism passage.",
                "scope_fit": "direct",
                "use_permission": "factual_support",
                "content_depth": "fulltext",
                "context_complete": True,
                "source_kind": "fulltext",
                "literature_roles": ["mechanism" if paper_index % 2 == 0 else "comparison"],
            })
    graph = SimpleNamespace(papers=papers, chunks=chunks)
    contract = SectionArgumentContract(
        schema_version="test",
        section_id="S01",
        core_question="Which mechanism governs the response?",
        central_judgment="The mechanism is condition-dependent.",
        argument_role="mechanism comparison",
        argument_tasks=[{"description": "Explain the governing mechanism."}],
    )
    _, selected = Phase3ArgumentOrchestrator._select_m2a_input(
        {"section_id": "S01", "title": "Mechanism comparison", "required_roles": ["mechanism", "comparison"]},
        contract,
        records,
        graph,
    )
    counts = {}
    for row in selected:
        counts[row["paper_id"]] = counts.get(row["paper_id"], 0) + 1
    assert len(counts) >= 4
    assert max(counts.values()) <= 4


def test_discovery_only_anchor_cannot_keep_direct_verdict(monkeypatch) -> None:
    import optomind_research.claim_evidence_verifier as verifier_module
    from optomind_research.claim_schema import Claim

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps({
                "bindings": [{
                    "claim_id": "S01-C01",
                    "verdict": "direct",
                    "confidence": "high",
                    "supporting_text_refs": ["T01"],
                    "evidence_spans": [{"text_ref": "T01", "quote": "Metadata only."}],
                }]
            }),
            "_llm_usage": {"input_tokens": 12, "output_tokens": 8},
        }

    monkeypatch.setattr(verifier_module, "call_qwen_chat", fake_chat)
    claim = Claim("S01-C01", "A concrete optical mechanism is established.", "mechanism")
    verifier = verifier_module.ClaimEvidenceVerifier(model_tier="cheap_model")
    result = verifier.verify_and_bind([claim], {
        "section_id": "S01",
        "candidate_text_chunks": [{
            "chunk_id": "c-discovery",
            "paper_id": "p1",
            "text_preview": "Metadata only.",
            "use_permission": "discovery_only",
            "scope_fit": "direct",
            "content_depth": "metadata",
        }],
    })[0]
    assert result.supporting_text_chunk_ids == []
    assert result.evidence_binding_status == "insufficient"
    assert any(flag.startswith("permission_rejected_refs") for flag in result.critic_flags)


def test_phase311_verifier_sends_only_compact_batch_anchors(monkeypatch) -> None:
    import optomind_research.claim_evidence_verifier as verifier_module
    from optomind_research.claim_schema import Claim

    captured = []

    def fake_chat(_agent, messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        captured.append(payload)
        rows = []
        for item in payload["claims"]:
            rows.append({
                "claim_id": item["claim_id"],
                "verdict": "direct",
                "confidence": "high",
                "supporting_text_refs": ["T01"],
                "evidence_spans": [{"text_ref": "T01", "quote": "A verified mechanism passage."}],
            })
        return {
            "content": json.dumps({"bindings": rows}),
            "_llm_usage": {"input_tokens": 80, "output_tokens": 40},
        }

    monkeypatch.setattr(verifier_module, "call_qwen_chat", fake_chat)
    claim = Claim("S01-C01", "A verified optical mechanism controls the response.", "mechanism")
    claim.supporting_text_chunk_ids = ["c1"]
    rows = []
    for index in range(6):
        rows.append({
            "chunk_id": f"c{index + 1}",
            "paper_id": f"p{index + 1}",
            "text_preview": "A verified optical mechanism controls the response under measured conditions.",
            "use_permission": "factual_support",
            "scope_fit": "direct",
            "content_depth": "fulltext",
            "source_kind": "fulltext",
            "provenance": {"very_large_nested_field": "x" * 5000, "provider": "semantic_scholar", "doi": f"10.1000/{index}"},
        })
    result = verifier_module.ClaimEvidenceVerifier(
        model_tier="cheap_model", strict_permissions=True
    ).verify_and_bind([claim], {"section_id": "S01", "title": "Mechanism", "candidate_text_chunks": rows})
    assert result[0].evidence_binding_status == "direct"
    assert len(captured) == 1
    assert len(captured[0]["text_anchors"]) <= 3
    assert "provenance" not in json.dumps(captured[0], ensure_ascii=False)
    assert captured[0]["text_anchors"][0]["audit"]["provider"] == "semantic_scholar"


def test_phase311_task_map_preserves_gap_removed_by_safe_rewrite() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import (
        SectionArgumentContract,
        _build_argument_task_coverage,
    )

    contract = SectionArgumentContract(
        "test", "S01", "Which comparison matters?", "Compare EP and Hermitian degeneracies.", "comparison",
        argument_tasks=[{
            "task_id": "S01:task:comparison",
            "description": "Compare Hermitian diabolical points with exceptional points.",
            "required": True,
        }],
    )
    claims = [{
        "claim_id": "S01-C02",
        "statement": "Unlike Hermitian diabolical points, exceptional points coalesce eigenvectors.",
        "supported_rewrite": "Exceptional points coalesce eigenvectors.",
        "missing_evidence_components": ["Hermitian diabolical point comparison"],
        "evidence_component_map": [{"component": "Eigenvector coalescence", "chunk_ids": ["c1"]}],
    }]
    coverage = _build_argument_task_coverage(
        contract,
        claims,
        {"claims": {"S01-C02": {"supporting_chunk_ids": ["c1"], "permission_status": "qualified_only"}}},
    )
    assert coverage[0]["status"] == "gap"
    assert any("Hermitian" in item for item in coverage[0]["missing_components"])


def test_phase311_bundle_assigns_each_claim_to_one_effective_category() -> None:
    from optomind_research.runtime.synthesis_bundle import build_synthesis_bundle

    claims = [
        {
            "claim_id": "C1",
            "statement": "The original broad claim is too strong.",
            "supported_rewrite": "The qualified claim is supported.",
            "evidence_binding_status": "partial",
            "permission_status": "qualified_only",
            "claim_state": "partially_grounded",
            "supporting_text_chunk_ids": ["c1"],
            "citation_paper_ids": ["p1"],
            "saturation_score": 2.5,
        },
        {
            "claim_id": "C2",
            "statement": "A directly measured result is reported.",
            "evidence_binding_status": "direct",
            "permission_status": "bound",
            "claim_state": "grounded",
            "supporting_text_chunk_ids": ["c2"],
            "citation_paper_ids": ["p2"],
            "saturation_score": 0.4,
        },
    ]
    bundle = build_synthesis_bundle(
        section={"section_id": "S01", "argument_role": "Mechanism"},
        claims=claims,
        allowed_paper_ids=["p1", "p2"],
        allowed_chunk_ids=["c1", "c2"],
        source_permissions={"p1": "contextual_or_qualified_support", "p2": "factual_support"},
        chunk_permissions={"c1": "contextual_or_qualified_support", "c2": "factual_support"},
        chunk_to_paper={"c1": "p1", "c2": "p2"},
        chunk_records=[
            {"chunk_id": "c1", "paper_id": "p1", "normalized_text": "qualified"},
            {"chunk_id": "c2", "paper_id": "p2", "normalized_text": "measured"},
        ],
    )
    assignments = {item["claim_id"]: item["category"] for item in bundle.claim_category_assignments}
    assert assignments["C1"] == "conditional_points"
    assert assignments["C2"] == "established_points"
    categories = [bundle.established_points, bundle.conditional_points, bundle.conflicts_or_boundaries]
    assert sum(statement in values for values in categories for statement in [
        "The qualified claim is supported.", "A directly measured result is reported."
    ]) == 2


def test_phase311_single_section_run_preserves_full_blueprint_context(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    fixture["blueprint"]["sections"][0].update({
        "review_mentor_advice": {"planning_principles": ["Keep the mechanism bounded."]},
        "synthesis_task": "Explain the mechanism before the comparison.",
        "target_word_range": [800, 1200],
        "visual_requirements": {"required": True},
    })
    result = Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=tmp_path / "single_section",
        section_ids_to_process=["S01"],
    ).run()
    run = json.loads((tmp_path / "single_section" / "PHASE3_RUN.json").read_text(encoding="utf-8"))
    assert run["blueprint_context"]["input_section_count"] == 2
    assert run["blueprint_context"]["processed_section_ids"] == ["S01"]
    contracts = json.loads((tmp_path / "single_section" / "SECTION_ARGUMENT_CONTRACTS.json").read_text(encoding="utf-8"))
    assert contracts["contracts"][0]["following_section_id"] == "S02"
    assert result["r4_entered"] is False


def test_phase312_legacy_context_values_reach_contract_and_m2a_payload(tmp_path: Path) -> None:
    fixture = _orchestrator_fixture(tmp_path, two_sections=True)
    fixture["blueprint"]["sections"][0].update({
        "mentor_guidance": ["Preserve the mechanism boundary."],
        "synthesis_task": "Connect the mechanism to the measurable response.",
        "transition_from_previous": "Start from the optical basis established earlier.",
        "transition_to_next": "Hand the unresolved comparison to the following section.",
        "target_word_range": [900, 1200],
        "visual_argument_slots": [{
            "slot_id": "V-S01-01",
            "purpose": "mechanism schematic",
            "required": True,
        }],
    })
    output = tmp_path / "phase312_context"
    Phase3ArgumentOrchestrator(
        blueprint=fixture["blueprint"],
        scope_map=fixture["scope_map"],
        coverage_atlas=fixture["coverage_atlas"],
        relation_graph=fixture["relation_graph"],
        shared_ledger_path=fixture["ledger"],
        shared_kb_paths=[fixture["kb"]],
        overlay_paths=fixture["overlays"],
        output_dir=output,
        section_ids_to_process=["S01"],
    ).run()
    contracts = json.loads((output / "SECTION_ARGUMENT_CONTRACTS.json").read_text(encoding="utf-8"))
    contract = contracts["contracts"][0]
    payloads = json.loads((output / "M2A_INPUT_PAYLOADS.json").read_text(encoding="utf-8"))
    payload_contract = payloads["sections"]["S01"]["section_contract"]
    expected = {
        "mentor_guidance": ["Preserve the mechanism boundary."],
        "synthesis_task": "Connect the mechanism to the measurable response.",
        "transition_from_previous": "Start from the optical basis established earlier.",
        "transition_to_next": "Hand the unresolved comparison to the following section.",
        "target_word_range": [900, 1200],
        "visual_argument_slots": [{
            "slot_id": "V-S01-01",
            "purpose": "mechanism schematic",
            "required": True,
        }],
    }
    for key, value in expected.items():
        assert contract[key] == value
        assert payload_contract[key] == value
    acceptance = json.loads((output / "PHASE3_ACCEPTANCE.json").read_text(encoding="utf-8"))
    assert acceptance["context_value_handoff_passed"] is True
    assert all(item["passed"] for item in acceptance["engineering_safety"].get("context_value_handoff", []) or [])


def test_phase312_queries_are_clustered_and_instruction_free() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import compile_coverage_queries

    queries = compile_coverage_queries(
        section={
            "section_id": "S01",
            "title": "Non-Hermitian exceptional-point topology",
            "argument_role": "Compare mathematical structure and measurable signatures",
        },
        missing_claims=[{
            "claim_id": "S01-C01",
            "statement": "The branch point requires explicit attribution and formal definition.",
            "importance": "load_bearing",
            "missing_evidence_components": [
                "explicit statement of Hermitian comparison",
                "branch-point Riemann-surface structure",
                "contains at least one resolvent singularity example",
            ],
        }],
        missing_roles=["foundation", "frontier"],
    )
    assert 3 <= len(queries) <= 5
    forbidden = {"explicit", "statement", "attribution", "establish", "formal", "definition", "where", "only", "contains", "least", "one"}
    assert all(not (set(query.lower().split()) & forbidden) for query in queries)


def test_phase311_paper_depth_aggregates_from_fulltext_chunk(tmp_path: Path) -> None:
    kb = tmp_path / "depth.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute("CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, title TEXT, text TEXT, source_kind TEXT, content_depth TEXT)")
        conn.execute("INSERT INTO text_chunks VALUES ('c1','p1','P1','A parsed full text passage with enough content.','fulltext','fulltext')")
        conn.commit()
    ledger = tmp_path / "depth.json"
    ledger.write_text(json.dumps({"sources": [{"paper_id": "p1", "title": "P1", "canonical_chunk_ids": ["c1"], "content_depth": "metadata", "scope_fit": "direct"}]}), encoding="utf-8")
    graph = build_canonical_asset_graph(material_package_path=None, source_ledger_path=ledger, work_dir=tmp_path / "graph", kb_paths=[kb])
    assert graph.papers["p1"].content_depth == "fulltext"


def test_coverage_queries_are_short_deduplicated_and_gap_scoped() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import compile_coverage_queries

    queries = compile_coverage_queries(
        section={
            "section_id": "S01",
            "title": "Broadband optical mechanism comparison",
            "argument_role": "Compare physical limits and measurement methods",
            "user_question": "A very long user question that must not be copied into retrieval queries.",
        },
        missing_roles=["frontier", "frontier"],
        missing_claims=[{
            "claim_id": "S01-C01",
            "statement": "Recent studies report a condition-dependent mechanism boundary.",
            "importance": "load_bearing",
        }],
    )
    assert len(queries) == len(set(queries))
    assert all(8 <= len(query.split()) <= 25 for query in queries)
    assert all("very long user question" not in query for query in queries)


def test_claim_permission_status_inherits_lowest_accepted_ceiling() -> None:
    from optomind_research.runtime.phase3_argument_orchestrator import (
        _claim_permission_status,
    )

    factual = {
        "use_permission": "factual_support",
        "scope_fit": "direct",
        "content_depth": "fulltext",
    }
    qualified = {
        "use_permission": "contextual_or_qualified_support",
        "scope_fit": "adjacent",
        "content_depth": "fulltext",
    }
    discovery = {
        "use_permission": "discovery_only",
        "scope_fit": "direct",
        "content_depth": "fulltext",
    }

    status, factual_ids, contextual_ids = _claim_permission_status(
        {"supporting_text_chunk_ids": ["f1"]}, {"f1": factual}
    )
    assert (status, factual_ids, contextual_ids) == ("bound", ["f1"], [])

    status, factual_ids, contextual_ids = _claim_permission_status(
        {"supporting_text_chunk_ids": ["f1", "q1"]},
        {"f1": factual, "q1": qualified},
    )
    assert status == "qualified_only"
    assert factual_ids == ["f1"]
    assert contextual_ids == ["q1"]

    status, factual_ids, contextual_ids = _claim_permission_status(
        {"supporting_text_chunk_ids": ["q1"]}, {"q1": qualified}
    )
    assert (status, factual_ids, contextual_ids) == (
        "qualified_only", [], ["q1"]
    )

    status, factual_ids, contextual_ids = _claim_permission_status(
        {"supporting_text_chunk_ids": ["d1"]}, {"d1": discovery}
    )
    assert (status, factual_ids, contextual_ids) == ("unbound", [], [])
