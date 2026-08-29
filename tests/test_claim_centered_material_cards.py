from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.claim_centered_material_cards import (
    build_claim_centered_material_packets,
)


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    kb = tmp_path / "kb.sqlite"
    with sqlite3.connect(kb) as conn:
        conn.execute(
            "CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, "
            "year INTEGER, venue TEXT, raw_json TEXT, discovery_route TEXT, "
            "materialization_route TEXT, content_depth TEXT, use_permission TEXT, "
            "scope_fit TEXT)"
        )
        conn.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT PRIMARY KEY, paper_id TEXT, "
            "doi TEXT, title TEXT, ordinal INTEGER, section_path TEXT, text TEXT, "
            "raw_json TEXT, evidence_level TEXT, source_kind TEXT, provenance_json TEXT, "
            "route_provenance_json TEXT, content_depth TEXT, use_permission TEXT, "
            "context_complete INTEGER, allowed_claim_kinds_json TEXT, scope_fit TEXT)"
        )
        papers = [
            (
                "main", "10.1/example", "Main inverse-design paper", 2024, "Journal",
                "{}", "s2", "s2_body", "abstract", "factual_support", "direct",
            ),
            (
                "supp", "10.1/example", "Supplemental Information: Main inverse-design paper",
                2024, "", "{}", "s2", "oa_fulltext", "fulltext", "factual_support", "direct",
            ),
            (
                "abstract", "10.1/abstract", "Abstract-only paper", 2023, "Journal",
                "{}", "s2", "abstract_claim", "abstract_claim",
                "contextual_or_qualified_support", "direct",
            ),
            (
                "metadata", "10.1/metadata", "Metadata-only paper", 2022, "Journal",
                "{}", "s2", "not_materialized", "metadata", "discovery_only", "direct",
            ),
        ]
        conn.executemany("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)", papers)
        chunks = [
            (
                "c-main-method", "main", "10.1/example", "Main inverse-design paper", 1,
                "Methods", "The differentiable solver uses an adjoint optimization method.",
                "{}", "body", "s2_body_snippet", "{}", "{}", "structured_snippet",
                "factual_support", 1, "[]", "direct",
            ),
            (
                "c-main-result", "main", "10.1/example", "Main inverse-design paper", 2,
                "Results", "Experimental validation shows a measured efficiency of 82%.",
                "{}", "body", "s2_body_snippet", "{}", "{}", "structured_snippet",
                "factual_support", 1, "[]", "direct",
            ),
            (
                "c-supp-limit", "supp", "10.1/example", "Supplemental Information: Main inverse-design paper", 3,
                "Discussion", "A fabrication tolerance limitation remains for translation.",
                "{}", "body", "fulltext", "{}", "{}", "fulltext",
                "factual_support", 1, "[]", "direct",
            ),
            (
                "c-abstract", "abstract", "10.1/abstract", "Abstract-only paper", 0,
                "abstract", "The authors report that their model improves prediction accuracy.",
                "{}", "abstract", "abstract", json.dumps({"abstract_provider": "openalex"}),
                "{}", "abstract_claim", "contextual_or_qualified_support", 0,
                "[\"paper_reported_claim\"]", "direct",
            ),
        ]
        conn.executemany("INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", chunks)

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "summary": {"discovery_only_paper_count": 1},
                "papers": [
                    {"paper_id": "main", "title": "Main inverse-design paper", "doi": "10.1/example", "material_status": "s2_body", "admitted_to_downstream": True},
                    {"paper_id": "supp", "title": "Supplemental Information: Main inverse-design paper", "doi": "10.1/example", "material_status": "oa_fulltext", "admitted_to_downstream": True},
                    {"paper_id": "abstract", "title": "Abstract-only paper", "doi": "10.1/abstract", "material_status": "abstract_claim", "admitted_to_downstream": True},
                    {"paper_id": "metadata", "title": "Metadata-only paper", "doi": "10.1/metadata", "material_status": "discovery_only", "admitted_to_downstream": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    query = tmp_path / "query.json"
    query.write_text(
        json.dumps(
            {
                "input": {"user_query": "How do differentiable solvers compare experimentally?"},
                "output": {
                    "scope_definition": {
                        "scope_items": [
                            "Differentiable electromagnetic solver methods",
                            "Experimental validation and fabrication tolerance",
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return kb, ledger, query


def test_packets_merge_duplicate_doi_and_ignore_discovery_only(tmp_path: Path) -> None:
    kb, ledger, query = _write_fixture(tmp_path)
    result = build_claim_centered_material_packets(
        kb_sqlite=kb,
        material_flow_ledger_path=ledger,
        query_plan_path=query,
    )

    assert result["source_material_record_count"] == 3
    assert result["canonical_work_count"] == 2
    assert result["duplicate_work_group_count"] == 1
    assert result["excluded_discovery_only_count"] == 1
    assert all(
        axis["origin"] == "user_question"
        for axis in result["seed_axis_catalog"]
    )
    assert result["packet_policy"]["scientific_axes_are_open_world"] is True
    merged = next(
        row for row in result["packets"]
        if set(row["member_paper_ids"]) == {"main", "supp"}
    )
    assert merged["canonical_identity"]["paper_id"] == "main"
    assert merged["canonical_identity"]["title"] == "Main inverse-design paper"
    assert merged["material_classes"] == ["oa_fulltext", "s2_body"]
    assert set(merged["all_available_chunk_ids"]) == {
        "c-main-method", "c-main-result", "c-supp-limit"
    }
    assert "material-emergent axes" in merged["downstream_rule"]


def test_packet_selection_is_bounded_balanced_and_traceable(tmp_path: Path) -> None:
    kb, ledger, query = _write_fixture(tmp_path)
    result = build_claim_centered_material_packets(
        kb_sqlite=kb,
        material_flow_ledger_path=ledger,
        query_plan_path=query,
        max_chunks_per_work=2,
    )

    merged = next(
        row for row in result["packets"]
        if set(row["member_paper_ids"]) == {"main", "supp"}
    )
    assert len(merged["selected_evidence"]) == 2
    assert merged["selection_audit"]["packet_is_bounded"] is True
    assert set(merged["selection_audit"]["selected_chunk_ids"]) <= set(
        merged["all_available_chunk_ids"]
    )
    assert "method_and_model" in merged["selection_audit"]["covered_packet_dimensions"]
    assert (
        "validation_and_translation"
        in merged["selection_audit"]["covered_packet_dimensions"]
    )


def test_abstract_claim_is_kept_exactly_with_qualified_ceiling(tmp_path: Path) -> None:
    kb, ledger, query = _write_fixture(tmp_path)
    result = build_claim_centered_material_packets(
        kb_sqlite=kb,
        material_flow_ledger_path=ledger,
        query_plan_path=query,
        max_chunks_per_work=1,
    )

    packet = next(
        row for row in result["packets"]
        if row["canonical_identity"]["paper_id"] == "abstract"
    )
    evidence = packet["selected_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["chunk_id"] == "c-abstract"
    assert evidence[0]["content_depth"] == "abstract_claim"
    assert evidence[0]["evidence_ceiling"] == "contextual_or_qualified_support"
    assert evidence[0]["text"] == (
        "The authors report that their model improves prediction accuracy."
    )


def test_supplementary_context_is_copied_into_packets(tmp_path: Path) -> None:
    kb, ledger, query = _write_fixture(tmp_path)
    query.write_text(
        json.dumps(
            {
                "input": {
                    "user_query": "How do differentiable solvers compare experimentally?"
                },
                "output": {
                    "scope_definition": {
                        "scope_items": [
                            "Differentiable electromagnetic solver methods",
                        ]
                    }
                },
                "supplementary_retrieval": {
                    "discovery_mode": "generated_only",
                    "relevance_context": {
                        "task_id": "task-c1",
                        "gap_type": "claim_evidence_gap",
                        "search_background_cue": (
                            "optical electromagnetic near-field fidelity"
                        ),
                        "coverage_catalog": [
                            {
                                "coverage_id": "F1",
                                "description": (
                                    "near-field truncation error"
                                ),
                                "target_type": "missing_fact",
                                "priority": 90,
                            },
                            {
                                "coverage_id": "F2",
                                "description": (
                                    "alignment positioning tolerance"
                                ),
                                "target_type": "missing_fact",
                                "priority": 90,
                            },
                        ],
                        "exclusion_boundaries": [
                            "purely biological imaging",
                            "unrelated fluid-only studies",
                        ],
                        "missing_fact_units": [
                            "near-field truncation error",
                            "alignment positioning tolerance",
                        ],
                        "reviewer_feedback": {
                            "mentor": (
                                "require explicit near-field error evidence"
                            )
                        },
                        "dynamic_axes": [
                            {
                                "axis_id": "Q01",
                                "description": (
                                    "PINN vs differentiable solver"
                                ),
                            }
                        ],
                        "user_question": (
                            "How do differentiable solvers compare "
                            "experimentally?"
                        ),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    result = build_claim_centered_material_packets(
        kb_sqlite=kb,
        material_flow_ledger_path=ledger,
        query_plan_path=query,
    )
    assert result["supplementary_context"]["gap_type"] == (
        "claim_evidence_gap"
    )
    for packet in result["packets"]:
        context = packet["supplementary_context"]
        assert context["task_id"] == "task-c1"
        assert context["gap_type"] == "claim_evidence_gap"
        assert context["search_background_cue"] == (
            "optical electromagnetic near-field fidelity"
        )
        assert [
            entry["coverage_id"] for entry in context["coverage_catalog"]
        ] == ["F1", "F2"]
        assert context["exclusion_boundaries"] == [
            "purely biological imaging",
            "unrelated fluid-only studies",
        ]
        assert "exclusion_boundaries" not in context["task_context_fields"]
        assert context["task_context_fields"]["missing_fact_units"] == [
            "near-field truncation error",
            "alignment positioning tolerance",
        ]
        assert context["task_context_fields"]["reviewer_feedback"] == {
            "mentor": "require explicit near-field error evidence"
        }
        assert context["task_context_fields"]["dynamic_axes"] == [
            {
                "axis_id": "Q01",
                "description": "PINN vs differentiable solver",
            }
        ]


def test_ordinary_packets_carry_empty_supplementary_context(
    tmp_path: Path,
) -> None:
    kb, ledger, query = _write_fixture(tmp_path)
    result = build_claim_centered_material_packets(
        kb_sqlite=kb,
        material_flow_ledger_path=ledger,
        query_plan_path=query,
    )
    assert result["supplementary_context"] == {}
    assert all(
        packet["supplementary_context"] == {}
        for packet in result["packets"]
    )
