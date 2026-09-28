"""Build bounded, claim-centered packets from an admitted material library.

This module performs no scientific inference and makes no network calls.  It
deduplicates source records into canonical works, keeps every source chunk
traceable, and selects a balanced packet for later proposition extraction.
Discovery-only records are deliberately excluded from this stage.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_store import atomic_write_json
from .argument_quality_policy import evidence_ceiling
from .material_unit_store import content_hash, question_identity


SCHEMA_VERSION = "optomind.claim_centered_material_packets.v1"

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SUPPLEMENT_PREFIX_RE = re.compile(
    r"^\s*(?:supplemental|supplementary)\s+(?:information|material|data)\s*[:.-]?\s*",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    {
        "about", "after", "also", "among", "and", "are", "based", "between",
        "both", "but", "can", "compare", "design", "for", "from", "have",
        "how", "into", "inverse", "its", "method", "methods", "more", "paper",
        "review", "should", "study", "than", "that", "the", "their", "these",
        "this", "through", "using", "what", "when", "where", "which", "with",
    }
)

_PACKET_DIMENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "problem_and_context",
        ("abstract", "introduction", "background", "motivation", "problem", "gap"),
    ),
    (
        "method_and_model",
        (
            "method", "solver", "model", "network", "algorithm", "optimization",
            "simulation", "training", "architecture", "adjoint", "differentiable",
        ),
    ),
    (
        "result_and_finding",
        (
            "result", "results", "demonstrate", "achieve", "performance", "accuracy",
            "error", "efficiency", "improve", "outperform", "finding", "observed",
        ),
    ),
    (
        "comparison_and_tradeoff",
        ("compare", "comparison", "versus", "benchmark", "tradeoff", "relative to"),
    ),
    (
        "validation_and_translation",
        (
            "experiment", "experimental", "fabrication", "measurement", "measured",
            "validation", "tolerance", "uncertainty", "robust", "manufacturing",
        ),
    ),
    (
        "limitation_and_boundary",
        (
            "limitation", "limitations", "challenge", "future", "outlook", "boundary",
            "fails", "failure", "uncertain", "however", "restricted",
        ),
    ),
)


def _text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(str(value or ""))
        if token.casefold() not in _STOPWORDS
    }


def _normalized_doi(value: Any) -> str:
    doi = _text(value).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
    return doi.strip().strip("/.,;")


def _normalized_title(value: Any) -> str:
    return _NON_ALNUM_RE.sub(" ", _text(value).casefold()).strip()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _material_strength(status: str) -> int:
    return {
        "s2_body": 4,
        "oa_fulltext": 4,
        "abstract_claim": 2,
        "discovery_only": 0,
    }.get(str(status or ""), 0)


def _preferred_identity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> tuple[int, int, int, str]:
        title = _text(row.get("title"))
        is_supplement = bool(_SUPPLEMENT_PREFIX_RE.match(title))
        has_doi = bool(_normalized_doi(row.get("doi")))
        return (
            0 if is_supplement else 1,
            1 if has_doi else 0,
            _material_strength(str(row.get("material_status") or "")),
            title.casefold(),
        )

    return max(rows, key=key)


def _work_group_key(row: Mapping[str, Any]) -> str:
    doi = _normalized_doi(row.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = _normalized_title(row.get("title"))
    stripped = _normalized_title(
        _SUPPLEMENT_PREFIX_RE.sub("", _text(row.get("title")))
    )
    if stripped and stripped != title:
        return f"title:{stripped}"
    paper_id = _text(row.get("paper_id"))
    return f"paper:{paper_id}"


def _stable_work_id(group_key: str) -> str:
    return "work:" + hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:20]


def _query_context(query_plan: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    input_value = query_plan.get("input") if isinstance(query_plan.get("input"), dict) else {}
    output = query_plan.get("output") if isinstance(query_plan.get("output"), dict) else {}
    scope = output.get("scope_definition") if isinstance(output.get("scope_definition"), dict) else {}
    question = _text(
        input_value.get("user_query")
        or query_plan.get("user_query")
        or output.get("problem_understanding")
    )
    raw_items = scope.get("scope_items") or []
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    facets = [
        {
            "axis_id": f"Q{index:02d}",
            "description": _text(item),
            "origin": "user_question",
            "status": "seed",
        }
        for index, item in enumerate(raw_items, 1)
        if _text(item)
    ]
    return question, facets


def _bounded_context_value(value: Any) -> Any:
    """Deterministic bounded copy for supplementary packet context."""

    if isinstance(value, list):
        return [_bounded_context_value(item) for item in value[:12]]
    if isinstance(value, dict):
        return {
            key: _bounded_context_value(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return value[:2000]
    return value


def _supplementary_packet_context(query_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Copy bounded supplementary task context into extraction packets.

    The query plan's ``supplementary_retrieval.relevance_context`` already
    contains the bounded task-specific projected cells used by query
    generation.  This function copies the task identity, gap type, search
    background, coverage targets, and the remaining task context fields so
    proposition extraction can judge relevance against the specific gap that
    caused retrieval, not only the overall user question.  Ordinary plans
    produce an empty context.
    """

    marker = query_plan.get("supplementary_retrieval")
    if not isinstance(marker, Mapping):
        return {}
    relevance = marker.get("relevance_context")
    if not isinstance(relevance, Mapping):
        return {}
    derived_keys = {
        "task_id",
        "gap_type",
        "search_background_cue",
        "coverage_catalog",
        "expansion_policy",
        "exclusion_boundaries",
        "context_fields",
    }
    return {
        "task_id": _text(relevance.get("task_id")),
        "gap_type": _text(relevance.get("gap_type")),
        "search_background_cue": _text(
            relevance.get("search_background_cue")
        ),
        "exclusion_boundaries": _bounded_context_value(
            relevance.get("exclusion_boundaries") or []
        ),
        "coverage_catalog": _bounded_context_value(
            relevance.get("coverage_catalog") or []
        ),
        "task_context_fields": {
            key: _bounded_context_value(value)
            for key, value in relevance.items()
            if key not in derived_keys
        },
    }


def _dimension_scores(text: str) -> dict[str, int]:
    lower = text.casefold()
    return {
        dimension: sum(lower.count(cue) for cue in cues)
        for dimension, cues in _PACKET_DIMENSIONS
    }


def _trim_evidence(text: str, *, limit: int) -> str:
    value = _text(text)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 3)].rstrip() + "..."


def _select_chunks(
    rows: list[dict[str, Any]],
    *,
    question: str,
    facets: list[dict[str, str]],
    max_chunks: int,
    max_chars_per_chunk: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query_tokens = _tokens(
        " ".join([question, *[item["description"] for item in facets]])
    )
    scored: list[dict[str, Any]] = []
    for row in rows:
        text = _text(row.get("text"))
        if not text:
            continue
        section_path = _text(row.get("section_path"))
        dimensions = _dimension_scores(f"{section_path} {text}")
        overlap = len(query_tokens & _tokens(f"{row.get('title', '')} {text}"))
        numeric = 1 if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|nm|um|mm|db|hz|khz|mhz|ghz|thz)\b", text, re.I) else 0
        ceiling, ceiling_reason = evidence_ceiling(row)
        score = (
            overlap * 5
            + min(8, sum(dimensions.values()))
            + numeric * 2
            + (3 if str(row.get("content_depth")) == "fulltext" else 0)
            + (2 if str(row.get("content_depth")) == "structured_snippet" else 0)
        )
        scored.append(
            {
                **row,
                "_score": score,
                "_dimensions": dimensions,
                "_evidence_ceiling": ceiling,
                "_ceiling_reason": ceiling_reason,
            }
        )
    scored.sort(
        key=lambda row: (
            -int(row["_score"]),
            int(row.get("ordinal") or 0),
            str(row.get("chunk_id") or ""),
        )
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def choose(candidates: Iterable[dict[str, Any]]) -> bool:
        for candidate in candidates:
            chunk_id = str(candidate.get("chunk_id") or "")
            if chunk_id and chunk_id not in selected_ids:
                selected.append(candidate)
                selected_ids.add(chunk_id)
                return True
        return False

    # Preserve an abstract claim exactly when this work has one.
    choose(
        row
        for row in scored
        if str(row.get("content_depth") or "") == "abstract_claim"
    )
    # Give every generic argumentative dimension a chance before filling by
    # aggregate relevance.  This prevents method-heavy papers from hiding all
    # limitations or validation passages in a bounded packet.
    for dimension, _ in _PACKET_DIMENSIONS:
        if len(selected) >= max_chunks:
            break
        choose(
            row
            for row in sorted(
                scored,
                key=lambda item: (
                    -int(item["_dimensions"].get(dimension, 0)),
                    -int(item["_score"]),
                    int(item.get("ordinal") or 0),
                ),
            )
            if int(row["_dimensions"].get(dimension, 0)) > 0
        )
    for row in scored:
        if len(selected) >= max_chunks:
            break
        choose([row])

    packet_rows = []
    covered_dimensions: set[str] = set()
    for row in selected:
        dimensions = [
            key for key, value in row["_dimensions"].items() if int(value) > 0
        ]
        covered_dimensions.update(dimensions)
        packet_rows.append(
            {
                "chunk_id": str(row.get("chunk_id") or ""),
                "material_unit_id": "unit:text:" + hashlib.sha1(
                    str(row.get("chunk_id") or content_hash(row.get("text"))).encode("utf-8")
                ).hexdigest()[:20],
                "content_hash": content_hash(row.get("text")),
                "source_paper_id": str(row.get("paper_id") or ""),
                "source_kind": str(row.get("source_kind") or ""),
                "content_depth": str(row.get("content_depth") or ""),
                "use_permission": str(row.get("use_permission") or ""),
                "evidence_ceiling": str(row["_evidence_ceiling"]),
                "evidence_ceiling_reason": str(row["_ceiling_reason"]),
                "section_path": _text(row.get("section_path")),
                "ordinal": int(row.get("ordinal") or 0),
                "text": _trim_evidence(
                    str(row.get("text") or ""), limit=max_chars_per_chunk
                ),
                "selection_dimensions": dimensions,
            }
        )
    return packet_rows, {
        "available_chunk_count": len(scored),
        "selected_chunk_count": len(packet_rows),
        "selected_chunk_ids": [row["chunk_id"] for row in packet_rows],
        "covered_packet_dimensions": sorted(covered_dimensions),
        "packet_is_bounded": len(packet_rows) <= max_chunks,
        "all_available_chunk_ids_retained_by_reference": True,
    }


def build_claim_centered_material_packets(
    *,
    kb_sqlite: Path,
    material_flow_ledger_path: Path,
    query_plan_path: Path,
    output_path: Path | None = None,
    max_chunks_per_work: int = 12,
    max_chars_per_chunk: int = 1800,
) -> dict[str, Any]:
    """Build one bounded extraction packet for every admitted canonical work."""

    ledger = _load_json(material_flow_ledger_path)
    query_plan = _load_json(query_plan_path)
    question, facets = _query_context(query_plan)
    supplementary_context = _supplementary_packet_context(query_plan)
    query_meta = question_identity(
        str(query_plan.get("query_id") or (query_plan.get("metadata") or {}).get("query_id") or ""),
        question,
    )
    ledger_rows = [
        dict(row)
        for row in ledger.get("papers") or []
        if isinstance(row, dict) and row.get("admitted_to_downstream")
    ]
    ledger_by_id = {
        str(row.get("paper_id") or ""): row
        for row in ledger_rows
        if str(row.get("paper_id") or "")
    }

    uri = f"file:{kb_sqlite.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        paper_rows = {
            str(row["paper_id"]): dict(row)
            for row in conn.execute(
                "SELECT paper_id, doi, title, year, venue, raw_json, "
                "discovery_route, materialization_route, content_depth, "
                "use_permission, scope_fit FROM papers"
            )
        }
        chunk_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in conn.execute(
            "SELECT chunk_id, paper_id, doi, title, ordinal, section_path, "
            "text, raw_json, evidence_level, source_kind, provenance_json, "
            "route_provenance_json, content_depth, use_permission, "
            "context_complete, allowed_claim_kinds_json, scope_fit "
            "FROM text_chunks ORDER BY paper_id, ordinal, chunk_id"
        ):
            item = dict(row)
            provenance = _json_object(item.get("provenance_json"))
            route_provenance = _json_object(item.get("route_provenance_json"))
            item["provenance"] = provenance or route_provenance
            item["route_provenance"] = route_provenance or provenance
            try:
                allowed = json.loads(item.get("allowed_claim_kinds_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                allowed = []
            item["allowed_claim_kinds"] = allowed if isinstance(allowed, list) else []
            chunk_rows[str(item.get("paper_id") or "")].append(item)

    missing_papers = sorted(set(ledger_by_id) - set(paper_rows))
    if missing_papers:
        raise ValueError(
            "Admitted material records are missing from the canonical paper table: "
            + ", ".join(missing_papers[:8])
        )

    merged_rows: list[dict[str, Any]] = []
    for paper_id, ledger_row in ledger_by_id.items():
        db_row = dict(paper_rows[paper_id])
        # The post-acquisition database may carry stricter verified metadata
        # than the pre-ingest material ledger (for example a DOI recovered by
        # OpenAlex).  Material status remains ledger-owned.
        merged_rows.append(
            {
                **ledger_row,
                **{key: value for key, value in db_row.items() if value not in (None, "")},
                "paper_id": paper_id,
                "material_status": str(ledger_row.get("material_status") or ""),
            }
        )

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in merged_rows:
        groups[_work_group_key(row)].append(row)

    packets: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    for group_key in sorted(groups):
        members = groups[group_key]
        identity = _preferred_identity(members)
        member_ids = sorted(str(row["paper_id"]) for row in members)
        all_chunks = [
            chunk
            for paper_id in member_ids
            for chunk in chunk_rows.get(paper_id, [])
        ]
        if not all_chunks:
            raise ValueError(
                "Admitted canonical work has no material chunks: "
                + ", ".join(member_ids)
            )
        selected, selection = _select_chunks(
            all_chunks,
            question=question,
            facets=facets,
            max_chunks=max(1, int(max_chunks_per_work)),
            max_chars_per_chunk=max(200, int(max_chars_per_chunk)),
        )
        if not selected:
            raise ValueError(
                "Admitted canonical work produced an empty extraction packet: "
                + ", ".join(member_ids)
            )
        material_classes = sorted(
            {str(row.get("material_status") or "") for row in members},
            key=lambda value: (-_material_strength(value), value),
        )
        all_chunk_ids = [str(row.get("chunk_id") or "") for row in all_chunks]
        work_id = _stable_work_id(group_key)
        packet = {
            "schema_version": "optomind.claim_centered_material_packet.v1",
            "canonical_work_id": work_id,
            "canonical_identity": {
                "paper_id": str(identity.get("paper_id") or ""),
                "doi": _normalized_doi(identity.get("doi")),
                "title": _text(identity.get("title")),
                "year": identity.get("year"),
                "venue": _text(identity.get("venue")),
            },
            "member_paper_ids": member_ids,
            "material_classes": material_classes,
            "strongest_material_class": material_classes[0],
            "question": question,
            "query_id": query_meta["query_id"],
            "question_hash": query_meta["question_hash"],
            "annotation_schema_version": query_meta["annotation_schema_version"],
            "seed_axis_catalog": facets,
            "supplementary_context": supplementary_context,
            "selected_evidence": selected,
            "all_available_chunk_ids": all_chunk_ids,
            "selection_audit": selection,
            "downstream_rule": (
                "Extract propositions only from selected_evidence and cite exact "
                "chunk_ids. Assign any matching seed axes, but also propose material-"
                "emergent axes when the evidence exposes a recurring scientific "
                "dimension not represented by the seed catalog. Abstract claims may "
                "report what authors claim but cannot be promoted beyond their "
                "recorded evidence ceiling."
            ),
        }
        packets.append(packet)
        if len(members) > 1:
            duplicate_groups.append(
                {
                    "canonical_work_id": work_id,
                    "group_key": group_key,
                    "member_paper_ids": member_ids,
                    "selected_canonical_paper_id": packet["canonical_identity"]["paper_id"],
                    "material_classes": material_classes,
                }
            )

    result = {
        "schema_version": SCHEMA_VERSION,
        "question": question,
        "query_id": query_meta["query_id"],
        "question_hash": query_meta["question_hash"],
        "annotation_schema_version": query_meta["annotation_schema_version"],
        "seed_axis_catalog": facets,
        "supplementary_context": supplementary_context,
        "source_material_record_count": len(ledger_rows),
        "canonical_work_count": len(packets),
        "duplicate_work_group_count": len(duplicate_groups),
        "duplicate_work_groups": duplicate_groups,
        "excluded_discovery_only_count": int(
            (ledger.get("summary") or {}).get("discovery_only_paper_count") or 0
        ),
        "material_class_record_counts": {
            status: sum(
                1 for row in ledger_rows if row.get("material_status") == status
            )
            for status in ("s2_body", "oa_fulltext", "abstract_claim")
        },
        "packet_policy": {
            "max_chunks_per_work": max(1, int(max_chunks_per_work)),
            "max_chars_per_chunk": max(200, int(max_chars_per_chunk)),
            "dimensions": [item[0] for item in _PACKET_DIMENSIONS],
            "candidate_inventory_is_not_reference_inventory": True,
            "discovery_only_records_are_not_classified": True,
            "packet_selection_dimensions_are_not_scientific_axes": True,
            "scientific_axes_are_open_world": True,
            "axis_sources": ["user_question", "material_emergent"],
        },
        "packets": packets,
    }
    if output_path is not None:
        atomic_write_json(output_path, result)
    return result


__all__ = [
    "SCHEMA_VERSION",
    "build_claim_centered_material_packets",
]
