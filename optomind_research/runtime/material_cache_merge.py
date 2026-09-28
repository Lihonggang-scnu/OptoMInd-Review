"""Safe versioned long-term material-cache merge for supplementary retrieval.

The canonical base cache (``MATERIAL_UNITS_FINAL.json`` plus
``material_vectors.sqlite``) is treated as read-only input.  Task-local
committed increments are merged into a brand-new snapshot directory under a
staging directory, published atomically by rename only after every unit has a
vector row, the vector schema/dimensions are compatible, no unit/content
collision exists, and ``PRAGMA integrity_check`` passes.  A failed merge never
leaves a completed-looking snapshot directory.

The module performs no network/model calls and touches no credentials.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "optomind.material_cache_merge.v1"
REPORT_FILENAME = "LONG_TERM_CACHE_MERGE_REPORT.json"
UNITS_FILENAME = "MATERIAL_UNITS_FINAL.json"
VECTORS_FILENAME = "material_vectors.sqlite"
VECTOR_TABLE = "semantic_vectors"
VECTOR_COLUMNS = (
    "unit_id",
    "content_hash",
    "embedding_model",
    "representation_version",
    "dimension",
    "vector",
    "surrogate",
    "created_at",
    "updated_at",
)
VISUAL_UNIT_KINDS = frozenset({"visual_asset", "visual_chunk", "visual"})


class MaterialCacheMergeError(RuntimeError):
    """Raised when a merge cannot be published safely."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


@dataclass(frozen=True, slots=True)
class MaterialCacheIncrement:
    """One committed task-local material increment."""

    units_path: Path
    vectors_path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_key(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _unit_content_hash(unit: Mapping[str, Any]) -> str:
    return str(
        ((unit.get("durable_content") or {}).get("content_hash") or "")
    ).strip()


def _load_units(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise MaterialCacheMergeError(
            f"missing units file for {label}: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MaterialCacheMergeError(
            f"invalid units file for {label}: {path} ({exc})"
        ) from exc
    units = payload.get("units") if isinstance(payload, Mapping) else None
    if not isinstance(units, list):
        raise MaterialCacheMergeError(
            f"units file for {label} has no units list: {path}"
        )
    return [dict(unit) for unit in units if isinstance(unit, Mapping)]


def _merge_unique_list(
    existing: list[Any], incoming: list[Any]
) -> list[Any]:
    seen = {_canonical_key(item) for item in existing}
    merged = list(existing)
    for item in incoming:
        key = _canonical_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(copy.deepcopy(item))
    return merged


def _merge_unit(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge unique annotations/refs/relations, preserving base fields."""

    merged = copy.deepcopy(dict(existing))
    for key in ("query_annotations", "embedding_refs", "relations"):
        existing_items = merged.get(key) or []
        incoming_items = incoming.get(key) or []
        if isinstance(existing_items, list) and isinstance(
            incoming_items, list
        ):
            merged[key] = _merge_unique_list(
                existing_items, incoming_items
            )
    return merged


def _backup_base_vectors(source_path: Path, target_path: Path) -> int:
    if not source_path.is_file():
        raise MaterialCacheMergeError(
            f"missing base vector DB: {source_path}"
        )
    source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    target = sqlite3.connect(str(target_path))
    try:
        _validate_vector_schema(source, label="base")
        source.backup(target)
        with target:
            row = target.execute(
                "SELECT COUNT(*) FROM semantic_vectors"
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        target.close()
        source.close()


def _vector_columns(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({VECTOR_TABLE})"
    ).fetchall()
    return {str(row[1]) for row in rows}


def _validate_vector_schema(
    connection: sqlite3.Connection, *, label: str
) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if VECTOR_TABLE not in tables:
        raise MaterialCacheMergeError(
            f"vector DB for {label} lacks table {VECTOR_TABLE}"
        )
    columns = _vector_columns(connection)
    missing = [column for column in VECTOR_COLUMNS if column not in columns]
    if missing:
        raise MaterialCacheMergeError(
            f"vector DB for {label} is missing columns: {','.join(missing)}"
        )


def _validate_vector_blob(value: Any, dimension: int) -> bytes:
    """Validate a production float32 vector BLOB without rewriting it.

    Production ``semantic_vectors.vector`` is a binary ``struct.pack``
    float32 buffer of exactly ``dimension * 4`` bytes; it must never be
    parsed as JSON or re-serialized.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    else:
        raise MaterialCacheMergeError(
            "vector value is not a float32 BLOB"
        )
    if len(raw) != int(dimension) * 4:
        raise MaterialCacheMergeError(
            "vector blob length does not match dimension * 4"
        )
    return raw


class MaterialCacheMerger:
    """Merge base cache plus committed increments into a versioned snapshot."""

    def __init__(
        self,
        *,
        base_units_path: str | Path,
        base_vectors_path: str | Path,
        increments: Iterable[MaterialCacheIncrement] = (),
        output_root: str | Path,
        supplementary_conflict_policy: bool = False,
    ) -> None:
        self.base_units_path = Path(base_units_path)
        self.base_vectors_path = Path(base_vectors_path)
        self.increments = list(increments)
        self.output_root = Path(output_root)
        self.supplementary_conflict_policy = bool(
            supplementary_conflict_policy
        )

    def merge(self) -> dict[str, Any]:
        """Run the merge; raises on failure with an attached report."""

        output_root = self.output_root.resolve()
        if output_root.exists():
            raise MaterialCacheMergeError(
                f"refusing to overwrite snapshot output: {output_root}"
            )
        parent = output_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / (
            output_root.name + f".staging-{uuid.uuid4().hex[:8]}"
        )
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "running",
            "created_at": _utc_now(),
            "base": {
                "units_path": str(self.base_units_path),
                "vectors_path": str(self.base_vectors_path),
            },
            "increments": [
                {
                    "units_path": str(increment.units_path),
                    "vectors_path": str(increment.vectors_path),
                }
                for increment in self.increments
            ],
            "counts": {
                "base_units": 0,
                "increment_units": 0,
                "output_units": 0,
                "added_units": 0,
                "reused_units": 0,
                "conflict_units": 0,
                "missing_vector_units": 0,
                "base_vectors": 0,
                "increment_vectors": 0,
                "output_vectors": 0,
                "added_vectors": 0,
                "reused_vectors": 0,
                "conflict_vectors": 0,
                "supplementary_skipped_duplicate_units": 0,
                "supplementary_skipped_duplicate_vectors": 0,
            },
            "conflicts": [],
            "supplementary_skipped_duplicates": [],
            "missing_vector_units": [],
            "integrity": "",
            "snapshot_dir": str(output_root),
        }
        try:
            staging.mkdir(parents=True)
            base_units = _load_units(self.base_units_path, "base")
            report["counts"]["base_units"] = len(base_units)
            units: dict[str, dict[str, Any]] = {
                str(unit.get("unit_id") or ""): copy.deepcopy(unit)
                for unit in base_units
                if str(unit.get("unit_id") or "").strip()
            }
            skipped_duplicate_unit_ids: set[str] = set()
            increment_unit_total = 0
            for increment in self.increments:
                for unit in _load_units(
                    increment.units_path, str(increment.units_path)
                ):
                    increment_unit_total += 1
                    unit_id = str(unit.get("unit_id") or "").strip()
                    if not unit_id:
                        report["counts"]["conflict_units"] += 1
                        report["conflicts"].append(
                            {
                                "unit_id": "",
                                "reason": "missing_unit_id",
                                "source": str(increment.units_path),
                            }
                        )
                        continue
                    content_hash = _unit_content_hash(unit)
                    if unit_id in units:
                        existing = units[unit_id]
                        existing_hash = _unit_content_hash(existing)
                        if (
                            existing_hash
                            and content_hash
                            and existing_hash != content_hash
                        ):
                            if self.supplementary_conflict_policy:
                                report["counts"][
                                    "supplementary_skipped_duplicate_units"
                                ] += 1
                                report[
                                    "supplementary_skipped_duplicates"
                                ].append(
                                    {
                                        "unit_id": unit_id,
                                        "reason": (
                                            "content_hash_collision_skipped"
                                        ),
                                        "canonical_content_hash": (
                                            existing_hash
                                        ),
                                        "increment_content_hash": content_hash,
                                        "source": str(increment.units_path),
                                    }
                                )
                                skipped_duplicate_unit_ids.add(unit_id)
                                continue
                            report["counts"]["conflict_units"] += 1
                            report["conflicts"].append(
                                {
                                    "unit_id": unit_id,
                                    "base_content_hash": existing_hash,
                                    "increment_content_hash": content_hash,
                                    "reason": "content_hash_collision",
                                    "source": str(increment.units_path),
                                }
                            )
                            continue
                        units[unit_id] = _merge_unit(existing, unit)
                        report["counts"]["reused_units"] += 1
                    else:
                        units[unit_id] = copy.deepcopy(unit)
                        report["counts"]["added_units"] += 1
            report["counts"]["increment_units"] = increment_unit_total
            report["counts"]["output_units"] = len(units)

            vector_db = staging / VECTORS_FILENAME
            report["counts"]["base_vectors"] = _backup_base_vectors(
                self.base_vectors_path, vector_db
            )
            increment_vector_total = 0
            for increment in self.increments:
                source_uri = (
                    "file:"
                    + increment.vectors_path.resolve().as_posix()
                    + "?mode=ro"
                )
                source = sqlite3.connect(source_uri, uri=True)
                try:
                    _validate_vector_schema(
                        source, label=str(increment.vectors_path)
                    )
                    rows = source.execute(
                        "SELECT "
                        + ", ".join(VECTOR_COLUMNS)
                        + f" FROM {VECTOR_TABLE}"
                    ).fetchall()
                finally:
                    source.close()
                increment_vector_total += len(rows)
                target = sqlite3.connect(str(vector_db))
                try:
                    target.execute("PRAGMA foreign_keys=OFF")
                    for row in rows:
                        row_map = dict(zip(VECTOR_COLUMNS, row))
                        unit_id = str(row_map.get("unit_id") or "").strip()
                        content_hash = str(
                            row_map.get("content_hash") or ""
                        ).strip()
                        dimension = int(row_map.get("dimension") or 0)
                        _validate_vector_blob(
                            row_map.get("vector"), dimension
                        )
                        embedding_model = str(
                            row_map.get("embedding_model") or ""
                        )
                        representation_version = str(
                            row_map.get("representation_version") or ""
                        )
                        existing_exact = target.execute(
                            "SELECT dimension FROM semantic_vectors "
                            "WHERE unit_id=? AND content_hash=? AND "
                            "embedding_model=? AND representation_version=?",
                            (
                                unit_id,
                                content_hash,
                                embedding_model,
                                representation_version,
                            ),
                        ).fetchone()
                        if existing_exact is not None:
                            if int(existing_exact[0]) != dimension:
                                report["counts"]["conflict_vectors"] += 1
                                report["conflicts"].append(
                                    {
                                        "unit_id": unit_id,
                                        "reason": (
                                            "exact_pk_dimension_mismatch"
                                        ),
                                        "source": str(
                                            increment.vectors_path
                                        ),
                                    }
                                )
                            else:
                                report["counts"]["reused_vectors"] += 1
                            continue
                        other_hash_row = target.execute(
                            "SELECT content_hash FROM semantic_vectors "
                            "WHERE unit_id=? LIMIT 1",
                            (unit_id,),
                        ).fetchone()
                        if (
                            other_hash_row is not None
                            and str(other_hash_row[0]) != content_hash
                        ):
                            if (
                                self.supplementary_conflict_policy
                                and unit_id in skipped_duplicate_unit_ids
                            ):
                                report["counts"][
                                    "supplementary_skipped_duplicate_vectors"
                                ] += 1
                                report[
                                    "supplementary_skipped_duplicates"
                                ].append(
                                    {
                                        "unit_id": unit_id,
                                        "reason": (
                                            "vector_content_hash_mismatch_"
                                            "skipped"
                                        ),
                                        "canonical_content_hash": str(
                                            other_hash_row[0]
                                        ),
                                        "increment_content_hash": content_hash,
                                        "source": str(
                                            increment.vectors_path
                                        ),
                                    }
                                )
                                continue
                            report["counts"]["conflict_vectors"] += 1
                            report["conflicts"].append(
                                {
                                    "unit_id": unit_id,
                                    "reason": "vector_content_hash_mismatch",
                                    "source": str(increment.vectors_path),
                                }
                            )
                            continue
                        target.execute(
                            "INSERT INTO semantic_vectors("
                            + ", ".join(VECTOR_COLUMNS)
                            + ") VALUES ("
                            + ",".join("?" for _ in VECTOR_COLUMNS)
                            + ")",
                            tuple(
                                row_map.get(column)
                                for column in VECTOR_COLUMNS
                            ),
                        )
                        report["counts"]["added_vectors"] += 1
                    target.commit()
                finally:
                    target.close()
            report["counts"]["increment_vectors"] = increment_vector_total
            target = sqlite3.connect(str(vector_db))
            try:
                report["counts"]["output_vectors"] = int(
                    target.execute(
                        f"SELECT COUNT(*) FROM {VECTOR_TABLE}"
                    ).fetchone()[0]
                )
                for unit_id in units:
                    unit_hash = _unit_content_hash(units[unit_id])
                    unit_hashes = [
                        str(row[0])
                        for row in target.execute(
                            "SELECT content_hash FROM semantic_vectors "
                            "WHERE unit_id=?",
                            (unit_id,),
                        ).fetchall()
                    ]
                    if not unit_hashes:
                        report["counts"]["missing_vector_units"] += 1
                        report["missing_vector_units"].append(unit_id)
                        continue
                    if unit_hash and any(
                        item != unit_hash for item in unit_hashes
                    ):
                        report["counts"]["conflict_vectors"] += 1
                        report["conflicts"].append(
                            {
                                "unit_id": unit_id,
                                "reason": (
                                    "unit_vector_content_hash_mismatch"
                                ),
                                "source": "merged_output",
                            }
                        )
                integrity_row = target.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                report["integrity"] = (
                    str(integrity_row[0]) if integrity_row else "failed"
                )
            finally:
                target.close()

            fatal = bool(
                report["counts"]["conflict_units"]
                or report["counts"]["conflict_vectors"]
                or report["counts"]["missing_vector_units"]
                or report["integrity"] != "ok"
            )
            if fatal:
                report["status"] = "failed"
                raise MaterialCacheMergeError(
                    "material cache merge failed validation: "
                    f"{report['counts']['conflict_units']} unit conflicts, "
                    f"{report['counts']['conflict_vectors']} vector conflicts, "
                    f"{report['counts']['missing_vector_units']} missing "
                    "vectors",
                    report=report,
                )

            visual_count = sum(
                1
                for unit in units.values()
                if str(unit.get("unit_kind") or "") in VISUAL_UNIT_KINDS
            )
            merged_payload = {
                "schema_version": "optomind.material_unit_store.v1",
                "created_at": _utc_now(),
                "query_annotation_policy": "separate_by_query_id_and_question_hash",
                "query_annotation_summary": {
                    "unit_count": len(units),
                    "annotated_unit_count": sum(
                        1
                        for unit in units.values()
                        if (unit.get("query_annotations") or [])
                    ),
                },
                "text_unit_count": len(units) - visual_count,
                "unit_count": len(units),
                "visual_unit_count": visual_count,
                "units": list(units.values()),
            }
            (staging / UNITS_FILENAME).write_text(
                json.dumps(merged_payload, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            report["status"] = "completed"
            report["counts"]["output_units"] = len(units)
            (staging / REPORT_FILENAME).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            staging.rename(output_root)
            return copy.deepcopy(report)
        except MaterialCacheMergeError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = f"{type(exc).__name__}:{exc}"
            shutil.rmtree(staging, ignore_errors=True)
            raise MaterialCacheMergeError(
                f"material cache merge failed: {exc}", report=report
            ) from exc


def merge_material_cache(
    *,
    base_units_path: str | Path,
    base_vectors_path: str | Path,
    increments: Iterable[MaterialCacheIncrement] = (),
    output_root: str | Path,
    supplementary_conflict_policy: bool = False,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`MaterialCacheMerger`."""

    return MaterialCacheMerger(
        base_units_path=base_units_path,
        base_vectors_path=base_vectors_path,
        increments=increments,
        output_root=output_root,
        supplementary_conflict_policy=supplementary_conflict_policy,
    ).merge()
