"""Sidecar adapter for the article-driven visual asset planner.

The adapter reads completed section drafts plus a visual-cache index and
writes:

* ``VISUAL_CONSTRUCTION_PLAN.json`` - the rich deterministic plan;
* ``VISUAL_EDITORIAL_PLAN.json`` - the factory-compatible subset;
* ``ARTICLE_VISUAL_IMAGE_REVIEW_QUEUE.json`` - final-candidate review state.

Supported visual-cache inputs:

* legacy ``visual_chunks`` SQLite tables (via ``VisualArgumentAligner``);
* published long-term visual-cache snapshots: a snapshot directory, its
  ``units.json`` payload, or its ``visual_cache.sqlite`` with nested JSON
  columns in the ``units`` table;
* legacy JSON/JSONL record lists.

Snapshot units are adapted into the existing planner record shape.  Image
paths are resolved relative to the snapshot directory.  Parent-context units
remain whole-figure candidates for the existing whole-figure selection rules.
``permission_state.use_permission`` never implies display permission on its
own: the planner status defaults to ``requires_review`` unless an explicit
visual reuse permission or permissive license exists.  Approval stays
``pending_multimodal_review`` until an explicit approval marker is present.

The adapter performs no model calls and no writes to existing visual
editor/factory modules.  It is intentionally additive and fails open.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from optomind_research.visual_argument_alignment import (
    VisualArgumentAligner,
)
from optomind_research.visual_argument_protocol import (
    VALID_VISUAL_ARGUMENT_TYPES,
    infer_visual_argument_type,
)

from .article_visual_asset_planner import (
    ArticleVisualAssetPlannerConfig,
    plan_article_visual_assets,
)
from .artifact_store import atomic_write_json

UNIT_SCHEMA_VERSION = "optomind.visual_unit.v1"

_SNAPSHOT_JSON_COLUMNS = {
    "source_identity_json": "source_identity",
    "figure_identity_json": "figure_identity",
    "caption_json": "caption",
    "semantic_json": "semantic",
    "argumentative_roles_json": "argumentative_roles",
    "provenance_json": "provenance",
    "permission_state_json": "permission_state",
    "hashes_json": "hashes",
    "vector_refs_json": "vector_refs",
    "lineage_json": "lineage",
    "use_history_json": "use_history",
    "review_json": "review",
    "approval_json": "approval",
    "source_map_json": "source_map",
    "paths_json": "paths",
    "crop_hygiene_json": "crop_hygiene",
}

_SNAPSHOT_SCALAR_COLUMNS = (
    "unit_id",
    "schema_version",
    "unit_kind",
    "unit_role",
    "parent_unit_id",
    "image_relpath",
    "original_image_relpath",
    "approval_state",
    "review_decision",
    "search_text",
    "created_at",
)

_PERMISSIVE_LICENSE_PREFIXES = (
    "cc-",
    "cc0",
    "creative commons",
    "public domain",
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _looks_like_snapshot_unit(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get("unit_id")) and (
        value.get("schema_version") == UNIT_SCHEMA_VERSION
        or isinstance(value.get("paths"), dict)
        or isinstance(value.get("permission_state"), dict)
    )


def _snapshot_units_from_json_value(
    value: Any,
) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        units = value.get("units")
        if isinstance(units, list):
            return [row for row in units if isinstance(row, dict)]
        return [row for row in value.values() if isinstance(row, dict)]
    return []


def _unit_caption(unit: Mapping[str, Any]) -> str:
    caption = _mapping(unit.get("caption"))
    return _text(
        caption.get("clean")
        or caption.get("original")
        or caption.get("subfigure_focus")
    )


def _unit_search_text(unit: Mapping[str, Any]) -> str:
    semantic = _mapping(unit.get("semantic"))
    caption = _mapping(unit.get("caption"))
    card = _mapping(semantic.get("visual_card"))
    parts = [
        caption.get("clean"),
        caption.get("original"),
        caption.get("subfigure_focus"),
        semantic.get("description"),
        " ".join(_list_strings(semantic.get("tags"))),
        semantic.get("nearby_text"),
        card.get("one_sentence_summary"),
        " ".join(_list_strings(semantic.get("body_callout_texts"))),
        " ".join(_list_strings(semantic.get("linked_text_chunk_ids"))),
    ]
    return " ".join(_text(part) for part in parts if _text(part))


def _unit_visual_role(unit: Mapping[str, Any]) -> str:
    semantic = _mapping(unit.get("semantic"))
    profile = _mapping(semantic.get("visual_profile"))
    intrinsic = _mapping(profile.get("intrinsic_visual_labels"))
    return _text(
        intrinsic.get("visual_role")
        or profile.get("visual_role")
        or intrinsic.get("visual_content_type")
        or profile.get("visual_content_type")
    )


def _unit_permission(unit: Mapping[str, Any]) -> dict[str, Any]:
    permission_state = _mapping(unit.get("permission_state"))
    marker = _text(
        permission_state.get("visual_reuse_permission")
        or permission_state.get("reuse_permission")
        or permission_state.get("display_permission")
        or permission_state.get("copyright_permission")
        or permission_state.get("permission_status")
    )
    reuse_allowed = permission_state.get("reuse_allowed")
    license_text = _text(permission_state.get("license")).lower()
    license_permissive = license_text.startswith(
        _PERMISSIVE_LICENSE_PREFIXES
    ) or license_text in {"cc by", "cc-by", "cc0"}
    marker_lower = marker.lower()
    restricted_markers = {
        "restricted",
        "denied",
        "forbidden",
        "no",
        "false",
        "prohibited",
        "copyrighted",
        "closed",
    }
    allowed_markers = {
        "allowed",
        "yes",
        "permitted",
        "granted",
        "true",
        "ok",
        "open",
        "cc-by",
        "cc0",
        "public domain",
        "creative commons",
    }
    if marker_lower in restricted_markers or (
        isinstance(reuse_allowed, bool) and not reuse_allowed
    ):
        status = "restricted"
    elif (
        marker_lower in allowed_markers
        or license_permissive
        or (isinstance(reuse_allowed, bool) and reuse_allowed)
    ):
        status = "allowed"
    else:
        status = "requires_review"
    notes = _list_strings(permission_state.get("notes"))
    note = "; ".join(notes)
    if not note:
        note = _text(permission_state.get("permission_source"))
    return {
        "status": status,
        "license": _text(permission_state.get("license")),
        "note": note,
        "use_permission": _text(permission_state.get("use_permission")),
        "permission_source": _text(
            permission_state.get("permission_source")
        ),
        "allowed_claim_kinds": _list_strings(
            permission_state.get("allowed_claim_kinds")
        ),
        "explicit_reuse_permission": status == "allowed",
    }


def _unit_approval(unit: Mapping[str, Any]) -> tuple[str, bool]:
    approval = _mapping(unit.get("approval"))
    state = _text(approval.get("state"))
    marker = _text(approval.get("source_marker"))
    if state == "approved" and marker:
        return "ok", False
    if state == "rejected":
        return "failed", True
    return "pending_multimodal_review", True


def _unit_image_path(
    unit: Mapping[str, Any],
    snapshot_dir: Path,
) -> str:
    paths = _mapping(unit.get("paths"))
    image_ref = _mapping(paths.get("image_ref"))
    relative = _text(
        image_ref.get("relative") or unit.get("image_relpath")
    )
    if not relative:
        return ""
    return str(Path(snapshot_dir) / relative)


def _resolve_snapshot_ref(
    ref: Any,
    snapshot_dir: Path,
) -> str:
    if isinstance(ref, str):
        relative = _text(ref)
    else:
        ref = _mapping(ref)
        relative = _text(ref.get("relative"))
    if not relative:
        return ""
    return str(Path(snapshot_dir) / relative)


def _adapt_snapshot_unit(
    unit: Mapping[str, Any],
    *,
    snapshot_dir: Path,
) -> dict[str, Any]:
    source = _mapping(unit.get("source_identity"))
    figure = _mapping(unit.get("figure_identity"))
    argument = _mapping(unit.get("argumentative_roles"))
    provenance = _mapping(unit.get("provenance"))
    semantic = _mapping(unit.get("semantic"))
    quality = _mapping(semantic.get("quality"))
    hashes = _mapping(unit.get("hashes"))
    review = _mapping(unit.get("review"))
    lineage = _mapping(unit.get("lineage"))
    source_map = _mapping(unit.get("source_map"))
    unit_id = _text(unit.get("unit_id"))
    unit_kind = _text(unit.get("unit_kind")) or "single_figure"
    caption_text = _unit_caption(unit)
    # Preserve the original caption and figure label explicitly instead of
    # relying on the clean-caption or parent-label fallbacks downstream.
    caption_original = _text((_mapping(unit.get("caption"))).get("original"))
    figure_label = _text(figure.get("figure_label"))
    approval_status, approval_needs_review = _unit_approval(unit)
    is_generated = unit_kind == "generated_visual" or _text(
        source.get("source_kind")
    ) == "ai_generated_explanatory_visual"
    generated_paper_id = (
        f"generated:{unit_id}" if is_generated and unit_id else ""
    )
    primary_type = _text(argument.get("primary"))
    if primary_type not in VALID_VISUAL_ARGUMENT_TYPES:
        primary_type = ""
    parent_chunk_id = (
        _text(lineage.get("parent_unit_id"))
        if unit_kind == "subfigure"
        else ""
    )
    source_file_ref = _mapping(provenance.get("source_file_ref"))
    permission = _unit_permission(unit)
    image_path = _unit_image_path(unit, snapshot_dir)
    paths = _mapping(unit.get("paths"))
    crop_hygiene = _mapping(unit.get("crop_hygiene"))
    hygiene_derivative = _mapping(crop_hygiene.get("derivative"))
    original_image_path = _resolve_snapshot_ref(
        paths.get("original_image_ref"),
        snapshot_dir,
    )
    image_origin = (
        "derived_clean"
        if str(crop_hygiene.get("status") or "").lower() == "derived_clean"
        and hygiene_derivative
        else "clean_or_original"
    )
    return {
        "chunk_id": unit_id,
        "visual_chunk_id": unit_id,
        "legacy_chunk_id": _text(figure.get("asset_id")),
        "paper_id": generated_paper_id
        or _text(source.get("paper_id"))
        or _text(source.get("doi")),
        "doi": _text(source.get("doi")),
        "title": _text(source.get("title")),
        "caption": caption_text,
        "caption_original": caption_original,
        "caption_missing": bool(
            _mapping(unit.get("caption")).get("missing")
            or _text(
                _mapping(unit.get("provenance")).get("caption_status")
            )
            == "missing_needs_review"
        ),
        "crop_contamination": bool(
            _mapping(
                _mapping(unit.get("crop_hygiene")).get(
                    "caption_contamination"
                )
            ).get("detected")
        ),
        "caption_preview": caption_text,
        "chunk_kind": unit_kind,
        "unit_role": _text(unit.get("unit_role")),
        "search_text": _unit_search_text(unit),
        "linked_text_chunk_ids": _list_strings(
            semantic.get("linked_text_chunk_ids")
        ),
        "body_callout_texts": _list_strings(
            semantic.get("body_callout_texts")
        ),
        "labels": _list_strings(semantic.get("tags")),
        "visual_argument_type": primary_type,
        "visual_argument_status": approval_status,
        "visual_argument_confidence": _text(argument.get("confidence"))
        or "medium",
        "visual_argument_claim": _text(argument.get("claim")),
        "visual_argument_needs_human_review": approval_needs_review,
        "visual_role": _unit_visual_role(unit),
        "review_utility": _text(argument.get("review_utility")).lower(),
        "parent_chunk_id": parent_chunk_id,
        "figure_label": figure_label,
        "parent_label": _text(figure.get("parent_label")),
        "subfigure_label": _text(figure.get("subfigure_label")),
        "local_image_path": image_path,
        "source_file": _text(source_file_ref.get("relative"))
        or _text(provenance.get("source_file")),
        "source_url": _text(provenance.get("source_url")),
        "remote_image_url": "",
        "parser": _text(provenance.get("parser")),
        "page": (
            figure.get("page")
            if figure.get("page") is not None
            else provenance.get("page")
        ),
        "checksum": _text(provenance.get("checksum"))
        or _text(hashes.get("image_sha256")),
        "permission": permission,
        "is_duplicate": _bool(quality.get("is_duplicate")),
        "failure_reason": _text(quality.get("failure_reason")),
        "supporting_claim_ids": [],
        "warnings": _list_strings(quality.get("warnings"))
        + _list_strings(review.get("review_flags")),
        "source_route": "generated_cache" if is_generated else "local_cache",
        "cache_format": "snapshot",
        "generated_visual": is_generated,
        "source_kind": _text(source.get("source_kind")),
        "source_map": source_map,
        "source_map_ref": {
            "unit_id": _text(source_map.get("unit_id")) or unit_id,
            "root_visual_node_id": _text(
                source_map.get("root_visual_node_id")
            ),
            "caption_node_ids": _list_strings(
                source_map.get("caption_node_ids")
            ),
            "linked_text_node_ids": _list_strings(
                source_map.get("linked_text_node_ids")
            ),
        },
        "generated_identity": (
            "article_owned_generated_visual" if is_generated else ""
        ),
        "required_disclosure": (
            "AI-generated explanatory visual" if is_generated else ""
        ),
        "crop_hygiene": crop_hygiene,
        "original_image_path": original_image_path,
        "hygiene_derivative_path": _resolve_snapshot_ref(
            hygiene_derivative.get("relpath"),
            snapshot_dir,
        )
        if hygiene_derivative.get("relpath")
        else "",
        "image_origin": image_origin,
        "snapshot_unit": {
            "unit_id": unit_id,
            "unit_kind": unit_kind,
            "unit_role": _text(unit.get("unit_role")),
            "approval": _mapping(unit.get("approval")),
            "permission_state": _mapping(unit.get("permission_state")),
            "lineage": lineage,
            "paths": _mapping(unit.get("paths")),
        },
    }


def _adapt_snapshot_units(
    units: Sequence[Mapping[str, Any]],
    *,
    snapshot_dir: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, Mapping) or not _text(unit.get("unit_id")):
            continue
        try:
            records.append(
                _adapt_snapshot_unit(unit, snapshot_dir=snapshot_dir)
            )
        except Exception:
            continue
    return records


def _sqlite_table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _load_units_sqlite(
    sqlite_path: Path,
    *,
    snapshot_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Decode the snapshot ``units`` table with nested JSON columns."""

    snapshot_dir = Path(snapshot_dir or sqlite_path.parent)
    conn = sqlite3.connect(str(sqlite_path))
    try:
        available = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(units)").fetchall()
        }
        if "unit_id" not in available:
            raise ValueError("units table is missing unit_id")
        columns = [
            column
            for column in [
                *_SNAPSHOT_SCALAR_COLUMNS,
                *_SNAPSHOT_JSON_COLUMNS.keys(),
            ]
            if column in available
        ]
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM units"
        ).fetchall()
    finally:
        conn.close()

    units: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(zip(columns, row))
        unit: dict[str, Any] = {}
        for column in _SNAPSHOT_SCALAR_COLUMNS:
            if column in raw:
                unit[column] = raw[column]
        for column, key in _SNAPSHOT_JSON_COLUMNS.items():
            if column not in raw:
                continue
            try:
                parsed = json.loads(str(raw[column] or "{}"))
            except Exception:
                parsed = {}
            unit[key] = parsed if isinstance(parsed, dict) else {}
        if not unit.get("paths") and unit.get("image_relpath"):
            unit["paths"] = {
                "image_ref": {
                    "root": "snapshot",
                    "relative": unit["image_relpath"],
                }
            }
        if (
            isinstance(unit.get("paths"), dict)
            and not (unit.get("paths") or {}).get("original_image_ref")
            and unit.get("original_image_relpath")
        ):
            unit["paths"]["original_image_ref"] = {
                "root": "snapshot",
                "relative": unit["original_image_relpath"],
            }
        units.append(unit)
    # The published SQLite index predates crop-hygiene metadata, so enrich
    # rows from the companion units.json payload when it exists.  This keeps
    # hygiene filtering identical across all snapshot formats without
    # duplicating snapshot parsing.
    companion = Path(snapshot_dir) / "units.json"
    if companion.is_file():
        try:
            full_units = _snapshot_units_from_json_value(
                json.loads(
                    companion.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                )
            )
            by_id = {
                _text(full_unit.get("unit_id")): full_unit
                for full_unit in full_units
                if isinstance(full_unit, Mapping)
            }
            for unit in units:
                full_unit = by_id.get(_text(unit.get("unit_id")))
                if not isinstance(full_unit, Mapping):
                    continue
                for key in ("crop_hygiene", "source_map"):
                    if key not in unit and key in full_unit:
                        unit[key] = full_unit[key]
        except Exception:
            pass
    return _adapt_snapshot_units(units, snapshot_dir=snapshot_dir)


def _load_units_json(units_path: Path) -> list[dict[str, Any]]:
    value = json.loads(
        units_path.read_text(encoding="utf-8", errors="replace")
    )
    units = _snapshot_units_from_json_value(value)
    return _adapt_snapshot_units(
        units,
        snapshot_dir=units_path.parent,
    )


def _load_snapshot_dir(snapshot_dir: Path) -> list[dict[str, Any]]:
    units_path = snapshot_dir / "units.json"
    if units_path.is_file():
        return _load_units_json(units_path)
    sqlite_path = snapshot_dir / "visual_cache.sqlite"
    if sqlite_path.is_file():
        return _load_units_sqlite(sqlite_path, snapshot_dir=snapshot_dir)
    raise ValueError(
        f"snapshot directory has no units.json or visual_cache.sqlite: "
        f"{snapshot_dir}"
    )


def _looks_like_final_visual_package(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and str(value.get("schema_version") or "").startswith(
            "research_harness.final_visual_package."
        )
        and isinstance(value.get("figures"), list)
    )


def _doi_from_source_path(value: Any) -> str:
    """Recover the common DOI-shaped folder convention when available."""

    text = str(value or "").replace("\\", "/")
    match = re.search(r"/(10\.\d{4,9})-([^/]+)/", text)
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _adapt_final_visual_package(
    value: Mapping[str, Any],
    *,
    package_dir: Path,
) -> list[dict[str, Any]]:
    """Expose an earlier final package as a remountable visual cache.

    This adapter preserves the package's review and permission boundary.  A
    source figure accepted for an internal draft remains permission-pending
    and publication-ineligible; generated explanatory figures are identified
    as article-owned and keep their AI disclosure.
    """

    records: list[dict[str, Any]] = []
    for raw in value.get("figures") or []:
        if not isinstance(raw, Mapping):
            continue
        figure = dict(raw)
        figure_id = _text(figure.get("figure_id"))
        if not figure_id:
            continue
        generated = _text(figure.get("generated_or_source")).lower() == (
            "generated"
        )
        local_path = Path(_text(figure.get("local_path")))
        if local_path and not local_path.is_absolute():
            local_path = package_dir / local_path
        source_attribution = _mapping(figure.get("source_attribution"))
        doi = _text(source_attribution.get("doi")) or _doi_from_source_path(
            figure.get("original_source_path")
        )
        paper_id = _text(source_attribution.get("paper_id"))
        if not paper_id:
            paper_id = (
                f"generated:{figure_id}"
                if generated
                else doi or f"visual-package:{figure_id}"
            )
        source_audit = _mapping(figure.get("source_audit"))
        review_decision = _text(figure.get("review_decision")).lower()
        audit_verdict = _text(source_audit.get("verdict")).lower()
        rejected = audit_verdict in {"reject", "rejected", "exclude"} or (
            "reject" in review_decision
        )
        approved = generated or audit_verdict in {"approve", "approved"} or (
            review_decision
            in {
                "approved",
                "accept",
                "accepted",
                "timeout_accepted_for_draft",
            }
        )
        permission = _mapping(figure.get("permission"))
        if generated:
            permission = {
                **permission,
                "status": "allowed",
                "use_permission": "contextual_or_qualified_support",
                "note": "Article-owned AI-generated explanatory visual.",
            }
        else:
            permission = {
                **permission,
                "status": _text(permission.get("status"))
                or "requires_review",
                "use_permission": _text(permission.get("use_permission"))
                or "contextual_or_qualified_support",
                "note": _text(permission.get("note"))
                or _text(figure.get("rights_notice")),
            }
        caption = _text(
            figure.get("caption_en")
            or figure.get("source_caption")
            or source_audit.get("editorial_caption")
        )
        purpose = _text(figure.get("purpose"))
        record = {
            "chunk_id": figure_id,
            "visual_chunk_id": figure_id,
            "paper_id": paper_id,
            "doi": doi,
            "title": purpose or caption,
            "caption": caption,
            "caption_original": _text(figure.get("source_caption"))
            or caption,
            "caption_preview": caption,
            "chunk_kind": (
                "generated_visual" if generated else "single_figure"
            ),
            "search_text": " ".join(
                part
                for part in (
                    purpose,
                    caption,
                    _text(source_audit.get("usefulness")),
                    _text(source_audit.get("reason")),
                )
                if part
            ),
            "labels": [
                _text(figure.get("figure_type")),
                _text(figure.get("data_provenance_level")),
            ],
            "visual_argument_status": (
                "failed" if rejected else "ok" if approved else "pending"
            ),
            "visual_argument_confidence": (
                "high" if approved and not rejected else "medium"
            ),
            "visual_argument_claim": purpose,
            "visual_argument_needs_human_review": False,
            "visual_role": _text(figure.get("figure_type")),
            "review_utility": "high" if approved else "medium",
            "figure_label": figure_id,
            "local_image_path": str(local_path),
            "original_image_path": _text(
                figure.get("original_source_path")
            ),
            "permission": permission,
            "publication_eligible": bool(
                figure.get("publication_eligible")
            )
            if not generated
            else True,
            "publication_eligible_reason": _text(
                figure.get("publication_eligible_reason")
            )
            or (
                "article_owned_generated_visual"
                if generated
                else "source_rights_review_required"
            ),
            "warnings": _list_strings(figure.get("review_flags")),
            "source_route": (
                "generated_cache" if generated else "final_package_cache"
            ),
            "generated_visual": generated,
            "source_kind": (
                "ai_generated_explanatory_visual"
                if generated
                else "source_figure"
            ),
            "source_map": _mapping(figure.get("source_map")),
            "required_disclosure": (
                "AI-generated explanatory visual" if generated else ""
            ),
        }
        record["visual_argument_type"] = infer_visual_argument_type(record)
        records.append(record)
    return records


def load_visual_cache_records(
    path: Path,
    aligner: Optional[VisualArgumentAligner] = None,
) -> list[Dict[str, Any]]:
    """Load searchable visual-cache records from snapshot or legacy formats."""

    source = Path(path)
    if source.is_dir():
        return _load_snapshot_dir(source)
    if not source.is_file():
        raise FileNotFoundError(f"visual cache index not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        tables = _sqlite_table_names(source)
        if "units" in tables:
            return _load_units_sqlite(source, snapshot_dir=source.parent)
        if "visual_chunks" in tables:
            loader = aligner or VisualArgumentAligner()
            return loader.load_visual_chunks_from_sqlite(source)
        raise ValueError(
            f"sqlite index has neither units nor visual_chunks: {source}"
        )
    if suffix == ".jsonl":
        records: list[Dict[str, Any]] = []
        for line_number, line in enumerate(
            source.read_text(encoding="utf-8", errors="replace").splitlines(),
            1,
        ):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL line {line_number} in {source}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"JSONL line {line_number} in {source} is not an object"
                )
            records.append(value)
        if records and all(
            _looks_like_snapshot_unit(row) for row in records
        ):
            return _adapt_snapshot_units(
                records,
                snapshot_dir=source.parent,
            )
        return records
    if suffix == ".json":
        value = json.loads(
            source.read_text(encoding="utf-8", errors="replace")
        )
        if _looks_like_final_visual_package(value):
            return _adapt_final_visual_package(
                value,
                package_dir=source.parent,
            )
        if isinstance(value, dict) and isinstance(value.get("units"), list):
            units = _snapshot_units_from_json_value(value)
            return _adapt_snapshot_units(
                units,
                snapshot_dir=source.parent,
            )
        if isinstance(value, list):
            if value and all(
                _looks_like_snapshot_unit(row) for row in value
            ):
                return _adapt_snapshot_units(
                    value,
                    snapshot_dir=source.parent,
                )
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            keyed_rows = [
                (key, row)
                for key, row in value.items()
                if isinstance(row, dict)
            ]
            rows = [row for _, row in keyed_rows]
            if rows and all(
                _looks_like_snapshot_unit(row) for row in rows
            ):
                return _adapt_snapshot_units(
                    rows,
                    snapshot_dir=source.parent,
                )
            records = []
            for key, row in keyed_rows:
                row = dict(row)
                row.setdefault("chunk_id", str(key))
                records.append(row)
            return records
        raise ValueError(
            f"JSON visual cache must be a list or object: {source}"
        )
    raise ValueError(
        f"unsupported visual cache format '{suffix}' for {source}"
    )


def _merge_section_text_files(
    sections: Sequence[Dict[str, Any]],
    section_text_files: Optional[Mapping[str, Path]],
) -> list[Dict[str, Any]]:
    merged: list[Dict[str, Any]] = []
    for section in sections:
        if isinstance(section, dict):
            row = dict(section)
        else:
            try:
                row = dict(section)
            except Exception:
                row = {}
        section_id = str(row.get("section_id") or "")
        if section_text_files and section_id in section_text_files:
            draft_path = Path(section_text_files[section_id])
            if draft_path.is_file():
                row["text"] = draft_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
        merged.append(row)
    return merged


def run_article_visual_asset_planner(
    *,
    sections: Sequence[Dict[str, Any]],
    visual_cache_paths: Sequence[Path] = (),
    section_text_files: Optional[Mapping[str, Path]] = None,
    output_dir: Optional[Path] = None,
    config: Optional[ArticleVisualAssetPlannerConfig] = None,
) -> Dict[str, Any]:
    """Run the deterministic sidecar planner and optionally persist artifacts.

    Fail-open behavior: unreadable cache files and malformed sections are
    recorded in ``validation.errors`` while valid sections still produce a
    partial plan.  No network or model calls are made.
    """

    merged_sections = _merge_section_text_files(
        sections,
        section_text_files,
    )
    records: list[Dict[str, Any]] = []
    load_errors: list[str] = []
    for raw_path in visual_cache_paths or ():
        path = Path(raw_path)
        try:
            records.extend(load_visual_cache_records(path))
        except Exception as exc:
            load_errors.append(
                f"visual_cache_load_failed:{path.name}:"
                f"{type(exc).__name__}:{exc}"
            )
    plan = plan_article_visual_assets(
        sections=merged_sections,
        visual_cache_records=records,
        config=config,
    )
    if load_errors:
        plan["validation"]["errors"].extend(load_errors)
        if plan["validation"]["status"] == "passed":
            plan["validation"]["status"] = "degraded"
        plan["validation"]["errors"] = plan["validation"]["errors"][:20]
    if output_dir is not None:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            target / "VISUAL_CONSTRUCTION_PLAN.json",
            plan,
        )
        atomic_write_json(
            target / "VISUAL_EDITORIAL_PLAN.json",
            plan["visual_editorial_plan"],
        )
        atomic_write_json(
            target / "ARTICLE_VISUAL_IMAGE_REVIEW_QUEUE.json",
            plan["image_review_queue"],
        )
    return plan


__all__ = [
    "load_visual_cache_records",
    "run_article_visual_asset_planner",
]
