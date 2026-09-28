"""Read-only adapters for legacy R4 artifacts that are candidates for R5.

The adapter is intentionally conservative.  It discovers real files and
records their provenance, but it never invents claims, relations, permissions,
or a current Phase-3 contract.  A missing Phase-3 contract is recorded as a
source limitation; genuinely missing core R5 inputs still produce a blocked
preflight and a concrete future command rather than a synthetic R5 package.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .r6_cross_topic_regression import (
    _artifact_files,
    _cost_from_roots,
    _find_json,
    _first,
    _known_phase3_status,
    _resolve,
    _safe_load_json,
    _walk_mappings,
)


ADAPTER_SCHEMA = "research_harness.r6_topic_context_adapter.v1"
LIVE_PLAN_SCHEMA = "research_harness.r6_live_execution_plan.v1"


def _unique_paths(paths: Iterable[Path | None]) -> list[Path]:
    """Return existing paths once, preserving the first explicit choice."""
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).resolve()
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _normalized_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _declared_string(value: Any, names: Sequence[str]) -> str | None:
    wanted = {name.lower() for name in names}
    for mapping in _walk_mappings(value):
        for key, item in mapping.items():
            if str(key).lower() in wanted and isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _read_scoped_sqlite_index(paths: Sequence[Path]) -> dict[str, Any]:
    """Read only the identity/permission columns needed by the R6 handoff.

    This is deliberately not a content importer.  A row is authoritative only
    when it exists in a manifest-scoped SQLite file; missing ledger IDs remain
    explicit unavailable/discovery records.
    """
    paper_ids: dict[str, str] = {}
    text_chunks: dict[str, dict[str, Any]] = {}
    visual_chunk_ids: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    database_paths: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            errors.append({"path": str(path), "error": "missing_scoped_database"})
            continue
        database_paths.append(str(path))
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            paper_columns = _sqlite_columns(connection, "papers")
            if "paper_id" in paper_columns:
                for row in connection.execute("SELECT paper_id FROM papers").fetchall():
                    value = str(row[0] or "").strip()
                    if value:
                        paper_ids.setdefault(_normalized_id(value), value)

            text_columns = _sqlite_columns(connection, "text_chunks")
            if "chunk_id" in text_columns:
                selected = [
                    column
                    for column in (
                        "chunk_id",
                        "paper_id",
                        "evidence_level",
                        "source_kind",
                        "provenance_json",
                    )
                    if column in text_columns
                ]
                query = f"SELECT {', '.join(selected)} FROM text_chunks"
                for raw_row in connection.execute(query).fetchall():
                    row = dict(zip(selected, raw_row))
                    chunk_id = str(row.get("chunk_id") or "").strip()
                    if not chunk_id:
                        continue
                    text_chunks.setdefault(
                        _normalized_id(chunk_id),
                        {
                            "chunk_id": chunk_id,
                            "paper_id": str(row.get("paper_id") or "").strip(),
                            "evidence_level": str(row.get("evidence_level") or "").strip(),
                            "source_kind": str(row.get("source_kind") or "").strip(),
                            "provenance_json": str(row.get("provenance_json") or "").strip(),
                            "source_database": str(path),
                        },
                    )

            visual_columns = _sqlite_columns(connection, "visual_chunks")
            if "chunk_id" in visual_columns:
                for row in connection.execute("SELECT chunk_id FROM visual_chunks").fetchall():
                    value = str(row[0] or "").strip()
                    if value:
                        visual_chunk_ids.setdefault(_normalized_id(value), value)
        except (sqlite3.DatabaseError, OSError) as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if connection is not None:
                connection.close()
    return {
        "database_paths": database_paths,
        "paper_ids": paper_ids,
        "text_chunks": text_chunks,
        "visual_chunk_ids": visual_chunk_ids,
        "errors": errors,
    }


def _ledger_records(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Extract real source-ledger rows without interpreting them as claims."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for path in paths:
        value = _safe_load_json(path)
        if value is None:
            continue
        for mapping in _walk_mappings(value):
            paper_id = str(mapping.get("paper_id") or mapping.get("doi") or "").strip()
            chunk_ids = tuple(_string_list(mapping.get("canonical_chunk_ids")))
            if not paper_id and not chunk_ids:
                continue
            key = (str(path), paper_id, chunk_ids)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "source_path": str(path.resolve()),
                    "section_id": str(mapping.get("section_id") or "").strip(),
                    "paper_id": paper_id,
                    "doi": str(mapping.get("doi") or "").strip(),
                    "canonical_chunk_ids": list(chunk_ids),
                    "acquisition_status": str(mapping.get("acquisition_status") or "").strip(),
                    "source_kind": str(mapping.get("source_kind") or "").strip(),
                    "evidence_level": str(mapping.get("evidence_level") or "").strip(),
                    "not_usable_for": _string_list(mapping.get("not_usable_for")),
                    "literature_role": str(mapping.get("literature_role") or "").strip(),
                }
            )
    return records


_PERMISSION_ORDER = {
    "unavailable": 0,
    "discovery_only": 1,
    "qualified_only": 2,
    "contextual_or_qualified_support": 3,
    "factual_support": 4,
}


def _permission_from_value(value: Any) -> str | None:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not token:
        return None
    if token in {"factual_support", "direct", "direct_fact", "fact", "fulltext", "full_text"}:
        return "factual_support"
    if token in {"contextual", "context", "background", "contextual_support", "qualified_support"}:
        return "contextual_or_qualified_support"
    if token in {"qualified", "qualified_only", "method_transfer"}:
        return "qualified_only"
    if token in {"discovery", "discovery_only", "metadata", "abstract_only"}:
        return "discovery_only"
    if token in {"unavailable", "missing"}:
        return "unavailable"
    return None


def _most_restrictive(permissions: Iterable[str]) -> str:
    values = list(permissions)
    if not values:
        return "unavailable"
    return min(values, key=lambda value: _PERMISSION_ORDER.get(value, 0))


def _permission_for_chunk(row: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> tuple[str, str, list[str]]:
    restrictions = sorted({item for record in records for item in _string_list(record.get("not_usable_for"))})
    candidates: list[str] = []
    for record in records:
        for key in ("permission", "use_permission", "source_permission", "evidence_permission", "evidence_level"):
            mapped = _permission_from_value(record.get(key))
            if mapped:
                candidates.append(mapped)
        if str(record.get("acquisition_status") or "").lower() == "abstract_only":
            candidates.append("discovery_only")
        if str(record.get("source_kind") or "").lower() == "method_transfer":
            candidates.append("qualified_only")

    if not candidates:
        candidates.extend(
            mapped
            for mapped in (
                _permission_from_value(row.get("evidence_level")),
                _permission_from_value(row.get("source_kind")),
            )
            if mapped
        )
    permission = _most_restrictive(candidates or ["unavailable"])
    if restrictions and permission == "factual_support":
        permission = "qualified_only"
    reason = "validated_scoped_text_chunk"
    if not records:
        reason = "scoped_kb_row_without_matching_ledger"
    elif restrictions:
        reason = "validated_chunk_with_source_scope_restrictions"
    return permission, reason, restrictions


def _build_authoritative_permission_artifact(
    topic_id: str,
    source_paths: Sequence[Path],
    scoped_kb_paths: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    index = _read_scoped_sqlite_index(scoped_kb_paths)
    ledger = _ledger_records(source_paths)
    records_by_chunk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records_by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ledger_paper_display: dict[str, str] = {}
    ledger_chunk_display: dict[str, str] = {}
    for record in ledger:
        paper_id = str(record.get("paper_id") or "").strip()
        if paper_id:
            records_by_paper[_normalized_id(paper_id)].append(record)
            ledger_paper_display.setdefault(_normalized_id(paper_id), paper_id)
        for chunk_id in record.get("canonical_chunk_ids") or []:
            normalized = _normalized_id(chunk_id)
            if not normalized:
                continue
            records_by_chunk[normalized].append(record)
            ledger_chunk_display.setdefault(normalized, str(chunk_id))

    db_paper_ids = set(index["paper_ids"])
    ledger_paper_ids = set(ledger_paper_display)
    missing_papers = sorted(
        ledger_paper_display[key] for key in ledger_paper_ids if key not in db_paper_ids
    )
    ledger_chunk_ids = set(ledger_chunk_display)
    db_chunk_ids = set(index["text_chunks"])
    missing_chunks = sorted(
        ledger_chunk_display[key] for key in ledger_chunk_ids if key not in db_chunk_ids
    )

    chunk_permissions: dict[str, dict[str, Any]] = {}
    all_chunk_keys = sorted(db_chunk_ids | ledger_chunk_ids)
    for normalized_chunk_id in all_chunk_keys:
        row = index["text_chunks"].get(normalized_chunk_id)
        display_id = (row or {}).get("chunk_id") or ledger_chunk_display.get(normalized_chunk_id) or normalized_chunk_id
        record_rows = records_by_chunk.get(normalized_chunk_id, [])
        if row is None:
            permission = "unavailable"
            reason = "ledger_chunk_absent_from_scoped_sqlite"
            restrictions = sorted({item for record in record_rows for item in _string_list(record.get("not_usable_for"))})
        else:
            permission, reason, restrictions = _permission_for_chunk(row, record_rows)
        chunk_permissions[str(display_id)] = {
            "permission": permission,
            "paper_id": str((row or {}).get("paper_id") or (record_rows[0].get("paper_id") if record_rows else "")),
            "present_in_scoped_kb": row is not None,
            "source_kind": str((row or {}).get("source_kind") or ""),
            "evidence_level": str((row or {}).get("evidence_level") or ""),
            "reason": reason,
            "not_usable_for": restrictions,
            "source_ledger_paths": sorted({str(item.get("source_path")) for item in record_rows}),
        }

    paper_permissions: dict[str, dict[str, Any]] = {}
    all_paper_keys = sorted(db_paper_ids | ledger_paper_ids)
    for normalized_paper_id in all_paper_keys:
        display_id = index["paper_ids"].get(normalized_paper_id) or ledger_paper_display.get(normalized_paper_id) or normalized_paper_id
        available = [
            chunk_id
            for chunk_id, item in chunk_permissions.items()
            if _normalized_id(item.get("paper_id")) == normalized_paper_id and item.get("present_in_scoped_kb")
        ]
        missing = sorted(
            chunk_id
            for chunk_id in ledger_chunk_display.values()
            if chunk_id in missing_chunks
            and any(
                _normalized_id(record.get("paper_id")) == normalized_paper_id
                and chunk_id in (record.get("canonical_chunk_ids") or [])
                for record in records_by_paper.get(normalized_paper_id, [])
            )
        )
        counts: dict[str, int] = defaultdict(int)
        for chunk_id in available:
            counts[str(chunk_permissions[chunk_id]["permission"])] += 1
        if missing:
            counts["unavailable"] += len(missing)
        permission = _most_restrictive(counts.keys()) if counts else "discovery_only"
        if normalized_paper_id not in db_paper_ids:
            permission = "discovery_only"
        paper_permissions[str(display_id)] = {
            "permission": permission,
            "valid_in_scoped_kb": normalized_paper_id in db_paper_ids,
            "available_chunk_ids": sorted(available),
            "missing_chunk_ids": missing,
            "permission_counts": dict(sorted(counts.items())),
            "ledger_sections": sorted({str(item.get("section_id")) for item in records_by_paper.get(normalized_paper_id, []) if item.get("section_id")}),
            "source_paths": sorted({str(item.get("source_path")) for item in records_by_paper.get(normalized_paper_id, [])}),
            "discovery_lead": normalized_paper_id not in db_paper_ids,
        }

    permission_counts: dict[str, int] = defaultdict(int)
    for item in chunk_permissions.values():
        permission_counts[str(item["permission"])] += 1
    paper_permission_counts: dict[str, int] = defaultdict(int)
    for item in paper_permissions.values():
        paper_permission_counts[str(item["permission"])] += 1
    factual_ids = sorted(
        chunk_id
        for chunk_id, item in chunk_permissions.items()
        if item["permission"] == "factual_support" and item["present_in_scoped_kb"]
    )
    excluded_ids = sorted(chunk_id for chunk_id in chunk_permissions if chunk_id not in factual_ids)
    reconciliation = {
        "ledger_source_record_count": len(ledger),
        "ledger_paper_count": len(ledger_paper_ids),
        "scoped_db_paper_count": len(db_paper_ids),
        "available_paper_count": len(ledger_paper_ids & db_paper_ids),
        "ledger_chunk_count": len(ledger_chunk_ids),
        "scoped_db_text_chunk_count": len(db_chunk_ids),
        "available_chunk_count": len(ledger_chunk_ids & db_chunk_ids),
        "missing_ledger_paper_ids": missing_papers,
        "missing_ledger_chunk_ids": missing_chunks,
        "db_read_errors": index["errors"],
        "no_rows_fabricated": True,
    }
    artifact = {
        "schema_version": "research_harness.r6_source_permissions_adapter.v2",
        "adapter_schema_version": ADAPTER_SCHEMA,
        "topic_id": topic_id,
        "source_paths": [str(path) for path in source_paths],
        "scoped_kb_paths": [str(path) for path in scoped_kb_paths],
        "permission_count_unit": "validated_text_chunk_rows",
        "permission_counts": dict(sorted(permission_counts.items())),
        "paper_permission_counts": dict(sorted(paper_permission_counts.items())),
        "paper_permissions": paper_permissions,
        "chunk_permissions": chunk_permissions,
        "factual_support_chunk_ids": factual_ids,
        "pruned_from_factual_support_chunk_ids": excluded_ids,
        "missing_ledger_papers": missing_papers,
        "missing_ledger_chunks": missing_chunks,
        "ledger_inventory_reconciliation": reconciliation,
        "policy": {
            "authoritative_for_r6": True,
            "deterministic_index_only": True,
            "absent_rows_can_support_facts": False,
            "discovery_only_can_support_facts": False,
            "non_factual_permissions_cannot_support_established_facts": True,
            "no_rows_fabricated": True,
        },
    }
    return artifact, index


def _visual_placements(value: Any) -> list[Mapping[str, Any]]:
    placements: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for mapping in _walk_mappings(value):
        visual_id = str(mapping.get("visual_chunk_id") or mapping.get("visual_id") or "").strip()
        paper_id = str(mapping.get("paper_id") or "").strip()
        local_path = str(mapping.get("local_image_path") or "").strip()
        if not visual_id and not (paper_id and local_path):
            continue
        key = (visual_id, paper_id, local_path)
        if key in seen:
            continue
        seen.add(key)
        placements.append(mapping)
    return placements


def _validate_visual_plan(
    candidate_path: Path | None,
    quality_report: Mapping[str, Any] | None,
    scoped_index: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    candidate = str(candidate_path) if candidate_path else None
    value = _safe_load_json(candidate_path) if candidate_path else None
    blocking = _string_list(quality_report.get("blocking_issues") if isinstance(quality_report, Mapping) else [])
    if any(str(item).strip().lower() == "visual_plan_topic_identity_mismatch" for item in blocking):
        reasons.append("quality_report:visual_plan_topic_identity_mismatch")
    if candidate_path is None:
        reasons.append("no_scoped_visual_plan_candidate")
    elif not isinstance(value, Mapping):
        reasons.append("visual_plan_missing_or_invalid_json")
    placements = _visual_placements(value) if isinstance(value, Mapping) else []
    invalid_placements: list[dict[str, Any]] = []
    for placement in placements:
        visual_id = str(placement.get("visual_chunk_id") or placement.get("visual_id") or "").strip()
        paper_id = str(placement.get("paper_id") or "").strip()
        invalid_reasons: list[str] = []
        if visual_id and _normalized_id(visual_id) not in scoped_index.get("visual_chunk_ids", {}):
            invalid_reasons.append("placement_visual_chunk_not_in_scoped_kb")
        if paper_id and _normalized_id(paper_id) not in scoped_index.get("paper_ids", {}):
            invalid_reasons.append("placement_paper_not_in_scoped_kb")
        if invalid_reasons:
            invalid_placements.append(
                {"visual_chunk_id": visual_id, "paper_id": paper_id, "reasons": invalid_reasons}
            )
            reasons.extend(invalid_reasons)
    deduplicated_reasons = list(dict.fromkeys(reasons))
    accepted = bool(candidate_path and isinstance(value, Mapping) and not deduplicated_reasons)
    return {
        "candidate_path": candidate,
        "selected_path": candidate if accepted else None,
        "accepted": accepted,
        "omitted": not accepted,
        "omission_reasons": [] if accepted else deduplicated_reasons,
        "placement_count": len(placements),
        "validated_placement_count": len(placements) - len(invalid_placements),
        "invalid_placement_count": len(invalid_placements),
        "invalid_placements": invalid_placements,
        "quality_report_blocking_issues": blocking,
        "policy": {
            "topic_identity_failure_omits_entire_plan": True,
            "no_replacement_visual_plan_created": True,
            "scoped_ids_only": True,
        },
    }


def _path_record(path: Path | None, role: str, *, warning: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "role": role,
        "path": str(path) if path else None,
        "exists": bool(path and path.exists()),
        "is_file": bool(path and path.is_file()),
        "is_directory": bool(path and path.is_dir()),
    }
    if warning:
        record["warning"] = warning
    return record


def _resolve_declared(value: Any, project_root: Path, roots: Sequence[Path | None]) -> Path | None:
    if not value:
        return None
    raw = Path(str(value))
    candidates = [raw] if raw.is_absolute() else [project_root / raw]
    for root in roots:
        if root and not raw.is_absolute():
            candidates.append(root / raw)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else None


def _discover_by_names(roots: Sequence[Path | None], names: Sequence[str], limit: int = 200) -> list[Path]:
    wanted = {name.lower() for name in names}
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in _artifact_files(root, limit=limit):
            if path.name.lower() not in wanted or path in seen:
                continue
            seen.add(path)
            found.append(path)
    return found


def _discover_by_tokens(roots: Sequence[Path | None], tokens: Sequence[str], limit: int = 120) -> list[Path]:
    lowered = tuple(token.lower() for token in tokens)
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in _artifact_files(root, limit=500):
            name = path.name.lower()
            if path in seen or not all(token in name for token in lowered):
                continue
            seen.add(path)
            found.append(path)
            if len(found) >= limit:
                return found
    return found


def _read_package(root: Path | None) -> tuple[Path | None, Mapping[str, Any]]:
    path = _find_json(root, ("REVIEW_CONTENT_PACKAGE.json",))
    value = _safe_load_json(path) if path else None
    return path, value if isinstance(value, Mapping) else {}


def _word_count(path: Path | None) -> int | None:
    if not path or not path.is_file():
        return None
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").split())
    except OSError:
        return None


def _record_paths(paths: Sequence[Path], role: str) -> list[dict[str, Any]]:
    return [_path_record(path, role) for path in sorted(set(paths), key=str)]


_IDENTITY_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "used", "using",
    "how", "what", "where", "when", "which", "their", "current", "should", "could",
    "system", "systems", "research", "review", "comprehensive", "literature", "based",
    "identify", "identified", "including", "while", "among", "through", "under", "such",
}


def _extract_query_fields(value: Any) -> dict[str, Any]:
    """Extract existing question/scope fields; never generate scientific text."""
    result: dict[str, Any] = {}
    for mapping in _walk_mappings(value):
        for key, item in mapping.items():
            key_lower = str(key).lower()
            if isinstance(item, str) and item.strip():
                if key_lower in {"user_question", "user_query", "question", "query"} and "user_question" not in result:
                    result["user_question"] = item.strip()
                elif key_lower in {"problem_understanding", "normalized_question", "research_question"} and "normalized_question" not in result:
                    result["normalized_question"] = item.strip()
                elif key_lower in {"main_scope", "scope_definition", "scope"} and "main_scope" not in result:
                    result["main_scope"] = item.strip()
            elif key_lower == "scope_items" and isinstance(item, list) and "scope_items" not in result:
                result["scope_items"] = [str(x).strip() for x in item if str(x).strip()]
    return result


def _deterministic_tokens(text: str, limit: int = 48) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower())
    output: list[str] = []
    for token in tokens:
        if token in _IDENTITY_STOPWORDS or token in output:
            continue
        output.append(token)
        if len(output) >= limit:
            break
    return output


def _identity_from_real_assets(
    topic: Mapping[str, Any],
    blueprint_paths: Sequence[Path],
    query_paths: Sequence[Path],
) -> dict[str, Any]:
    blueprint = _safe_load_json(blueprint_paths[0]) if blueprint_paths else None
    query_values = [_safe_load_json(path) for path in query_paths]
    query_values = [value for value in query_values if value is not None]
    fields: dict[str, Any] = {}
    for value in query_values + ([blueprint] if blueprint is not None else []):
        extracted = _extract_query_fields(value)
        for key, item in extracted.items():
            if key not in fields or not fields[key]:
                fields[key] = item
    if isinstance(blueprint, Mapping):
        extracted = _extract_query_fields(blueprint.get("input_context", {}))
        for key, item in extracted.items():
            fields.setdefault(key, item)
    normalized = str(fields.get("normalized_question") or fields.get("user_question") or topic.get("scientific_scope") or "").strip()
    raw_question = str(fields.get("user_question") or "").strip()
    scope_items = list(fields.get("scope_items") or [])
    main_scope = str(fields.get("main_scope") or "").strip()
    if not scope_items and isinstance(blueprint, Mapping):
        sections = blueprint.get("sections") or []
        scope_items = [str(section.get("title", "")).strip() for section in sections if isinstance(section, Mapping) and section.get("title")]
    anchor_source = " ".join([normalized, main_scope, *scope_items])
    anchor_tokens = _deterministic_tokens(anchor_source)
    anchor_phrases = [item for item in scope_items[:12] if item]
    fingerprint_source = json.dumps(
        {"topic_id": topic.get("topic_id"), "normalized_question": normalized, "scope_items": scope_items},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return {
        "schema_version": "research_harness.topic_identity.v1",
        "adapter_schema_version": ADAPTER_SCHEMA,
        "topic_id": str(topic["topic_id"]),
        "category": topic.get("category"),
        "source_user_question": raw_question,
        "normalized_question": normalized,
        "scope_definition": {"main_scope": main_scope, "scope_items": scope_items},
        "core_anchor_tokens": anchor_tokens,
        "anchor_phrases": anchor_phrases,
        "fingerprint": hashlib.sha256(fingerprint_source).hexdigest(),
        "valid": bool(normalized and anchor_tokens),
        "placeholder_markers": [],
        "policy": {
            "deterministic_only": True,
            "source_text_reused_without_model_generation": True,
            "no_claims_or_relations_created": True,
        },
    }


def _scope_map_from_blueprint(topic: Mapping[str, Any], blueprint_paths: Sequence[Path]) -> dict[str, Any]:
    blueprint = _safe_load_json(blueprint_paths[0]) if blueprint_paths else None
    sections = blueprint.get("sections", []) if isinstance(blueprint, Mapping) else []
    records: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("section_id") or section.get("id") or f"S{index + 1:02d}")
        records.append(
            {
                "section_id": section_id,
                "title": str(section.get("title") or ""),
                "argument_role": str(section.get("argument_role") or ""),
                "required_roles": list(section.get("required_roles") or []),
                "source": "review_blueprint",
                "claims": [],
                "evidence_ids": [],
            }
        )
    return {
        "schema_version": "research_harness.review_scope_map.v1",
        "adapter_schema_version": ADAPTER_SCHEMA,
        "topic_id": str(topic["topic_id"]),
        "deterministic_from_blueprint": True,
        "sections": records,
        "claim_count": 0,
        "relation_count": 0,
        "policy": {
            "section_tasks_preserved": True,
            "claims_not_invented": True,
            "evidence_not_invented": True,
            "relations_not_invented": True,
        },
    }


def _empty_relation_graph(
    topic: Mapping[str, Any],
    source_roots: Sequence[Path | None],
    source_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    source_status = "empty_verified" if source_paths else "empty_unavailable"
    source_limitation = (
        "A real relation artifact was found, but it contains no usable relation edges; no edges were synthesized."
        if source_paths
        else "No current Phase3 relation graph was found in the legacy R4 assets."
    )
    return {
        "schema_version": "research_harness.relation_graph.v1",
        "adapter_schema_version": ADAPTER_SCHEMA,
        "topic_id": str(topic["topic_id"]),
        "status": source_status,
        "nodes": [],
        "relations": [],
        "node_count": 0,
        "relation_count": 0,
        "source_limitation": source_limitation,
        "source_roots_checked": [str(path) for path in source_roots if path],
        "source_artifact_paths": [str(path) for path in source_paths],
        "policy": {
            "empty_is_not_success": True,
            "empty_is_not_failure": True,
            "no_inferred_edges": True,
            "no_claim_nodes_created": True,
        },
    }


def _handoff_limitations(
    topic: Mapping[str, Any],
    package: Mapping[str, Any],
    phase3_contract: Mapping[str, Any],
    phase3_claim_paths: Sequence[Path],
    phase3_relation_paths: Sequence[Path],
    permission_paths: Sequence[Path],
    permission_counts: Mapping[str, int],
    scoped_kb_paths: Sequence[Path],
    legacy_base: Mapping[str, Any] | None,
) -> dict[str, Any]:
    status = str(_first(package, "status", default="missing"))
    limitations: list[dict[str, Any]] = []
    claims_available = phase3_contract.get("status") == "available" and bool(phase3_claim_paths)
    relations_available = bool(phase3_relation_paths)
    if not claims_available:
        limitations.append(
            {
                "code": "current_phase3_claims_missing",
                "severity": "source_limitation",
                "message": "No current Phase3 claim artifact was found; R5 must derive opportunities from the final review and permitted source assets.",
            }
        )
    if not relations_available:
        limitations.append(
            {
                "code": "current_phase3_relations_missing",
                "severity": "source_limitation",
                "message": "No current Phase3 relation graph was found; the compatibility relation graph is explicitly empty and unavailable.",
            }
        )
    if status in {"awaiting_human_review", "needs_more_literature", "partial"}:
        limitations.append(
            {
                "code": "r4_candidate_not_final",
                "severity": "candidate_limit",
                "message": f"R4 package status is {status}; it may inform R5 but cannot be treated as an executed result.",
            }
        )
    return {
        "schema_version": "research_harness.legacy_r4_handoff_limitations.v1",
        "adapter_schema_version": ADAPTER_SCHEMA,
        "topic_id": str(topic["topic_id"]),
        "r4_status": status,
        "r4_candidate_status": status in {"awaiting_human_review", "needs_more_literature", "partial"},
        "phase3_contract_status": phase3_contract.get("status"),
        "phase3_claim_artifacts": [str(path) for path in phase3_claim_paths],
        "phase3_relation_artifacts": [str(path) for path in phase3_relation_paths],
        "limitations": limitations,
        "permission_sources": [str(path) for path in permission_paths],
        "permission_counts": dict(permission_counts),
        "scoped_kb_paths": [str(path) for path in scoped_kb_paths],
        "legacy_declared_base_kb": legacy_base,
        "allowed_r5_inputs": [
            "real final review text",
            "real source-permission ledgers",
            "manifest-scoped knowledge bases",
            "deterministic section scope map",
        ],
        "forbidden_shortcuts": [
            "Do not promote discovery-only material to factual evidence.",
            "Do not treat an empty relation graph as evidence of no relationships.",
            "Do not use the broad legacy package database when it is outside the manifest allowlist.",
            "Do not convert an R4 candidate into an executed result.",
            "Do not invent claim IDs, evidence IDs, or relation edges.",
        ],
    }


def _write_compatibility_artifacts(output_dir: Path, artifacts: Mapping[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for name, payload in artifacts.items():
        path = output_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        paths[name] = str(path)
    return paths


def adapt_topic_context(
    topic: Mapping[str, Any],
    project_root: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Discover and normalize one topic without creating scientific content."""
    topic_id = str(topic["topic_id"])
    r4_root = _resolve(project_root, topic.get("r4_root"))
    r5_root = _resolve(project_root, topic.get("r5_root"))
    phase3_root = _resolve(project_root, topic.get("phase3_artifacts_root"))
    roots = [r4_root, phase3_root, r5_root]
    package_path, package = _read_package(r4_root)

    declared_paths: dict[str, dict[str, Any]] = {}
    for field, role in (
        ("base_kb_sqlite", "legacy_package_base_kb"),
        ("query_plan_path", "query_plan"),
        ("final_review_path", "review_draft"),
        ("visual_editorial_plan_path", "visual_plan"),
        ("research_plan_path", "legacy_research_plan"),
        ("topic_identity_path", "topic_identity"),
    ):
        resolved = _resolve_declared(package.get(field), project_root, roots)
        if resolved:
            declared_paths[field] = _path_record(resolved, role)

    declared_quality = _declared_string(
        package,
        ("quality_report_json", "quality_report_path", "review_harness_quality_report"),
    )
    resolved_quality = _resolve_declared(declared_quality, project_root, roots)
    if resolved_quality:
        declared_paths["quality_report_path"] = _path_record(resolved_quality, "quality_report")
    declared_technical = _declared_string(
        package,
        ("technical_audit_path", "technical_audit", "full_review_package_path"),
    )
    resolved_technical = _resolve_declared(declared_technical, project_root, roots)
    if resolved_technical:
        declared_paths["technical_audit_path"] = _path_record(resolved_technical, "technical_audit")

    blueprint_paths = _discover_by_names(
        roots,
        ("REVIEW_BLUEPRINT.json", "review_blueprint.json", "BLUEPRINT.json"),
    )
    review_paths: list[Path] = []
    declared_review = declared_paths.get("final_review_path", {}).get("path")
    if declared_review:
        review_paths.append(Path(declared_review))
    review_paths.extend(
        _discover_by_names(
            roots,
            ("FINAL_REVIEW_EN.md", "FULL_REVIEW_DRAFT_EN.md", "FINAL_REVIEW.md"),
        )
    )

    scoped_kb_paths: list[Path] = []
    for raw_path in topic.get("scoped_kb_paths", []):
        path = _resolve(project_root, raw_path)
        if path:
            scoped_kb_paths.append(path)
    scoped_kb_set = {str(path.resolve()).lower() for path in scoped_kb_paths}
    scoped_base_paths = [
        path for index, path in enumerate(scoped_kb_paths)
        if index == 0 and "supplemental" not in path.name.lower()
    ]
    supplemental_paths = [
        path for path in scoped_kb_paths if "supplemental" in path.name.lower() or "supplemental" in str(path.parent).lower()
    ]
    effective_base_paths = scoped_base_paths or scoped_kb_paths[:1]
    legacy_base = declared_paths.get("base_kb_sqlite", {}).get("path")
    unscoped_base_warning = bool(legacy_base and str(Path(legacy_base).resolve()).lower() not in scoped_kb_set)

    staging_candidates = [
        path
        for path in _discover_by_tokens(roots, ("staging",), limit=80)
        if path.suffix.lower() in {".sqlite", ".db"} or "sqlite" in path.name.lower()
    ]
    if not staging_candidates:
        staging_candidates = [
            path
            for path in scoped_kb_paths
            if "staging" in path.name.lower() or "staging" in str(path.parent).lower()
        ]

    coverage_roots = []
    if phase3_root and phase3_root.exists():
        coverage_roots.append(phase3_root)
    section_coverage = r4_root / "section_coverage" if r4_root else None
    if section_coverage and section_coverage.exists() and section_coverage not in coverage_roots:
        coverage_roots.append(section_coverage)
    source_permission_paths = _discover_by_names(
        coverage_roots + roots,
        (
            "SOURCE_PERMISSIONS.json",
            "SOURCE_PERMISSION_AUDIT.json",
            "SECTION_SOURCE_LEDGER.json",
            "PROGRAM_SHARED_CONTEXT.json",
            "SOURCE_ROUTE_AUDIT.json",
        ),
        limit=500,
    )
    technical_audit_paths = _unique_paths(
        [
            Path(declared_paths["technical_audit_path"]["path"])
            if declared_paths.get("technical_audit_path", {}).get("path")
            else None,
            *_discover_by_names(
                roots,
                (
                    "FULL_REVIEW_PACKAGE.json",
                    "R4_REAL_ACCEPTANCE_SUMMARY.json",
                    "R4_PHASE3_HANDOFF.json",
                    "REVIEW_BLUEPRINT_VALIDATION.json",
                    "TECHNICAL_AUDIT.json",
                    "GLOBAL_AUDIT_REPORT.json",
                ),
                limit=180,
            ),
            *_discover_by_tokens(roots, ("audit",), limit=180),
        ]
    )
    quality_report_paths = _unique_paths(
        [
            Path(declared_paths["quality_report_path"]["path"])
            if declared_paths.get("quality_report_path", {}).get("path")
            else None,
            *_discover_by_names(
                roots,
                ("REVIEW_HARNESS_QUALITY_REPORT.json", "QUALITY_REPORT.json"),
                limit=80,
            ),
        ]
    )
    selected_quality_report = next(
        (path for path in quality_report_paths if path.name == "REVIEW_HARNESS_QUALITY_REPORT.json"),
        quality_report_paths[0] if quality_report_paths else None,
    )
    technical_priority = (
        "FULL_REVIEW_PACKAGE.json",
        "R4_REAL_ACCEPTANCE_SUMMARY.json",
        "R4_PHASE3_HANDOFF.json",
        "REVIEW_BLUEPRINT_VALIDATION.json",
        "TECHNICAL_AUDIT.json",
        "GLOBAL_AUDIT_REPORT.json",
        "REVIEW_HARNESS_QUALITY_REPORT.json",
    )
    selected_technical_audit = next(
        (
            path
            for name in technical_priority
            for path in technical_audit_paths
            if path.name == name
        ),
        None,
    )
    visual_plan_paths: list[Path] = []
    declared_visual = declared_paths.get("visual_editorial_plan_path", {}).get("path")
    if declared_visual:
        visual_plan_paths.append(Path(declared_visual))
    visual_plan_paths.extend(
        _discover_by_names(
            roots,
            (
                "VISUAL_EDITORIAL_PLAN.json",
                "FINAL_VISUAL_PACKAGE.json",
                "ARTICLE_VISUAL_CONTRACT.json",
            ),
            limit=120,
        )
    )
    relation_paths = _discover_by_names(
        roots,
        ("RELATION_GRAPH.json", "RELATION_GRAPH_MIGRATED.json", "CLAIM_GRAPH.json", "EVIDENCE_NETWORK.json"),
        limit=80,
    )
    scope_paths = _discover_by_names(
        roots,
        ("REVIEW_SCOPE_MAP.json", "SCOPE_MAP.json", "QUERY_PLAN_REVIEW_PACKAGE.json"),
        limit=80,
    )
    gap_paths = _discover_by_names(
        coverage_roots + roots,
        ("SECTION_GAP_REPORT.json", "COVERAGE_REQUESTS.json", "SECTION_COVERAGE_PLAN.json"),
        limit=300,
    )
    query_paths: list[Path] = []
    declared_query = declared_paths.get("query_plan_path", {}).get("path")
    if declared_query:
        query_paths.append(Path(declared_query))
    query_paths.extend(_discover_by_names(roots, ("query_plan.json", "ORIGINAL_USER_QUESTION.json"), limit=40))
    phase3_contract = _known_phase3_status(phase3_root)
    phase3_claim_paths = _discover_by_names(
        [phase3_root],
        ("CLAIM_GRAPH.json", "MATERIAL_BINDINGS.json", "SYNTHESIS_BUNDLES.json"),
        limit=80,
    )
    phase3_relation_paths = [
        path
        for path in relation_paths
        if phase3_root and path.is_relative_to(phase3_root.resolve())
    ]
    permission_artifact, scoped_index = _build_authoritative_permission_artifact(
        topic_id,
        source_permission_paths,
        scoped_kb_paths,
    )
    permissions = permission_artifact.get("permission_counts", {})
    package_status = str(_first(package, "status", default="missing"))
    quality_summary = _first(package, "quality_summary", "quality_gate", default={})
    unresolved_visual_count = 0
    visual_plan_path = next((path for path in visual_plan_paths if path.name == "VISUAL_EDITORIAL_PLAN.json"), None)
    visual_plan = _safe_load_json(visual_plan_path) if visual_plan_path else None
    if isinstance(visual_plan, Mapping):
        unresolved = _first(visual_plan, "unfilled_visual_needs", "unfilled", default=[])
        unresolved_visual_count = len(unresolved) if isinstance(unresolved, list) else 0
    quality_report = _safe_load_json(selected_quality_report) if selected_quality_report else None
    quality_report = quality_report if isinstance(quality_report, Mapping) else None
    visual_plan_validation = _validate_visual_plan(visual_plan_path, quality_report, scoped_index)

    identity_artifact = _identity_from_real_assets(topic, blueprint_paths, query_paths)
    scope_artifact = _scope_map_from_blueprint(topic, blueprint_paths)
    relation_artifact = _empty_relation_graph(topic, roots, relation_paths)
    limitations_artifact = _handoff_limitations(
        topic,
        package,
        phase3_contract,
        phase3_claim_paths,
        phase3_relation_paths,
        source_permission_paths,
        permissions,
        scoped_kb_paths,
        declared_paths.get("base_kb_sqlite"),
    )
    compatibility_payloads = {
        "TOPIC_IDENTITY.json": identity_artifact,
        "REVIEW_SCOPE_MAP.json": scope_artifact,
        "RELATION_GRAPH.json": relation_artifact,
        "LEGACY_R4_HANDOFF_LIMITATIONS.json": limitations_artifact,
        "R6_SOURCE_PERMISSIONS_ADAPTER.json": permission_artifact,
    }
    compatibility_paths = _write_compatibility_artifacts(output_dir, compatibility_payloads) if output_dir else {}
    compatibility_root = output_dir.resolve() if output_dir else None

    required = {
        "blueprint": bool(blueprint_paths),
        "review_draft": any(path.is_file() for path in review_paths),
        "scoped_base_kb": bool(scoped_kb_paths) and all(path.is_file() for path in scoped_kb_paths),
        "source_permissions": bool(source_permission_paths),
        "technical_audit": bool(selected_technical_audit and selected_technical_audit.is_file()),
        "topic_identity": bool(identity_artifact.get("valid")),
    }
    optional = {
        "coverage_root": any(path.exists() for path in coverage_roots),
        "visual_plan": bool(visual_plan_validation.get("accepted")),
        "quality_report": bool(selected_quality_report and selected_quality_report.is_file()),
        "review_scope_map": bool(scope_artifact.get("sections")),
        "relation_graph": bool(relation_paths),
        "current_phase3_contract": phase3_contract["status"] == "available",
    }
    mandatory_missing = [
        key
        for key in (
            "blueprint",
            "review_draft",
            "scoped_base_kb",
            "source_permissions",
            "technical_audit",
            "topic_identity",
        )
        if not required[key]
    ]
    nonblocking_limitations = []
    if not relation_paths:
        nonblocking_limitations.append("current_relation_graph_unavailable_empty_compatibility_graph")
    if phase3_contract["status"] != "available":
        nonblocking_limitations.append("current_phase3_claim_contract_unavailable_legacy_assets_only")
    if not optional["coverage_root"]:
        nonblocking_limitations.append("coverage_root_unavailable_r5_may_need_compatibility_input")
    if not scope_artifact.get("sections"):
        nonblocking_limitations.append("review_scope_map_has_no_blueprint_sections")
    if not optional["quality_report"]:
        nonblocking_limitations.append("full_review_quality_report_unavailable")
    if visual_plan_validation.get("omitted"):
        nonblocking_limitations.append("visual_plan_omitted_from_live_r5_command")
    candidate_constraints = {
        "r4_status": package_status,
        "r4_status_is_nonterminal_candidate": package_status in {"awaiting_human_review", "needs_more_literature", "partial"},
        "must_preserve_candidate_limitations": package_status in {"awaiting_human_review", "needs_more_literature", "partial"},
        "may_be_used_as_executed_result": False,
        "quality_summary": quality_summary,
        "unresolved_visual_count": unresolved_visual_count,
        "legacy_package_base_kb_is_unscoped": unscoped_base_warning,
        "visual_plan_topic_identity_valid": bool(visual_plan_validation.get("accepted")),
    }
    missing_actions = {
        "topic_identity": "Use the topic identity from the current normalized query package; do not infer it from a title.",
        "blueprint": "Locate the real review blueprint before a formal R5 run.",
        "review_draft": "Locate the real final review or durable candidate draft before a formal R5 run.",
        "coverage_root": "Locate the real section-coverage root before a formal R5 run.",
        "scoped_base_kb": "Use only manifest-scoped KB files; do not fall back to a broad package database.",
        "source_permissions": "Locate the real source-permission ledgers before a formal R5 run.",
        "technical_audit": "Locate a real technical/quality audit before a formal R5 run.",
        "quality_report": "Pass the complete REVIEW_HARNESS_QUALITY_REPORT.json when available; it preserves partial and blocked review limitations.",
    }
    return {
        "schema_version": ADAPTER_SCHEMA,
        "topic_id": topic_id,
        "category": topic.get("category"),
        "scientific_scope": topic.get("scientific_scope"),
        "read_only": True,
        "source_roots": {
            "r4_root": _path_record(r4_root, "r4_root"),
            "r5_root": _path_record(r5_root, "r5_root"),
            "phase3_artifacts_root": _path_record(phase3_root, "phase3_artifacts_root"),
            "coverage_roots": _record_paths(coverage_roots, "coverage_root"),
            "r6_compatibility_root": _path_record(compatibility_root, "r6_compatibility_root"),
        },
        "discovered_inputs": {
            "package": _path_record(package_path, "r4_package"),
            "blueprints": _record_paths(blueprint_paths, "blueprint"),
            "review_drafts": [
                {**_path_record(path, "review_draft"), "word_count": _word_count(path)}
                for path in sorted(set(review_paths), key=str)
                if path.exists()
            ],
            "scoped_knowledge_bases": [
                _path_record(
                    path,
                    "manifest_scoped_supplemental_kb"
                    if path in supplemental_paths
                    else ("manifest_scoped_base_kb" if path in scoped_base_paths else "manifest_scoped_auxiliary_kb"),
                )
                for path in scoped_kb_paths
            ],
            "base_knowledge_bases": [
                _path_record(
                    path,
                    "base_kb" if path in scoped_base_paths else "effective_base_kb_from_only_scoped_asset",
                    warning=("No separate scoped base KB exists; the only scoped asset is used as the effective base input." if path not in scoped_base_paths else None),
                )
                for path in effective_base_paths
            ],
            "supplemental_knowledge_bases": _record_paths(supplemental_paths, "supplemental_kb"),
            "legacy_declared_base_kb": declared_paths.get("base_kb_sqlite"),
            "staging_knowledge_bases": _record_paths(staging_candidates, "staging_kb"),
            "query_assets": _record_paths(query_paths, "query_asset"),
            "source_permissions": _record_paths(source_permission_paths, "source_permissions"),
            "phase3_claim_artifacts": _record_paths(phase3_claim_paths, "phase3_claim_artifact"),
            "phase3_relation_artifacts": _record_paths(phase3_relation_paths, "phase3_relation_artifact"),
            "technical_audits": _record_paths(sorted(set(technical_audit_paths), key=str), "technical_audit"),
            "quality_reports": _record_paths(quality_report_paths, "quality_report"),
            "visual_plans": _record_paths(sorted(set(visual_plan_paths), key=str), "visual_plan"),
            "review_scope_maps": _record_paths(scope_paths, "review_scope_map"),
            "relation_graphs": _record_paths(relation_paths, "relation_graph"),
            "gap_reports": _record_paths(gap_paths, "gap_report"),
            "compatibility_artifacts": [
                {"role": "r6_compatibility_artifact", "path": path, "exists": True, "is_file": True, "is_directory": False}
                for path in sorted(compatibility_paths.values())
            ],
        },
        "declared_package_paths": declared_paths,
        "source_permission_audit": {
            "counts": permissions,
            "path_count": len(source_permission_paths),
            "authoritative_adapter_path": compatibility_paths.get("R6_SOURCE_PERMISSIONS_ADAPTER.json"),
            "permission_count_unit": permission_artifact.get("permission_count_unit"),
            "paper_permission_counts": permission_artifact.get("paper_permission_counts", {}),
            "ledger_inventory_reconciliation": permission_artifact.get("ledger_inventory_reconciliation", {}),
        },
        "input_routing": {
            "quality_report_path": str(selected_quality_report) if selected_quality_report else None,
            "technical_audit_path": str(selected_technical_audit) if selected_technical_audit else None,
            "visual_plan": visual_plan_validation,
        },
        "ledger_inventory_reconciliation": permission_artifact.get("ledger_inventory_reconciliation", {}),
        "scoped_sqlite_inventory": {
            "database_paths": scoped_index.get("database_paths", []),
            "paper_count": len(scoped_index.get("paper_ids", {})),
            "text_chunk_count": len(scoped_index.get("text_chunks", {})),
            "visual_chunk_count": len(scoped_index.get("visual_chunk_ids", {})),
            "read_errors": scoped_index.get("errors", []),
        },
        "phase3_contract": phase3_contract,
        "compatibility_artifacts": compatibility_paths,
        "candidate_constraints": candidate_constraints,
        "scientific_content_policy": {
            "claims_materialized": False,
            "relations_materialized": False,
            "synthetic_claims_created": 0,
            "synthetic_relations_created": 0,
            "discovery_only_can_support_facts": False,
        },
        "preflight": {
            "required_fields": required,
            "optional_fields": optional,
            "mandatory_missing": mandatory_missing,
            "status": "blocked_honest_stop" if mandatory_missing else "ready_for_r5_context_consumption",
            "missing_field_actions": {key: missing_actions[key] for key in mandatory_missing if key in missing_actions},
            "nonblocking_limitations": nonblocking_limitations,
        },
        "legacy_to_current_policy": {
            "reuse_real_paths": True,
            "use_manifest_scoped_kb_not_unscoped_package_kb": True,
            "preserve_permissions_and_gaps": True,
            "never_upgrade_candidate_to_executed_result": True,
            "never_invent_phase3_claims_or_relations": True,
        },
        "estimated_existing_cost": _cost_from_roots(roots, package),
    }


def _quote(path: str | Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def _command_for_context(
    topic: Mapping[str, Any],
    context: Mapping[str, Any],
    project_root: Path,
    cost_cap_override: float | None = None,
) -> dict[str, Any]:
    plan = topic.get("live_r5_plan") or {}
    budget = float(plan.get("hard_cost_cap_cny", 2.0))
    if cost_cap_override is not None:
        budget = min(budget, float(cost_cap_override))
    token_budget = int(plan.get("token_budget", 120_000))
    max_iters = int(plan.get("max_iters", 8))
    model_tier = str(plan.get("model_tier", "advanced_model"))
    topic_id = str(topic["topic_id"])
    inputs = context.get("discovered_inputs", {})
    def first_file(key: str) -> str | None:
        records = inputs.get(key) or []
        for record in records:
            if record.get("is_file"):
                return record.get("path")
        return None

    def compatibility_file(name: str) -> str | None:
        value = (context.get("compatibility_artifacts") or {}).get(name)
        return str(value) if value else None

    blueprint = first_file("blueprints")
    review = first_file("review_drafts")
    coverage_records = context.get("source_roots", {}).get("coverage_roots") or []
    coverage = coverage_records[0].get("path") if coverage_records else None
    kb_records = inputs.get("scoped_knowledge_bases") or []
    base_kb = kb_records[0].get("path") if kb_records else None
    staging_records = inputs.get("staging_knowledge_bases") or inputs.get("supplemental_knowledge_bases") or []
    staging = next((record.get("path") for record in staging_records if record.get("is_file")), None)
    if staging is None and len(kb_records) > 1:
        staging = kb_records[1].get("path")
    phase3 = (context.get("source_roots", {}).get("r6_compatibility_root") or {}).get("path")
    if not phase3:
        phase3 = (context.get("source_roots", {}).get("phase3_artifacts_root") or {}).get("path")
    routing = context.get("input_routing") or {}
    visual_routing = routing.get("visual_plan") or {}
    visual = visual_routing.get("selected_path") if visual_routing.get("accepted") else None
    quality = routing.get("quality_report_path")
    technical = routing.get("technical_audit_path")
    scope = compatibility_file("REVIEW_SCOPE_MAP.json") or first_file("review_scope_maps")
    permissions = compatibility_file("R6_SOURCE_PERMISSIONS_ADAPTER.json") or first_file("source_permissions")
    relation = compatibility_file("RELATION_GRAPH.json") or first_file("relation_graphs")
    output_dir = project_root / "outputs" / "r6_live_r5" / topic_id
    args: list[str] = [
        "py -3.11 scripts/run_research_program.py",
        "--blueprint", _quote(blueprint) if blueprint else "<MISSING_BLUEPRINT>",
        "--review", _quote(review) if review else "<MISSING_REVIEW_DRAFT>",
        "--coverage-root", _quote(coverage) if coverage else "<MISSING_COVERAGE_ROOT>",
        "--base-kb", _quote(base_kb) if base_kb else "<MISSING_SCOPED_BASE_KB>",
        "--output-dir", _quote(output_dir),
        "--run-id", _quote(f"r6_{topic_id}_r5"),
        "--model-tier", _quote(model_tier),
        "--budget-cny", str(budget),
        "--token-budget", str(token_budget),
        "--max-iters", str(max_iters),
    ]
    optional = [
        ("--phase3-artifacts-root", phase3),
        ("--staging-kb", staging),
        ("--visual-plan", visual),
        ("--quality-report", quality),
        ("--technical-audit", technical),
        ("--review-scope-map", scope),
        ("--source-permissions", permissions),
        ("--relation-graph", relation),
    ]
    for flag, value in optional:
        if value:
            args.extend([flag, _quote(value)])
    command = " ".join(args)
    missing = list(context.get("preflight", {}).get("mandatory_missing") or [])
    resolved_r5_root = (context.get("source_roots", {}).get("r5_root") or {}).get("path")
    return {
        "topic_id": topic_id,
        "enabled": bool(plan.get("enabled", False)),
        "execution_status": "blocked_preflight" if missing else "ready_but_disabled",
        "hard_cost_cap_cny": budget,
        "token_budget": token_budget,
        "max_iters": max_iters,
        "model_tier": model_tier,
        "resolved_r5_root": resolved_r5_root,
        "estimated_cost_range_cny": [0.0, budget],
        "mandatory_missing": missing,
        "quality_report_path": quality,
        "technical_audit_path": technical,
        "visual_plan_routing": visual_routing,
        "command": command,
        "resume_plan_only_command": command + " --resume-plan-only",
        "output_dir": str(output_dir),
        "external_call_policy": {
            "qwen": "not executed by R6; only allowed if a future operator explicitly enables this plan",
            "semantic_scholar": "0 in this R5 continuation plan",
            "downloads": "0 in this R5 continuation plan",
        },
        "safety_note": "This command is recorded for a future operator. R6 does not execute it. The compatibility graph is explicitly empty and current Phase3 claims/relations remain a source limitation.",
    }


def build_live_execution_plan(
    manifest: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
    project_root: Path,
    cost_cap_override: float | None = None,
) -> dict[str, Any]:
    topics_by_id = {str(item["topic_id"]): item for item in manifest.get("topics", [])}
    plans = []
    for context in contexts:
        topic = topics_by_id[context["topic_id"]]
        if not topic.get("live_r5_plan"):
            continue
        plans.append(_command_for_context(topic, context, project_root, cost_cap_override))
    actionable_count = sum(
        1 for item in plans if item.get("execution_status") == "ready_but_disabled"
    )
    return {
        "schema_version": LIVE_PLAN_SCHEMA,
        "mode": "plan_only_offline",
        "generated_by": "R6 deterministic context adapter",
        "external_calls_made": {"qwen": 0, "semantic_scholar": 0, "downloads": 0},
        "manifest_topic_count": len(topics_by_id),
        "command_topic_count": len(plans),
        "actionable_topic_count": actionable_count,
        "topic_count": len(plans),
        "topic_count_semantics": "deprecated_alias_of_command_topic_count",
        "topics": plans,
        "global_policy": {
            "default_enabled": False,
            "hard_cost_cap_required": True,
            "do_not_modify_r5_runtime": True,
            "do_not_use_unscoped_legacy_base_kb": True,
            "do_not_fabricate_phase3_claims_or_relations": True,
        },
    }


def live_plan_markdown(plan: Mapping[str, Any]) -> str:
    lines = [
        "# R6 Live R5 Execution Plan",
        "",
        "This is a plan-only artifact. No command in this file was executed by R6, and no Qwen, Semantic Scholar, OpenAlex, or downloader call was made.",
        "",
        "| Topic | Status | Hard cap (CNY) | Missing preflight fields |",
        "|---|---|---:|---|",
    ]
    for item in plan.get("topics", []):
        missing = ", ".join(item.get("mandatory_missing") or []) or "none"
        lines.append(f"| {item['topic_id']} | {item['execution_status']} | {item['hard_cost_cap_cny']} | {missing} |")
    lines.extend(["", "## Commands", ""])
    for item in plan.get("topics", []):
        lines.extend(
            [
                f"### {item['topic_id']}",
                "",
                f"- Primary command (`{item['execution_status']}`):",
                "",
                "```powershell",
                item["command"],
                "```",
                "",
                "- Plan-only resume command after the focus artifacts exist:",
                "",
                "```powershell",
                item["resume_plan_only_command"],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Safety rules",
            "",
            "- `needs_more_literature` and `awaiting_human_review` remain candidate states.",
            "- Legacy ledgers are inventory and permission evidence only; they do not become a fabricated current relation graph.",
            "- The scoped manifest databases are used instead of broad package-declared databases.",
            "- A future live run must stop at the hard cost cap and preserve its event/cost ledger.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["ADAPTER_SCHEMA", "LIVE_PLAN_SCHEMA", "adapt_topic_context", "build_live_execution_plan", "live_plan_markdown"]
