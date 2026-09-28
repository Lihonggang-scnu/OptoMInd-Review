"""Durable, question-independent material units.

The literature database is an acquisition record.  This module turns every
admitted text chunk (and optional visual record) into a stable ``MaterialUnit``
that can be reused by later questions without downloading the source again.
It deliberately performs no scientific inference.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_store import atomic_write_json

SCHEMA_VERSION = "optomind.material_unit.v1"
STORE_SCHEMA_VERSION = "optomind.material_unit_store.v1"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def content_hash(value: Any, *, media_type: str = "text/plain") -> str:
    """Return a content-address used by both durable storage and embeddings."""
    if isinstance(value, bytes):
        raw = value
    else:
        raw = _text(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(media_type.encode("ascii") + b"\0" + raw).hexdigest()


def question_identity(query_id: str | None, question: str | None) -> dict[str, str]:
    question_text = _text(question)
    digest = hashlib.sha256(question_text.encode("utf-8")).hexdigest()
    return {
        "query_id": _text(query_id) or "query:" + digest[:20],
        "question_hash": "sha256:" + digest,
        "annotation_schema_version": "optomind.query_annotation.v1",
    }


def _normalize_supplementary_task_reference(value: Any) -> dict[str, Any] | None:
    """Whitelist the compact supplementary task reference contract.

    Only ``task_id``, ``gap_type``, ``coverage_ids``, and ``context_sha256``
    are stored.  Extra keys (full context, reviewer content, arbitrary nested
    values) are ignored.  A reference without a non-empty task_id and gap_type
    is ignored fail-safe.
    """

    if not isinstance(value, Mapping):
        return None
    task_id = str(value.get("task_id") or "").strip()
    gap_type = str(value.get("gap_type") or "").strip()
    if not task_id or not gap_type:
        return None
    raw_coverage = value.get("coverage_ids")
    if isinstance(raw_coverage, (list, tuple, set)):
        coverage_ids: list[str] = []
        for item in raw_coverage:
            text = str(item).strip()
            if text and text not in coverage_ids:
                coverage_ids.append(text)
    else:
        text = str(raw_coverage).strip() if raw_coverage is not None else ""
        coverage_ids = [text] if text else []
    return {
        "task_id": task_id,
        "gap_type": gap_type,
        "coverage_ids": coverage_ids,
        "context_sha256": str(value.get("context_sha256") or "").strip(),
    }


def _work_id(row: Mapping[str, Any]) -> str:
    doi = _text(row.get("doi")).casefold().replace("https://doi.org/", "")
    paper_id = _text(row.get("paper_id"))
    key = "doi:" + doi if doi else "paper:" + paper_id
    return "work:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def _source_locator(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = row.get("provenance") or row.get("route_provenance") or {}
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except (TypeError, ValueError, json.JSONDecodeError):
            provenance = {}
    locator: dict[str, Any] = {}
    for key in (
        "doi", "paper_id", "chunk_id", "figure_id", "visual_id", "path",
        "uri", "url", "source_url", "s2_paper_id", "openalex_id", "page",
    ):
        value = row.get(key)
        if value not in (None, ""):
            locator[key] = value
        elif isinstance(provenance, Mapping) and provenance.get(key) not in (None, ""):
            locator[key] = provenance[key]
    return locator


def material_unit_from_text_chunk(chunk: Mapping[str, Any], paper: Mapping[str, Any] | None = None) -> dict[str, Any]:
    paper = paper or {}
    text = _text(chunk.get("text"))
    if not text:
        raise ValueError("A text MaterialUnit requires non-empty text")
    merged = {**dict(paper), **dict(chunk)}
    chash = content_hash(text)
    source_id = _text(chunk.get("chunk_id")) or chash
    unit_id = "unit:text:" + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:20]
    ceiling = _text(chunk.get("use_permission")) or "contextual_or_qualified_support"
    return {
        "schema_version": SCHEMA_VERSION,
        "unit_id": unit_id,
        "work_id": _work_id(merged),
        "unit_kind": "text_chunk",
        "identity": {
            "paper_id": _text(merged.get("paper_id")),
            "chunk_id": source_id,
            "doi": _text(merged.get("doi")),
            "title": _text(merged.get("title")),
            "locator": _source_locator(merged),
        },
        "durable_content": {
            "media_type": "text/plain",
            "raw_text": str(chunk.get("text") or ""),
            "normalized_text": text,
            "content_hash": chash,
            "content_depth": _text(chunk.get("content_depth")) or _text(paper.get("content_depth")) or "fulltext",
            "section_path": _text(chunk.get("section_path")),
            "ordinal": chunk.get("ordinal"),
            "char_start": chunk.get("char_start"),
            "char_end": chunk.get("char_end"),
        },
        "durable_content_card": {
            "content_quality": {
                "context_complete": bool(chunk.get("context_complete", True)),
                "char_count": len(text),
                "source_kind": _text(chunk.get("source_kind")),
                "evidence_ceiling": ceiling,
                "allowed_claim_kinds": chunk.get("allowed_claim_kinds") or [],
            },
            "scientific_inference_performed": False,
        },
        "embedding_refs": [],
        "query_annotations": [],
        "relations": [],
        "audit": {
            "traceable": bool(_source_locator(merged)),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_provenance": merged.get("provenance") or merged.get("route_provenance") or {},
        },
    }


def material_unit_from_visual(record: Mapping[str, Any], paper: Mapping[str, Any] | None = None) -> dict[str, Any]:
    paper = paper or {}
    media_type = _text(record.get("media_type")) or "image/*"
    raw = record.get("bytes") if isinstance(record.get("bytes"), bytes) else _text(record.get("path") or record.get("uri") or record.get("content"))
    if not raw:
        raise ValueError("A visual MaterialUnit requires bytes, path, uri, or content")
    chash = content_hash(raw, media_type=media_type)
    source_id = _text(record.get("visual_id")) or _text(record.get("figure_id")) or chash
    return {
        "schema_version": SCHEMA_VERSION,
        "unit_id": "unit:visual:" + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:20],
        "work_id": _work_id({**dict(paper), **dict(record)}),
        "unit_kind": _text(record.get("unit_kind")) or "visual",
        "identity": {
            "paper_id": _text(record.get("paper_id") or paper.get("paper_id")),
            "doi": _text(record.get("doi") or paper.get("doi")),
            "title": _text(record.get("title") or paper.get("title")),
            "figure_id": _text(record.get("figure_id") or record.get("visual_id")),
            "page": record.get("page"),
            "subfigure": _text(record.get("subfigure")),
            "locator": _source_locator({**dict(paper), **dict(record)}),
        },
        "durable_content": {
            "media_type": media_type,
            "content_ref": raw if isinstance(raw, str) else None,
            "content_hash": chash,
            "caption": _text(record.get("caption")),
            "normalized_description": _text(record.get("description") or record.get("ocr_text")),
        },
        "durable_content_card": {
            "observable_content": _text(record.get("observable_content") or record.get("description") or record.get("ocr_text")),
            "readable_numbers": record.get("readable_numbers") or [],
            "trends": record.get("trends") or [],
            "caption": _text(record.get("caption")),
            "cannot_confirm": record.get("cannot_confirm") or [],
            "scientific_inference_performed": False,
        },
        "embedding_refs": [],
        "query_annotations": [],
        "relations": [],
        "audit": {
            "traceable": bool(_source_locator({**dict(paper), **dict(record)})),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "visual_source": _text(record.get("path") or record.get("uri")),
        },
    }


def build_material_unit_store(*, kb_sqlite: Path, material_flow_ledger_path: Path, output_path: Path | None = None, visual_records: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    ledger = json.loads(Path(material_flow_ledger_path).read_text(encoding="utf-8"))
    admitted = {str(row.get("paper_id")): dict(row) for row in ledger.get("papers") or [] if isinstance(row, Mapping) and row.get("admitted_to_downstream")}
    uri = f"file:{Path(kb_sqlite).resolve().as_posix()}?mode=ro"
    units: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        papers = {str(r["paper_id"]): dict(r) for r in conn.execute("SELECT * FROM papers")}
        for row in conn.execute("SELECT * FROM text_chunks ORDER BY paper_id, ordinal, chunk_id"):
            chunk = dict(row)
            if chunk.get("paper_id") not in admitted:
                continue
            paper = {**papers.get(str(chunk.get("paper_id")), {}), **admitted[str(chunk.get("paper_id"))]}
            for field in ("provenance_json", "route_provenance_json", "allowed_claim_kinds_json"):
                if field in chunk and isinstance(chunk[field], str):
                    try: chunk[field.replace("_json", "")] = json.loads(chunk[field])
                    except (TypeError, ValueError, json.JSONDecodeError): pass
            unit = material_unit_from_text_chunk(chunk, paper)
            units[unit["unit_id"]] = unit
    for record in visual_records:
        unit = material_unit_from_visual(record)
        units[unit["unit_id"]] = unit
    result = {
        "schema_version": STORE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "unit_count": len(units),
        "text_unit_count": sum(u["unit_kind"] == "text_chunk" for u in units.values()),
        "visual_unit_count": sum(u["unit_kind"] != "text_chunk" for u in units.values()),
        "units": sorted(units.values(), key=lambda u: u["unit_id"]),
        "query_annotation_policy": "separate_by_query_id_and_question_hash",
    }
    if output_path is not None: atomic_write_json(output_path, result)
    return result


def attach_query_annotations(
    store: Mapping[str, Any],
    proposition_cards: Iterable[Mapping[str, Any]],
    *,
    query_id: str,
    question: str,
) -> dict[str, Any]:
    """Attach query-specific claims only to the chunks that support them."""

    result = json.loads(json.dumps(dict(store), ensure_ascii=False))
    units = [unit for unit in result.get("units") or [] if isinstance(unit, dict)]
    by_chunk_id = {
        str((unit.get("identity") or {}).get("chunk_id") or ""): unit
        for unit in units
        if str((unit.get("identity") or {}).get("chunk_id") or "")
    }
    identity = question_identity(query_id, question)
    annotated_unit_ids: set[str] = set()
    missing_chunk_ids: set[str] = set()

    def attach_rows(
        card: Mapping[str, Any],
        field: str,
        basis_field: str,
    ) -> None:
        for row in card.get(field) or []:
            if not isinstance(row, Mapping):
                continue
            for chunk_id in row.get(basis_field) or []:
                unit = by_chunk_id.get(str(chunk_id))
                if unit is None:
                    missing_chunk_ids.add(str(chunk_id))
                    continue
                annotations = unit.setdefault("query_annotations", [])
                annotation = next(
                    (
                        item for item in annotations
                        if isinstance(item, dict)
                        and item.get("query_id") == identity["query_id"]
                        and item.get("question_hash") == identity["question_hash"]
                    ),
                    None,
                )
                if annotation is None:
                    annotation = {
                        **identity,
                        "model_version": str(
                            (card.get("query_annotation") or {}).get("model_version") or ""
                        ),
                        "canonical_work_id": str(card.get("canonical_work_id") or ""),
                        "question_relevance": str(card.get("question_relevance") or ""),
                        "paper_functions": list(card.get("paper_functions") or []),
                        "seed_axis_assignments": [],
                        "emergent_axis_candidates": [],
                        "propositions": [],
                        "background_contexts": [],
                    }
                    annotations.append(annotation)
                card_query_annotation = card.get("query_annotation")
                if isinstance(card_query_annotation, Mapping):
                    task_reference = card_query_annotation.get(
                        "supplementary_task_reference"
                    )
                    normalized_reference = (
                        _normalize_supplementary_task_reference(task_reference)
                    )
                    if normalized_reference is not None:
                        references = annotation.setdefault(
                            "supplementary_task_references", []
                        )
                        if normalized_reference not in references:
                            references.append(normalized_reference)
                target = annotation.setdefault(field, [])
                value = dict(row)
                if value not in target:
                    target.append(value)
                annotated_unit_ids.add(str(unit.get("unit_id") or ""))

    card_count = 0
    for card in proposition_cards:
        if not isinstance(card, Mapping):
            continue
        card_count += 1
        attach_rows(card, "seed_axis_assignments", "basis_chunk_ids")
        attach_rows(card, "emergent_axis_candidates", "basis_chunk_ids")
        attach_rows(card, "propositions", "evidence_chunk_ids")
        attach_rows(card, "background_contexts", "basis_chunk_ids")

    result["query_annotation_summary"] = {
        **identity,
        "card_count": card_count,
        "annotated_unit_count": len(annotated_unit_ids),
        "unreferenced_unit_count": max(0, len(units) - len(annotated_unit_ids)),
        "missing_chunk_ids": sorted(missing_chunk_ids),
    }
    return result


__all__ = ["SCHEMA_VERSION", "STORE_SCHEMA_VERSION", "content_hash", "question_identity", "material_unit_from_text_chunk", "material_unit_from_visual", "build_material_unit_store", "attach_query_annotations"]
