"""Conservative migration of pre-R1 source ledgers and SQLite assets."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .review_quality_contract import (
    normalize_content_depth,
    normalize_scope_fit,
    permission_for_content,
    source_route_record,
)
from .section_asset_overlay import build_section_asset_overlay


def _json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def _rows(conn: sqlite3.Connection, table: str, wanted: Iterable[str]) -> list[dict[str, Any]]:
    columns = _columns(conn, table)
    selected = [item for item in wanted if item in columns]
    if not selected:
        return []
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM {table}").fetchall()
    return [dict(zip(selected, row)) for row in rows]


_SCOPE_RANK = {
    "out_of_scope": 0,
    "unreviewed": 1,
    "contextual": 2,
    "adjacent": 3,
    "direct": 4,
}


def _merge_json(*values: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for value in values:
        payload = _json(value)
        if payload:
            merged.update(payload)
    return merged


def _route_is_known(value: Any) -> bool:
    return str(value or "").strip().casefold() not in {
        "", "unknown", "legacy_unresolved", "not_materialized"
    }


def _explicit_scope(row: dict[str, Any], raw: dict[str, Any]) -> str:
    source_kind = str(
        row.get("source_kind") or raw.get("source_kind") or ""
    ).casefold()
    raw_scope = raw.get("scope_fit") or raw.get("relevance_scope")
    if raw_scope:
        return normalize_scope_fit(raw_scope)
    if source_kind in {"method_transfer", "cross_domain_analogy"}:
        return "adjacent"
    if source_kind in {"out_of_scope", "off_domain"}:
        return "out_of_scope"
    return ""


def _infer_chunk_route(
    row: dict[str, Any],
    source: dict[str, Any],
    paper_route: dict[str, Any],
) -> dict[str, Any]:
    """Infer one SQLite chunk without confusing discovery and materialization.

    A paper may be found through S2 and later materialized from a PDF/HTML.
    The row's content evidence wins over the paper discovery channel.  A mere
    row in ``text_chunks`` is not enough to promote an abstract to an S2 body
    snippet.
    """

    raw = _merge_json(row.get("provenance_json"), row.get("raw_json"))
    source_raw = _merge_json(source.get("provenance_json"), source.get("raw_json"))
    source_depth = normalize_content_depth(
        source.get("content_depth") or source_raw.get("content_depth"),
        default="",
    )
    row_depth = normalize_content_depth(
        row.get("content_depth") or raw.get("content_depth"),
        default="",
    )
    source_kind = str(row.get("source_kind") or raw.get("source_kind") or "").casefold()
    text_provenance = str(
        row.get("text_provenance") or raw.get("text_provenance") or ""
    ).casefold()
    material_existing = str(
        row.get("materialization_route")
        or raw.get("materialization_route")
        or ""
    ).strip()
    explicit_fulltext = (
        row_depth in {"fulltext", "partial_fulltext"}
        or source_kind in {
            "fulltext", "publisher_html", "pdf", "pdf_pymupdf",
            "html_markdown", "jats_xml", "local_fulltext", "method_transfer",
        }
        or text_provenance.startswith("local_")
        or "fulltext" in material_existing.casefold()
        or "publisher_html" in material_existing.casefold()
        or "pdf" in material_existing.casefold()
    )
    explicit_s2_snippet = (
        row_depth == "structured_snippet"
        or text_provenance in {"s2_body_snippet", "semantic_scholar_body_snippet"}
        or source_kind in {"s2_body_snippet", "structured_snippet"}
        or "s2_structured_snippet" in material_existing.casefold()
    )

    # An explicit abstract on the paper is never upgraded merely because a
    # generic text_chunks row happens to exist.
    if source_depth in {"abstract", "tldr", "metadata"} and not (
        explicit_fulltext or explicit_s2_snippet
    ):
        if source_depth in {"abstract", "tldr"}:
            depth = source_depth
        else:
            depth = "metadata"
    elif explicit_fulltext:
        depth = "fulltext"
    elif explicit_s2_snippet:
        depth = "structured_snippet"
    else:
        depth = "metadata"

    discovery = str(
        row.get("discovery_route")
        or raw.get("discovery_route")
        or paper_route.get("discovery_route")
        or "legacy_unresolved"
    ).strip()
    if not _route_is_known(discovery):
        discovery = "legacy_unresolved"
    if depth == "fulltext":
        materialization = material_existing if _route_is_known(material_existing) else "local_cached_fulltext"
    elif depth == "structured_snippet":
        materialization = "s2_structured_snippet"
    elif depth in {"abstract", "tldr"}:
        materialization = "abstract_fallback"
    else:
        materialization = "not_materialized"

    scope = normalize_scope_fit(
        row.get("scope_fit") or raw.get("scope_fit") or paper_route.get("scope_fit")
    )
    explicit_row_scope = _explicit_scope(row, raw)
    conflicts = list(source.get("metadata_conflicts") or [])
    if explicit_row_scope and explicit_row_scope != scope:
        conflicts.append(
            f"scope_conflict:ledger={scope};chunk={explicit_row_scope}"
        )
        scope = min(
            (scope, explicit_row_scope),
            key=lambda item: _SCOPE_RANK.get(item, 1),
        )
    complete = bool(
        row.get("context_complete")
        if row.get("context_complete") is not None
        else raw.get("context_complete", False)
    )
    if depth == "fulltext":
        complete = True
    elif depth != "structured_snippet":
        complete = False
    permission = permission_for_content(
        depth,
        scope_fit=scope,
        context_complete=complete,
    )
    if discovery == "legacy_unresolved" and depth == "metadata":
        permission = permission_for_content(
            "metadata", scope_fit=scope, context_complete=False
        )
    return {
        **permission,
        "discovery_route": discovery,
        "materialization_route": materialization,
        "context_complete": complete,
        "metadata_conflicts": list(dict.fromkeys(conflicts)),
        "route_provenance": {
            "migration": "r3_2",
            "discovery_route": discovery,
            "materialization_route": materialization,
            "source_kind": source_kind,
            "scope_evidence": explicit_row_scope or "ledger_or_default",
        },
        "legacy_unresolved": discovery == "legacy_unresolved" and depth == "metadata",
    }


def _infer_source_route(source: dict[str, Any], chunk_rows: list[dict[str, Any]]) -> dict[str, Any]:
    existing_depth = normalize_content_depth(source.get("content_depth"), default="")
    existing_discovery = str(source.get("discovery_route") or "").strip()
    existing_materialization = str(source.get("materialization_route") or "").strip()
    scope = normalize_scope_fit(source.get("scope_fit"))
    statuses = " ".join(
        str(source.get(key) or "").casefold()
        for key in ("acquisition_status", "source_kind", "retrieval_backend", "backend")
    )
    raw = _json(source.get("raw_json"))
    raw_text = json.dumps(raw, ensure_ascii=False).casefold()
    matching_chunks = [
        row
        for row in chunk_rows
        if str(row.get("paper_id") or "") == str(source.get("paper_id") or "")
    ]
    unresolved = False
    conflicts = list(source.get("metadata_conflicts") or [])
    chunk_routes = [
        _infer_chunk_route(row, source, {
            "discovery_route": existing_discovery,
            "scope_fit": scope,
        })
        for row in matching_chunks
    ]
    # Materialization is decided from explicit chunk evidence.  An S2
    # discovery marker never outranks a publisher/PDF/fulltext marker.
    if any(item["content_depth"] == "fulltext" for item in chunk_routes):
        depth = "fulltext"
        materialization = "local_cached_fulltext"
        reason = "chunk_explicit_fulltext_materialization"
    elif any(item["content_depth"] == "structured_snippet" for item in chunk_routes):
        depth = "structured_snippet"
        materialization = "s2_structured_snippet"
        reason = "chunk_explicit_s2_body_snippet"
    elif any(
        marker in statuses
        for marker in ("fulltext", "publisher_html", "pdf", "jats_xml")
    ):
        depth = "fulltext"
        materialization = existing_materialization if _route_is_known(existing_materialization) else "local_cached_fulltext"
        reason = "legacy_fulltext_route_marker"
    elif existing_depth in {"abstract", "tldr"} or "abstract" in statuses or source.get("abstract"):
        depth = existing_depth if existing_depth in {"abstract", "tldr"} else "abstract"
        materialization = existing_materialization if _route_is_known(existing_materialization) else "abstract_fallback"
        reason = "legacy_abstract_without_fulltext_or_s2_body_evidence"
    elif existing_depth == "metadata" or source.get("paper_id") or source.get("doi") or source.get("title"):
        depth = "metadata"
        materialization = existing_materialization if _route_is_known(existing_materialization) else "not_materialized"
        reason = "identity_only"
    else:
        depth = "metadata"
        materialization = "not_materialized"
        reason = "no_identity_or_route"

    if _route_is_known(existing_discovery):
        discovery = existing_discovery
    elif "s2" in statuses or "semantic_scholar" in statuses or "semantic_scholar" in raw_text:
        discovery = "s2_legacy_recovered"
    elif depth == "fulltext":
        discovery = "local_prior"
    elif source.get("paper_id") or source.get("doi") or source.get("title"):
        discovery = "legacy_metadata"
    else:
        discovery = "legacy_unresolved"
    unresolved = discovery in {"legacy_unresolved", "legacy_metadata"} and depth == "metadata"

    for item in chunk_routes:
        chunk_scope = normalize_scope_fit(item.get("scope_fit"))
        if chunk_scope != scope:
            conflicts.append(
                f"scope_conflict:ledger={scope};chunk={chunk_scope}"
            )
            scope = min(
                (scope, chunk_scope),
                key=lambda value: _SCOPE_RANK.get(value, 1),
            )
    unresolved = unresolved and not matching_chunks
    record = source_route_record(
        discovery_route=discovery,
        materialization_route=materialization,
        content_depth=depth,
        scope_fit=scope,
        context_complete=(
            depth == "fulltext"
            or any(item.get("context_complete") for item in chunk_routes if item["content_depth"] == "structured_snippet")
        ),
        events=[
            {
                "event": "legacy_migration",
                "reason": reason,
                "legacy_unresolved": unresolved,
            }
        ],
        metadata_conflicts=conflicts,
    )
    if unresolved:
        record["discovery_route"] = "legacy_unresolved"
        record["materialization_route"] = "not_materialized"
        record["content_depth"] = "metadata"
        record["use_permission"] = "discovery_only"
        record["allowed_claim_kinds"] = ["discovery", "candidate_lead"]
        record["legacy_unresolved"] = True
    return record


@dataclass(slots=True)
class LegacyMigrationReport:
    schema_version: str = "research_harness.legacy_migration.v1"
    source_ledger_path: str = ""
    output_ledger_path: str = ""
    papers_seen: int = 0
    papers_updated: int = 0
    chunks_seen: int = 0
    chunks_updated: int = 0
    unresolved_assets: list[dict[str, Any]] = field(default_factory=list)
    route_counts: dict[str, int] = field(default_factory=dict)
    permission_counts: dict[str, int] = field(default_factory=dict)
    migrated_kb_paths: list[str] = field(default_factory=list)
    missing_chunk_ids: list[str] = field(default_factory=list)
    chunk_unavailable_ids: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migrate_sqlite_copy(
    kb_path: Path,
    *,
    output_path: Path,
    sources_by_paper: dict[str, dict[str, Any]],
    routes_by_paper: dict[str, dict[str, Any]],
) -> tuple[int, list[str]]:
    """Copy a KB and write route/permission fields onto its text chunks."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(kb_path)) as source_conn, sqlite3.connect(str(output_path)) as out_conn:
        source_conn.backup(out_conn)
    conn = sqlite3.connect(str(output_path))
    try:
        if "text_chunks" not in {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            return 0, []
        declarations = {
            "discovery_route": "TEXT",
            "materialization_route": "TEXT",
            "content_depth": "TEXT",
            "context_complete": "INTEGER",
            "use_permission": "TEXT",
            "allowed_claim_kinds_json": "TEXT",
            "route_provenance_json": "TEXT",
            "scope_fit": "TEXT",
            "relation_roles_json": "TEXT",
        }
        for name, declaration in declarations.items():
            _ensure_column(conn, "text_chunks", name, declaration)
        columns = _columns(conn, "text_chunks")
        wanted = [
            "rowid", "chunk_id", "paper_id", "source_kind", "content_depth",
            "provenance_json", "raw_json", "discovery_route",
            "materialization_route", "context_complete", "scope_fit",
        ]
        selected = [item for item in wanted if item == "rowid" or item in columns]
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM text_chunks"
        ).fetchall()
        index = {name: idx for idx, name in enumerate(selected)}
        updated = 0
        unavailable: list[str] = []
        for values in rows:
            row = {name: values[idx] for name, idx in index.items()}
            paper_id = str(row.get("paper_id") or "")
            source = sources_by_paper.get(paper_id)
            if not source:
                continue
            route = _infer_chunk_route(
                row,
                source,
                routes_by_paper.get(paper_id) or {},
            )
            allowed = list(route.get("allowed_claim_kinds") or [])
            provenance = _merge_json(
                row.get("provenance_json"), row.get("raw_json")
            )
            provenance.update(route.get("route_provenance") or {})
            set_values = {
                "discovery_route": route["discovery_route"],
                "materialization_route": route["materialization_route"],
                "content_depth": route["content_depth"],
                "context_complete": 1 if route["context_complete"] else 0,
                "use_permission": route["use_permission"],
                "allowed_claim_kinds_json": json.dumps(allowed, ensure_ascii=False),
                "route_provenance_json": json.dumps(provenance, ensure_ascii=False),
                "scope_fit": route["scope_fit"],
            }
            assignments = ", ".join(f"{key}=?" for key in set_values)
            conn.execute(
                f"UPDATE text_chunks SET {assignments} WHERE rowid=?",
                [*set_values.values(), row["rowid"]],
            )
            updated += 1
            if route.get("legacy_unresolved"):
                unavailable.append(str(row.get("chunk_id") or ""))
        conn.commit()
        return updated, [item for item in unavailable if item]
    finally:
        conn.close()


def migrate_source_ledger(
    source_ledger_path: Path,
    *,
    kb_paths: Iterable[Path] = (),
    output_dir: Path | None = None,
) -> tuple[Path, LegacyMigrationReport]:
    """Write a migrated copy; original legacy artifacts are never mutated."""

    source_ledger_path = Path(source_ledger_path)
    kb_paths = [Path(item) for item in kb_paths if item]
    output_dir = Path(output_dir or source_ledger_path.parent / "r1_r3_migration")
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger = _read(source_ledger_path)
    all_chunk_rows: list[dict[str, Any]] = []
    for kb_path in kb_paths:
        if not kb_path or not Path(kb_path).exists():
            continue
        conn = sqlite3.connect(str(kb_path))
        try:
            all_chunk_rows.extend(
                _rows(
                    conn,
                    "text_chunks",
                    [
                        "chunk_id",
                        "paper_id",
                        "source_kind",
                        "content_depth",
                        "provenance_json",
                    ],
                )
            )
        finally:
            conn.close()
    report = LegacyMigrationReport(source_ledger_path=str(source_ledger_path))
    migrated = dict(ledger)
    sources_out: list[dict[str, Any]] = []
    routes_by_paper: dict[str, dict[str, Any]] = {}
    route_counts: dict[str, int] = {}
    permission_counts: dict[str, int] = {}
    unresolved: list[dict[str, Any]] = []
    paper_unresolved_count = 0
    for source in ledger.get("sources") or []:
        if not isinstance(source, dict):
            continue
        report.papers_seen += 1
        row = dict(source)
        route = _infer_source_route(row, all_chunk_rows)
        routes_by_paper[str(row.get("paper_id") or "")] = route
        for key in (
            "discovery_route",
            "materialization_route",
            "content_depth",
            "use_permission",
            "allowed_claim_kinds",
            "context_complete",
            "scope_fit",
            "route_events",
            "metadata_conflicts",
        ):
            row[key] = route[key]
        if route.get("legacy_unresolved"):
            paper_unresolved_count += 1
            unresolved.append(
                {
                    "asset_type": "paper",
                    "paper_id": row.get("paper_id", ""),
                    "title": row.get("title", ""),
                    "reason": "route_cannot_be_confirmed_from_legacy_artifacts",
                }
            )
        route_counts[route["discovery_route"]] = route_counts.get(route["discovery_route"], 0) + 1
        permission_counts[route["use_permission"]] = permission_counts.get(route["use_permission"], 0) + 1
        report.papers_updated += 1
        sources_out.append(row)

    sources_by_paper = {
        str(row.get("paper_id") or ""): row
        for row in sources_out
        if str(row.get("paper_id") or "")
    }
    migrated_kb_paths: list[Path] = []
    chunk_unavailable: list[str] = []
    for index, kb_path in enumerate(
        Path(item) for item in kb_paths if item and Path(item).exists()
    ):
        migrated_kb = output_dir / f"MIGRATED_{index:02d}_{kb_path.stem}.sqlite"
        updated, unavailable = _migrate_sqlite_copy(
            kb_path,
            output_path=migrated_kb,
            sources_by_paper=sources_by_paper,
            routes_by_paper=routes_by_paper,
        )
        report.chunks_updated += updated
        report.chunks_seen += sum(
            1
            for row in all_chunk_rows
            if str(row.get("paper_id") or "") in sources_by_paper
        ) if index == 0 else 0
        chunk_unavailable.extend(unavailable)
        migrated_kb_paths.append(migrated_kb)

    # A ledger may refer to an S2 chunk that is no longer present in any KB.
    # Do not leave it in the authoring allowlist: it is recorded as unavailable
    # and can later be recovered from the persistent S2 cache/API.
    present_chunk_ids: set[str] = set()
    for kb_path in migrated_kb_paths:
        conn = sqlite3.connect(str(kb_path))
        try:
            present_chunk_ids.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT chunk_id FROM text_chunks WHERE chunk_id IS NOT NULL"
                ).fetchall()
            )
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    expected_chunk_ids = {
        str(chunk_id).strip()
        for source in sources_out
        for chunk_id in source.get("canonical_chunk_ids") or []
        if str(chunk_id).strip()
    }
    missing_chunk_ids = sorted(expected_chunk_ids - present_chunk_ids)
    chunk_unavailable.extend(missing_chunk_ids)
    chunk_unavailable = sorted(set(item for item in chunk_unavailable if item))
    if chunk_unavailable:
        for source in sources_out:
            original = list(source.get("canonical_chunk_ids") or [])
            removed = [item for item in original if str(item) in chunk_unavailable]
            if not removed:
                continue
            source["canonical_chunk_ids"] = [
                item for item in original if str(item) not in chunk_unavailable
            ]
            source["unavailable_chunk_ids"] = sorted(
                set(source.get("unavailable_chunk_ids") or []) | set(map(str, removed))
            )
            source.setdefault("route_events", []).append(
                {
                    "event": "chunk_unavailable",
                    "chunk_ids": list(map(str, removed)),
                    "reason": "missing_from_migrated_kb; no fabricated_text_allowed",
                }
            )
        unresolved.extend(
            {
                "asset_type": "text_chunk",
                "chunk_id": chunk_id,
                "reason": "unavailable_after_migration; recovery_required",
            }
            for chunk_id in chunk_unavailable
        )
    migrated["schema_version"] = "research_harness.section_source_ledger.r1_r3.v1"
    migrated["sources"] = sources_out
    migrated["migration"] = {
        "status": "completed_with_audit",
        "original_path": str(source_ledger_path),
        "unresolved_count": len(unresolved),
    }
    output_ledger = output_dir / "MIGRATED_SECTION_SOURCE_LEDGER.json"
    output_ledger.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report.output_ledger_path = str(output_ledger)
    report.unresolved_assets = unresolved
    report.migrated_kb_paths = [str(path) for path in migrated_kb_paths]
    report.missing_chunk_ids = missing_chunk_ids
    report.chunk_unavailable_ids = chunk_unavailable
    report.route_counts = dict(sorted(route_counts.items()))
    report.permission_counts = dict(sorted(permission_counts.items()))
    report.coverage = {
        "route_known_or_conservatively_classified": report.papers_updated - paper_unresolved_count,
        "route_unresolved": paper_unresolved_count,
        "chunk_assets_unavailable": len(chunk_unavailable),
        "coverage_ratio": round(
            (report.papers_updated - paper_unresolved_count) / max(1, report.papers_seen),
            4,
        ),
    }
    report_path = output_dir / "LEGACY_ASSET_MIGRATION_REPORT.json"
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_ledger, report


def migrate_shared_legacy_assets(
    source_ledger_paths: Iterable[Path],
    *,
    kb_paths: Iterable[Path] = (),
    output_dir: Path,
    overlay_dir: Path | None = None,
) -> tuple[Path, LegacyMigrationReport, dict[str, Path], dict[str, Any]]:
    """Migrate a shared KB once and emit lightweight per-section overlays.

    The merged ledger is conservative when the same paper appears in several
    sections: scope and permission keep the least permissive value, while
    section-specific differences are recorded in overlays.  Only the shared
    SQLite copies are materialized; overlays contain IDs and policy metadata.
    """

    ledger_paths = [Path(item) for item in source_ledger_paths if item and Path(item).exists()]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    overlay_dir = Path(overlay_dir or output_dir / "overlays")
    merged_sources: dict[str, dict[str, Any]] = {}
    section_rows: dict[str, list[dict[str, Any]]] = {}
    scope_rank = {"out_of_scope": 0, "unreviewed": 1, "contextual": 2, "adjacent": 3, "direct": 4}
    permission_rank = {
        "discovery_only": 0,
        "background_and_candidate_only": 1,
        "contextual_or_qualified_support": 2,
        "factual_support": 3,
    }
    for path in ledger_paths:
        payload = _read(path)
        section_id = str(payload.get("section_id") or path.parent.name or path.stem)
        rows = [item for item in payload.get("sources") or [] if isinstance(item, dict)]
        section_rows[section_id] = rows
        for source in rows:
            paper_id = str(source.get("paper_id") or "").strip()
            if not paper_id:
                continue
            existing = merged_sources.get(paper_id)
            if existing is None:
                existing = dict(source)
                existing["canonical_chunk_ids"] = _unique(source.get("canonical_chunk_ids") or [])
                merged_sources[paper_id] = existing
                continue
            existing["canonical_chunk_ids"] = _unique(
                list(existing.get("canonical_chunk_ids") or [])
                + list(source.get("canonical_chunk_ids") or [])
            )
            existing["literature_role"] = ", ".join(
                _unique(
                    str(item).strip()
                    for value in (existing.get("literature_role"), source.get("literature_role"))
                    for item in str(value or "").split(",")
                    if item.strip()
                )
            )
            current_scope = normalize_scope_fit(existing.get("scope_fit"))
            incoming_scope = normalize_scope_fit(source.get("scope_fit"))
            if scope_rank.get(incoming_scope, 1) < scope_rank.get(current_scope, 1):
                existing["scope_fit"] = incoming_scope
            current_permission = str(existing.get("use_permission") or "")
            incoming_permission = str(source.get("use_permission") or "")
            if permission_rank.get(incoming_permission, 0) < permission_rank.get(current_permission, 0):
                existing["use_permission"] = incoming_permission
            if current_scope != incoming_scope:
                existing.setdefault("metadata_conflicts", []).append(
                    f"section_scope_conflict:{current_scope}!={incoming_scope}"
                )
    merged_ledger = output_dir / "SHARED_MERGED_SOURCE_LEDGER.json"
    merged_payload = {
        "schema_version": "research_harness.shared_legacy_ledger.r3_3.v1",
        "sources": list(merged_sources.values()),
        "source_section_count": len(section_rows),
        "source_ledger_paths": [str(path) for path in ledger_paths],
    }
    merged_ledger.write_text(json.dumps(merged_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    migrated_ledger, report = migrate_source_ledger(
        merged_ledger,
        kb_paths=[Path(item) for item in kb_paths if item],
        output_dir=output_dir / "shared_migration",
    )
    resolved = {
        str(item.get("paper_id")): item
        for item in _read(migrated_ledger).get("sources") or []
        if str(item.get("paper_id") or "")
    }
    overlay_paths: dict[str, Path] = {}
    for section_id, rows in section_rows.items():
        enriched: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            base = resolved.get(str(row.get("paper_id") or ""), {})
            # Route/depth are global facts recovered from the shared KB.  A
            # section's relevance and writing permission are local judgements
            # and must remain the values recorded by this section.  Falling
            # back to the shared value is allowed only when the section did
            # not record a judgement at all.
            for key in (
                "discovery_route", "materialization_route", "content_depth",
                "allowed_claim_kinds", "context_complete", "metadata_conflicts",
            ):
                if key in base:
                    item[key] = base[key]
            if not str(item.get("scope_fit") or "").strip() and base.get("scope_fit"):
                item["scope_fit"] = base["scope_fit"]
            if not str(item.get("use_permission") or "").strip() and base.get("use_permission"):
                item["use_permission"] = base["use_permission"]
            item["policy_authority"] = "section_overlay"
            enriched.append(item)
        overlay_path = overlay_dir / f"{section_id}.json"
        build_section_asset_overlay(
            section_id=section_id,
            sources=enriched,
            shared_kb_paths=report.migrated_kb_paths,
            output_path=overlay_path,
        )
        overlay_paths[section_id] = overlay_path
    stats = {
        "shared_database_copy_count": len(report.migrated_kb_paths),
        "section_database_copy_count": 0,
        "unique_active_chunks": len(
            {
                str(chunk_id)
                for rows in section_rows.values()
                for row in rows
                for chunk_id in row.get("canonical_chunk_ids") or []
                if str(chunk_id).strip()
            }
        ),
        "section_chunk_references": sum(
            len(row.get("canonical_chunk_ids") or [])
            for rows in section_rows.values()
            for row in rows
        ),
        "rows_updated": report.chunks_updated,
        "overlay_count": len(overlay_paths),
    }
    (output_dir / "SHARED_MIGRATION_STATS.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return migrated_ledger, report, overlay_paths, stats
