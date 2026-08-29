"""Ingest existing pending visual candidates into durable visual units.

The long-term visual cache is additive: it reads visual chunks, visual
assets, supplemental staging candidates, and editorial-plan/final-package
records without modifying any of those sources.  Candidate processing is
fail-open (one bad asset is skipped with a report entry), while snapshot
publication remains fail-closed.

No model/network calls happen here.  Image files are copied into a
content-addressed asset directory; the original source files are never
overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image

from optomind_research.visual_argument_protocol import infer_visual_argument_type

from .visual_cache_schemas import (
    ASSETS_DIRNAME,
    ASSET_KINDS,
    DEFAULT_PERMISSION,
    GENERATED_DISCLOSURE,
    GENERATED_SOURCE_KIND,
    GENERATED_VISUAL_KIND,
    PERMISSION_LEVELS,
    UNIT_SCHEMA_VERSION,
    canonical_json_hash,
    derive_approval_state,
    derive_asset_kind,
    derive_publication_eligibility,
    portable_path_ref,
)
from .visual_crop_hygiene import (
    QWEN_ADVISOR_CROP_METHOD,
    audit_crop_hygiene,
    materialize_advisor_crop,
)
from .visual_local_vectors import attach_local_vector_refs
from .visual_source_contracts import build_visual_source_map


INGEST_SCHEMA_VERSION = "optomind.visual_cache_ingest.v1"
_SAFE_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
)


class CandidateIngestError(RuntimeError):
    """Isolated per-candidate failure; ingestion continues for other rows."""


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _list_strings(value: Any, limit: int = 64) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ok"}
    return bool(value)


def _float(value: Any) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _pick(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _advisor_value(record: Mapping[str, Any]) -> Any:
    """Return a parsed or raw Qwen advisor payload from a candidate."""

    for key in ("qwen_crop_advisor", "crop_advisor", "advisor"):
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_image_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if suffix in _SAFE_IMAGE_SUFFIXES else ".png"


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    path = Path(path)
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def candidates_from_visual_chunk_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Return ``visual_chunk.v1`` / HQ-tagged records from a JSONL file."""

    return read_jsonl(path)


def candidates_from_visual_asset_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Return ``visual_asset.v1.1`` protocol records from a JSONL file."""

    return read_jsonl(path)


def _normalize_staging_path(value: str) -> str:
    """Normalize a stored path for stable cross-table matching."""

    try:
        path = Path(str(value))
        if path.exists():
            path = path.resolve()
        return path.as_posix().casefold()
    except (OSError, ValueError):
        return str(value).replace("\\", "/").casefold()


def _staging_identity_keys(data: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Return stable id keys and image keys for one staging row.

    Ids alone can collide across tables, so image hash/path keys are also
    collected.  ``parent_asset_id`` is deliberately excluded: a child crop
    and its parent figure are related but must remain separate candidates.
    """

    id_keys: set[str] = set()
    for key in (
        "candidate_visual_id",
        "chunk_id",
        "asset_id",
        "visual_id",
        "figure_id",
    ):
        value = _text(data.get(key))
        if value:
            id_keys.add(value)
    image_keys: set[str] = set()
    for key in ("image_sha256", "sha256"):
        value = _text(data.get(key)).removeprefix("sha256:")
        if value:
            image_keys.add(value.casefold())
    local_resources = _mapping(data.get("local_resources"))
    for key in (
        "local_image_path",
        "image_path",
        "path",
        "local_path",
    ):
        value = _text(data.get(key) or local_resources.get(key))
        if value:
            image_keys.add(_normalize_staging_path(value))
    return id_keys, image_keys


def _merge_staging_rows(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> dict[str, Any]:
    """Fill empty fields from secondary without overwriting semantic rows."""

    merged = dict(primary)
    for key, value in secondary.items():
        if key in merged and merged[key] not in (None, ""):
            continue
        merged[key] = value
    return merged


def candidates_from_staging_kb(path: Path | str) -> list[dict[str, Any]]:
    """Read and merge pending visual candidates from a supplemental staging DB.

    Semantic/VLM rows in ``visual_chunks`` and raw provenance/crop rows in
    ``visual_assets`` / ``visual_candidate_queue`` frequently use different
    ids for the same extracted image.  Rows are joined on stable ids,
    image hashes, or normalized image paths; the highest-priority semantic
    row is preserved and empty provenance/crop fields are filled from the
    raw tables.  Unrelated figures are never collapsed because parent ids
    are not merge keys.
    """

    path = Path(path)
    if not path.is_file():
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, id_column, rank in (
            ("visual_chunks", "chunk_id", 0),
            ("visual_assets", "asset_id", 1),
            ("visual_candidate_queue", "candidate_visual_id", 2),
        ):
            if table not in tables:
                continue
            try:
                table_rows = conn.execute(
                    f"SELECT * FROM {table}"
                ).fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in table_rows:
                data = dict(row)
                candidate_id = _text(data.get(id_column))
                raw_json = data.get("raw_json")
                if isinstance(raw_json, str) and raw_json.strip():
                    try:
                        raw = json.loads(raw_json)
                        if isinstance(raw, dict):
                            data = {**data, **raw}
                    except (ValueError, json.JSONDecodeError):
                        pass
                if not candidate_id and not _text(
                    data.get("image_path")
                ) and not _text(data.get("local_image_path")):
                    continue
                rows.append((rank, data))
    finally:
        conn.close()

    if not rows:
        return []

    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    identities = [
        _staging_identity_keys(data)
        for _, data in rows
    ]
    for left in range(len(rows)):
        left_ids, left_images = identities[left]
        for right in range(left + 1, len(rows)):
            right_ids, right_images = identities[right]
            if left_ids & right_ids or left_images & right_images:
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(rows)):
        groups.setdefault(find(index), []).append(index)

    merged: list[dict[str, Any]] = []
    for group in groups.values():
        group.sort(key=lambda index: (rows[index][0], index))
        primary = rows[group[0]][1]
        for index in group[1:]:
            primary = _merge_staging_rows(primary, rows[index][1])
        merged.append(primary)

    def row_sort_key(data: Mapping[str, Any]) -> str:
        return _text(
            data.get("chunk_id")
            or data.get("asset_id")
            or data.get("candidate_visual_id")
            or data.get("figure_id")
            or ""
        ).casefold()

    return sorted(merged, key=row_sort_key)


def _normalize_generation_metadata(
    record: Mapping[str, Any],
    raw_generation: Mapping[str, Any],
    *,
    is_generated: bool,
    review_decision: str,
) -> dict[str, Any]:
    """Normalize generation/attempt/review metadata for generated visuals."""

    generation = dict(raw_generation)
    if not is_generated:
        return generation
    model = _pick(
        generation.get("model_version"),
        generation.get("model_name"),
        generation.get("generation_model_used"),
        record.get("image_model"),
        record.get("model_version"),
    )
    prompt = _pick(
        generation.get("prompt"),
        generation.get("generation_brief"),
        generation.get("prompt_text"),
        record.get("prompt"),
        record.get("generation_brief"),
    )
    prompt_ref = _pick(
        generation.get("prompt_ref"),
        generation.get("prompt_path"),
        record.get("prompt_path"),
    )
    created_at = _pick(
        generation.get("created_at"),
        generation.get("generation_created_at"),
        record.get("created_at"),
    ) or _now_utc()
    disclosure = (
        _pick(generation.get("disclosure")) or GENERATED_DISCLOSURE
    )
    review_history = generation.get("review_history")
    if not isinstance(review_history, list):
        review_history = []
    if not review_history:
        review_history = [
            {
                "decision": _text(review_decision) or "approved",
                "reviewer": "human_review",
                "reviewed_at": _now_utc(),
                "note": "approved_generated_visual",
            }
        ]
    attempt_history = (
        generation.get("attempt_history")
        or generation.get("image_generation_attempts")
        or generation.get("retry_history")
        or record.get("attempt_history")
    )
    if not isinstance(attempt_history, list):
        attempt_history = []
    return {
        **generation,
        "model_version": model,
        "model_name": _pick(generation.get("model_name")) or model,
        "prompt": prompt,
        "prompt_ref": prompt_ref,
        "disclosure": disclosure,
        "created_at": created_at,
        "review_history": review_history,
        "attempt_history": attempt_history,
    }


def _normalize_candidate(record: Mapping[str, Any]) -> dict[str, Any]:
    r = dict(record)
    raw_generation = _mapping(
        r.get("generation")
        or r.get("generation_info")
        or r.get("generation_result")
    )
    declared_kind = _text(r.get("unit_kind"))
    route = _text(r.get("source_route") or r.get("generated_or_source"))
    source_kind_value = _text(
        r.get("source_kind") or raw_generation.get("source_kind")
    )
    is_generated_visual = bool(
        declared_kind == GENERATED_VISUAL_KIND
        or source_kind_value == GENERATED_SOURCE_KIND
        or route == "conceptual_generated"
        or _text(r.get("generation_status")) == "ai_generated"
    )
    paper = _mapping(r.get("paper"))
    ident = _mapping(r.get("asset_identity"))
    local_resources = _mapping(r.get("local_resources"))
    source_prov = _mapping(r.get("source_provenance"))
    document_ctx = _mapping(r.get("document_context"))
    text_link = _mapping(r.get("text_linkage"))
    profile = _mapping(r.get("visual_profile"))
    intrinsic = _mapping(profile.get("intrinsic_visual_labels"))
    task = _mapping(profile.get("review_task_labels"))
    qa = _mapping(profile.get("qa"))
    card = _mapping(r.get("visual_card") or profile.get("visual_card"))
    quality = _mapping(r.get("quality"))
    crop_quality = _mapping(r.get("visual_crop_quality"))
    permission = _mapping(r.get("permission_state"))
    hq = _mapping(r.get("hq_labeling"))

    local_paths = r.get("local_image_paths")
    first_local = ""
    if isinstance(local_paths, (list, tuple)) and local_paths:
        first_local = str(local_paths[0] or "")
    image_path = _pick(
        local_resources.get("local_image_path"),
        r.get("local_image_path"),
        r.get("local_path"),
        r.get("image_path"),
        r.get("path"),
        first_local,
    )
    parent_image_path = _pick(
        r.get("parent_image_path"),
        r.get("parent_local_image_path"),
    )
    overlay_path = _pick(
        r.get("overlay_path"),
        crop_quality.get("overlay_path"),
    )

    figure_label = _pick(
        ident.get("label"),
        r.get("parent_label"),
        r.get("label"),
        r.get("figure_label"),
        r.get("figure_id"),
    )
    subfigure_label = _text(r.get("subfigure_label"))
    subpanel_labels = _list_strings(
        ident.get("subpanel_labels") or r.get("subpanel_labels")
    )
    chunk_kind = _text(r.get("chunk_kind"))
    if chunk_kind in {"parent_figure", "composite", "parent"}:
        unit_kind = "parent_figure"
    elif chunk_kind in {"subfigure", "panel"} or subfigure_label:
        unit_kind = "subfigure"
    elif chunk_kind in {"single_figure", "figure"}:
        unit_kind = "single_figure"
    elif len(subpanel_labels) >= 2 and not subfigure_label:
        unit_kind = "parent_figure"
    else:
        unit_kind = "single_figure"
    if is_generated_visual:
        unit_kind = GENERATED_VISUAL_KIND

    caption_original = _pick(
        ident.get("caption_original"),
        r.get("caption_original"),
        r.get("caption_text"),
        r.get("caption_en"),
        r.get("caption"),
    )
    caption_clean = _pick(
        ident.get("caption_clean"),
        r.get("caption_clean"),
        r.get("caption"),
        r.get("caption_text"),
        r.get("caption_en"),
        ident.get("caption_original"),
        r.get("caption_original"),
    )
    caption_confidence = _pick(
        ident.get("caption_confidence"),
        r.get("caption_confidence"),
    )
    if is_generated_visual and GENERATED_DISCLOSURE not in caption_clean:
        caption_clean = f"{caption_clean} {GENERATED_DISCLOSURE}".strip()

    tags: list[str] = []
    for value in (
        r.get("tags"),
        intrinsic.get("visual_role"),
        intrinsic.get("functional_visual_type"),
        intrinsic.get("visual_content_type"),
        task.get("review_utility"),
        r.get("visible_panel_type_hint"),
        r.get("source_asset_type"),
    ):
        for item in (
            value
            if isinstance(value, (list, tuple, set))
            else [value]
        ):
            text = _text(item)
            if text and text not in tags:
                tags.append(text)

    description = _pick(
        r.get("semantic_description"),
        r.get("description"),
        card.get("one_sentence_summary"),
        intrinsic.get("concise_label"),
        r.get("observable_content"),
        r.get("subfigure_caption_focus"),
    )
    domain_hints = (
        dict(r.get("domain_hints"))
        if isinstance(r.get("domain_hints"), Mapping)
        else {}
    )

    claims = _list_strings(
        task.get("candidate_claims_supported_by_caption_or_text")
        or task.get("candidate_claims_supported_by_caption")
    )
    argument_claim = _pick(
        r.get("visual_argument_claim"),
        "; ".join(claims),
        r.get("argumentative_purpose"),
    )
    review_decision = _pick(
        r.get("review_decision"),
        (r.get("review") or {}).get("review_decision")
        if isinstance(r.get("review"), Mapping)
        else "",
    )
    visual_argument_status = _pick(
        r.get("visual_argument_status"),
        r.get("status"),
    )
    needs_human_review = _bool(
        r.get("visual_argument_needs_human_review")
        or r.get("needs_human_review")
        or qa.get("needs_human_review")
    )

    use_permission = _text(
        r.get("use_permission") or permission.get("use_permission")
    ).lower()
    permission_notes: list[str] = []
    if use_permission not in PERMISSION_LEVELS:
        if use_permission:
            permission_notes.append(
                f"unknown_use_permission_mapped_to_{DEFAULT_PERMISSION}"
            )
        use_permission = DEFAULT_PERMISSION
    allowed_claim_kinds = _list_strings(
        r.get("allowed_claim_kinds")
        or permission.get("allowed_claim_kinds")
    )

    source_file = _pick(
        source_prov.get("source_file"),
        r.get("source_file"),
        r.get("source_path"),
    )
    generation_status = _pick(
        r.get("generated_status"),
        r.get("source_route"),
        r.get("generated_or_source"),
        r.get("generation_status"),
    )
    if not generation_status:
        generation_status = "source_derived"
    lower_generation = generation_status.lower()
    if any(
        marker in lower_generation
        for marker in ("conceptual_generated", "ai_generated", "generated")
    ):
        generation_status = "ai_generated"
    elif "enhanc" in lower_generation:
        generation_status = "enhanced"
    else:
        generation_status = "source_derived"
    if is_generated_visual:
        generation_status = "ai_generated"

    vector_entries = r.get("embedding_refs")
    if not isinstance(vector_entries, list):
        vector_entries = _mapping(r.get("vector_refs")).get("entries") or []
    use_history = _mapping(r.get("use_history"))
    if not isinstance(use_history.get("used_in_run_ids"), list):
        use_history = {
            "schema_version": "optomind.visual_use_history.v1",
            "used_in_run_ids": [],
            "citations": [],
            "last_used_at": "",
            "notes": [],
        }

    enhancement_history = r.get("enhancement_history")
    if not isinstance(enhancement_history, list):
        enhancement_history = []
    generation = _normalize_generation_metadata(
        r,
        raw_generation,
        is_generated=is_generated_visual,
        review_decision=review_decision,
    )

    return {
        "schema_version": _text(r.get("schema_version")),
        "chunk_id": _text(
            r.get("chunk_id")
            or ident.get("asset_id")
            or r.get("candidate_visual_id")
            or r.get("figure_id")
        ),
        "asset_id": _text(ident.get("asset_id") or r.get("asset_id")),
        "paper_id": _pick(paper.get("paper_id"), r.get("paper_id")),
        "doi": (
            ""
            if is_generated_visual
            else _pick(paper.get("doi"), r.get("doi"))
        ),
        "title": _pick(paper.get("title"), r.get("paper_title"), r.get("title")),
        "year": paper.get("year", r.get("year")),
        "venue": _pick(paper.get("venue"), r.get("venue")),
        "source_kind": (
            GENERATED_SOURCE_KIND if is_generated_visual else ""
        ),
        "is_generated_visual": is_generated_visual,
        "figure_label": figure_label,
        "subfigure_label": subfigure_label,
        "parent_asset_id": _text(
            r.get("parent_asset_id")
            or ident.get("parent_asset_id")
        ),
        "parent_label": _text(r.get("parent_label") or figure_label),
        "subpanel_labels": subpanel_labels,
        "unit_kind": unit_kind,
        "unit_role": "parent_context" if unit_kind == "parent_figure" else "review_asset",
        "image_path": image_path,
        "parent_image_path": parent_image_path,
        "overlay_path": overlay_path,
        "caption_original": caption_original,
        "caption_clean": caption_clean,
        "caption_confidence": caption_confidence,
        "subfigure_caption_focus": _text(
            r.get("subfigure_caption_focus")
        ),
        "nearby_text": _pick(
            r.get("nearby_text"),
            document_ctx.get("nearby_text"),
            r.get("search_text"),
        ),
        "body_callout_texts": _list_strings(
            r.get("body_callout_texts")
            or [x.get("paragraph_text") for x in text_link.get("body_callouts", []) if isinstance(x, Mapping)]
        ),
        "linked_text_chunk_ids": _list_strings(
            r.get("linked_text_chunk_ids")
            or text_link.get("linked_chunk_ids")
        ),
        "description": description,
        "tags": tags,
        "domain_hints": domain_hints,
        "visual_card": card,
        "visual_profile": profile,
        "quality": quality,
        "crop_quality": crop_quality,
        "bbox_px": r.get("bbox_px"),
        "bbox_original_px": r.get("bbox_original_px"),
        "bbox_padding_ratio": r.get("bbox_padding_ratio"),
        "region_bbox": (
            r.get("bbox_pdf")
            or source_prov.get("bbox")
            or r.get("region_bbox")
        ),
        "caption_bbox": (
            r.get("caption_bbox")
            or source_prov.get("caption_bbox")
        ),
        "render_scale": r.get("render_scale"),
        "argument_primary": _text(r.get("visual_argument_type")),
        "argument_secondary": _list_strings(
            r.get("secondary_visual_argument_types")
        ),
        "argument_claim": argument_claim,
        "supported_aspect": _pick(
            r.get("supported_aspect"),
            task.get("argument_function"),
        ),
        "argument_basis": _list_strings(r.get("argument_basis")),
        "argument_confidence": (
            _float(
                r.get("visual_argument_confidence")
                or r.get("confidence")
                or qa.get("confidence")
            )
            or _text(
                r.get("visual_argument_confidence")
                or r.get("confidence")
                or qa.get("confidence")
            )[:32]
        ),
        "argument_needs_human_review": needs_human_review,
        "argument_schema_version": _pick(
            r.get("visual_argument_schema_version"),
            r.get("classification_status"),
        ),
        "review_utility": _text(task.get("review_utility")),
        "extraction_method": _pick(
            r.get("extraction_method"),
            source_prov.get("parser"),
            r.get("parser"),
        ),
        "source_format": _text(
            source_prov.get("source_format")
            or r.get("source_format")
        ),
        "parser": _text(source_prov.get("parser") or r.get("parser")),
        "parser_version": _text(
            source_prov.get("parser_version")
            or r.get("parser_version")
        ),
        "extraction_run_id": _pick(
            source_prov.get("extraction_run_id"),
            r.get("extraction_run_id"),
            hq.get("model_tier"),
        ),
        "source_url": _text(
            source_prov.get("source_url")
            or r.get("source_url")
        ),
        "source_file": source_file,
        "page": (
            source_prov.get("page", r.get("page"))
            if source_prov.get("page") is not None
            else r.get("page")
        ),
        "checksum": _text(
            source_prov.get("checksum")
            or r.get("checksum")
            or r.get("source_checksum")
        ),
        "use_permission": use_permission,
        "allowed_claim_kinds": allowed_claim_kinds,
        "license": _pick(
            r.get("license"),
            r.get("license_"),
            permission.get("license"),
            r.get("s2_oa_license"),
        ),
        "is_oa": (
            permission.get("is_oa", r.get("is_oa"))
            if r.get("is_oa") is not None or permission.get("is_oa") is not None
            else None
        ),
        "permission_source": _text(
            r.get("permission_source")
            or permission.get("permission_source")
        ),
        "permission_notes": permission_notes,
        "asset_kind": derive_asset_kind(r),
        "asset_kind_source": "candidate_or_heuristic",
        "qwen_crop_advisor": _advisor_value(r),
        "image_sha256": _text(r.get("image_sha256")),
        "review_decision": review_decision,
        "visual_argument_status": visual_argument_status,
        "human_review_status": _text(
            r.get("human_review_status")
        ),
        "needs_human_review": needs_human_review,
        "review_flags": _list_strings(r.get("review_flags")),
        "status": _text(r.get("status")),
        "generation_status": generation_status,
        "generation": generation,
        "enhancement_history": enhancement_history,
        "vector_entries": vector_entries,
        "use_history": use_history,
        "raw_record": r,
    }


def _resolve_image_path(
    value: str,
    roots: Mapping[str, Path],
) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path if path.is_file() else None
    for root in roots.values():
        candidate = Path(root) / path
        if candidate.is_file():
            return candidate
    return None


def _copy_image_asset(source: Path, assets_dir: Path) -> tuple[str, str]:
    asset_root = assets_dir / ASSETS_DIRNAME
    asset_root.mkdir(parents=True, exist_ok=True)
    sha = _hash_file(source)
    suffix = _safe_image_suffix(source)
    filename = f"{sha}{suffix}"
    destination = asset_root / filename
    if destination.exists():
        existing_sha = _hash_file(destination)
        if existing_sha != sha:
            raise CandidateIngestError(
                f"asset_hash_collision:{destination.name}"
            )
    else:
        shutil.copy2(source, destination)
    return f"{ASSETS_DIRNAME}/{filename}", sha


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return _text(
        candidate.get("chunk_id")
        or candidate.get("asset_id")
        or candidate.get("paper_id")
    ) or "<unknown>"


def _neutral_fallback_caption(candidate: Mapping[str, Any]) -> str:
    """Neutral, traceable, non-claim caption for figures without a caption.

    The figure label is kept as traceable identity context; no nearby text is
    ever promoted into a claim.  When no label exists, an explicit non-claim
    placeholder is used so the durable unit remains publishable.
    """

    label = _text(
        candidate.get("figure_label")
        or candidate.get("parent_label")
        or candidate.get("label")
    )
    if label:
        return f"{label}; caption unavailable; inspect the source figure."
    return "Caption unavailable; inspect the source figure."


def _caption_contamination_summary(
    hygiene: Mapping[str, Any],
) -> dict[str, Any]:
    """Summarize caption/page-prose contamination detected in image pixels.

    Derived deterministically from the crop-hygiene audit evidence; the unit
    stays publishable while the signal is recorded for down-ranking.
    """

    evidence = _mapping(hygiene.get("evidence"))
    caption_band = _mapping(evidence.get("caption_band"))
    prose = _mapping(evidence.get("prose_contamination"))
    # A low-confidence tiny bottom band on an otherwise clean image is not
    # caption contamination; only a meaningful (high-confidence) band or an
    # explicit page-prose verdict counts.
    band_detected = bool(
        caption_band.get("found")
        and str(caption_band.get("confidence") or "") == "high"
    )
    verdict = _text(prose.get("verdict"))
    page_prose = bool(verdict and verdict != "none")
    detected = bool(band_detected or page_prose)
    status = _text(hygiene.get("status"))
    return {
        "detected": detected,
        "requires_review": bool(
            detected and status in {"needs_review", "rejected"}
        ),
        "page_prose": page_prose,
        "reason": _text(hygiene.get("reason")),
        "verdict": verdict,
    }


def _advisor_failed_hygiene(
    candidate: Mapping[str, Any],
    advisor: Mapping[str, Any],
    *,
    reason: str,
    now: str,
) -> dict[str, Any]:
    """Fail-open hygiene record when the optional advisor is unusable.

    The original candidate is preserved; nothing is cropped or overwritten.
    """

    return {
        "schema_version": "optomind.visual_crop_hygiene.v1",
        "status": "needs_review",
        "source_kind": QWEN_ADVISOR_CROP_METHOD,
        "extraction_method": _text(candidate.get("extraction_method"))
        or QWEN_ADVISOR_CROP_METHOD,
        "reason": reason,
        "evidence": {"advisor": dict(advisor)},
        "derivative": None,
        "advisor": {
            "asset_kind": _text(advisor.get("asset_kind")) or "unknown",
            "confidence": advisor.get("confidence"),
            "needs_review": True,
            "errors": _list_strings(advisor.get("errors")),
        },
        "created_at": now,
    }


def _audit_candidate_hygiene(
    candidate: Mapping[str, Any],
    source_path: Path,
    derivative_dir: Path,
    now: str,
) -> dict[str, Any]:
    """Audit one candidate, optionally using Qwen semantic crop advice.

    When no advisor payload is present the existing local audit runs
    unchanged.  When a payload is present but unusable (malformed JSON, low
    confidence, invalid/overlapping boxes, derivative audit failure), the
    original candidate is preserved with ``needs_review``.  A valid advisor
    crop is materialized by local Pillow code and then audited locally.
    """

    advisor_raw = candidate.get("qwen_crop_advisor")
    advisor: dict[str, Any] | None = None
    if isinstance(advisor_raw, Mapping) and advisor_raw:
        advisor = dict(advisor_raw)
    elif isinstance(advisor_raw, str) and advisor_raw.strip():
        try:
            from .visual_qwen_crop_advisor import parse_qwen_crop_advice

            with Image.open(source_path) as opened:
                width, height = opened.size
            advisor = parse_qwen_crop_advice(
                advisor_raw,
                image_width=width,
                image_height=height,
            )
        except Exception as exc:
            advisor = {
                "schema_version": "optomind.visual_qwen_crop_advisor.v1",
                "ok": False,
                "needs_review": True,
                "asset_kind": "unknown",
                "errors": [
                    f"advisor_parse_failed:{type(exc).__name__}:{exc}"
                ],
                "confidence": 0.0,
                "contamination_notes": [],
            }

    if advisor is None:
        try:
            return audit_crop_hygiene(
                candidate,
                source_path,
                derivative_dir=derivative_dir,
                create_derivative=True,
            )
        except Exception as exc:
            return {
                "schema_version": "optomind.visual_crop_hygiene.v1",
                "status": "needs_review",
                "source_kind": _text(candidate.get("extraction_method"))
                or "unknown",
                "extraction_method": _text(candidate.get("extraction_method")),
                "reason": f"audit_failed:{type(exc).__name__}:{exc}",
                "evidence": {},
                "derivative": None,
                "created_at": now,
            }
    if advisor.get("needs_review"):
        return _advisor_failed_hygiene(
            candidate,
            advisor,
            reason=(
                "qwen_advisor_unavailable_or_low_confidence:"
                + ";".join(_list_strings(advisor.get("errors")))
            ),
            now=now,
        )
    try:
        return materialize_advisor_crop(
            source_path,
            output_dir=derivative_dir,
            advice=advisor,
        )
    except Exception as exc:
        return _advisor_failed_hygiene(
            candidate,
            advisor,
            reason=(
                "qwen_advisor_crop_materialization_failed:"
                f"{type(exc).__name__}:{exc}"
            ),
            now=now,
        )


def _build_unit(
    candidate: Mapping[str, Any],
    *,
    roots: Mapping[str, Path],
    assets_dir: Path,
    approve_markers: set[str] | None,
    reject_markers: set[str] | None,
    now: str,
) -> dict[str, Any]:
    c = dict(candidate)
    source_path = _resolve_image_path(str(c.get("image_path") or ""), roots)
    if source_path is None:
        raise CandidateIngestError("image_path_unresolved")
    image_rel, image_sha = _copy_image_asset(source_path, assets_dir)
    original_rel = image_rel
    original_sha = image_sha

    hygiene = _audit_candidate_hygiene(
        candidate,
        source_path,
        assets_dir / ASSETS_DIRNAME,
        now,
    )

    asset_kind = _text(c.get("asset_kind"))
    asset_kind_source = _text(c.get("asset_kind_source"))
    advisor_summary = _mapping(hygiene.get("advisor"))
    advisor_kind = _text(advisor_summary.get("asset_kind"))
    local_kind = derive_asset_kind(c)
    if local_kind == "table" and advisor_kind != "table":
        # A local Table label/type is ground truth: a model misclassification
        # must never silently turn Table 1 into a source_figure.
        asset_kind = "table"
        asset_kind_source = "local_table_label"
    elif advisor_kind in ASSET_KINDS:
        asset_kind = advisor_kind
        asset_kind_source = "qwen_advisor"
    elif asset_kind not in ASSET_KINDS:
        asset_kind = derive_asset_kind(c)
        asset_kind_source = asset_kind_source or "candidate_or_heuristic"
    elif not asset_kind_source:
        asset_kind_source = "candidate_or_heuristic"

    raw_derivative = (
        _mapping(hygiene.get("derivative"))
        if isinstance(hygiene.get("derivative"), Mapping)
        else None
    )
    if hygiene.get("status") == "derived_clean" and raw_derivative:
        selected_rel = (
            f"{ASSETS_DIRNAME}/{raw_derivative.get('filename')}"
        )
        selected_sha = _text(raw_derivative.get("sha256"))
        original_ref = {"root": "snapshot", "relative": original_rel}
        hygiene_derivative = {
            "filename": _text(raw_derivative.get("filename")),
            "relpath": selected_rel,
            "sha256": _text(raw_derivative.get("sha256")),
            "parent_sha256": _text(raw_derivative.get("parent_sha256")),
            "crop_bbox_px": list(
                raw_derivative.get("crop_bbox_px") or []
            ),
            "width": raw_derivative.get("width"),
            "height": raw_derivative.get("height"),
            "reason": _text(raw_derivative.get("reason")),
        }
        crop_bbox = list(raw_derivative.get("crop_bbox_px") or [])
        hygiene = dict(hygiene)
        hygiene["derivative"] = hygiene_derivative
    else:
        selected_rel = image_rel
        selected_sha = image_sha
        original_ref = None
        hygiene_derivative = None
        crop_bbox = []

    source_root = roots.get("source")
    source_ref = portable_path_ref(
        source_path,
        root=source_root,
        root_key="source",
    )
    overlay_ref = None
    overlay_value = str(c.get("overlay_path") or "")
    if overlay_value:
        overlay_ref = portable_path_ref(
            overlay_value,
            root=source_root,
            root_key="source",
        )
    source_file_ref = None
    if str(c.get("source_file") or ""):
        source_file_ref = portable_path_ref(
            str(c["source_file"]),
            root=source_root,
            root_key="source",
        )

    caption = {
        "original": _text(c.get("caption_original")),
        "clean": _text(c.get("caption_clean")),
        "subfigure_focus": _text(c.get("subfigure_caption_focus")),
        "confidence": _text(c.get("caption_confidence")),
    }
    caption_missing = not (
        caption["original"]
        or caption["clean"]
        or caption["subfigure_focus"]
    )
    if caption_missing:
        caption["clean"] = _neutral_fallback_caption(c)
        caption["missing"] = True
        caption["fallback_reason"] = (
            "caption_unavailable_non_claim_placeholder"
        )
    argument_primary = _text(c.get("argument_primary"))
    if not argument_primary:
        argument_primary = infer_visual_argument_type(
            _mapping(c.get("raw_record"))
        )
    argument_confidence = c.get("argument_confidence")
    if argument_confidence is None:
        argument_confidence = ""
    tags = _list_strings(c.get("tags"))
    search_text = " ".join(
        part
        for part in (
            c.get("title"),
            c.get("figure_label"),
            c.get("parent_label"),
            c.get("subfigure_label"),
            caption["clean"],
            caption["subfigure_focus"],
            c.get("description"),
            " ".join(tags),
            c.get("nearby_text"),
        )
        if part
    )

    approval = derive_approval_state(
        {
            **dict(c),
            "approval": _mapping(c.get("raw_record")).get("approval"),
        },
        approve_markers=approve_markers,
        reject_markers=reject_markers,
    )
    if approval["state"] == "approved" and not approval["approved_at"]:
        approval["approved_at"] = now

    unit_kind = _text(c.get("unit_kind")) or "single_figure"
    unit_role = _text(c.get("unit_role"))
    if unit_kind == "parent_figure":
        unit_role = "parent_context"
    elif unit_kind != "parent_figure":
        unit_role = "review_asset"
    is_generated = bool(c.get("is_generated_visual"))

    source_identity: dict[str, Any] = {
        "paper_id": _text(c.get("paper_id")),
        "doi": _text(c.get("doi")),
        "title": _text(c.get("title")),
        "year": c.get("year"),
        "venue": _text(c.get("venue")),
        "locator": {
            key: c.get(key)
            for key in ("source_url", "page")
            if c.get(key) not in (None, "")
        },
    }
    if is_generated:
        source_identity["source_kind"] = GENERATED_SOURCE_KIND
        source_identity["rights"] = {
            "disclosure": GENERATED_DISCLOSURE,
            "attribution_kind": "none",
            "doi_policy": "no_source_doi",
        }
    permission_state: dict[str, Any] = {
        "use_permission": _text(c.get("use_permission"))
        or DEFAULT_PERMISSION,
        "allowed_claim_kinds": _list_strings(
            c.get("allowed_claim_kinds")
        ),
        "license": _text(c.get("license")),
        "is_oa": c.get("is_oa"),
        "permission_source": _text(c.get("permission_source")),
        "notes": _list_strings(c.get("permission_notes")),
    }
    if is_generated:
        permission_state["evidence_ceiling"] = "explanatory_only"
        permission_state["empirical_evidence_allowed"] = False
        permission_state["quantitative_evidence_allowed"] = False

    unit: dict[str, Any] = {
        "schema_version": UNIT_SCHEMA_VERSION,
        "unit_id": "",
        "unit_kind": unit_kind,
        "unit_role": unit_role,
        "asset_typing": {
            "asset_kind": asset_kind,
            "table": asset_kind == "table",
            "source": asset_kind_source,
            "notes": [],
        },
        "source_identity": source_identity,
        "figure_identity": {
            "asset_id": _text(c.get("asset_id") or c.get("chunk_id")),
            "asset_kind": asset_kind,
            "figure_label": _text(c.get("figure_label")),
            "subfigure_label": _text(c.get("subfigure_label")),
            "parent_asset_id": _text(c.get("parent_asset_id")),
            "parent_label": _text(c.get("parent_label")),
            "subpanel_labels": _list_strings(c.get("subpanel_labels")),
            "page": c.get("page"),
            "bbox_px": c.get("bbox_px"),
            "bbox_original_px": c.get("bbox_original_px"),
            "bbox_padding_ratio": c.get("bbox_padding_ratio"),
        },
        "caption": {
            **caption,
            "disclosure": (
                GENERATED_DISCLOSURE if is_generated else ""
            ),
        },
        "semantic": {
            "description": _text(c.get("description")),
            "tags": tags,
            "domain_hints": dict(c.get("domain_hints") or {}),
            "visual_card": dict(c.get("visual_card") or {}),
            "visual_profile": dict(c.get("visual_profile") or {}),
            "quality": dict(c.get("quality") or {}),
            "nearby_text": _text(c.get("nearby_text")),
            "body_callout_texts": _list_strings(c.get("body_callout_texts")),
            "linked_text_chunk_ids": _list_strings(
                c.get("linked_text_chunk_ids")
            ),
        },
        "argumentative_roles": {
            "primary": argument_primary,
            "secondary": _list_strings(c.get("argument_secondary")),
            "claim": _text(c.get("argument_claim")),
            "supported_aspect": _text(c.get("supported_aspect")),
            "basis": _list_strings(c.get("argument_basis")),
            "confidence": argument_confidence,
            "needs_human_review": _bool(c.get("argument_needs_human_review")),
            "review_utility": _text(c.get("review_utility")),
            "schema_version": _text(c.get("argument_schema_version")),
        },
        "provenance": {
            "schema_version": _text(c.get("schema_version")),
            "extraction_method": _text(c.get("extraction_method")),
            "source_format": _text(c.get("source_format")),
            "parser": _text(c.get("parser")),
            "parser_version": _text(c.get("parser_version")),
            "extraction_run_id": _text(c.get("extraction_run_id")),
            "source_url": _text(c.get("source_url")),
            "page": c.get("page"),
            "checksum": _text(c.get("checksum")),
            "source_file_ref": source_file_ref,
            "generation_disclosure": (
                GENERATED_DISCLOSURE if is_generated else ""
            ),
            "ingested_at": now,
        },
        "permission_state": permission_state,
        "hashes": {
            "image_sha256": selected_sha,
            "content_hash": "sha256:" + selected_sha,
            "record_sha256": "",
            "source_checksum": _text(c.get("checksum")),
        },
        "vector_refs": {
            "schema_version": "optomind.visual_vector_refs.v1",
            "entries": list(c.get("vector_entries") or []),
            "indexed": False,
        },
        "lineage": {
            "generation_status": _text(c.get("generation_status"))
            or "source_derived",
            "parent_unit_id": "",
            "parent_unavailable": False,
            "crop": {
                "bbox_px": c.get("bbox_px"),
                "bbox_original_px": c.get("bbox_original_px"),
                "bbox_padding_ratio": c.get("bbox_padding_ratio"),
                "crop_bbox": crop_bbox,
                "parent_image_hash": (
                    original_sha if original_ref else ""
                ),
                "parent_image_ref": original_ref,
                "derivative": hygiene_derivative,
                "overlay_ref": overlay_ref,
                "crop_quality": dict(c.get("crop_quality") or {}),
            },
            "enhancement_history": list(c.get("enhancement_history") or []),
            "generation": dict(c.get("generation") or {}),
        },
        "use_history": dict(c.get("use_history") or {}),
        "review": {
            "review_decision": _text(c.get("review_decision")),
            "visual_argument_status": _text(
                c.get("visual_argument_status")
            ),
            "human_review_status": _text(c.get("human_review_status"))
            or ("approved" if is_generated else "pending"),
            "needs_human_review": (
                False if is_generated else _bool(c.get("needs_human_review"))
            ),
            "review_flags": _list_strings(c.get("review_flags")),
        },
        "approval": approval,
        "crop_hygiene": hygiene,
        "paths": {
            "image_ref": {"root": "snapshot", "relative": selected_rel},
            "original_image_ref": original_ref,
            "source_ref": source_ref,
            "parent_image_ref": None,
            "overlay_ref": overlay_ref,
        },
        "created_at": now,
    }
    if caption_missing:
        unit["provenance"]["caption_status"] = "missing_needs_review"
        unit["review"]["review_flags"] = list(
            dict.fromkeys(
                [
                    *unit["review"].get("review_flags", []),
                    "caption_missing",
                ]
            )
        )
    caption_contamination = _caption_contamination_summary(hygiene)
    if caption_contamination["detected"]:
        unit["crop_hygiene"]["caption_contamination"] = (
            caption_contamination
        )
        contamination_flags = ["caption_in_pixels"]
        if caption_contamination.get("page_prose"):
            contamination_flags.append("page_prose")
        unit["review"]["review_flags"] = list(
            dict.fromkeys(
                [
                    *unit["review"].get("review_flags", []),
                    *contamination_flags,
                ]
            )
        )
    eligibility = derive_publication_eligibility(unit)
    unit["permission_state"]["publication_eligible"] = eligibility[
        "publication_eligible"
    ]
    unit["permission_state"]["publication_eligible_reason"] = eligibility[
        "reason"
    ]
    unit["permission_state"]["external_discovery_only"] = (
        unit["permission_state"]["use_permission"] == "discovery_only"
        and not is_generated
    )
    unit["source_map"] = build_visual_source_map(
        unit_id="",
        source_identity=unit["source_identity"],
        figure_identity=unit["figure_identity"],
        caption=unit["caption"],
        semantic=unit["semantic"],
        provenance=unit["provenance"],
        paths=unit["paths"],
    )
    unit["hashes"]["record_sha256"] = canonical_json_hash(
        {**unit, "hashes": {**unit["hashes"], "record_sha256": ""}}
    )
    unit_id_source = "|".join(
        (
            _text(c.get("paper_id")),
            _text(c.get("doi")),
            _text(c.get("figure_label")),
            _text(c.get("subfigure_label")),
            selected_sha,
        )
    )
    unit["unit_id"] = (
        "unit:visual:"
        + hashlib.sha1(unit_id_source.encode("utf-8")).hexdigest()[:24]
    )
    unit["source_map"]["unit_id"] = unit["unit_id"]
    unit["hashes"]["record_sha256"] = canonical_json_hash(
        {**unit, "hashes": {**unit["hashes"], "record_sha256": ""}}
    )
    try:
        unit = attach_local_vector_refs(
            unit,
            Path(assets_dir)
            / str(unit["paths"]["image_ref"]["relative"]),
        )
    except Exception:
        # Fail-open: keep the unit publishable with an explicit unindexed
        # status rather than dropping a valid traceable asset.
        unit["vector_refs"]["indexed"] = False
    return unit


def ingest_visual_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    source_root: Path | str | None = None,
    extra_roots: Mapping[str, Path | str] | None = None,
    copy_assets_to: Path | str,
    approve_markers: set[str] | None = None,
    reject_markers: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build durable visual units from existing candidate records.

    Individual bad candidates are skipped with report entries (fail-open);
    the returned units are validated before snapshot publication elsewhere.
    """

    assets_dir = Path(copy_assets_to)
    roots: dict[str, Path] = {}
    if source_root is not None:
        roots["source"] = Path(source_root)
    for key, value in dict(extra_roots or {}).items():
        roots[str(key)] = Path(value)
    now = _now_utc()
    report: dict[str, Any] = {
        "schema_version": INGEST_SCHEMA_VERSION,
        "status": "ok",
        "created_at": now,
        "candidates_seen": 0,
        "units_created": 0,
        "duplicates_skipped": 0,
        "caption_missing_count": 0,
        "caption_contamination_count": 0,
        "asset_kind_counts": {},
        "errors": [],
        "warnings": [],
    }
    units: dict[str, dict[str, Any]] = {}
    seen_hashes: dict[tuple[str, str, str], str] = {}
    parent_by_asset_id: dict[str, str] = {}
    parent_by_figure_key: dict[tuple[str, str], str] = {}

    def remember_parent(unit: Mapping[str, Any]) -> None:
        unit_id = _text(unit.get("unit_id"))
        figure = _mapping(unit.get("figure_identity"))
        source = _mapping(unit.get("source_identity"))
        asset_id = _text(figure.get("asset_id"))
        if asset_id:
            parent_by_asset_id[asset_id] = unit_id
        paper_id = _text(source.get("paper_id"))
        parent_label = _text(figure.get("parent_label") or figure.get("figure_label"))
        if paper_id and parent_label:
            parent_by_figure_key[(paper_id, parent_label)] = unit_id

    def find_parent(candidate: Mapping[str, Any]) -> str:
        asset_id = _text(candidate.get("parent_asset_id"))
        if asset_id and asset_id in parent_by_asset_id:
            return parent_by_asset_id[asset_id]
        paper_id = _text(candidate.get("paper_id"))
        parent_label = _text(candidate.get("parent_label"))
        return parent_by_figure_key.get((paper_id, parent_label), "")

    def add_unit(unit: Mapping[str, Any]) -> None:
        unit_id = _text(unit.get("unit_id"))
        units[unit_id] = dict(unit)
        report["units_created"] = len(units)
        typing = _mapping(unit.get("asset_typing"))
        kind = _text(typing.get("asset_kind")) or _text(
            _mapping(unit.get("figure_identity")).get("asset_kind")
        ) or "figure"
        report["asset_kind_counts"][kind] = (
            report["asset_kind_counts"].get(kind, 0) + 1
        )
        remember_parent(unit)

    def record_caption_missing(
        unit: Mapping[str, Any],
        candidate_id: str,
    ) -> None:
        if (unit.get("caption") or {}).get("missing"):
            report["caption_missing_count"] += 1
            report["warnings"].append(f"caption_missing:{candidate_id}")

    def record_crop_contamination(
        unit: Mapping[str, Any],
        candidate_id: str,
    ) -> None:
        contamination = _mapping(
            (unit.get("crop_hygiene") or {}).get(
                "caption_contamination"
            )
        )
        if contamination.get("detected"):
            report["caption_contamination_count"] += 1
            report["warnings"].append(
                f"caption_contamination:{candidate_id}"
            )

    for raw in candidates:
        report["candidates_seen"] += 1
        if not isinstance(raw, Mapping):
            report["errors"].append(
                {"candidate_id": "<unknown>", "reason": "candidate_not_object"}
            )
            report["status"] = "degraded"
            continue
        candidate = _normalize_candidate(raw)
        candidate_id = _candidate_id(candidate)
        if candidate.get("is_generated_visual"):
            generation_status = _text(
                _mapping(candidate.get("generation")).get(
                    "generation_status"
                )
                or candidate.get("generation_status")
                or (raw.get("generation_result") or {}).get(
                    "generation_status"
                )
            )
            if generation_status in {
                "rejected",
                "exhausted",
                "failed",
                "model_rejected_or_revision_required",
                "image_generation_skipped_by_budget",
            }:
                report["errors"].append(
                    {
                        "candidate_id": candidate_id,
                        "reason": (
                            "generated_visual_rejected_or_exhausted:"
                            f"{generation_status}"
                        ),
                    }
                )
                report["status"] = "degraded"
                continue
            approval = derive_approval_state(
                raw,
                approve_markers=approve_markers,
                reject_markers=reject_markers,
            )
            if approval["state"] != "approved":
                report["errors"].append(
                    {
                        "candidate_id": candidate_id,
                        "reason": "generated_visual_not_approved",
                    }
                )
                report["status"] = "degraded"
                continue
        try:
            image_path = _resolve_image_path(
                str(candidate.get("image_path") or ""),
                roots,
            )
            if image_path is None:
                raise CandidateIngestError("image_path_unresolved")
            image_sha = _hash_file(image_path)
            subfigure_label = _text(candidate.get("subfigure_label"))
            unit_kind = _text(candidate.get("unit_kind")) or "single_figure"
            dedupe_key = (image_sha, unit_kind, subfigure_label)
            existing = seen_hashes.get(dedupe_key)
            if existing:
                report["duplicates_skipped"] += 1
                report["warnings"].append(
                    f"duplicate_skipped:{candidate_id}->{existing}"
                )
                continue
            collision = next(
                (
                    owner
                    for key, owner in seen_hashes.items()
                    if key[0] == image_sha and key[1:] != dedupe_key[1:]
                ),
                "",
            )
            if collision:
                raise CandidateIngestError(
                    f"duplicate_image_hash_different_identity:{collision}"
                )
            seen_hashes[dedupe_key] = candidate_id

            parent_unit_id = ""
            parent_unavailable = False
            parent_warning = ""
            if unit_kind == "subfigure":
                parent_unit_id = find_parent(candidate)
                if not parent_unit_id:
                    parent_image = _resolve_image_path(
                        str(candidate.get("parent_image_path") or ""),
                        roots,
                    )
                    if parent_image is not None and _hash_file(parent_image) != image_sha:
                        parent_candidate = {
                            **dict(candidate),
                            "schema_version": candidate.get("schema_version"),
                            "chunk_id": candidate.get("parent_asset_id")
                            or f"{candidate.get('paper_id')}-parent",
                            "asset_id": candidate.get("parent_asset_id"),
                            "figure_label": candidate.get("parent_label"),
                            "parent_label": candidate.get("parent_label"),
                            "subfigure_label": "",
                            "image_path": str(parent_image),
                            "parent_image_path": "",
                            "unit_kind": "parent_figure",
                            "unit_role": "parent_context",
                            "subpanel_labels": candidate.get("subpanel_labels"),
                        }
                        try:
                            parent_unit = _build_unit(
                                parent_candidate,
                                roots=roots,
                                assets_dir=assets_dir,
                                approve_markers=approve_markers,
                                reject_markers=reject_markers,
                                now=now,
                            )
                            add_unit(parent_unit)
                            record_caption_missing(
                                parent_unit,
                                _candidate_id(parent_candidate),
                            )
                            record_crop_contamination(
                                parent_unit,
                                _candidate_id(parent_candidate),
                            )
                            parent_unit_id = parent_unit["unit_id"]
                        except (CandidateIngestError, OSError) as exc:
                            parent_warning = f"parent_build_failed:{exc}"
                    else:
                        parent_warning = (
                            "parent_image_unavailable_or_identical_to_child"
                        )
                    if not parent_unit_id:
                        parent_unavailable = True
                if parent_warning:
                    report["warnings"].append(parent_warning)

            unit = _build_unit(
                candidate,
                roots=roots,
                assets_dir=assets_dir,
                approve_markers=approve_markers,
                reject_markers=reject_markers,
                now=now,
            )
            if unit_kind == "subfigure":
                unit["lineage"]["parent_unit_id"] = parent_unit_id
                unit["lineage"]["parent_unavailable"] = parent_unavailable
            add_unit(unit)
            record_caption_missing(unit, candidate_id)
            record_crop_contamination(unit, candidate_id)
        except (CandidateIngestError, OSError) as exc:
            report["errors"].append(
                {
                    "candidate_id": candidate_id,
                    "reason": str(exc),
                }
            )
            report["status"] = "degraded"
            continue

    report["units_created"] = len(units)
    if report["errors"]:
        report["status"] = "degraded"
    elif not units:
        report["status"] = "no_units"
    return sorted(units.values(), key=lambda unit: str(unit.get("unit_id"))), report


__all__ = [
    "INGEST_SCHEMA_VERSION",
    "CandidateIngestError",
    "candidates_from_staging_kb",
    "candidates_from_visual_asset_jsonl",
    "candidates_from_visual_chunk_jsonl",
    "ingest_visual_candidates",
    "read_jsonl",
]
