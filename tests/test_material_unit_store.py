from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from optomind_research.runtime.material_unit_store import (
    attach_query_annotations,
    build_material_unit_store,
    material_unit_from_text_chunk,
    material_unit_from_visual,
)


def test_text_and_visual_units_share_contract_and_keep_traceability() -> None:
    text = material_unit_from_text_chunk(
        {"paper_id": "p1", "chunk_id": "c1", "doi": "10.1/x", "title": "Paper", "text": "A measured result.", "section_path": "Results", "use_permission": "factual_support"}
    )
    visual = material_unit_from_visual(
        {"paper_id": "p1", "figure_id": "fig1", "path": "fig1.png", "media_type": "image/png", "caption": "Measured spectrum", "description": "A peak."}
    )
    assert set(("unit_id", "work_id", "unit_kind", "identity", "durable_content", "durable_content_card", "embedding_refs", "relations", "audit")).issubset(text)
    assert set(("unit_id", "work_id", "unit_kind", "identity", "durable_content", "durable_content_card", "embedding_refs", "relations", "audit")).issubset(visual)
    assert text["identity"]["locator"]["chunk_id"] == "c1"
    assert visual["identity"]["figure_id"] == "fig1"
    assert visual["durable_content_card"]["observable_content"] == "A peak."
    assert text["durable_content_card"]["scientific_inference_performed"] is False


def test_store_builds_every_admitted_chunk_and_excludes_discovery_only(tmp_path: Path) -> None:
    db = tmp_path / "kb.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE papers (paper_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, venue TEXT, content_depth TEXT)")
        conn.execute("CREATE TABLE text_chunks (chunk_id TEXT PRIMARY KEY, paper_id TEXT, doi TEXT, title TEXT, ordinal INTEGER, section_path TEXT, text TEXT, provenance_json TEXT, route_provenance_json TEXT, content_depth TEXT, use_permission TEXT, context_complete INTEGER, source_kind TEXT, allowed_claim_kinds_json TEXT)")
        conn.execute("INSERT INTO papers VALUES ('p1','10.1/x','Paper',2024,'J','fulltext')")
        conn.execute("INSERT INTO papers VALUES ('p2','10.1/y','Discovery',2024,'J','metadata')")
        conn.execute("INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("c1","p1","10.1/x","Paper",0,"Results","One result.","{}","{}","fulltext","factual_support",1,"s2_body","[]"))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"papers": [{"paper_id":"p1","admitted_to_downstream":True},{"paper_id":"p2","admitted_to_downstream":False}]}), encoding="utf-8")
    result = build_material_unit_store(kb_sqlite=db, material_flow_ledger_path=ledger)
    assert result["unit_count"] == 1
    assert result["text_unit_count"] == 1
    assert result["units"][0]["identity"]["chunk_id"] == "c1"


def _store_with_chunk(chunk_id: str = "c1") -> dict:
    return {
        "schema_version": "optomind.material_unit_store.v1",
        "unit_count": 1,
        "text_unit_count": 1,
        "visual_unit_count": 0,
        "query_annotation_policy": "separate_by_query_id_and_question_hash",
        "units": [
            {
                "unit_id": f"unit:text:{chunk_id}",
                "work_id": "work:abc",
                "unit_kind": "text_chunk",
                "identity": {"chunk_id": chunk_id, "title": "Paper"},
                "durable_content": {
                    "content_depth": "fulltext",
                    "content_hash": "sha256:h",
                    "normalized_text": "text",
                },
                "durable_content_card": {"observable_content": "card"},
                "embedding_refs": [],
                "relations": [],
                "query_annotations": [],
                "audit": {},
            }
        ],
    }


def _card(
    *,
    reference: dict | None = None,
    chunk_id: str = "c1",
) -> dict:
    query_annotation = {"model_version": "qwen3.7-flash"}
    if reference is not None:
        query_annotation["supplementary_task_reference"] = reference
    return {
        "canonical_work_id": "work:abc",
        "question_relevance": "central",
        "paper_functions": ["method_or_model"],
        "query_annotation": query_annotation,
        "propositions": [
            {
                "statement": "A measured result.",
                "evidence_chunk_ids": [chunk_id],
            }
        ],
    }


def test_attach_copies_supplementary_task_reference_without_full_context() -> None:
    reference = {
        "task_id": "task-c1",
        "gap_type": "claim_evidence_gap",
        "coverage_ids": ["F1"],
        "context_sha256": "sha256:ref1",
    }
    store = _store_with_chunk()
    result = attach_query_annotations(
        store,
        [_card(reference=reference)],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert annotation["supplementary_task_references"] == [reference]
    assert "task_context_fields" not in annotation
    assert "coverage_catalog" not in annotation


def test_attach_ignores_malformed_reference_and_keeps_ordinary_shape() -> None:
    store = _store_with_chunk()
    result = attach_query_annotations(
        store,
        [_card(reference="not-a-mapping")],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert "supplementary_task_references" not in annotation
    assert set(annotation) == {
        "query_id",
        "question_hash",
        "annotation_schema_version",
        "model_version",
        "canonical_work_id",
        "question_relevance",
        "paper_functions",
        "seed_axis_assignments",
        "emergent_axis_candidates",
        "propositions",
        "background_contexts",
    }


def test_attach_appends_distinct_references_for_same_query_identity() -> None:
    store = _store_with_chunk()
    first = {
        "task_id": "task-a",
        "gap_type": "claim_evidence_gap",
        "coverage_ids": ["F1"],
        "context_sha256": "sha256:ref-a",
    }
    second = {
        "task_id": "task-b",
        "gap_type": "review_structure_gap",
        "coverage_ids": ["S01"],
        "context_sha256": "sha256:ref-b",
    }
    result = attach_query_annotations(
        store,
        [_card(reference=first), _card(reference=second)],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert annotation["supplementary_task_references"] == [first, second]


def test_attach_whitelists_only_compact_reference_fields() -> None:
    store = _store_with_chunk()
    reference = {
        "task_id": "task-c1",
        "gap_type": "claim_evidence_gap",
        "coverage_ids": ["F1", "F2", "F1"],
        "context_sha256": "sha256:ref1",
        "task_context_fields": {"missing_fact_units": ["near-field error"]},
        "coverage_catalog": [{"coverage_id": "F1", "description": "x"}],
        "reviewer_feedback": {"mentor": "secret note"},
        "arbitrary": {"nested": ["value"]},
    }
    result = attach_query_annotations(
        store,
        [_card(reference=reference)],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert annotation["supplementary_task_references"] == [
        {
            "task_id": "task-c1",
            "gap_type": "claim_evidence_gap",
            "coverage_ids": ["F1", "F2"],
            "context_sha256": "sha256:ref1",
        }
    ]


def test_attach_ignores_reference_with_missing_identity() -> None:
    store = _store_with_chunk()
    result = attach_query_annotations(
        store,
        [
            _card(reference={"task_id": "", "gap_type": "claim_evidence_gap"}),
            _card(reference={"task_id": "task-x", "gap_type": "   "}),
        ],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert "supplementary_task_references" not in annotation


def test_attach_normalizes_scalar_coverage_and_hash_fields() -> None:
    store = _store_with_chunk()
    reference = {
        "task_id": "task-c1",
        "gap_type": "claim_evidence_gap",
        "coverage_ids": "F1",
        "context_sha256": 12345,
    }
    result = attach_query_annotations(
        store,
        [_card(reference=reference)],
        query_id="q1",
        question="Question?",
    )
    annotation = result["units"][0]["query_annotations"][0]
    assert annotation["supplementary_task_references"] == [
        {
            "task_id": "task-c1",
            "gap_type": "claim_evidence_gap",
            "coverage_ids": ["F1"],
            "context_sha256": "12345",
        }
    ]
