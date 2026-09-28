"""Deterministic, read-only cross-topic regression for the R6 milestone.

This module deliberately does not invoke the research harness, an LLM, Semantic
Scholar, OpenAlex, or a downloader.  It consumes existing R4/R5 artifacts and
scoped SQLite assets, computes the same audit shape for every topic, and writes
new reports only under the requested output directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


R6_SCHEMA = "research_harness.r6_cross_topic_acceptance.v1"
MANIFEST_SCHEMA = "research_harness.r6_topic_manifest.v1"
R5_OVERRIDE_SCHEMA = "research_harness.r6_r5_root_overrides.v1"
NONTERMINAL_STATUSES = {
    "awaiting_human_review",
    "needs_more_literature",
    "partial",
    "candidate",
}
R5_SUCCESSFUL_STOP_REASONS = frozenset(
    {
        "all_gates_passed",
        "deterministic_post_validation_passed",
    }
)
KNOWN_PHASE3_FILES = {
    "COVERAGE_ATLAS.json",
    "SYNTHESIS_BUNDLES.json",
    "MATERIAL_BINDINGS.json",
    "CLAIM_GRAPH.json",
    "RELATION_GRAPH_MIGRATED.json",
    "R4_PHASE3_HANDOFF.json",
}
PATH_COLUMN_HINTS = (
    "path",
    "file",
    "image",
    "asset",
    "source",
    "url",
)
TEXT_ID_COLUMNS = ("chunk_id", "text_chunk_id", "id")
VISUAL_ID_COLUMNS = ("visual_chunk_id", "chunk_id", "asset_id", "id")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_load_json(path: Path) -> Any | None:
    try:
        return _load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(values: Iterable[str]) -> str:
    canonical = "\n".join(sorted({str(value) for value in values if value}))
    return _sha256_bytes(canonical.encode("utf-8"))


def _file_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _resolve(root: Path, value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (root / candidate).resolve()


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) for root in roots)


def _r5_root_record(
    topic_id: str,
    requested: Any,
    resolved: Path | None,
    *,
    source: str,
    override_file: Path | None = None,
    gate_path: Path | None = None,
    research_plan_path: Path | None = None,
    research_plan_valid: bool | None = None,
) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "source": source,
        "requested": str(requested) if requested not in (None, "") else None,
        "resolved": str(resolved) if resolved else None,
        "exists": bool(resolved and resolved.is_dir()),
        "override_file": str(override_file) if override_file else None,
        "program_focus_gate_path": str(gate_path) if gate_path else None,
        "research_plan": {
            "path": str(research_plan_path) if research_plan_path else None,
            "present": bool(research_plan_path),
            "valid_json": research_plan_valid,
            "strict_audit": bool(research_plan_path),
        },
    }


def _resolve_r5_roots(
    manifest: Mapping[str, Any],
    project_root: Path,
    override_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Resolve explicit R5 roots; never discover a latest directory implicitly."""
    raw_topics = [item for item in manifest.get("topics", []) if isinstance(item, Mapping)]
    topic_ids = {str(item.get("topic_id")) for item in raw_topics if item.get("topic_id")}
    overrides: Mapping[str, Any] = {}
    override_file: Path | None = None
    if override_path:
        override_file = Path(override_path).resolve()
        payload = _load_json(override_file)
        if not isinstance(payload, Mapping) or payload.get("schema_version") != R5_OVERRIDE_SCHEMA:
            raise ValueError(f"Unsupported R6 R5 override file: {override_file}")
        raw_overrides = payload.get("overrides")
        if not isinstance(raw_overrides, Mapping):
            raise ValueError("R6 R5 override file must contain an object field named 'overrides'")
        unknown = sorted(set(str(key) for key in raw_overrides) - topic_ids)
        if unknown:
            raise ValueError(f"R6 R5 override contains unknown topic IDs: {', '.join(unknown)}")
        overrides = {str(key): value for key, value in raw_overrides.items()}

    allowed_roots = [project_root.resolve(), (project_root / "outputs").resolve()]
    records: dict[str, dict[str, Any]] = {}
    effective_topics: list[dict[str, Any]] = []
    for raw_topic in raw_topics:
        topic_id = str(raw_topic["topic_id"])
        topic = dict(raw_topic)
        if topic_id not in overrides:
            resolved = _resolve(project_root, topic.get("r5_root"))
            records[topic_id] = _r5_root_record(
                topic_id,
                topic.get("r5_root"),
                resolved,
                source="manifest",
            )
            effective_topics.append(topic)
            continue

        requested = overrides[topic_id]
        if not isinstance(requested, (str, Path)) or not str(requested).strip():
            raise ValueError(f"R6 R5 override for {topic_id} must be a non-empty path")
        candidate = Path(requested)
        resolved = (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not _is_within(resolved, allowed_roots):
            raise ValueError(f"R6 R5 override for {topic_id} is outside the project/output scope: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"R6 R5 override for {topic_id} does not exist as a directory: {resolved}")
        gate_path = resolved / "PROGRAM_FOCUS_GATE.json"
        if not gate_path.is_file():
            raise ValueError(f"R6 R5 override for {topic_id} lacks PROGRAM_FOCUS_GATE.json: {resolved}")
        try:
            gate_payload = _load_json(gate_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"R6 R5 override gate is invalid JSON for {topic_id}: {gate_path}") from exc
        if not isinstance(gate_payload, Mapping):
            raise ValueError(f"R6 R5 override gate must be a JSON object for {topic_id}: {gate_path}")

        plan_path = resolved / "RESEARCH_PLAN.json"
        plan_valid: bool | None = None
        if plan_path.exists():
            try:
                plan_payload = _load_json(plan_path)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"R6 strict audit cannot parse RESEARCH_PLAN.json for {topic_id}: {plan_path}") from exc
            if not isinstance(plan_payload, Mapping):
                raise ValueError(f"R6 strict audit requires RESEARCH_PLAN.json to be an object for {topic_id}: {plan_path}")
            plan_valid = True

        topic["r5_root"] = str(resolved)
        records[topic_id] = _r5_root_record(
            topic_id,
            requested,
            resolved,
            source="explicit_override",
            override_file=override_file,
            gate_path=gate_path,
            research_plan_path=plan_path if plan_path.exists() else None,
            research_plan_valid=plan_valid,
        )
        effective_topics.append(topic)

    seen: dict[str, str] = {}
    for topic_id, record in records.items():
        resolved = record.get("resolved")
        if not resolved:
            continue
        key = str(Path(resolved).resolve()).lower()
        previous = seen.get(key)
        if previous and previous != topic_id:
            raise ValueError(f"R6 R5 root is reused across topics: {previous} and {topic_id}: {resolved}")
        seen[key] = topic_id
    return effective_topics, records


def _first(mapping: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _walk_mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk_mappings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_mappings(item)


def _text(value: Any) -> str:
    return str(value).replace("\\", "/").lower() if value is not None else ""


def _looks_like_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    lowered = text.lower().replace("\\", "/")
    return (
        "://" in lowered
        or "/" in lowered
        or "\\" in text
        or lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".pdf", ".sqlite"))
        or lowered.startswith(("c:", "outputs/", "output/", "data/"))
    )


def _column(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    lowered = {str(item).lower(): str(item) for item in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


@dataclass(frozen=True)
class TopicInventory:
    topic_id: str
    db_paths: tuple[Path, ...]
    paper_ids: frozenset[str]
    text_chunk_ids: frozenset[str]
    visual_chunk_ids: frozenset[str]
    path_values: tuple[str, ...]
    database_fingerprints: tuple[dict[str, Any], ...]
    missing_db_paths: tuple[str, ...]

    def report(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "scoped_database_count": len(self.db_paths),
            "missing_database_paths": list(self.missing_db_paths),
            "databases": list(self.database_fingerprints),
            "allowlists": {
                "paper": {
                    "count": len(self.paper_ids),
                    "fingerprint_sha256": _fingerprint(self.paper_ids),
                    "sample": sorted(self.paper_ids)[:5],
                },
                "text_chunk": {
                    "count": len(self.text_chunk_ids),
                    "fingerprint_sha256": _fingerprint(self.text_chunk_ids),
                    "sample": sorted(self.text_chunk_ids)[:5],
                },
                "visual_chunk": {
                    "count": len(self.visual_chunk_ids),
                    "fingerprint_sha256": _fingerprint(self.visual_chunk_ids),
                    "sample": sorted(self.visual_chunk_ids)[:5],
                },
            },
            "path_value_count": len(self.path_values),
            "path_samples": list(self.path_values[:10]),
        }


def _inventory_sqlite(path: Path, topic_id: str) -> TopicInventory:
    papers: set[str] = set()
    text_chunks: set[str] = set()
    visual_chunks: set[str] = set()
    paths: set[str] = set()
    if not path.is_file():
        return TopicInventory(
            topic_id=topic_id,
            db_paths=(path,),
            paper_ids=frozenset(),
            text_chunk_ids=frozenset(),
            visual_chunk_ids=frozenset(),
            path_values=(),
            database_fingerprints=(),
            missing_db_paths=(str(path),),
        )

    table_counts: dict[str, int] = {}
    try:
        connection = _read_only_connection(path)
    except (OSError, sqlite3.Error):
        return TopicInventory(
            topic_id=topic_id,
            db_paths=(path,),
            paper_ids=frozenset(),
            text_chunk_ids=frozenset(),
            visual_chunk_ids=frozenset(),
            path_values=(),
            database_fingerprints=(),
            missing_db_paths=(f"unreadable:{path}",),
        )
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, target, candidates in (
            ("papers", papers, ("paper_id", "id", "paperId")),
            ("text_chunks", text_chunks, TEXT_ID_COLUMNS),
            ("visual_chunks", visual_chunks, VISUAL_ID_COLUMNS),
        ):
            if table not in tables:
                continue
            columns = _table_columns(connection, table)
            id_column = _column(columns, candidates)
            if id_column is None:
                continue
            path_columns = [
                column
                for column in columns
                if any(hint in column.lower() for hint in PATH_COLUMN_HINTS)
            ]
            select_columns = [id_column] + [c for c in path_columns if c != id_column]
            quoted = ", ".join(f'"{column}"' for column in select_columns)
            for row in connection.execute(f'SELECT {quoted} FROM "{table}"'):
                if row[0] not in (None, ""):
                    target.add(str(row[0]))
                for value in row[1:]:
                    if _looks_like_path(value):
                        paths.add(str(value))
            table_counts[table] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
        if "visual_chunks" not in tables and "visual_assets" in tables:
            columns = _table_columns(connection, "visual_assets")
            id_column = _column(columns, ("asset_id", "visual_chunk_id", "id"))
            if id_column:
                for row in connection.execute(f'SELECT "{id_column}" FROM "visual_assets"'):
                    if row[0] not in (None, ""):
                        visual_chunks.add(str(row[0]))
                table_counts["visual_assets"] = int(
                    connection.execute('SELECT COUNT(*) FROM "visual_assets"').fetchone()[0]
                )
    finally:
        connection.close()

    return TopicInventory(
        topic_id=topic_id,
        db_paths=(path,),
        paper_ids=frozenset(papers),
        text_chunk_ids=frozenset(text_chunks),
        visual_chunk_ids=frozenset(visual_chunks),
        path_values=tuple(sorted(paths)),
        database_fingerprints=(
            {
                "path": str(path),
                "sha256": _file_fingerprint(path),
                "table_counts": table_counts,
            },
        ),
        missing_db_paths=(),
    )


def build_topic_inventory(topic: Mapping[str, Any], project_root: Path) -> TopicInventory:
    topic_id = str(topic["topic_id"])
    paths: list[Path] = []
    for raw_path in topic.get("scoped_kb_paths", []):
        resolved = _resolve(project_root, raw_path)
        if resolved is not None:
            paths.append(resolved)
    paper_ids: set[str] = set()
    text_ids: set[str] = set()
    visual_ids: set[str] = set()
    path_values: set[str] = set()
    fingerprints: list[dict[str, Any]] = []
    missing: list[str] = []
    for db_path in paths:
        item = _inventory_sqlite(db_path, topic_id)
        paper_ids.update(item.paper_ids)
        text_ids.update(item.text_chunk_ids)
        visual_ids.update(item.visual_chunk_ids)
        path_values.update(item.path_values)
        fingerprints.extend(item.database_fingerprints)
        missing.extend(item.missing_db_paths)
    return TopicInventory(
        topic_id=topic_id,
        db_paths=tuple(paths),
        paper_ids=frozenset(paper_ids),
        text_chunk_ids=frozenset(text_ids),
        visual_chunk_ids=frozenset(visual_ids),
        path_values=tuple(sorted(path_values)),
        database_fingerprints=tuple(fingerprints),
        missing_db_paths=tuple(missing),
    )


def _artifact_files(
    root: Path | None,
    limit: int = 1200,
    extensions: set[str] | None = None,
) -> list[Path]:
    if root is None or not root.exists():
        return []
    if root.is_file():
        return [root]
    result: list[Path] = []
    wanted_extensions = extensions or {".json", ".jsonl", ".md"}
    try:
        candidates: Iterator[Path]
        if len(wanted_extensions) == 1:
            candidates = root.rglob(f"*{next(iter(wanted_extensions))}")
        else:
            paths: list[Path] = []
            for extension in sorted(wanted_extensions):
                paths.extend(root.rglob(f"*{extension}"))
            candidates = iter(paths)
        seen: set[Path] = set()
        for path in candidates:
            if len(result) >= limit:
                break
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file() or path.suffix.lower() not in wanted_extensions:
                continue
            parts = {part.lower() for part in path.parts}
            if "_runtime_archive" in parts or "__pycache__" in parts:
                continue
            try:
                if path.stat().st_size > 12 * 1024 * 1024:
                    continue
            except OSError:
                continue
            result.append(path)
    except OSError:
        return result
    return result


def _read_artifact_text(path: Path, maximum: int = 8 * 1024 * 1024) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(maximum)
    except OSError:
        return ""


def _known_phase3_status(root: Path | None) -> dict[str, Any]:
    if root is None or not root.exists():
        return {"status": "missing", "files": [], "section_ledger_count": 0}
    direct = [p for p in _artifact_files(root, limit=250) if p.name in KNOWN_PHASE3_FILES]
    section_ledgers = [p for p in _artifact_files(root, limit=500) if p.name == "SECTION_SOURCE_LEDGER.json"]
    if direct:
        return {
            "status": "available",
            "files": [str(p) for p in direct],
            "section_ledger_count": len(section_ledgers),
        }
    if section_ledgers:
        return {
            "status": "legacy_section_ledgers_only",
            "files": [],
            "section_ledger_count": len(section_ledgers),
        }
    return {"status": "missing", "files": [], "section_ledger_count": 0}


def _recursive_counts(value: Any, key_predicate: Any, value_predicate: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mapping in _walk_mappings(value):
        for key, item in mapping.items():
            if key_predicate(str(key)) and value_predicate(item):
                label = str(item).strip().lower()
                counts[label] = counts.get(label, 0) + 1
    return counts


def _permission_counts(roots: Sequence[Path | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for root in roots:
        for path in _artifact_files(root, limit=450):
            if path.suffix.lower() == ".md":
                continue
            value = _safe_load_json(path)
            if value is None:
                continue
            found = _recursive_counts(
                value,
                lambda key: any(token in key.lower() for token in ("permission", "use_permission", "evidence_level")),
                lambda item: isinstance(item, str),
            )
            for key, count in found.items():
                counts[key] = counts.get(key, 0) + count
    return dict(sorted(counts.items()))


def _find_json(root: Path | None, names: Sequence[str]) -> Path | None:
    if root is None:
        return None
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for path in _artifact_files(root, limit=1600):
        if path.name in names:
            return path
    return None


_ACCOUNTING_FIELDS = (
    "model_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "estimated_cost_cny",
    "wall_time_seconds",
)


def _accounting_numbers(value: Mapping[str, Any] | None) -> dict[str, float]:
    value = value or {}
    result: dict[str, float] = {}
    for field in _ACCOUNTING_FIELDS:
        raw = value.get(field, 0)
        try:
            result[field] = float(raw or 0)
        except (TypeError, ValueError):
            result[field] = 0.0
    return result


def _sum_accounting_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    total = {field: 0.0 for field in _ACCOUNTING_FIELDS}
    for row in rows:
        numbers = _accounting_numbers(row)
        for field in _ACCOUNTING_FIELDS:
            total[field] += numbers[field]
    return total


def _phase_accounting_cost(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    schema = str(value.get("schema_version") or "")
    lifetime = value.get("lifetime_total")
    if isinstance(lifetime, Mapping):
        numbers = _accounting_numbers(lifetime)
        return {
            "estimated_cost_cny": numbers["estimated_cost_cny"],
            "input_tokens": int(numbers["input_tokens"]),
            "output_tokens": int(numbers["output_tokens"]),
            "model_calls": int(numbers["model_calls"]),
            "tool_calls": int(numbers["tool_calls"]),
            "wall_time_seconds": numbers["wall_time_seconds"],
            "source": str(path),
            "source_kind": "r5_phase_accounting_lifetime_total",
            "accounting_schema": schema,
            "accounting_precision": "authoritative_lifetime_total",
            "historical_ambiguity": False,
            "deduplication": {"applied": False, "reason": "authoritative_lifetime_total_present"},
        }

    phases = value.get("phases")
    phases = phases if isinstance(phases, Mapping) else {}
    run_rows: dict[str, dict[str, Any]] = {}
    duplicate_run_ids: list[str] = []
    conflicting_run_ids: list[str] = []
    anonymous_rows: list[dict[str, Any]] = []
    phase_totals: list[Mapping[str, Any]] = []
    for phase_name, raw_phase in phases.items():
        if not isinstance(raw_phase, Mapping):
            continue
        totals = raw_phase.get("totals")
        if isinstance(totals, Mapping):
            phase_totals.append(totals)
        runs = raw_phase.get("runs")
        if not isinstance(runs, list):
            continue
        for index, raw_run in enumerate(runs):
            if not isinstance(raw_run, Mapping):
                continue
            run = dict(raw_run)
            run_id = str(run.get("run_id") or "").strip()
            if not run_id:
                anonymous_rows.append(run)
                continue
            previous = run_rows.get(run_id)
            if previous is None:
                run_rows[run_id] = run
                continue
            duplicate_run_ids.append(run_id)
            previous_numbers = _accounting_numbers(previous)
            current_numbers = _accounting_numbers(run)
            if previous_numbers != current_numbers:
                conflicting_run_ids.append(run_id)
                # A repeated run ID cannot be counted twice safely. Retain a
                # fieldwise maximum as a conservative deduplicated estimate
                # and mark the result as historically ambiguous.
                merged = dict(previous)
                for field in _ACCOUNTING_FIELDS:
                    merged[field] = max(previous_numbers[field], current_numbers[field])
                run_rows[run_id] = merged

    # A v1 ledger without a lifetime total cannot prove that phase totals are
    # mutually exclusive across resumed runs.  Even when every observed run
    # ID is unique, expose that historical limitation instead of presenting
    # the reconstructed sum as authoritative.
    ambiguity_reasons: list[str] = ["lifetime_total_missing"]
    if conflicting_run_ids:
        ambiguity_reasons.append("repeated_run_ids_have_conflicting_accounting")
    if anonymous_rows:
        ambiguity_reasons.append("run_rows_without_run_id")
    if run_rows or anonymous_rows:
        numbers = _sum_accounting_rows([*run_rows.values(), *anonymous_rows])
        precision = "deduplicated_run_estimate"
    else:
        numbers = _sum_accounting_rows(phase_totals)
        precision = "phase_total_estimate"
        ambiguity_reasons.append("no_run_level_accounting_available")
    if not phases:
        ambiguity_reasons.append("phase_accounting_has_no_phases")
    return {
        "estimated_cost_cny": numbers["estimated_cost_cny"],
        "input_tokens": int(numbers["input_tokens"]),
        "output_tokens": int(numbers["output_tokens"]),
        "model_calls": int(numbers["model_calls"]),
        "tool_calls": int(numbers["tool_calls"]),
        "wall_time_seconds": numbers["wall_time_seconds"],
        "source": str(path),
        "source_kind": "r5_phase_accounting_without_lifetime_total",
        "accounting_schema": schema,
        "accounting_precision": precision,
        "historical_ambiguity": True,
        "ambiguity_reasons": list(dict.fromkeys(ambiguity_reasons)),
        "deduplication": {
            "applied": True,
            "unique_run_count": len(run_rows),
            "anonymous_run_count": len(anonymous_rows),
            "duplicate_run_ids": sorted(set(duplicate_run_ids)),
            "conflicting_run_ids": sorted(set(conflicting_run_ids)),
        },
    }


def _cost_from_roots(roots: Sequence[Path | None], package: Mapping[str, Any] | None = None) -> dict[str, Any]:
    package = package or {}
    for root in roots:
        path = _find_json(root, ("R5_PHASE_ACCOUNTING.json",))
        value = _safe_load_json(path) if path else None
        if isinstance(value, Mapping):
            return _phase_accounting_cost(path, value)

    cost = {
        "estimated_cost_cny": _first(package, "total_cost_cny", "estimated_cost_cny", default=0.0),
        "input_tokens": _first(package, "total_input_tokens", "input_tokens", default=0),
        "output_tokens": _first(package, "total_output_tokens", "output_tokens", default=0),
        "source": "package",
        "source_kind": "package_or_single_run_cost",
        "accounting_precision": "reported_artifact",
        "historical_ambiguity": False,
    }
    for root in roots:
        path = _find_json(root, ("HARNESS_COST.json", "COST.json", "HARNESS_METRICS.json"))
        value = _safe_load_json(path) if path else None
        if not isinstance(value, Mapping):
            continue
        cost["estimated_cost_cny"] = _first(
            value,
            "total_cost_cny",
            "estimated_cost_cny",
            "total_cost",
            default=cost["estimated_cost_cny"],
        )
        cost["input_tokens"] = _first(value, "total_input_tokens", "input_tokens", default=cost["input_tokens"])
        cost["output_tokens"] = _first(value, "total_output_tokens", "output_tokens", default=cost["output_tokens"])
        cost["source"] = str(path)
        break
    return cost


def _reuse_count(roots: Sequence[Path | None]) -> int:
    count = 0
    for root in roots:
        for path in _artifact_files(root, limit=600):
            if path.suffix.lower() == ".md":
                continue
            value = _safe_load_json(path)
            if value is None:
                continue
            for mapping in _walk_mappings(value):
                for key, item in mapping.items():
                    normalized = str(key).lower()
                    if normalized in {"reused", "deterministic_replay", "cache_hit", "reused_stage"} and item is True:
                        count += 1
                    elif normalized in {"reuse_count", "reused_count"} and isinstance(item, (int, float)):
                        count += int(item)
    return count


def _count_nested_items(value: Any, names: Sequence[str]) -> int:
    wanted = {name.lower() for name in names}
    total = 0
    for mapping in _walk_mappings(value):
        for key, item in mapping.items():
            if str(key).lower() in wanted and isinstance(item, (list, tuple, Mapping)):
                total += len(item)
    return total


def _audit_visual(root: Path | None, package: Mapping[str, Any] | None) -> dict[str, Any]:
    candidates: list[Path] = []
    if isinstance(package, Mapping):
        raw = _first(package, "final_visual_package_path", "visual_package_path")
        if raw:
            path = Path(str(raw))
            candidates.append(path if path.is_absolute() else ((root or Path.cwd()) / path))
    if root is not None:
        candidates.extend(
            [
                root / "visual_editor" / "final" / "FINAL_VISUAL_PACKAGE.json",
                root / "visual_editor" / "final" / "FINAL_VISUAL_AUDIT_REPORT.json",
            ]
        )
    visual_path = next((path for path in candidates if path.is_file()), None)
    value = _safe_load_json(visual_path) if visual_path else None
    if not isinstance(value, Mapping):
        return {
            "status": "missing_or_unreadable",
            "path": str(visual_path) if visual_path else None,
            "placement_count": 0,
            "conceptual_request_count": 0,
            "unfilled_count": 0,
            "existing_asset_count": 0,
        }
    return {
        "status": str(_first(value, "status", "visual_status", "audit_status", default="available")),
        "path": str(visual_path),
        "placement_count": _count_nested_items(value, ("visual_placements", "placements", "existing_visuals")),
        "conceptual_request_count": _count_nested_items(value, ("conceptual_requests", "generated_requests")),
        "unfilled_count": _count_nested_items(value, ("unfilled", "unfilled_needs", "missing_visuals")),
        "existing_asset_count": _count_nested_items(value, ("assets", "visual_assets", "selected_assets")),
    }


def audit_r4_topic(topic: Mapping[str, Any], project_root: Path) -> dict[str, Any]:
    r4_root = _resolve(project_root, topic.get("r4_root"))
    phase3_root = _resolve(project_root, topic.get("phase3_artifacts_root"))
    package_path = _resolve(r4_root or project_root, topic.get("expected_r4_package"))
    if package_path is None or not package_path.is_file():
        package_path = _find_json(r4_root, ("REVIEW_CONTENT_PACKAGE.json",))
    package = _safe_load_json(package_path) if package_path else None
    package = package if isinstance(package, Mapping) else {}
    raw_status = str(_first(package, "status", default="missing"))
    if raw_status in NONTERMINAL_STATUSES:
        classification = "candidate_not_failure_or_success"
    elif raw_status == "completed":
        classification = "completed_candidate"
    elif raw_status == "missing":
        classification = "missing"
    else:
        classification = "failed_or_incomplete"
    quality = _first(package, "quality_summary", "quality_gate", default={})
    if not isinstance(quality, Mapping):
        quality = {"value": quality}
    phase3 = _known_phase3_status(phase3_root)
    visual = _audit_visual(r4_root, package)
    artifact_roots = [r4_root, phase3_root]
    return {
        "status": raw_status,
        "status_classification": classification,
        "quality": dict(quality),
        "package_path": str(package_path) if package_path else None,
        "section_count": _count_nested_items(package, ("sections", "section_status", "stage_status")),
        "word_count": _first(package, "word_count", "total_words", default=None),
        "phase3_contract": phase3,
        "visual": visual,
        "permissions": {
            "counts": _permission_counts(artifact_roots),
            "source_roots": [str(path) for path in artifact_roots if path],
        },
        "cost": _cost_from_roots(artifact_roots, package),
        "reuse_count": _reuse_count(artifact_roots),
        "stop_reason": _first(
            package,
            "stop_reason",
            "failure_reason",
            "status_reason",
            default=("nonterminal_candidate" if raw_status in NONTERMINAL_STATUSES else raw_status),
        ),
        "artifact_root_exists": bool(r4_root and r4_root.exists()),
    }


def _contains_future_branch_leak(value: Any, future_ids: set[str], path: str = "") -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    if isinstance(value, str):
        if value in future_ids:
            leaks.append({"future_id": value, "path": path})
    elif isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            leaks.extend(_contains_future_branch_leak(item, future_ids, next_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            leaks.extend(_contains_future_branch_leak(item, future_ids, f"{path}[{index}]"))
    return leaks


def _find_discovery_fact_leaks(value: Any, path: str = "") -> list[dict[str, str]]:
    leaks: list[dict[str, str]] = []
    for mapping in _walk_mappings(value):
        permission = _first(mapping, "permission", "use_permission", "source_permission", "evidence_permission")
        if str(permission).lower() != "discovery_only":
            continue
        keys = {str(key).lower() for key in mapping}
        fact_like = any(
            any(token in key for token in ("fact", "evidence", "support", "premise", "established"))
            for key in keys
        )
        if fact_like or mapping.get("used_as_fact") is True or mapping.get("supports_claim") is True:
            leaks.append({"path": path or "nested_record", "keys": ",".join(sorted(keys))})
    return leaks


def _find_verification_violations(value: Any, path: str = "") -> tuple[list[str], list[str]]:
    deferred: list[str] = []
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            key_lower = str(key).lower()
            if key_lower in {"verification_status", "execution_status", "result_status", "status"}:
                status = str(item).lower() if isinstance(item, str) else ""
                if "verification_deferred" in status:
                    deferred.append(next_path)
                elif key_lower != "status" and status in {"planned", "not_run", "pending", "unrun", "deferred"}:
                    violations.append(next_path)
            child_deferred, child_violations = _find_verification_violations(item, next_path)
            deferred.extend(child_deferred)
            violations.extend(child_violations)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child_deferred, child_violations = _find_verification_violations(item, f"{path}[{index}]")
            deferred.extend(child_deferred)
            violations.extend(child_violations)
    return deferred, violations


def _id_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _execution_future_branch_leaks(
    plan: Mapping[str, Any],
    future_hypothesis_ids: set[str],
    future_opportunity_ids: set[str],
) -> list[dict[str, str]]:
    """Check only executable plan scopes, not the future-branch registry itself."""
    forbidden = future_hypothesis_ids | future_opportunity_ids
    leaks: list[dict[str, str]] = []
    for field in ("work_packages", "traceability_matrix", "experiments"):
        if field in plan:
            leaks.extend(_contains_future_branch_leak(plan.get(field), forbidden, field))
    return leaks


def _plan_id_consistency(gate: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    gate_hypotheses = _id_values(gate.get("main_hypothesis_ids"))
    gate_opportunities = _id_values(gate.get("selected_opportunity_ids"))
    plan_hypotheses = _id_values(plan.get("main_hypothesis_ids"))
    work_package_hypotheses: set[str] = set()
    work_package_opportunities: set[str] = set()
    work_packages = plan.get("work_packages")
    if isinstance(work_packages, list):
        for item in work_packages:
            if not isinstance(item, Mapping):
                continue
            work_package_hypotheses.update(
                _id_values(item.get("hypothesis_ids") or item.get("hypothesis_id"))
            )
            work_package_opportunities.update(
                _id_values(item.get("opportunity_ids") or item.get("opportunity_id"))
            )
    errors: list[str] = []
    if not gate_hypotheses:
        errors.append("focus_gate_main_hypothesis_ids_missing")
    if not gate_opportunities:
        errors.append("focus_gate_selected_opportunity_ids_missing")
    if gate_hypotheses != plan_hypotheses:
        errors.append("focus_plan_main_hypothesis_ids_mismatch")
    if gate_hypotheses != work_package_hypotheses:
        errors.append("focus_work_package_hypothesis_ids_mismatch")
    if gate_opportunities != work_package_opportunities:
        errors.append("focus_work_package_opportunity_ids_mismatch")
    return {
        "passed": not errors,
        "errors": errors,
        "focus_main_hypothesis_ids": sorted(gate_hypotheses),
        "plan_main_hypothesis_ids": sorted(plan_hypotheses),
        "work_package_hypothesis_ids": sorted(work_package_hypotheses),
        "focus_selected_opportunity_ids": sorted(gate_opportunities),
        "work_package_opportunity_ids": sorted(work_package_opportunities),
    }


def _deferred_item(item: Any) -> bool:
    if isinstance(item, str):
        return "verification_deferred" in item.casefold()
    if isinstance(item, Mapping):
        return str(item.get("verification_status") or "").casefold() == "verification_deferred"
    return False


_EXECUTED_RESULT_PATTERNS = (
    ("first_person_past_result", re.compile(r"\bwe\s+(?:measured|achieved|observed|found|demonstrated|validated)\b", re.I)),
    ("direct_results_claim", re.compile(r"\b(?:the\s+)?results?\s+(?:show|shows|showed|demonstrate|demonstrates|demonstrated|indicate|indicates|indicated)\b", re.I)),
    ("completed_test", re.compile(r"\bcompleted\s+(?:the\s+|an?\s+)?(?:experiment|simulation|measurement|study|test|trial)\b", re.I)),
    ("passive_past_result", re.compile(r"\b(?:was|were|has\s+been|have\s+been)\s+(?:measured|achieved|observed|demonstrated|validated)\b", re.I)),
    ("statistical_result", re.compile(r"\bstatistically\s+significant\b", re.I)),
)
_FUTURE_RESULT_CUE = re.compile(
    r"\b(?:expect|expected|anticipate|anticipated|project|projected|hypothesi[sz]e|hypothesized|"
    r"will|would|could|may|might|should|target|proposed|planned|if|unless|stop|pivot)\b",
    re.I,
)


def _scalar_paths(value: Any, path: str) -> Iterator[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            yield from _scalar_paths(item, next_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _scalar_paths(item, f"{path}[{index}]")
    else:
        yield path, value


def _executed_result_language_violations(plan: Mapping[str, Any]) -> list[str]:
    """Find claims that present planned R5 work as already executed.

    The scan is deliberately limited to result-bearing plan scopes.  Future or
    proposed outcomes remain valid under the global verification-deferred
    contract; the audit targets past-tense execution claims, not scientific
    expectations.
    """
    scopes = (
        "work_packages",
        "experiments",
        "expected_results",
        "results",
        "result_summary",
        "experimental_results",
        "simulation_results",
    )
    violations: list[str] = []
    for scope in scopes:
        if scope not in plan:
            continue
        for path, value in _scalar_paths(plan.get(scope), scope):
            if not isinstance(value, str) or not value.strip():
                continue
            leaf_key = path.rsplit(".", 1)[-1].split("[", 1)[0].casefold()
            if leaf_key in {"execution_status", "verification_status", "result_status", "experiment_status"}:
                if value.casefold() in {"executed", "completed", "verified", "measured", "observed"}:
                    violations.append(f"{path}:executed_status")
            future_cued = bool(_FUTURE_RESULT_CUE.search(value))
            for label, pattern in _EXECUTED_RESULT_PATTERNS:
                if not pattern.search(value):
                    continue
                # "A statistically significant improvement is expected" is
                # a planned outcome, unlike "we observed a statistically
                # significant improvement" (caught by the past-result rule).
                if label == "statistical_result" and future_cued:
                    continue
                violations.append(f"{path}:{label}")
    return list(dict.fromkeys(violations))


def _plan_verification_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    deferred_paths, _legacy_violations = _find_verification_violations(plan)
    violations: list[str] = []
    executed_result_violations: list[str] = []
    work_packages = plan.get("work_packages")
    work_packages = work_packages if isinstance(work_packages, list) else []
    for index, item in enumerate(work_packages):
        if not isinstance(item, Mapping):
            violations.append(f"work_packages[{index}]_invalid")
            continue
        status = str(item.get("verification_status") or "").casefold()
        if status != "verification_deferred":
            violations.append(f"work_packages[{index}].verification_status_not_deferred")
            if status in {"executed", "completed", "verified", "measured", "observed"}:
                executed_result_violations.append(f"work_packages[{index}].verification_status={status}")

    results_status = str(plan.get("results_status") or "").casefold()
    if results_status != "verification_deferred":
        violations.append("results_status_not_verification_deferred")
        if results_status in {"executed", "completed", "verified", "measured", "observed"}:
            executed_result_violations.append(f"results_status={results_status}")
    # The global result status plus every work package's deferred status is the
    # authoritative execution contract. Planned experiments and expected
    # outcomes do not need to repeat the marker item by item.
    executed_result_violations.extend(_executed_result_language_violations(plan))

    executed_fields = {
        "executed_results",
        "actual_results",
        "observed_results",
        "measured_results",
        "validated_results",
    }
    for mapping in _walk_mappings(plan):
        for key, item in mapping.items():
            if str(key).casefold() not in executed_fields:
                continue
            if item not in (None, "", [], {}):
                executed_result_violations.append(str(key))
    violations.extend(f"executed_result_present:{item}" for item in executed_result_violations)
    violations = list(dict.fromkeys(violations))
    return {
        "passed": not violations,
        "verification_deferred_paths": sorted(set(deferred_paths)),
        "violations": violations,
        "executed_result_violations": list(dict.fromkeys(executed_result_violations)),
        "language_audit_scope": [
            "work_packages",
            "experiments",
            "expected_results",
            "results",
            "result_summary",
            "experimental_results",
            "simulation_results",
        ],
        "work_package_count": len(work_packages),
        "work_packages_deferred": bool(work_packages) and all(
            isinstance(item, Mapping)
            and str(item.get("verification_status") or "").casefold() == "verification_deferred"
            for item in work_packages
        ),
        "results_status": results_status or None,
    }


def _artifact_status(path: Path | None) -> tuple[Mapping[str, Any], str | None]:
    value = _safe_load_json(path) if path else None
    if not isinstance(value, Mapping):
        return {}, None
    return value, str(value.get("status") or "").casefold() or None


def audit_r5_topic(topic: Mapping[str, Any], project_root: Path, r4_report: Mapping[str, Any]) -> dict[str, Any]:
    r5_root = _resolve(project_root, topic.get("r5_root"))
    gate_path = _resolve(r5_root or project_root, topic.get("expected_r5_focus_gate"))
    if gate_path is None or not gate_path.is_file():
        gate_path = _find_json(r5_root, ("PROGRAM_FOCUS_GATE.json",))
    gate = _safe_load_json(gate_path) if gate_path else None
    gate = gate if isinstance(gate, Mapping) else None
    plan_path = _find_json(r5_root, ("RESEARCH_PLAN.json",))
    raw_plan = _safe_load_json(plan_path) if plan_path else None
    plan = raw_plan if isinstance(raw_plan, Mapping) else {}
    plan_md_path = _find_json(r5_root, ("RESEARCH_PLAN.md",))
    plan_audit_path = _find_json(r5_root, ("RESEARCH_PLAN_AUDIT.json",))
    plan_audit, plan_audit_status = _artifact_status(plan_audit_path)
    cleanup_audit_path = _find_json(
        r5_root,
        ("RESEARCH_PLAN_CLEANUP_AUDIT.json", "CLEANUP_AUDIT.json"),
    )
    cleanup_audit, cleanup_audit_status = _artifact_status(cleanup_audit_path)
    result_path = _find_json(r5_root, ("RESULT.json",))
    result, result_status = _artifact_status(result_path)
    shared_path = _find_json(r5_root, ("PROGRAM_SHARED_CONTEXT.json",))
    shared = _safe_load_json(shared_path) if shared_path else {}
    shared = shared if isinstance(shared, Mapping) else {}
    reconciliation_path = _find_json(r5_root, ("R5_RECONCILIATION.json",))
    reconciliation = _safe_load_json(reconciliation_path) if reconciliation_path else {}
    reconciliation = reconciliation if isinstance(reconciliation, Mapping) else {}
    if gate is None:
        status = "missing_formal_focus_gate"
        next_step = "Run the current production R5 focus-gate path for this topic before comparing R6 program quality."
        main_problem_count = 0
        main_hypothesis_count = 0
        future_ids: set[str] = set()
        future_branches: list[Any] = []
    else:
        status = str(_first(gate, "status", default="unknown"))
        main_problem = _first(gate, "main_problem", default=None)
        main_problem_count = int(
            _first(gate, "main_problem_count", default=1 if main_problem else 0) or 0
        )
        main_ids = _first(gate, "main_hypothesis_ids", default=[])
        main_hypothesis_count = len(main_ids) if isinstance(main_ids, list) else 0
        future_ids = {
            str(item)
            for item in (_first(gate, "future_hypothesis_ids", default=[]) or [])
            if item
        }
        future_branches = _first(gate, "future_branches", default=[]) or []
        next_step = "Resume or refine the bounded research-plan stage only after preserving this gate."
    future_opportunity_ids = {
        str(item.get("opportunity_id") or item.get("id") or "").strip()
        for item in future_branches
        if isinstance(item, Mapping) and (item.get("opportunity_id") or item.get("id"))
    }
    future_leaks = _execution_future_branch_leaks(plan, future_ids, future_opportunity_ids)
    discovery_leaks = _find_discovery_fact_leaks(plan)
    verification_contract = _plan_verification_contract(plan)
    deferred = verification_contract["verification_deferred_paths"]
    verification_violations = verification_contract["violations"]
    shared_limitations = _first(shared, "r4_candidate_limitations", default=[])
    shared_limitations = shared_limitations if isinstance(shared_limitations, list) else []
    plan_limitations = plan.get("source_limitations")
    plan_limitations = plan_limitations if isinstance(plan_limitations, list) else []
    source_context = plan.get("source_context")
    source_context = source_context if isinstance(source_context, Mapping) else {}
    contextual_limitations = _first(source_context, "r4_candidate_limitations", "limitations", default=[])
    contextual_limitations = contextual_limitations if isinstance(contextual_limitations, list) else []
    limitation_count = len(shared_limitations) + len(plan_limitations) + len(contextual_limitations)
    candidate_status = r4_report.get("status") in {"awaiting_human_review", "needs_more_literature", "partial"}
    limitation_audit = {
        "r4_candidate": candidate_status,
        "limitation_count": limitation_count,
        "preserved": (not candidate_status) or limitation_count > 0,
        "sources": {
            "program_shared_context": len(shared_limitations),
            "research_plan_source_limitations": len(plan_limitations),
            "research_plan_source_context": len(contextual_limitations),
        },
    }
    id_consistency = _plan_id_consistency(gate or {}, plan)
    result_stop_reason = str(result.get("stop_reason") or "").casefold()
    result_successful_stop = result_stop_reason in R5_SUCCESSFUL_STOP_REASONS
    plan_md_present = bool(plan_md_path and plan_md_path.is_file() and plan_md_path.stat().st_size > 0)
    blocking_reasons: list[str] = []
    if gate is None:
        blocking_reasons.append("formal_focus_gate_missing")
    elif str(gate.get("status") or "").casefold() != "passed":
        blocking_reasons.append("formal_focus_gate_not_passed")
    if not isinstance(raw_plan, Mapping):
        blocking_reasons.append("research_plan_json_missing_or_invalid")
    if not plan_md_present:
        blocking_reasons.append("research_plan_markdown_missing_or_empty")
    if plan_audit_status != "passed":
        blocking_reasons.append("research_plan_audit_not_passed")
    if cleanup_audit_path and cleanup_audit_status != "passed":
        blocking_reasons.append("research_plan_cleanup_audit_not_passed")
    if result_status != "completed":
        blocking_reasons.append("result_status_not_completed")
    if result.get("validation_passed") is not True:
        blocking_reasons.append("result_validation_not_passed")
    if not result_successful_stop:
        blocking_reasons.append("result_stop_reason_not_canonical_success")
    if future_leaks:
        blocking_reasons.append("future_branch_leakage")
    if discovery_leaks:
        blocking_reasons.append("discovery_only_fact_support")
    if verification_violations:
        blocking_reasons.append("verification_contract_failed")
    if not id_consistency["passed"]:
        blocking_reasons.append("focus_plan_selected_ids_inconsistent")
    if not limitation_audit["preserved"]:
        blocking_reasons.append("r4_candidate_limitations_not_preserved")
    blocking_reasons = list(dict.fromkeys(blocking_reasons))
    validated_completed_plan = not blocking_reasons
    durable_plan_contract = {
        "status": "passed" if validated_completed_plan else "failed",
        "validated_completed_plan": validated_completed_plan,
        "blocking_reasons": blocking_reasons,
        "artifacts": {
            "research_plan_json": {
                "path": str(plan_path) if plan_path else None,
                "present": bool(plan_path and plan_path.is_file()),
                "valid_object": isinstance(raw_plan, Mapping),
            },
            "research_plan_markdown": {
                "path": str(plan_md_path) if plan_md_path else None,
                "present": plan_md_present,
            },
            "research_plan_audit": {
                "path": str(plan_audit_path) if plan_audit_path else None,
                "present": bool(plan_audit_path and plan_audit_path.is_file()),
                "status": plan_audit_status,
                "errors": list(plan_audit.get("errors") or []) if isinstance(plan_audit.get("errors"), list) else [],
            },
            "cleanup_audit": {
                "path": str(cleanup_audit_path) if cleanup_audit_path else None,
                "present": bool(cleanup_audit_path and cleanup_audit_path.is_file()),
                "status": cleanup_audit_status,
                "passed_or_not_required": cleanup_audit_path is None or cleanup_audit_status == "passed",
            },
            "result": {
                "path": str(result_path) if result_path else None,
                "present": bool(result_path and result_path.is_file()),
                "status": result_status,
                "validation_passed": result.get("validation_passed"),
                "stop_reason": result.get("stop_reason"),
                "successful_stop_reason": result_successful_stop,
                "accepted_stop_reasons": sorted(R5_SUCCESSFUL_STOP_REASONS),
            },
        },
        "focus_plan_id_consistency": id_consistency,
        "verification_contract": verification_contract,
        "candidate_limitations": limitation_audit,
        "future_branch_leak_count": len(future_leaks),
        "discovery_only_fact_support_count": len(discovery_leaks),
    }
    cost = _cost_from_roots([r5_root], {})
    return {
        "status": status,
        "formal_focus_gate": gate is not None,
        "formal_focus_passed": bool(gate and str(gate.get("status") or "").casefold() == "passed"),
        "validated_completed_plan": validated_completed_plan,
        "durable_plan_contract": durable_plan_contract,
        "focus_gate_path": str(gate_path) if gate_path else None,
        "project_type": _first(gate or {}, "project_type", default=None),
        "main_problem_count": main_problem_count,
        "main_hypothesis_count": main_hypothesis_count,
        "future_branch_count": len(future_branches),
        "future_hypothesis_ids": sorted(future_ids),
        "future_branch_leakage": future_leaks,
        "discovery_only_fact_support": discovery_leaks,
        "verification_deferred": deferred,
        "verification_violations": verification_violations,
        "candidate_limitations": limitation_audit,
        "reconciliation": {
            "path": str(reconciliation_path) if reconciliation_path else None,
            "status": _first(reconciliation, "status", "reconciliation_status", default=None),
            "recomputed_opportunities": _first(reconciliation, "recomputed_opportunities", default=None),
            "recomputed_hypotheses": _first(reconciliation, "recomputed_hypotheses", default=None),
            "recomputed_focus": _first(reconciliation, "recomputed_focus", default=None),
        },
        "cost": cost,
        "reuse_count": _reuse_count([r5_root]),
        "stop_reason": next_step if status == "missing_formal_focus_gate" else _first(
            gate or {}, "stop_reason", "failure_reason", default="formal_focus_gate_preserved"
        ),
        "plan_path": str(plan_path) if plan_path else None,
        "plan_markdown_path": str(plan_md_path) if plan_md_path else None,
        "plan_audit_path": str(plan_audit_path) if plan_audit_path else None,
        "cleanup_audit_path": str(cleanup_audit_path) if cleanup_audit_path else None,
        "result_path": str(result_path) if result_path else None,
    }


def _topic_markers(topic: Mapping[str, Any]) -> set[str]:
    markers = {str(topic.get("topic_id", "")).lower()}
    for key in ("path_markers", "forbidden_path_markers", "scientific_scope"):
        value = topic.get(key, [])
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            markers.update(
                token.lower()
                for item in value
                for token in re.split(r"[^a-z0-9]+", str(item).lower())
                if len(token) >= 5
            )
    return markers


def _path_cross_topic_hits(topic: Mapping[str, Any], path_values: Sequence[str], all_topics: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    own_id = str(topic["topic_id"])
    own_markers = _topic_markers(topic)
    hits: list[dict[str, str]] = []
    for other in all_topics:
        other_id = str(other["topic_id"])
        if other_id == own_id:
            continue
        # Shared generic vocabulary (for example "topic", "optical", or
        # "review") is not evidence of cross-topic leakage.  Only markers
        # unique to the other topic may trigger the isolation gate.
        markers = _topic_markers(other) - own_markers - {own_id.lower()}
        for value in path_values:
            normalized = _text(value)
            matched = sorted(marker for marker in markers if marker and marker in normalized)
            if matched:
                hits.append({"path": str(value), "other_topic_id": other_id, "markers": ",".join(matched[:5])})
    return hits[:50]


def _foreign_ids_in_value(value: Any, foreign_ids: set[str]) -> set[str]:
    """Find structured foreign IDs without scanning prose with a huge regex."""
    found: set[str] = set()
    for item in _walk(value):
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if candidate in foreign_ids:
            found.add(candidate)
            continue
        # Some legacy JSON stores a short comma/space-delimited ID list.  Do
        # bounded tokenization for short values, but never run this over a
        # whole manuscript or raw full-text field.
        if 0 < len(candidate) <= 4096:
            tokens = re.findall(r"[A-Za-z0-9_.:/-]+", candidate)
            found.update(token for token in tokens if token in foreign_ids)
    return found


def _artifact_id_hits(topic: Mapping[str, Any], project_root: Path, other_ids: set[str]) -> list[dict[str, Any]]:
    roots = [
        _resolve(project_root, topic.get("r4_root")),
        _resolve(project_root, topic.get("r5_root")),
        _resolve(project_root, topic.get("phase3_artifacts_root")),
    ]
    if not other_ids:
        return []
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        for path in _artifact_files(root, limit=600, extensions={".json", ".jsonl"}):
            values: list[Any] = []
            if path.suffix.lower() == ".jsonl":
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            if line.strip():
                                try:
                                    values.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                except OSError:
                    continue
            else:
                value = _safe_load_json(path)
                if value is not None:
                    values.append(value)
            foreign_in_file: set[str] = set()
            for value in values:
                foreign_in_file.update(_foreign_ids_in_value(value, other_ids))
            for foreign_id in sorted(foreign_in_file):
                key = (str(path), foreign_id)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"artifact": str(path), "foreign_id": foreign_id})
                if len(hits) >= 50:
                    return hits
    return hits


def _pairwise_intersections(inventories: Mapping[str, TopicInventory]) -> dict[str, dict[str, int]]:
    topic_ids = sorted(inventories)
    result: dict[str, dict[str, int]] = {}
    for index, left_id in enumerate(topic_ids):
        for right_id in topic_ids[index + 1 :]:
            left = inventories[left_id]
            right = inventories[right_id]
            result[f"{left_id}__{right_id}"] = {
                "paper_ids": len(left.paper_ids & right.paper_ids),
                "text_chunk_ids": len(left.text_chunk_ids & right.text_chunk_ids),
                "visual_chunk_ids": len(left.visual_chunk_ids & right.visual_chunk_ids),
            }
    return result


def _classify_scientific_readiness(
    r4: Mapping[str, Any],
    r5: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify evidence readiness independently from engineering acceptance."""
    preflight = context.get("preflight") if isinstance(context.get("preflight"), Mapping) else {}
    permission_audit = (
        context.get("source_permission_audit")
        if isinstance(context.get("source_permission_audit"), Mapping)
        else {}
    )
    permission_counts = (
        permission_audit.get("counts")
        if isinstance(permission_audit.get("counts"), Mapping)
        else {}
    )
    reconciliation = (
        permission_audit.get("ledger_inventory_reconciliation")
        if isinstance(permission_audit.get("ledger_inventory_reconciliation"), Mapping)
        else context.get("ledger_inventory_reconciliation")
    )
    reconciliation = reconciliation if isinstance(reconciliation, Mapping) else {}
    missing_chunks = list(reconciliation.get("missing_ledger_chunk_ids") or [])
    missing_papers = list(reconciliation.get("missing_ledger_paper_ids") or [])
    db_errors = list(reconciliation.get("db_read_errors") or [])
    factual_count = sum(
        int(permission_counts.get(key) or 0)
        for key in ("factual_support", "direct_factual_support", "fulltext", "full_text")
    )
    qualified_count = int(permission_counts.get("qualified_only") or 0)
    unavailable_count = int(permission_counts.get("unavailable") or 0)
    limitation_count = int((r5.get("candidate_limitations") or {}).get("limitation_count") or 0)
    r4_status = str(r4.get("status") or "missing")
    preflight_status = str(preflight.get("status") or "unknown")
    mandatory_missing = list(preflight.get("mandatory_missing") or [])
    reasons: list[str] = []
    if preflight_status == "blocked_honest_stop" or mandatory_missing:
        reasons.append("context_preflight_blocked")
    if r4_status in NONTERMINAL_STATUSES:
        reasons.append("r4_candidate_not_final")
    elif r4_status != "completed":
        reasons.append("r4_not_completed")
    if not permission_counts:
        reasons.append("source_permission_inventory_missing")
    elif factual_count == 0:
        reasons.append("no_direct_factual_support_permission")
    if missing_chunks:
        reasons.append("ledger_chunks_missing_from_scoped_kb")
    if missing_papers:
        reasons.append("ledger_papers_missing_from_scoped_kb")
    if db_errors:
        reasons.append("scoped_kb_read_errors")
    if unavailable_count:
        reasons.append("unavailable_evidence_records_present")
    if limitation_count:
        reasons.append("r5_candidate_limitations_recorded")
    reasons = list(dict.fromkeys(reasons))

    if "context_preflight_blocked" in reasons:
        classification = "blocked_context"
    elif any(
        reason in reasons
        for reason in (
            "r4_candidate_not_final",
            "r4_not_completed",
            "source_permission_inventory_missing",
            "no_direct_factual_support_permission",
            "ledger_chunks_missing_from_scoped_kb",
            "ledger_papers_missing_from_scoped_kb",
            "scoped_kb_read_errors",
        )
    ):
        classification = "evidence_incomplete_candidate"
    elif reasons:
        classification = "ready_with_recorded_limitations"
    else:
        classification = "evidence_ready"
    return {
        "classification": classification,
        "evidence_complete": classification == "evidence_ready",
        "affects_engineering_acceptance": False,
        "reasons": reasons,
        "signals": {
            "r4_status": r4_status,
            "context_preflight_status": preflight_status,
            "mandatory_missing": mandatory_missing,
            "factual_support_count": factual_count,
            "qualified_only_count": qualified_count,
            "unavailable_count": unavailable_count,
            "missing_ledger_chunk_count": len(missing_chunks),
            "missing_ledger_paper_count": len(missing_papers),
            "db_read_error_count": len(db_errors),
            "r5_candidate_limitation_count": limitation_count,
        },
    }


def _overall_scientific_readiness(per_topic: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    classifications = [
        str((item.get("scientific_readiness") or {}).get("classification") or "unknown")
        for item in per_topic
    ]
    counts = {value: classifications.count(value) for value in sorted(set(classifications))}
    if classifications and all(value == "evidence_ready" for value in classifications):
        classification = "evidence_ready"
    elif "blocked_context" in classifications:
        classification = "not_evidence_complete"
    elif "evidence_incomplete_candidate" in classifications:
        classification = "not_evidence_complete"
    else:
        classification = "ready_with_recorded_limitations"
    return {
        "classification": classification,
        "all_topics_evidence_complete": bool(classifications)
        and all(value == "evidence_ready" for value in classifications),
        "affects_engineering_acceptance": False,
        "topic_classification_counts": counts,
        "note": "Scientific evidence readiness is reported separately and cannot be inferred from the R6 engineering-contract result.",
    }


def _topic_markdown(report: Mapping[str, Any]) -> str:
    topic = report["topic_id"]
    inventory = report["inventory"]
    r4 = report["r4"]
    r5 = report["r5"]
    lines = [
        f"# R6 Topic Report: {topic}",
        "",
        f"- Category: {report.get('category')}",
        f"- R6 topic status: **{report.get('r6_topic_status')}**",
        f"- Same-code-path replay: {report.get('same_code_path')}",
        f"- Stop reason: {r5.get('stop_reason') or r4.get('stop_reason')}",
        f"- Context-adapter status: {report.get('context_adapter', {}).get('status')}",
        f"- Scientific readiness: `{report.get('scientific_readiness', {}).get('classification')}`",
        f"- Evidence complete: `{report.get('scientific_readiness', {}).get('evidence_complete')}`",
        "",
        "## Scoped assets",
        "",
        f"- Papers: {inventory['allowlists']['paper']['count']} ({inventory['allowlists']['paper']['fingerprint_sha256']})",
        f"- Text chunks: {inventory['allowlists']['text_chunk']['count']} ({inventory['allowlists']['text_chunk']['fingerprint_sha256']})",
        f"- Visual chunks: {inventory['allowlists']['visual_chunk']['count']} ({inventory['allowlists']['visual_chunk']['fingerprint_sha256']})",
        f"- Missing scoped databases: {len(inventory.get('missing_database_paths', []))}",
        "",
        "## R4",
        "",
        f"- Raw status: `{r4.get('status')}`; classification: `{r4.get('status_classification')}`",
        f"- Phase-3 contract: `{r4.get('phase3_contract', {}).get('status')}`",
        f"- Visual status: `{r4.get('visual', {}).get('status')}`",
        f"- Cost: CNY {r4.get('cost', {}).get('estimated_cost_cny', 0)}; input {r4.get('cost', {}).get('input_tokens', 0)}; output {r4.get('cost', {}).get('output_tokens', 0)}",
        "",
        "## R5",
        "",
        f"- Formal focus gate: `{r5.get('formal_focus_passed')}`; status: `{r5.get('status')}`",
        f"- Validated completed plan: `{r5.get('validated_completed_plan')}`",
        f"- Plan blockers: {', '.join(r5.get('durable_plan_contract', {}).get('blocking_reasons', [])) or 'none'}",
        f"- Main problems: {r5.get('main_problem_count')}; main hypotheses: {r5.get('main_hypothesis_count')}",
        f"- Future-branch leakage: {len(r5.get('future_branch_leakage', []))}",
        f"- Discovery-only fact-support violations: {len(r5.get('discovery_only_fact_support', []))}",
        f"- Verification-deferred records: {len(r5.get('verification_deferred', []))}; invalid unrun records: {len(r5.get('verification_violations', []))}",
        f"- Candidate limitations preserved: `{r5.get('candidate_limitations', {}).get('preserved')}`",
        f"- Cost source: `{r5.get('cost', {}).get('source_kind')}` from `{r5.get('cost', {}).get('source')}`",
        f"- Cost accounting precision: `{r5.get('cost', {}).get('accounting_precision')}`; historical ambiguity: `{r5.get('cost', {}).get('historical_ambiguity')}`",
        f"- Synthetic claims/relations created: {int(report.get('context_adapter', {}).get('claims_materialized', False))}/{int(report.get('context_adapter', {}).get('relations_materialized', False))}",
        "",
        "## Next step",
        "",
        f"{report.get('next_step')}",
        "",
    ]
    if report.get("cross_topic_path_hits"):
        lines.extend(["## Cross-topic path warnings", ""])
        lines.extend(f"- `{item}`" for item in report["cross_topic_path_hits"][:10])
        lines.append("")
    if report.get("cross_topic_id_hits"):
        lines.extend(["## Cross-topic ID warnings", ""])
        lines.extend(f"- `{item}`" for item in report["cross_topic_id_hits"][:10])
        lines.append("")
    return "\n".join(lines)


def run_r6_cross_topic_regression(
    manifest_path: str | Path,
    output_dir: str | Path | None = None,
    *,
    emit_live_plan: bool = True,
    live_cost_cap_cny: float | None = None,
    r5_overrides_path: str | Path | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Replay the R6 contract for every manifest topic without external calls."""
    manifest_file = Path(manifest_path).resolve()
    manifest = _load_json(manifest_file)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError(f"Unsupported R6 manifest: {manifest_file}")
    project_root = _resolve(manifest_file.parent, manifest.get("project_root")) or manifest_file.parent.parent
    raw_topics = manifest.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("R6 manifest contains no topics")
    output = Path(output_dir).resolve() if output_dir else project_root / "outputs" / "r6_cross_topic_regression_offline"
    output.mkdir(parents=True, exist_ok=True)
    topics, r5_root_resolution = _resolve_r5_roots(manifest, project_root, r5_overrides_path)
    inventories = {
        str(topic["topic_id"]): build_topic_inventory(topic, project_root)
        for topic in topics
        if isinstance(topic, Mapping) and topic.get("topic_id")
    }
    # The adapter is imported lazily so the R6 runtime remains usable as a
    # standalone inventory module and does not create an import cycle.
    from .r6_topic_context_adapter import (
        adapt_topic_context,
        build_live_execution_plan,
        live_plan_markdown,
    )

    contexts = {
        str(topic["topic_id"]): adapt_topic_context(
            topic,
            project_root,
            output_dir=output / str(topic["topic_id"]),
        )
        for topic in topics
        if isinstance(topic, Mapping) and topic.get("topic_id")
    }
    intersections = _pairwise_intersections(inventories)
    per_topic: list[dict[str, Any]] = []
    for topic in topics:
        if not isinstance(topic, Mapping):
            continue
        topic_id = str(topic["topic_id"])
        inventory = inventories[topic_id]
        r4 = audit_r4_topic(topic, project_root)
        r5 = audit_r5_topic(topic, project_root, r4)
        context = contexts[topic_id]
        other_ids = set().union(
            *(set(item.paper_ids) | set(item.text_chunk_ids) | set(item.visual_chunk_ids)
              for key, item in inventories.items() if key != topic_id)
        )
        path_hits = _path_cross_topic_hits(topic, inventory.path_values, topics)
        id_hits = _artifact_id_hits(topic, project_root, other_ids)
        scoped_ok = not inventory.missing_db_paths and bool(inventory.paper_ids)
        formal_r5 = bool(r5["formal_focus_passed"])
        validated_plan = bool(r5["validated_completed_plan"])
        if path_hits or id_hits:
            topic_status = "failed_scope_isolation"
            next_step = "Remove foreign paths/IDs from this topic's artifacts and rebuild its scoped allowlist."
        elif validated_plan:
            topic_status = "validated_completed_r5_candidate"
            next_step = "The durable R5 plan contract passed; include it in strict three-topic R6 acceptance."
        elif formal_r5:
            topic_status = "focus_passed_plan_incomplete"
            next_step = "Complete and validate the durable R5 plan artifacts before strict R6 acceptance."
        elif context["preflight"]["status"] == "blocked_honest_stop":
            topic_status = "needs_context_adapter"
            missing = ", ".join(context["preflight"]["mandatory_missing"])
            next_step = f"Resolve the R6 context-adapter preflight before R5: {missing}."
        else:
            topic_status = "ready_for_formal_r5"
            next_step = "Enable the bounded R5 command after reviewing the compatibility limitations and preserve its focus-gate traceability."
        scientific_readiness = _classify_scientific_readiness(r4, r5, context)
        report = {
            "schema_version": R6_SCHEMA,
            "topic_id": topic_id,
            "category": topic.get("category"),
            "scientific_scope": topic.get("scientific_scope"),
            "same_code_path": True,
            "inventory": inventory.report(),
            "r4": r4,
            "r5": r5,
            "r5_root_resolution": r5_root_resolution.get(topic_id),
            "context_adapter": {
                "schema_version": context["schema_version"],
                "status": context["preflight"]["status"],
                "mandatory_missing": context["preflight"]["mandatory_missing"],
                "claims_materialized": context["scientific_content_policy"]["claims_materialized"],
                "relations_materialized": context["scientific_content_policy"]["relations_materialized"],
                "input_routing": context.get("input_routing", {}),
                "ledger_inventory_reconciliation": context.get("ledger_inventory_reconciliation", {}),
                "source_permission_audit": context.get("source_permission_audit", {}),
            },
            "cross_topic_path_hits": path_hits,
            "cross_topic_id_hits": id_hits,
            "scoped_asset_gate": {
                "passed": scoped_ok,
                "reason": "scoped_allowlist_present" if scoped_ok else "missing_or_empty_scoped_database",
            },
            "r6_topic_status": topic_status,
            "scientific_readiness": scientific_readiness,
            "next_step": next_step,
            "external_calls": {"qwen": 0, "semantic_scholar": 0, "openalex": 0, "downloads": 0},
        }
        topic_output = output / topic_id
        _write_json(topic_output / "R6_TOPIC_REPORT.json", report)
        _write_json(topic_output / "R6_TOPIC_CONTEXT.json", context)
        (topic_output / "R6_TOPIC_REPORT.md").write_text(_topic_markdown(report), encoding="utf-8")
        per_topic.append(report)

    live_plan = None
    if emit_live_plan:
        live_plan = build_live_execution_plan(
            manifest,
            list(contexts.values()),
            project_root,
            cost_cap_override=live_cost_cap_cny,
        )
        _write_json(output / "R6_LIVE_EXECUTION_PLAN.json", live_plan)
        (output / "R6_LIVE_EXECUTION_PLAN.md").write_text(live_plan_markdown(live_plan), encoding="utf-8")

    contamination = [
        {"topic_id": report["topic_id"], "paths": report["cross_topic_path_hits"], "ids": report["cross_topic_id_hits"]}
        for report in per_topic
        if report["cross_topic_path_hits"] or report["cross_topic_id_hits"]
    ]
    all_formal_r5 = all(report["r5"]["formal_focus_passed"] for report in per_topic)
    all_validated_completed_plans = all(
        report["r5"]["validated_completed_plan"] for report in per_topic
    )
    all_r4_handoffs_acceptable = all(
        report["r4"]["status"] == "completed"
        or (
            report["r4"]["status"] in NONTERMINAL_STATUSES
            and report["r5"]["candidate_limitations"]["preserved"]
        )
        for report in per_topic
    )
    all_scoped = all(report["scoped_asset_gate"]["passed"] for report in per_topic)
    intersections_pass = all(all(value == 0 for value in pair.values()) for pair in intersections.values())
    hard_failure = bool(contamination) or not all_scoped or not intersections_pass or len(per_topic) != 3
    if hard_failure:
        status = "failed"
        status_reason = "scope_or_manifest_integrity_failure"
    elif strict and (not all_validated_completed_plans or not all_r4_handoffs_acceptable):
        status = "not_ready"
        status_reason = (
            "strict_r6_requires_acceptable_r4_handoffs"
            if all_validated_completed_plans and not all_r4_handoffs_acceptable
            else "strict_r6_requires_three_validated_completed_r5_plans"
        )
    elif not all_formal_r5:
        status = "not_ready"
        status_reason = "metalens_and_or_multilayer_lacks_formal_r5_focus_gate"
    else:
        status = "passed"
        status_reason = (
            "three_topics_have_validated_completed_r5_plans"
            if all_validated_completed_plans
            else "non_strict_readiness_all_topics_have_formal_focus_gates"
        )
    total_cost = sum(
        float(report["r4"]["cost"].get("estimated_cost_cny") or 0)
        + float(report["r5"]["cost"].get("estimated_cost_cny") or 0)
        for report in per_topic
    )
    scientific_readiness = _overall_scientific_readiness(per_topic)
    engineering_contract_passed = (
        (not hard_failure)
        and all_validated_completed_plans
        and all_r4_handoffs_acceptable
    )
    command_topic_count = len(live_plan.get("topics", [])) if live_plan else 0
    actionable_topic_count = (
        int(live_plan.get("actionable_topic_count", 0)) if live_plan else 0
    )
    acceptance = {
        "schema_version": R6_SCHEMA,
        "manifest_path": str(manifest_file),
        "r5_root_overrides_path": str(Path(r5_overrides_path).resolve()) if r5_overrides_path else None,
        "r5_root_resolution": list(r5_root_resolution.values()),
        "generated_at": _utc_now(),
        "mode": "offline_deterministic_replay",
        "strict_mode": bool(strict),
        "status": status,
        "status_reason": status_reason,
        "engineering_contract": {
            "status": "passed" if engineering_contract_passed else ("failed" if hard_failure else "not_ready"),
            "passed": engineering_contract_passed,
            "strict_mode": bool(strict),
            "meaning": "Engineering/genericity contract only; it does not assert scientific evidence completeness.",
        },
        "scientific_readiness": scientific_readiness,
        "status_semantics": {
            "needs_more_literature": "candidate_not_failure_or_success",
            "awaiting_human_review": "candidate_not_failure_or_success",
            "partial": "incomplete_candidate_not_automatically_failed",
            "missing_formal_r5": "readiness_blocker_not_scope_failure",
        },
        "acceptance_gates": {
            "exactly_three_topics": len(per_topic) == 3,
            "three_distinct_categories": len({report.get("category") for report in per_topic}) == 3,
            "same_code_path": all(report.get("same_code_path") for report in per_topic),
            "scoped_allowlists_present": all_scoped,
            "zero_pairwise_id_intersection": intersections_pass,
            "zero_cross_topic_path_or_id_contamination": not contamination,
            "honest_nonterminal_status_semantics": True,
            "no_external_calls": True,
            "all_topics_formal_r5": all_formal_r5,
            "all_topics_validated_completed_r5_plan": all_validated_completed_plans,
            "all_r4_handoffs_acceptable": all_r4_handoffs_acceptable,
            "strict_acceptance_passed": (
                (not hard_failure)
                and all_validated_completed_plans
                and all_r4_handoffs_acceptable
            ),
        },
        "pairwise_intersections": intersections,
        "scope_contamination": contamination,
        "topics": per_topic,
        "live_execution_plan": {
            "json_path": str(output / "R6_LIVE_EXECUTION_PLAN.json") if live_plan else None,
            "markdown_path": str(output / "R6_LIVE_EXECUTION_PLAN.md") if live_plan else None,
            "manifest_topic_count": len(per_topic),
            "command_topic_count": command_topic_count,
            "actionable_topic_count": actionable_topic_count,
            "topic_count": command_topic_count,
            "topic_count_semantics": "deprecated_alias_of_command_topic_count",
            "executed": False,
            "emitted": live_plan is not None,
            "cost_cap_override_cny": live_cost_cap_cny,
        },
        "cost": {
            "r6_calls": {"qwen": 0, "semantic_scholar": 0, "openalex": 0, "downloads": 0},
            "r6_cost_cny": 0.0,
            "historical_asset_cost_cny": total_cost,
            "historical_accounting_ambiguity": any(
                bool(report["r4"]["cost"].get("historical_ambiguity"))
                or bool(report["r5"]["cost"].get("historical_ambiguity"))
                for report in per_topic
            ),
            "per_topic_provenance": [
                {
                    "topic_id": report["topic_id"],
                    "r4": report["r4"]["cost"],
                    "r5": report["r5"]["cost"],
                }
                for report in per_topic
            ],
            "note": "Historical R4/R5 costs are reported per topic; R6 replay itself makes no model or network calls.",
        },
        "next_steps": [
            report["next_step"] for report in per_topic
        ],
    }
    _write_json(output / "R6_CROSS_TOPIC_ACCEPTANCE.json", acceptance)
    (output / "R6_CROSS_TOPIC_ACCEPTANCE.md").write_text(_acceptance_markdown(acceptance), encoding="utf-8")
    return acceptance


def _acceptance_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# R6 Cross-Topic Acceptance",
        "",
        f"- Status: **{report.get('status')}**",
        f"- Mode: `{report.get('mode')}`",
        f"- Strict mode: `{report.get('strict_mode')}`",
        f"- Reason: {report.get('status_reason')}",
        f"- Engineering contract: `{report.get('engineering_contract', {}).get('status')}`",
        f"- Scientific readiness: `{report.get('scientific_readiness', {}).get('classification')}`",
        "",
        "## Meaning of the result",
        "",
        "This is a deterministic replay of existing R4/R5 assets. It does not call Qwen, Semantic Scholar, OpenAlex, or a downloader. `awaiting_human_review`, `needs_more_literature`, and `partial` remain candidate/incomplete states; they are not silently converted into success or failure.",
        "",
        "## Acceptance gates",
        "",
    ]
    for key, value in (report.get("acceptance_gates") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Pairwise scoped-ID intersections", ""])
    for pair, values in (report.get("pairwise_intersections") or {}).items():
        lines.append(f"- `{pair}`: papers={values['paper_ids']}, text={values['text_chunk_ids']}, visual={values['visual_chunk_ids']}")
    lines.extend(["", "## Topic results", "", "| Topic | Category | R4 | R5 | R6 state | Scientific readiness | Papers | Text | Visual |", "|---|---|---|---|---|---|---:|---:|---:|"])
    for topic in report.get("topics", []):
        counts = topic["inventory"]["allowlists"]
        lines.append(
            f"| {topic['topic_id']} | {topic.get('category')} | {topic['r4'].get('status')} | {topic['r5'].get('status')} | {topic.get('r6_topic_status')} | {topic.get('scientific_readiness', {}).get('classification')} | {counts['paper']['count']} | {counts['text_chunk']['count']} | {counts['visual_chunk']['count']} |"
        )
    lines.extend(["", "## Cost and external calls", "", "- R6 Qwen calls: 0", "- R6 Semantic Scholar calls: 0", "- R6 downloads: 0", "- R6 replay cost: CNY 0", f"- Historical asset cost reported in manifests: CNY {report['cost']['historical_asset_cost_cny']}", "", "## Next steps", ""])
    lines.extend(f"- {item}" for item in report.get("next_steps", []))
    plan = report.get("live_execution_plan") or {}
    lines.extend(
        [
            "",
            "## Future R5 plan",
            "",
            f"- Emitted: `{plan.get('emitted')}`",
            f"- Executed by R6: `{plan.get('executed')}`",
            f"- Plan JSON: `{plan.get('json_path')}`",
            f"- Manifest topics audited: {plan.get('manifest_topic_count', 0)}",
            f"- Commands emitted: {plan.get('command_topic_count', 0)}",
            f"- Actionable commands: {plan.get('actionable_topic_count', 0)}",
            f"- Deprecated `topic_count` alias (command count): {plan.get('topic_count', 0)}",
        ]
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "MANIFEST_SCHEMA",
    "R6_SCHEMA",
    "TopicInventory",
    "audit_r4_topic",
    "audit_r5_topic",
    "build_topic_inventory",
    "run_r6_cross_topic_regression",
]
