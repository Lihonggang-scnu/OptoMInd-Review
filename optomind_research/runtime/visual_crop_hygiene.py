"""Deterministic crop-hygiene audit for extracted visual images.

The PDF caption renderer deliberately includes the caption band in
``rendered_region_from_caption`` images.  This module audits such candidates
so captions or whole-page prose are never accepted as review images.

Rules are deliberately conservative and contain no OCR, network, or model
calls:

* true embedded raster / HTML DOM images pass with an audited reason;
* rendered regions use the source PDF ``caption_bbox`` / ``bbox_pdf`` when
  available to map the caption band into image pixels;
* otherwise a bottom-band text heuristic may remove a caption band only when
  confidence is high;
* uncertain separation is ``needs_review`` (never ``clean``);
* whole-page prose contamination is ``needs_review`` or ``rejected``;
* a cleaned crop is always a derivative: the original is never overwritten.

Only whole-figure caption-band removal is supported here; subpanel cropping
is out of scope.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from PIL import Image


SCHEMA_VERSION = "optomind.visual_crop_hygiene.v1"
AUDIT_STATUSES = frozenset(
    {"clean", "derived_clean", "needs_review", "rejected"}
)

RENDERED_REGION_METHOD = "rendered_region_from_caption"
QWEN_ADVISOR_CROP_METHOD = "qwen_advisor_content_crop"
PASS_THROUGH_METHODS = frozenset(
    {
        "embedded_raster_image",
        "html_figure_dom",
        "html_table_dom",
        "html_image",
    }
)

_DARK_THRESHOLD = 160
_COLOR_SPREAD_THRESHOLD = 24
PREVIEW_MAX_DIM = 360


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)):
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) <= 0 or (y1 - y0) <= 0:
        return None
    return [x0, y0, x1, y1]


def clamp_bbox_px(
    bbox: Any,
    *,
    image_width: int,
    image_height: int,
) -> list[int] | None:
    """Clamp and validate a pixel-space crop box against an image size.

    Out-of-range coordinates are clamped to the image edges; degenerate or
    non-numeric boxes are rejected with ``None``.  This is the local pixel
    authority - model coordinates are never trusted past this point.
    """

    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (int(round(float(item))) for item in bbox)
    except (TypeError, ValueError):
        return None
    if image_width <= 0 or image_height <= 0:
        return None
    x0 = max(0, min(image_width - 1, x0))
    y0 = max(0, min(image_height - 1, y0))
    x1 = max(1, min(image_width, x1))
    y1 = max(1, min(image_height, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def bboxes_overlap_px(
    left: list[int] | tuple[int, int, int, int],
    right: list[int] | tuple[int, int, int, int],
    *,
    tolerance_px: int = 2,
) -> bool:
    """Return whether two pixel boxes overlap beyond a small tolerance."""

    lx0, ly0, lx1, ly1 = (int(item) for item in left)
    rx0, ry0, rx1, ry1 = (int(item) for item in right)
    return bool(
        lx0 < rx1 - tolerance_px
        and rx0 < lx1 - tolerance_px
        and ly0 < ry1 - tolerance_px
        and ry0 < ly1 - tolerance_px
    )


def validate_advisor_boxes(
    advice: Mapping[str, Any],
    *,
    image_width: int,
    image_height: int,
    tolerance_px: int = 2,
) -> tuple[dict[str, Any], list[str]]:
    """Validate advisor semantic boxes into clamped pixel boxes.

    Returns ``(validated, errors)``.  ``errors`` is empty only when the
    content box is non-degenerate, the caption box does not overlap content,
    and every panel box lies inside content without overlapping its peers.
    """

    advice = _mapping(advice)
    errors: list[str] = []
    content_norm = advice.get("content_bbox")
    caption_norm = advice.get("caption_bbox")
    panel_norm = advice.get("panel_boxes") or []
    if not isinstance(panel_norm, (list, tuple)):
        panel_norm = []

    def to_px(normalized: Any) -> list[int] | None:
        box = _normalize_bbox(normalized)
        if box is None:
            return None
        return clamp_bbox_px(
            [
                box[0] * image_width,
                box[1] * image_height,
                box[2] * image_width,
                box[3] * image_height,
            ],
            image_width=image_width,
            image_height=image_height,
        )

    content_px = to_px(content_norm)
    if content_px is None:
        errors.append("content_bbox_invalid")
    caption_px = to_px(caption_norm)
    if caption_norm is not None and caption_px is None:
        errors.append("caption_bbox_invalid")

    panel_px: list[list[int]] = []
    for index, panel in enumerate(panel_norm[:24]):
        box = to_px(panel)
        if box is None:
            errors.append(f"panel_box_invalid:{index}")
            continue
        if content_px is not None and not bboxes_overlap_px(
            box,
            content_px,
            tolerance_px=tolerance_px,
        ):
            errors.append(f"panel_box_outside_content_bbox:{index}")
        panel_px.append(box)
    for left in range(len(panel_px)):
        for right in range(left + 1, len(panel_px)):
            if bboxes_overlap_px(
                panel_px[left],
                panel_px[right],
                tolerance_px=tolerance_px,
            ):
                errors.append(f"panel_boxes_overlap:{left}:{right}")

    if (
        content_px is not None
        and caption_px is not None
        and bboxes_overlap_px(
            content_px,
            caption_px,
            tolerance_px=tolerance_px,
        )
    ):
        errors.append("caption_bbox_overlaps_content_bbox")

    validated = {
        "image_width": int(image_width),
        "image_height": int(image_height),
        "content_bbox_px": content_px,
        "caption_bbox_px": caption_px,
        "panel_boxes_px": panel_px,
        "tolerance_px": int(tolerance_px),
    }
    return validated, errors


def _is_rendered_region_method(method: str) -> bool:
    return method in {
        RENDERED_REGION_METHOD,
        "pymupdf_caption_crop",
    } or "caption_crop" in method.lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _row_stats_from_arrays(
    gray_pixels: list[int],
    rgb_pixels: list[tuple[int, int, int]],
    width: int,
    start: int,
) -> dict[str, float]:
    gray_pixels = gray_pixels[start : start + width]
    rgb_pixels = rgb_pixels[start : start + width]
    dark = 0
    transitions = 0
    max_run = 0
    colored = 0
    previous_dark: bool | None = None
    current_run = 0
    for index, value in enumerate(gray_pixels):
        is_dark = value < _DARK_THRESHOLD
        if is_dark:
            dark += 1
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 0
        if previous_dark is not None and is_dark != previous_dark:
            transitions += 1
        previous_dark = is_dark
        r, g, b = rgb_pixels[index]
        if max(r, g, b) - min(r, g, b) >= _COLOR_SPREAD_THRESHOLD:
            colored += 1
    total = max(1, width)
    return {
        "dark_ratio": dark / total,
        "transitions": float(transitions),
        "max_run_ratio": max_run / total,
        "colored_ratio": colored / total,
    }


def _row_stats(image: Image.Image, row: int) -> dict[str, float]:
    """Backward-compatible per-row stats wrapper (single-row extraction)."""

    width, _ = image.size
    gray = list(image.convert("L").getdata())
    rgb = list(image.convert("RGB").getdata())
    return _row_stats_from_arrays(
        gray,
        rgb,
        width,
        row * width,
    )


def _is_text_like(stats: Mapping[str, float]) -> bool:
    return bool(
        0.004 <= stats["dark_ratio"] <= 0.30
        and stats["transitions"] >= 5
        and stats["max_run_ratio"] <= 0.45
    )


def _image_stats(image: Image.Image) -> dict[str, Any]:
    width, height = image.size
    gray_pixels = list(image.convert("L").getdata())
    rgb_pixels = list(image.convert("RGB").getdata())
    rows: list[dict[str, float]] = []
    text_rows: list[bool] = []
    colored_rows = 0
    for row in range(height):
        stats = _row_stats_from_arrays(
            gray_pixels,
            rgb_pixels,
            width,
            row * width,
        )
        rows.append(stats)
        text_rows.append(_is_text_like(stats))
        if stats["colored_ratio"] >= 0.005:
            colored_rows += 1
    return {
        "width": width,
        "height": height,
        "rows": rows,
        "text_rows": text_rows,
        "colored_row_ratio": colored_rows / max(1, height),
    }


def _detect_bottom_caption_band(
    stats: Mapping[str, Any],
) -> dict[str, Any]:
    height = int(stats["height"])
    if height <= 0:
        return {
            "found": False,
            "confidence": "none",
            "band_top": None,
            "band_bottom": None,
        }
    block = _bottom_caption_block(stats)
    if block is None:
        return {
            "found": False,
            "confidence": "none",
            "band_top": None,
            "band_bottom": None,
            "reason": "no_bottom_anchored_ink_block",
        }
    band_top = int(block["band_top"])
    band_bottom = int(block["band_bottom"])
    band_height = band_bottom - band_top + 1
    band_top_ratio = band_top / max(1, height)
    if band_top_ratio < 0.55:
        # A block starting in the upper half is figure content (plot lines,
        # axes, legends), not a bottom caption band.
        return {
            "found": False,
            "confidence": "none",
            "band_top": None,
            "band_bottom": None,
            "reason": "band_not_bottom_anchored",
        }
    text_rows = stats["text_rows"]
    above_window = text_rows[max(0, band_top - 5) : band_top]
    above_text_like = bool(above_window) and all(above_window)
    colored_row_ratio = float(stats["colored_row_ratio"])
    band_colored = _band_colored_ratio(stats, band_top, band_bottom)
    band_transition_avg = _band_transition_average(
        stats,
        band_top,
        band_bottom,
    )
    multiline_band = band_height >= max(16, int(0.08 * height))
    transition_threshold = 16 if multiline_band else 24
    confident = (
        band_height >= max(2, int(0.01 * height))
        and band_top_ratio >= 0.60
        and band_colored < 0.05
        and band_transition_avg >= transition_threshold
        and (not above_text_like or colored_row_ratio >= 0.01)
    )
    return {
        "found": True,
        "confidence": "high" if confident else "low",
        "band_top": band_top,
        "band_bottom": band_bottom,
        "band_height": band_height,
        "band_top_ratio": round(band_top_ratio, 4),
        "band_colored_ratio": round(band_colored, 4),
        "band_transition_avg": round(band_transition_avg, 2),
        "above_text_like": above_text_like,
    }


def _is_ink_row(stats: Mapping[str, float]) -> bool:
    """Bottom-caption-like row: dense dark text with low color.

    ``_is_text_like`` is stricter on dark ratio (<= 0.30) and misses dense
    rendered caption rows (dark ratio up to ~0.5).  This predicate is used
    only for bottom-anchored caption blocks, never for page-prose scoring.
    """

    return bool(
        stats["dark_ratio"] >= 0.05
        and stats["transitions"] >= 8
        and stats["colored_ratio"] < 0.05
        and stats["max_run_ratio"] <= 0.60
    )


def _is_caption_content_row(stats: Mapping[str, float]) -> bool:
    """Low-color text row that belongs to a bottom caption block.

    ``_is_ink_row`` requires dark_ratio >= 0.05 and misses light caption
    lines (e.g., the first line of a multi-line caption).  This predicate
    additionally accepts lighter low-color text rows so the whole caption
    block is removed, while still excluding ultra-light noise and colored
    figure content.
    """

    if _is_ink_row(stats):
        return True
    return bool(
        stats["dark_ratio"] >= 0.02
        and stats["transitions"] >= 8
        and stats["colored_ratio"] < 0.05
        and stats["max_run_ratio"] <= 0.60
    )


def _bottom_caption_block(
    stats: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Bottom-up scan for the bottom-most low-color ink block.

    Starts at the lowest ink row within the trailing-whitespace tolerance and
    extends upward across multiline whitespace gaps (bounded gap tolerance),
    stopping at the first large gap so figure text above is not absorbed.
    """

    height = int(stats["height"])
    rows = stats.get("rows") or []
    if height <= 0 or len(rows) != height:
        return None
    caption_rows = [_is_caption_content_row(row) for row in rows]
    trailing_tolerance = max(8, int(0.12 * height))
    last_ink = max(
        (
            row
            for row, flag in enumerate(caption_rows)
            if flag and row >= height - trailing_tolerance
        ),
        default=None,
    )
    if last_ink is None:
        return None
    gap_tolerance = max(4, int(0.05 * height))
    gap = 0
    top = last_ink
    for row in range(last_ink - 1, -1, -1):
        row_stats = rows[row]
        if caption_rows[row]:
            gap = 0
            top = row
        elif (
            float(row_stats["colored_ratio"]) >= 0.05
            or float(row_stats["dark_ratio"]) >= 0.60
        ):
            # Colored figure content or a dense rule: hard stop so figure
            # regions are never absorbed into the caption block.
            break
        else:
            gap += 1
            if gap > gap_tolerance:
                break
    return {
        "band_top": top,
        "band_bottom": last_ink,
        "band_height": last_ink - top + 1,
    }


def _band_colored_ratio(
    stats: Mapping[str, Any],
    band_top: int,
    band_bottom: int,
) -> float:
    rows = stats.get("rows") or []
    total = 0.0
    count = 0
    for row in range(band_top, band_bottom + 1):
        if 0 <= row < len(rows):
            total += float(rows[row].get("colored_ratio") or 0.0)
            count += 1
    return total / max(1, count)


def _band_transition_average(
    stats: Mapping[str, Any],
    band_top: int,
    band_bottom: int,
) -> float:
    rows = stats.get("rows") or []
    total = 0.0
    count = 0
    for row in range(band_top, band_bottom + 1):
        if 0 <= row < len(rows):
            transitions = float(rows[row].get("transitions") or 0.0)
            if transitions >= 8:
                # Only dense text rows contribute; blank whitespace between
                # multiline caption lines must not dilute the density.
                total += transitions
                count += 1
    return total / max(1, count)


def _scale_band_to_original(
    band: Mapping[str, Any],
    scale_x: float,
    scale_y: float,
) -> dict[str, Any]:
    """Scale preview coordinates back to original-image coordinates."""

    scaled = dict(band)
    preview_top = band.get("band_top")
    preview_bottom = band.get("band_bottom")
    if preview_top is not None and preview_bottom is not None:
        # Floor the top boundary so the scaled crop ends strictly above the
        # first caption row (rounding up could leave a caption sliver).
        top = int(float(preview_top) * scale_y)
        bottom = int(round(float(preview_bottom) * scale_y))
        scaled["band_top"] = top
        scaled["band_bottom"] = bottom
        scaled["band_height"] = bottom - top + 1
        scaled["preview_band_top"] = int(preview_top)
        scaled["preview_band_bottom"] = int(preview_bottom)
    if scale_x != 1.0 and band.get("band_top") is None:
        scaled["scale"] = {"x": round(scale_x, 6), "y": round(scale_y, 6)}
    return scaled


def _prose_contamination(
    stats: Mapping[str, Any],
    caption_band: Mapping[str, Any],
    *,
    rendered_region: bool = False,
) -> dict[str, Any] | None:
    height = int(stats["height"])
    band_top = caption_band.get("band_top")
    band_bottom = caption_band.get("band_bottom")
    text_rows = stats["text_rows"]
    band_height = 0
    if band_top is not None and band_bottom is not None:
        band_height = band_bottom - band_top + 1
    content_rows = 0
    content_text_rows = 0
    for row in range(height):
        if band_top is not None and band_bottom is not None:
            if band_top <= row <= band_bottom:
                continue
        content_rows += 1
        if text_rows[row]:
            content_text_rows += 1
    colored_row_ratio = float(stats["colored_row_ratio"])
    if content_rows <= 0:
        # The detected band covers the whole page: that is page prose, not a
        # removable bottom caption.
        text_fraction = sum(1 for row in text_rows if row) / max(
            1, height
        )
        content_text_rows = sum(1 for row in text_rows if row)
    else:
        text_fraction = content_text_rows / content_rows
    if band_height >= max(64, int(0.6 * height)):
        verdict = (
            "rejected"
            if colored_row_ratio < 0.05
            else "needs_review"
        )
        return {
            "content_text_row_fraction": round(text_fraction, 4),
            "colored_row_ratio": round(colored_row_ratio, 4),
            "verdict": verdict,
        }
    return {
        "content_text_row_fraction": round(text_fraction, 4),
        "colored_row_ratio": round(colored_row_ratio, 4),
        "verdict": (
            "rejected"
            if text_fraction >= 0.75 and colored_row_ratio < 0.05
            else (
                "needs_review"
                if text_fraction >= 0.45 and colored_row_ratio < 0.15
                else (
                    "needs_review"
                    if rendered_region and text_fraction >= 0.55
                    else "none"
                )
            )
        ),
    }


def _mapped_caption_band(
    image: Image.Image,
    region_bbox: list[float] | None,
    caption_bbox: list[float] | None,
) -> dict[str, Any] | None:
    if region_bbox is None or caption_bbox is None:
        return None
    width, height = image.size
    region_width = region_bbox[2] - region_bbox[0]
    region_height = region_bbox[3] - region_bbox[1]
    if region_width <= 0 or region_height <= 0:
        return None
    scale_x = width / region_width
    scale_y = height / region_height
    y0 = int(round((caption_bbox[1] - region_bbox[1]) * scale_y))
    y1 = int(round((caption_bbox[3] - region_bbox[1]) * scale_y))
    x0 = int(round((caption_bbox[0] - region_bbox[0]) * scale_x))
    x1 = int(round((caption_bbox[2] - region_bbox[0]) * scale_x))
    y0 = max(0, min(height, y0))
    y1 = max(0, min(height, y1))
    x0 = max(0, min(width, x0))
    x1 = max(0, min(width, x1))
    if y1 <= y0:
        return None
    caption_width = max(1, x1 - x0)
    horizontal_coverage = caption_width / max(1, width)
    caption_center = (x0 + x1) / 2.0
    image_center = width / 2.0
    center_offset = abs(caption_center - image_center) / max(
        1.0, width / 2.0
    )
    multi_column_ambiguity = bool(
        horizontal_coverage < 0.60 and center_offset > 0.25
    )
    top_ratio = y0 / max(1, height)
    if y0 <= 0.10 * height and y1 <= 0.40 * height:
        return {
            "position": "top",
            "crop_bbox_px": [0, y1, width, height],
            "caption_bbox_px": [x0, y0, x1, y1],
            "confidence": "high",
            "horizontal_coverage": round(horizontal_coverage, 4),
            "center_offset": round(center_offset, 4),
            "multi_column_ambiguity": multi_column_ambiguity,
        }
    if top_ratio >= 0.55 and y1 <= height:
        return {
            "position": "bottom",
            "crop_bbox_px": [0, 0, width, y0],
            "caption_bbox_px": [x0, y0, x1, y1],
            "confidence": "high",
            "horizontal_coverage": round(horizontal_coverage, 4),
            "center_offset": round(center_offset, 4),
            "multi_column_ambiguity": multi_column_ambiguity,
        }
    return {
        "position": "overlapping",
        "crop_bbox_px": None,
        "caption_bbox_px": [x0, y0, x1, y1],
        "confidence": "low",
        "horizontal_coverage": round(horizontal_coverage, 4),
        "center_offset": round(center_offset, 4),
        "multi_column_ambiguity": multi_column_ambiguity,
    }


def create_cleaned_derivative(
    source_path: Path | str,
    *,
    output_dir: Path | str,
    crop_bbox_px: list[int] | tuple[int, int, int, int],
    reason: str = "caption_band_removal",
) -> dict[str, Any]:
    """Write a cleaned derivative crop without ever overwriting the source.

    Coordinates are clamped to the source image bounds and rejected only when
    the clamped box is degenerate.  The parent file's SHA-256 is recorded so
    every derived image keeps explicit crop provenance.
    """

    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parent_sha = _sha256_file(source_path)
    if len(crop_bbox_px) != 4:
        raise ValueError("crop_bbox_px must contain x0,y0,x1,y1")
    with Image.open(source_path) as image:
        width, height = image.size
        clamped = clamp_bbox_px(
            crop_bbox_px,
            image_width=width,
            image_height=height,
        )
        if clamped is None:
            raise ValueError(f"invalid crop bbox: {crop_bbox_px}")
        x0, y0, x1, y1 = clamped
        crop = image.crop((x0, y0, x1, y1))
        filename = (
            f"{Path(source_path).stem}_cleaned_{parent_sha[:8]}.png"
        )
        destination = output_dir / filename
        if destination.exists():
            existing = _sha256_file(destination)
        else:
            crop.save(destination, format="PNG")
            existing = _sha256_file(destination)
    return {
        "filename": filename,
        "path": str(destination),
        "sha256": existing,
        "parent_sha256": parent_sha,
        "crop_bbox_px": [x0, y0, x1, y1],
        "width": int(x1 - x0),
        "height": int(y1 - y0),
        "reason": reason,
    }


def materialize_advisor_crop(
    source_path: Path | str,
    *,
    output_dir: Path | str,
    advice: Mapping[str, Any],
    image_width: int | None = None,
    image_height: int | None = None,
) -> dict[str, Any]:
    """Materialize a Qwen advisor content crop with full local authority.

    The advisor only supplies normalized semantic boxes; this function
    clamps/validates them, writes an immutable derived image (parent is never
    overwritten), records parent-hash/provenance, and then runs the existing
    local crop-hygiene audit on the derivative.  Any invalid geometry, model
    failure, or derivative audit rejection fails open to ``needs_review``
    with ``derivative=None`` so the original candidate remains the fallback.
    """

    source_path = Path(source_path)
    output_dir = Path(output_dir)
    advice = _mapping(advice)
    now = _now_utc()
    try:
        with Image.open(source_path) as opened:
            actual_width, actual_height = opened.size
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": QWEN_ADVISOR_CROP_METHOD,
            "extraction_method": QWEN_ADVISOR_CROP_METHOD,
            "caption_bbox": advice.get("caption_bbox"),
            "region_bbox": advice.get("content_bbox"),
            "reason": f"advisor_image_unreadable:{type(exc).__name__}",
            "evidence": {"advisor": dict(advice)},
            "derivative": None,
            "advisor": {
                "asset_kind": _text(advice.get("asset_kind")) or "unknown",
                "confidence": advice.get("confidence"),
                "needs_review": True,
            },
            "created_at": now,
        }
    width = int(image_width or actual_width)
    height = int(image_height or actual_height)
    validated, errors = validate_advisor_boxes(
        advice,
        image_width=width,
        image_height=height,
    )
    evidence: dict[str, Any] = {
        "advisor": dict(advice),
        "advisor_validation": validated,
        "image_size": [actual_width, actual_height],
    }
    if errors or validated.get("content_bbox_px") is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": QWEN_ADVISOR_CROP_METHOD,
            "extraction_method": QWEN_ADVISOR_CROP_METHOD,
            "caption_bbox": advice.get("caption_bbox"),
            "region_bbox": advice.get("content_bbox"),
            "reason": "advisor_box_validation_failed:" + ";".join(errors),
            "evidence": evidence,
            "derivative": None,
            "advisor": {
                "asset_kind": _text(advice.get("asset_kind")) or "unknown",
                "confidence": advice.get("confidence"),
                "needs_review": True,
                "errors": errors[:16],
            },
            "created_at": now,
        }
    content_bbox_px = validated["content_bbox_px"]
    try:
        derivative = create_cleaned_derivative(
            source_path,
            output_dir=output_dir,
            crop_bbox_px=content_bbox_px,
            reason="qwen_advisor_content_crop",
        )
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": QWEN_ADVISOR_CROP_METHOD,
            "extraction_method": QWEN_ADVISOR_CROP_METHOD,
            "caption_bbox": advice.get("caption_bbox"),
            "region_bbox": advice.get("content_bbox"),
            "reason": f"advisor_derivative_creation_failed:{type(exc).__name__}",
            "evidence": evidence,
            "derivative": None,
            "advisor": {
                "asset_kind": _text(advice.get("asset_kind")) or "unknown",
                "confidence": advice.get("confidence"),
                "needs_review": True,
            },
            "created_at": now,
        }

    derivative_audit = audit_crop_hygiene(
        {"extraction_method": QWEN_ADVISOR_CROP_METHOD},
        derivative["path"],
    )
    evidence["derivative_candidate"] = derivative
    evidence["derivative_audit"] = derivative_audit
    audit_status = _text(derivative_audit.get("status"))
    if audit_status in {"needs_review", "rejected"}:
        try:
            Path(derivative["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": QWEN_ADVISOR_CROP_METHOD,
            "extraction_method": QWEN_ADVISOR_CROP_METHOD,
            "caption_bbox": advice.get("caption_bbox"),
            "region_bbox": advice.get("content_bbox"),
            "reason": f"advisor_derivative_audit_failed:{audit_status}",
            "evidence": evidence,
            "derivative": None,
            "advisor": {
                "asset_kind": _text(advice.get("asset_kind")) or "unknown",
                "confidence": advice.get("confidence"),
                "needs_review": True,
            },
            "created_at": now,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "derived_clean",
        "source_kind": QWEN_ADVISOR_CROP_METHOD,
        "extraction_method": QWEN_ADVISOR_CROP_METHOD,
        "caption_bbox": advice.get("caption_bbox"),
        "region_bbox": advice.get("content_bbox"),
        "reason": "qwen_advisor_content_crop_materialized_and_hygiene_clean",
        "evidence": evidence,
        "derivative": derivative,
        "advisor": {
            "asset_kind": _text(advice.get("asset_kind")) or "unknown",
            "confidence": advice.get("confidence"),
            "needs_review": False,
        },
        "created_at": now,
    }


def audit_crop_hygiene(
    record: Mapping[str, Any],
    image_path: Path | str,
    *,
    derivative_dir: Path | str | None = None,
    create_derivative: bool = False,
) -> dict[str, Any]:
    """Audit one candidate and optionally materialize a cleaned derivative.

    Returns a ``crop_hygiene`` record.  ``derived_clean`` is only returned
    when a derivative was actually created; otherwise a removable caption
    band yields ``needs_review`` so the source image is never mistaken for a
    clean review image.
    """

    record = _mapping(record)
    source_prov = _mapping(record.get("source_provenance"))
    extraction_method = _text(
        record.get("extraction_method")
        or source_prov.get("parser")
        or record.get("parser")
        or record.get("source_kind")
    )
    region_bbox = _normalize_bbox(
        record.get("region_bbox")
        or record.get("bbox_pdf")
        or source_prov.get("bbox")
        or record.get("bbox")
    )
    caption_bbox = _normalize_bbox(
        record.get("caption_bbox")
        or source_prov.get("caption_bbox")
    )
    image_path = Path(image_path)
    if not image_path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "rejected",
            "source_kind": extraction_method or "unknown",
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": "image_unreadable",
            "evidence": {},
            "derivative": None,
            "created_at": _now_utc(),
        }
    try:
        with Image.open(image_path) as opened:
            original = opened.convert("RGB")
            preview = original.copy()
            preview.thumbnail((PREVIEW_MAX_DIM, PREVIEW_MAX_DIM))
        stats = _image_stats(preview)
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "rejected",
            "source_kind": extraction_method or "unknown",
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": f"image_decode_failed:{type(exc).__name__}",
            "evidence": {},
            "derivative": None,
            "created_at": _now_utc(),
        }

    scale_x = original.width / max(1, preview.width)
    scale_y = original.height / max(1, preview.height)
    preview_band = _detect_bottom_caption_band(stats)
    prose = _prose_contamination(
        stats,
        preview_band,
        rendered_region=_is_rendered_region_method(extraction_method),
    )
    heuristic_band = _scale_band_to_original(
        preview_band,
        scale_x,
        scale_y,
    )
    mapped_band = _mapped_caption_band(
        original, region_bbox, caption_bbox
    )

    source_kind = (
        RENDERED_REGION_METHOD
        if _is_rendered_region_method(extraction_method)
        else (
            extraction_method
            if extraction_method in PASS_THROUGH_METHODS
            else "unknown"
        )
    )
    evidence = {
        "image": {
            "width": original.width,
            "height": original.height,
            "colored_row_ratio": round(
                float(stats["colored_row_ratio"]), 4
            ),
        },
        "preview": {
            "width": preview.width,
            "height": preview.height,
            "max_dim": PREVIEW_MAX_DIM,
        },
        "caption_band": heuristic_band,
        "preview_caption_band": dict(preview_band),
        "mapped_caption_band": mapped_band,
        "prose_contamination": prose,
    }

    if source_kind in PASS_THROUGH_METHODS:
        if prose and prose["verdict"] != "none":
            status = prose["verdict"]
            reason = f"embedded_or_html_image_with_prose_signal:{prose['verdict']}"
        else:
            status = "clean"
            reason = (
                "embedded_or_html_image; no caption-band/prose signal found"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "source_kind": source_kind,
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": reason,
            "evidence": evidence,
            "derivative": None,
            "created_at": _now_utc(),
        }

    mapped_ambiguity = bool(
        mapped_band and mapped_band.get("multi_column_ambiguity")
    )
    if mapped_ambiguity:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": source_kind,
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": (
                "multi_column_or_off_center_ambiguity:"
                "horizontal_coverage="
                f"{mapped_band.get('horizontal_coverage')},"
                "center_offset="
                f"{mapped_band.get('center_offset')}"
            ),
            "evidence": evidence,
            "derivative": None,
            "created_at": _now_utc(),
        }

    selected_crop: list[int] | None = None
    crop_reason = ""
    if mapped_band is not None and mapped_band.get("crop_bbox_px"):
        selected_crop = mapped_band["crop_bbox_px"]
        crop_reason = (
            f"caption_bbox_removed:{mapped_band.get('position')}"
        )
    elif (
        heuristic_band.get("found")
        and heuristic_band.get("confidence") == "high"
        and heuristic_band.get("band_top") is not None
    ):
        selected_crop = [
            0,
            0,
            original.width,
            int(heuristic_band["band_top"]),
        ]
        crop_reason = "high_confidence_bottom_caption_band_removed"

    if prose and prose["verdict"] != "none":
        return {
            "schema_version": SCHEMA_VERSION,
            "status": prose["verdict"],
            "source_kind": source_kind,
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": (
                f"whole_page_or_prose_contamination:{prose['verdict']}"
            ),
            "evidence": evidence,
            "derivative": None,
            "created_at": _now_utc(),
        }

    if selected_crop is not None:
        if create_derivative and derivative_dir is not None:
            try:
                derivative = create_cleaned_derivative(
                    image_path,
                    output_dir=derivative_dir,
                    crop_bbox_px=selected_crop,
                    reason=crop_reason,
                )
            except Exception as exc:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "needs_review",
                    "source_kind": source_kind,
                    "extraction_method": extraction_method,
                    "caption_bbox": caption_bbox,
                    "region_bbox": region_bbox,
                    "reason": (
                        "cleaned_derivative_required_but_creation_failed:"
                        f"{type(exc).__name__}"
                    ),
                    "evidence": evidence,
                    "derivative": None,
                    "created_at": _now_utc(),
                }
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "derived_clean",
                "source_kind": source_kind,
                "extraction_method": extraction_method,
                "caption_bbox": caption_bbox,
                "region_bbox": region_bbox,
                "reason": crop_reason,
                "evidence": evidence,
                "derivative": derivative,
                "created_at": _now_utc(),
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_review",
            "source_kind": source_kind,
            "extraction_method": extraction_method,
            "caption_bbox": caption_bbox,
            "region_bbox": region_bbox,
            "reason": (
                "caption_band_detected; cleaned_derivative_required_"
                "but_not_created"
            ),
            "evidence": evidence,
            "derivative": None,
            "created_at": _now_utc(),
        }

    if source_kind == RENDERED_REGION_METHOD:
        status = "needs_review"
        reason = (
            "rendered_region_from_caption; caption separation uncertain "
            "without high-confidence band"
        )
    else:
        status = "clean"
        reason = "no_caption_band_or_prose_signal_detected"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "source_kind": source_kind,
        "extraction_method": extraction_method,
        "caption_bbox": caption_bbox,
        "region_bbox": region_bbox,
        "reason": reason,
        "evidence": evidence,
        "derivative": None,
        "created_at": _now_utc(),
    }


__all__ = [
    "AUDIT_STATUSES",
    "PASS_THROUGH_METHODS",
    "PREVIEW_MAX_DIM",
    "QWEN_ADVISOR_CROP_METHOD",
    "RENDERED_REGION_METHOD",
    "SCHEMA_VERSION",
    "audit_crop_hygiene",
    "bboxes_overlap_px",
    "clamp_bbox_px",
    "create_cleaned_derivative",
    "materialize_advisor_crop",
    "validate_advisor_boxes",
]
