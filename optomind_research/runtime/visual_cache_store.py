"""Versioned long-term visual cache store.

Each immutable snapshot version is a directory containing:

* ``units.json`` - the durable JSON visual units;
* ``visual_cache.sqlite`` - a searchable SQLite index with FTS5;
* ``assets/`` - content-addressed image copies;
* ``manifest.json`` - hashes used by integrity verification.

Publication builds a staging directory next to the cache root and renames it
into place only after every unit, asset hash, and ``PRAGMA integrity_check``
passes.  A failed or corrupt publication never leaves a completed-looking
snapshot.  This module performs no network/model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .visual_cache_schemas import (
    ASSETS_DIRNAME,
    LATEST_FILENAME,
    LATEST_SCHEMA_VERSION,
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    SQLITE_FILENAME,
    STORE_SCHEMA_VERSION,
    UNITS_FILENAME,
    resolve_path_ref,
    validate_version,
    validate_visual_unit,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_ROOT = PROJECT_ROOT / "data" / "long_term_visual_cache"


class VisualCachePublicationError(RuntimeError):
    """Raised when a snapshot cannot be published or verified safely."""

    def __init__(
        self,
        message: str,
        *,
        errors: list[str] | None = None,
        report: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.errors = list(errors or [])
        self.report = dict(report or {})


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _replace_with_retry(
    temporary_path: str,
    destination: Path,
    *,
    attempts: int = 8,
) -> None:
    target = str(Path(destination).resolve())
    for attempt in range(attempts):
        try:
            os.replace(temporary_path, target)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.025 * (2**attempt))


def _write_json_atomic(path: Path, value: Any) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )
        _replace_with_retry(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _build_sqlite_index(snapshot_dir: Path, units: list[dict[str, Any]]) -> None:
    db_path = snapshot_dir / SQLITE_FILENAME
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE visual_cache_meta(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE units(
              unit_id TEXT PRIMARY KEY,
              schema_version TEXT NOT NULL,
              source_kind TEXT NOT NULL,
              unit_kind TEXT NOT NULL,
              asset_kind TEXT NOT NULL DEFAULT '',
              unit_role TEXT NOT NULL,
              asset_typing_json TEXT NOT NULL DEFAULT '{}',
              source_identity_json TEXT NOT NULL,
              figure_identity_json TEXT NOT NULL,
              caption_json TEXT NOT NULL,
              semantic_json TEXT NOT NULL,
              argumentative_roles_json TEXT NOT NULL,
              provenance_json TEXT NOT NULL,
              permission_state_json TEXT NOT NULL,
              hashes_json TEXT NOT NULL,
              vector_refs_json TEXT NOT NULL,
              lineage_json TEXT NOT NULL,
              use_history_json TEXT NOT NULL,
              review_json TEXT NOT NULL,
              approval_json TEXT NOT NULL,
              source_map_json TEXT NOT NULL,
              paths_json TEXT NOT NULL,
              crop_hygiene_json TEXT NOT NULL,
              parent_unit_id TEXT,
              image_relpath TEXT NOT NULL,
              original_image_relpath TEXT NOT NULL DEFAULT '',
              approval_state TEXT NOT NULL,
              review_decision TEXT NOT NULL,
              search_text TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE assets(
              asset_id TEXT PRIMARY KEY,
              unit_id TEXT NOT NULL,
              role TEXT NOT NULL,
              relpath TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              kind TEXT NOT NULL
            );
            CREATE TABLE source_nodes(
              node_id TEXT PRIMARY KEY,
              unit_id TEXT NOT NULL,
              node_type TEXT NOT NULL,
              paper_id TEXT NOT NULL,
              external_id TEXT NOT NULL,
              page TEXT,
              label TEXT NOT NULL,
              content TEXT NOT NULL,
              node_json TEXT NOT NULL
            );
            CREATE TABLE source_links(
              link_id TEXT PRIMARY KEY,
              unit_id TEXT NOT NULL,
              source_node_id TEXT NOT NULL,
              target_node_id TEXT NOT NULL,
              relation TEXT NOT NULL,
              inverse_relation TEXT NOT NULL,
              bidirectional_lookup INTEGER NOT NULL
            );
            """
        )
        fts_available = True
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
                  unit_id UNINDEXED,
                  caption,
                  semantic_description,
                  tags,
                  search_text,
                  tokenize='unicode61'
                )
                """
            )
        except sqlite3.OperationalError:
            fts_available = False
        with conn:
            conn.execute(
                "INSERT INTO visual_cache_meta(key,value) VALUES(?,?)",
                ("store_schema_version", STORE_SCHEMA_VERSION),
            )
            for index, unit in enumerate(units):
                lineage = _mapping(unit.get("lineage"))
                review = _mapping(unit.get("review"))
                approval = _mapping(unit.get("approval"))
                paths = _mapping(unit.get("paths"))
                image_ref = _mapping(paths.get("image_ref"))
                image_relpath = _text(image_ref.get("relative"))
                original_image_ref = _mapping(paths.get("original_image_ref"))
                original_image_relpath = _text(
                    original_image_ref.get("relative")
                )
                semantic = _mapping(unit.get("semantic"))
                caption = _mapping(unit.get("caption"))
                search_text = " ".join(
                    part
                    for part in (
                        _text(caption.get("clean")),
                        _text(caption.get("subfigure_focus")),
                        _text(semantic.get("description")),
                        " ".join(semantic.get("tags") or []),
                        _text(semantic.get("nearby_text")),
                    )
                    if part
                )
                conn.execute(
                    """
                    INSERT INTO units(
                      unit_id,schema_version,source_kind,unit_kind,asset_kind,
                      unit_role,asset_typing_json,
                      source_identity_json,figure_identity_json,caption_json,
                      semantic_json,argumentative_roles_json,provenance_json,
                      permission_state_json,hashes_json,vector_refs_json,
                      lineage_json,use_history_json,review_json,approval_json,
                      source_map_json,paths_json,crop_hygiene_json,parent_unit_id,image_relpath,
                      original_image_relpath,approval_state,review_decision,
                      search_text,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _text(unit.get("unit_id")),
                        _text(unit.get("schema_version")),
                        _text(
                            _mapping(unit.get("source_identity")).get(
                                "source_kind"
                            )
                        ),
                        _text(unit.get("unit_kind")),
                        _text(
                            _mapping(unit.get("figure_identity")).get(
                                "asset_kind"
                            )
                        )
                        or _text(
                            _mapping(unit.get("asset_typing")).get(
                                "asset_kind"
                            )
                        ),
                        _text(unit.get("unit_role")),
                        _json(unit.get("asset_typing") or {}),
                        _json(unit.get("source_identity") or {}),
                        _json(unit.get("figure_identity") or {}),
                        _json(unit.get("caption") or {}),
                        _json(semantic),
                        _json(unit.get("argumentative_roles") or {}),
                        _json(unit.get("provenance") or {}),
                        _json(unit.get("permission_state") or {}),
                        _json(unit.get("hashes") or {}),
                        _json(unit.get("vector_refs") or {}),
                        _json(lineage),
                        _json(unit.get("use_history") or {}),
                        _json(review),
                        _json(approval),
                        _json(unit.get("source_map") or {}),
                        _json(paths),
                        _json(unit.get("crop_hygiene") or {}),
                        _text(lineage.get("parent_unit_id")),
                        image_relpath,
                        original_image_relpath,
                        _text(approval.get("state")),
                        _text(review.get("review_decision")),
                        search_text,
                        _text(unit.get("created_at")),
                    ),
                )
                image_sha = _text(
                    _mapping(unit.get("hashes")).get("image_sha256")
                )
                conn.execute(
                    """
                    INSERT INTO assets(
                      asset_id,unit_id,role,relpath,sha256,kind
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        f"{_text(unit.get('unit_id'))}:image",
                        _text(unit.get("unit_id")),
                        "image",
                        image_relpath,
                        image_sha,
                        "image",
                    ),
                )
                source_map = _mapping(unit.get("source_map"))
                for node in source_map.get("nodes") or []:
                    if not isinstance(node, Mapping):
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO source_nodes(
                          node_id,unit_id,node_type,paper_id,external_id,
                          page,label,content,node_json
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            _text(node.get("node_id")),
                            _text(unit.get("unit_id")),
                            _text(node.get("node_type")),
                            _text(node.get("paper_id")),
                            _text(node.get("external_id")),
                            str(node.get("page") or ""),
                            _text(node.get("label")),
                            _text(node.get("content")),
                            _json(node),
                        ),
                    )
                for link in source_map.get("links") or []:
                    if not isinstance(link, Mapping):
                        continue
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO source_links(
                          link_id,unit_id,source_node_id,target_node_id,
                          relation,inverse_relation,bidirectional_lookup
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            _text(link.get("link_id")),
                            _text(unit.get("unit_id")),
                            _text(link.get("source_node_id")),
                            _text(link.get("target_node_id")),
                            _text(link.get("relation")),
                            _text(link.get("inverse_relation")),
                            1 if link.get("bidirectional_lookup") else 0,
                        ),
                    )
                parent_ref = _mapping(paths.get("parent_image_ref"))
                if _text(parent_ref.get("relative")):
                    conn.execute(
                        """
                        INSERT INTO assets(
                          asset_id,unit_id,role,relpath,sha256,kind
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            f"{_text(unit.get('unit_id'))}:parent_image",
                            _text(unit.get("unit_id")),
                            "parent_image",
                            _text(parent_ref.get("relative")),
                            "",
                            "parent_image",
                        ),
                    )
                try:
                    conn.execute(
                        """
                        INSERT INTO units_fts(
                          unit_id,caption,semantic_description,tags,search_text
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            _text(unit.get("unit_id")),
                            _text(caption.get("clean")),
                            _text(semantic.get("description")),
                            " ".join(semantic.get("tags") or []),
                            search_text,
                        ),
                    )
                except sqlite3.OperationalError:
                    fts_available = False
                if index and index % 200 == 0:
                    conn.commit()
            conn.execute(
                "INSERT OR REPLACE INTO visual_cache_meta(key,value) VALUES(?,?)",
                ("fts_available", "1" if fts_available else "0"),
            )
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]) != "ok":
            raise VisualCachePublicationError(
                "sqlite integrity check failed during publish",
                errors=[str(integrity[0] if integrity else "no_integrity_row")],
            )
    finally:
        conn.close()


def _verify_snapshot_dir(
    snapshot_dir: Path,
    path_roots: Mapping[str, Path] | None = None,
    *,
    expected_dir_name: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest = _read_json(snapshot_dir / MANIFEST_FILENAME)
    if not manifest:
        return {
            "status": "failed",
            "errors": ["manifest_missing_or_invalid"],
            "warnings": [],
            "unit_count": 0,
        }
    manifest_version = _text(manifest.get("version"))
    expected_name = expected_dir_name or snapshot_dir.name
    if manifest_version and expected_name != manifest_version:
        errors.append(
            f"manifest_version_mismatch:{manifest_version}!={expected_name}"
        )
    for filename in (UNITS_FILENAME, SQLITE_FILENAME):
        path = snapshot_dir / filename
        if not path.is_file():
            errors.append(f"missing_file:{filename}")
            continue
        expected = _text(manifest.get("files", {}).get(filename))
        actual = _sha256_file(path)
        if expected and actual != expected:
            errors.append(f"hash_mismatch:{filename}")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict):
        errors.append("manifest_files_not_object")
        manifest_files = {}
    for relpath, expected_hash in manifest_files.items():
        if not str(relpath).startswith(ASSETS_DIRNAME + "/"):
            continue
        asset_path = snapshot_dir / str(relpath)
        if not asset_path.is_file():
            errors.append(f"asset_missing:{relpath}")
            continue
        if _sha256_file(asset_path) != _text(expected_hash):
            errors.append(f"asset_hash_mismatch:{relpath}")

    payload = _read_json(snapshot_dir / UNITS_FILENAME)
    if (
        isinstance(payload, dict)
        and _text(payload.get("version"))
        and _text(payload.get("version")) != expected_name
    ):
        errors.append(
            f"units_version_mismatch:{_text(payload.get('version'))}"
        )
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("units"), list)
        and int(payload.get("unit_count") or -1) != len(payload["units"])
    ):
        errors.append("units_json_unit_count_mismatch")
    units = payload.get("units") if isinstance(payload, dict) else None
    if not isinstance(units, list):
        errors.append("units_json_missing_units_list")
        units = []
    unit_ids: set[str] = set()
    for unit in units:
        if not isinstance(unit, dict):
            errors.append("unit_not_object")
            continue
        unit_errors = validate_visual_unit(unit)
        errors.extend(unit_errors)
        unit_id = _text(unit.get("unit_id"))
        if unit_id in unit_ids:
            errors.append(f"duplicate_unit_id:{unit_id}")
        unit_ids.add(unit_id)
    for unit in units:
        if not isinstance(unit, dict):
            continue
        paths = _mapping(unit.get("paths"))
        parent_unit_id = _text(
            _mapping(unit.get("lineage")).get("parent_unit_id")
        )
        if parent_unit_id and parent_unit_id not in unit_ids:
            errors.append(f"missing_parent_unit:{parent_unit_id}")
        for ref_key in ("image_ref", "parent_image_ref"):
            ref = _mapping(paths.get(ref_key))
            relpath = _text(ref.get("relative"))
            if relpath:
                if not (snapshot_dir / relpath).is_file():
                    errors.append(f"asset_missing:{relpath}")

    db_path = snapshot_dir / SQLITE_FILENAME
    if db_path.is_file():
        conn = sqlite3.connect(db_path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]) != "ok":
                errors.append(
                    f"sqlite_integrity:{integrity[0] if integrity else 'none'}"
                )
            try:
                sqlite_count = int(
                    conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
                )
            except sqlite3.DatabaseError as exc:
                errors.append(f"sqlite_units_query_failed:{exc}")
                sqlite_count = -1
            if sqlite_count >= 0 and sqlite_count != len(unit_ids):
                errors.append(
                    f"sqlite_unit_count_mismatch:{sqlite_count}!={len(unit_ids)}"
                )
            for unit_id in sorted(unit_ids):
                row = conn.execute(
                    "SELECT 1 FROM units WHERE unit_id=?", (unit_id,)
                ).fetchone()
                if row is None:
                    errors.append(f"sqlite_missing_unit:{unit_id}")
            for row in conn.execute(
                "SELECT relpath,sha256 FROM assets WHERE sha256<>''"
            ).fetchall():
                relpath = _text(row[0])
                expected = _text(row[1])
                asset_path = snapshot_dir / relpath
                if not asset_path.is_file():
                    errors.append(f"asset_missing:{relpath}")
                    continue
                if _sha256_file(asset_path) != expected:
                    errors.append(f"asset_hash_mismatch:{relpath}")
        except sqlite3.DatabaseError as exc:
            errors.append(f"sqlite_open_failed:{exc}")
        finally:
            conn.close()
    else:
        errors.append("missing_file:visual_cache.sqlite")

    if path_roots:
        for unit in units:
            if not isinstance(unit, dict):
                continue
            source_ref = _mapping(_mapping(unit.get("paths")).get("source_ref"))
            resolved = resolve_path_ref(source_ref, path_roots)
            if (
                _text(source_ref.get("root")) in path_roots
                and resolved is not None
                and not resolved.is_file()
            ):
                warnings.append(
                    f"source_path_missing:{_text(unit.get('unit_id'))}"
                )
    status = "passed" if not errors else "failed"
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "unit_count": len(unit_ids),
    }


class VisualCacheStore:
    """Manage immutable versioned visual cache snapshots under one root."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or DEFAULT_CACHE_ROOT).resolve()

    def publish_snapshot(
        self,
        *,
        version: str,
        units: Iterable[Mapping[str, Any]],
        assets_dir: Path | str,
        path_roots: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically publish one immutable snapshot version."""

        version = validate_version(version)
        self.root.mkdir(parents=True, exist_ok=True)
        units = [dict(unit) for unit in units if isinstance(unit, Mapping)]
        if not units:
            raise VisualCachePublicationError("refusing to publish empty snapshot")
        errors: list[str] = []
        unit_ids: set[str] = set()
        for unit in units:
            unit_id = _text(unit.get("unit_id"))
            if unit_id in unit_ids:
                errors.append(f"duplicate_unit_id:{unit_id}")
            unit_ids.add(unit_id)
            errors.extend(validate_visual_unit(unit))
        for unit in units:
            parent_unit_id = _text(
                _mapping(unit.get("lineage")).get("parent_unit_id")
            )
            if not parent_unit_id:
                continue
            if parent_unit_id not in unit_ids:
                errors.append(f"missing_parent_unit:{parent_unit_id}")
                continue
            parent = next(
                (
                    row
                    for row in units
                    if _text(row.get("unit_id")) == parent_unit_id
                ),
                {},
            )
            if _text(parent.get("unit_kind")) != "parent_figure":
                errors.append(
                    f"parent_not_parent_figure:{parent_unit_id}"
                )

        assets_dir = Path(assets_dir)
        assets_src = assets_dir / ASSETS_DIRNAME
        if not assets_src.is_dir():
            raise VisualCachePublicationError(
                "assets_dir must contain an assets/ subdirectory"
            )
        for unit in units:
            paths = _mapping(unit.get("paths"))
            for ref_key in ("image_ref", "parent_image_ref"):
                ref = _mapping(paths.get(ref_key))
                relpath = _text(ref.get("relative"))
                if not relpath:
                    continue
                asset_path = assets_dir / relpath
                if not asset_path.is_file():
                    errors.append(f"missing_asset:{relpath}")
                    continue
                if ref_key == "image_ref":
                    expected = _text(
                        _mapping(unit.get("hashes")).get("image_sha256")
                    )
                    if _sha256_file(asset_path) != expected:
                        errors.append(f"asset_hash_mismatch:{relpath}")
        if errors:
            raise VisualCachePublicationError(
                "visual snapshot validation failed",
                errors=errors,
            )

        final_dir = self.root / version
        if final_dir.exists():
            raise VisualCachePublicationError(
                f"snapshot version already exists: {version}"
            )
        staging = self.root / (
            f".staging-{version}-{uuid.uuid4().hex[:8]}"
        )
        staging.mkdir(parents=True)
        try:
            (staging / ASSETS_DIRNAME).mkdir(parents=True)
            for source in sorted(assets_src.iterdir()):
                if source.is_file():
                    shutil.copy2(source, staging / ASSETS_DIRNAME / source.name)
            payload = {
                "schema_version": STORE_SCHEMA_VERSION,
                "version": version,
                "created_at": _now_utc(),
                "unit_count": len(units),
                "units": sorted(
                    units, key=lambda unit: _text(unit.get("unit_id"))
                ),
            }
            _write_json_atomic(staging / UNITS_FILENAME, payload)
            _build_sqlite_index(staging, payload["units"])

            files: dict[str, str] = {}
            for filename in (UNITS_FILENAME, SQLITE_FILENAME):
                files[filename] = _sha256_file(staging / filename)
            for asset in sorted((staging / ASSETS_DIRNAME).iterdir()):
                if asset.is_file():
                    files[f"{ASSETS_DIRNAME}/{asset.name}"] = _sha256_file(
                        asset
                    )
            manifest = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "version": version,
                "published_at": _now_utc(),
                "unit_count": len(units),
                "files": files,
                "path_roots": {
                    key: {"kind": "optional_source_originals"}
                    for key in dict(path_roots or {})
                },
            }
            _write_json_atomic(staging / MANIFEST_FILENAME, manifest)

            normalized_roots = {
                str(key): Path(value)
                for key, value in dict(path_roots or {}).items()
            }
            verification = _verify_snapshot_dir(
                staging,
                normalized_roots,
                expected_dir_name=version,
            )
            if verification["status"] != "passed":
                raise VisualCachePublicationError(
                    "staged snapshot verification failed",
                    errors=verification["errors"],
                    report=verification,
                )
            staging.rename(final_dir)
            self._write_latest(version)
            report = {
                "schema_version": "optomind.visual_cache_publish.v1",
                "status": "published",
                "version": version,
                "snapshot_dir": str(final_dir),
                "unit_count": len(units),
                "integrity": "ok",
                "published_at": manifest["published_at"],
            }
            return report
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, VisualCachePublicationError):
                raise
            raise VisualCachePublicationError(
                f"visual snapshot publish failed: {exc}"
            ) from exc

    def _write_latest(self, version: str) -> None:
        payload = {
            "schema_version": LATEST_SCHEMA_VERSION,
            "version": version,
            "published_at": _now_utc(),
        }
        _write_json_atomic(self.root / LATEST_FILENAME, payload)

    def latest_version(self) -> str | None:
        latest = _text(
            _read_json(self.root / LATEST_FILENAME).get("version")
        )
        if latest and (self.root / latest).is_dir():
            return latest
        versions = self.list_versions()
        return versions[-1] if versions else None

    def list_versions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        versions = [
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir()
            and (entry / MANIFEST_FILENAME).is_file()
            and not entry.name.startswith(".staging-")
        ]
        return sorted(
            versions,
            key=lambda value: [
                int(part) if part.isdigit() else part.lower()
                for part in re.split(r"(\d+)", value)
            ],
        )

    def snapshot_path(self, version: str) -> Path:
        path = self.root / validate_version(version)
        if not path.is_dir():
            raise VisualCachePublicationError(
                f"snapshot version not found: {version}"
            )
        return path

    def load_snapshot(self, version: str) -> dict[str, Any]:
        return _read_json(self.snapshot_path(version) / UNITS_FILENAME)

    def verify_snapshot(
        self,
        version: str,
        *,
        path_roots: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        normalized_roots = {
            str(key): Path(value)
            for key, value in dict(path_roots or {}).items()
        }
        return _verify_snapshot_dir(
            self.snapshot_path(version), normalized_roots
        )

    def get_unit(self, version: str, unit_id: str) -> dict[str, Any] | None:
        payload = self.load_snapshot(version)
        for unit in payload.get("units") or []:
            if isinstance(unit, dict) and _text(unit.get("unit_id")) == unit_id:
                return dict(unit)
        return None

    def resolve_unit_paths(
        self,
        version: str,
        unit: Mapping[str, Any],
        *,
        path_roots: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        snapshot_dir = self.snapshot_path(version)
        roots = {str(key): Path(value) for key, value in dict(path_roots or {}).items()}
        resolved: dict[str, str] = {}
        paths = _mapping(unit.get("paths"))
        image_ref = _mapping(paths.get("image_ref"))
        image_rel = _text(image_ref.get("relative"))
        if image_rel:
            resolved["image_path"] = str(snapshot_dir / image_rel)
        parent_ref = _mapping(paths.get("parent_image_ref"))
        parent_rel = _text(parent_ref.get("relative"))
        if parent_rel:
            resolved["parent_image_path"] = str(snapshot_dir / parent_rel)
        source_path = resolve_path_ref(paths.get("source_ref"), roots)
        if source_path is not None:
            resolved["source_path"] = str(source_path)
        return {**dict(unit), "resolved_paths": resolved}

    def search(
        self,
        version: str,
        query: str,
        *,
        limit: int = 50,
        path_roots: Mapping[str, Path | str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search the SQLite index; falls back to LIKE if FTS5 is absent."""

        snapshot_dir = self.snapshot_path(version)
        db_path = snapshot_dir / SQLITE_FILENAME
        query_text = _text(query)
        limit = max(1, min(int(limit), 500))
        conn = sqlite3.connect(str(db_path))
        try:
            unit_ids: list[str] = []
            try:
                fts_query = '"' + query_text.replace('"', '""') + '"'
                unit_ids = [
                    _text(row[0])
                    for row in conn.execute(
                        "SELECT unit_id FROM units_fts "
                        "WHERE units_fts MATCH ? ORDER BY rank LIMIT ?",
                        (fts_query, limit),
                    ).fetchall()
                ]
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pattern = f"%{query_text}%"
                unit_ids = [
                    _text(row[0])
                    for row in conn.execute(
                        "SELECT unit_id FROM units WHERE "
                        "lower(search_text) LIKE lower(?) OR "
                        "lower(caption_json) LIKE lower(?) LIMIT ?",
                        (pattern, pattern, limit),
                    ).fetchall()
                ]
        finally:
            conn.close()
        by_id = {
            _text(unit.get("unit_id")): unit
            for unit in self.load_snapshot(version).get("units") or []
            if isinstance(unit, dict)
        }
        roots = {
            str(key): Path(value)
            for key, value in dict(path_roots or {}).items()
        }
        return [
            self.resolve_unit_paths(
                version, by_id[unit_id], path_roots=roots
            )
            for unit_id in unit_ids
            if unit_id in by_id
        ]

    def related_source_nodes(
        self,
        version: str,
        node_id: str,
        *,
        relation: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Traverse source-map links in either direction.

        Older snapshots have no source-map tables and therefore return an
        empty list.  This keeps source maps additive while giving downstream
        claim, figure, and caption tools a stable bidirectional lookup API.
        """

        node_id = _text(node_id)
        if not node_id:
            return []
        relation = _text(relation)
        limit = max(1, min(int(limit), 500))
        db_path = self.snapshot_path(version) / SQLITE_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            relation_sql = ""
            if relation:
                relation_sql = (
                    " AND (l.relation=? OR l.inverse_relation=?)"
                )
            rows = conn.execute(
                """
                SELECT l.*, n.node_json,
                  CASE WHEN l.source_node_id=? THEN 'outgoing'
                       ELSE 'incoming' END AS traversal_direction,
                  CASE WHEN l.source_node_id=? THEN l.target_node_id
                       ELSE l.source_node_id END AS related_node_id
                FROM source_links AS l
                JOIN source_nodes AS n
                  ON n.node_id=(
                    CASE WHEN l.source_node_id=? THEN l.target_node_id
                         ELSE l.source_node_id END
                  )
                WHERE (l.source_node_id=? OR l.target_node_id=?)
                """
                + relation_sql
                + " ORDER BY l.relation,l.link_id LIMIT ?",
                [
                    node_id,
                    node_id,
                    node_id,
                    node_id,
                    node_id,
                    *(
                        [relation, relation]
                        if relation
                        else []
                    ),
                    limit,
                ],
            ).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            row_data = dict(row)
            try:
                related_node = json.loads(row_data.pop("node_json") or "{}")
            except Exception:
                related_node = {}
            result.append({**row_data, "related_node": related_node})
        return result


def publish_visual_snapshot(
    *,
    cache_root: Path | str,
    version: str,
    units: Iterable[Mapping[str, Any]],
    assets_dir: Path | str,
    path_roots: Mapping[str, Path | str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`VisualCacheStore.publish_snapshot`."""

    return VisualCacheStore(cache_root).publish_snapshot(
        version=version,
        units=units,
        assets_dir=assets_dir,
        path_roots=path_roots,
    )


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "VisualCachePublicationError",
    "VisualCacheStore",
    "publish_visual_snapshot",
]
