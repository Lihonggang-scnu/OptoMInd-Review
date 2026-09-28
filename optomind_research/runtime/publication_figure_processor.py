"""Prepare source-derived figures for a reader-facing manuscript.

The visual pipeline stores rich source context for traceability.  A publication
figure is a different asset: it must contain the graphic only, while the new
manuscript caption is written outside the image.  This module performs a
conservative deterministic crop when an embedded source-caption band is
detected or explicitly annotated.  It never synthesizes quantitative data.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def clean_public_caption(value: Any) -> str:
    """Remove provenance fragments that belong in an audit ledger, not a PDF."""

    text = " ".join(str(value or "").replace("\n", " ").split())
    text = re.sub(r"(?i)\bsource\s+figure\s+context\s*:\s*.*?(?=(?:\bsource\s*:|$))", "", text)
    text = re.sub(r"(?i)\bsource\s*:\s*[^.]+\.?", "", text)
    text = re.sub(r"(?i)^\s*(?:fig(?:ure)?\.?\s*\d+[A-Za-z]?\s*[:.\-]?\s*)", "", text)
    # A few legacy visual cards append an abbreviated DOI tail (for example
    # ``1038/s41467-...``).  Provenance stays in the asset ledger and source
    # citation; it is not reader-facing caption prose.
    text = re.sub(
        r"(?i)\b(?:10\.)?\d{4,9}/[A-Z0-9._;()/:+\-]+\b",
        "",
        text,
    )
    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _explicit_crop_box(item: dict[str, Any], width: int, height: int) -> tuple[int, int, int, int] | None:
    raw = item.get("publication_crop_box") or item.get("image_crop_box")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            values = [float(value) for value in raw]
        except (TypeError, ValueError):
            return None
        # Normalized [0, 1] coordinates are easier to persist across image
        # resolutions; pixel coordinates remain accepted for older assets.
        if all(0.0 <= value <= 1.0 for value in values):
            left, top, right, bottom = (
                int(values[0] * width), int(values[1] * height),
                int(values[2] * width), int(values[3] * height),
            )
        else:
            left, top, right, bottom = [int(value) for value in values]
        if 0 <= left < right <= width and 0 <= top < bottom <= height:
            return left, top, right, bottom
    return None


def _caption_crop_boundary(image: Any) -> int | None:
    """Return a safe crop boundary above an obvious bottom caption band.

    The detector intentionally favours false negatives.  A graph with labels
    must never be shortened merely because its lower axis resembles text.
    """

    try:
        import numpy as np
    except Exception:
        return None
    gray = np.asarray(image.convert("L"), dtype="uint8")
    height, width = gray.shape
    if height < 100 or width < 160:
        return None
    dark_fraction = (gray < 185).mean(axis=1)
    white_fraction = (gray > 245).mean(axis=1)
    # Captions beneath dense multi-panel figures may start just below the
    # halfway mark; restricting the scan to the bottom half misses them.
    # A portrait-shaped publisher page usually places the figure before its
    # caption and body copy.  Start earlier and keep the first strong page
    # break in that case; for ordinary figure crops, retain the conservative
    # lower-page scan and final qualifying gap.
    page_like = height > width * 1.05
    start = max(1, int(height * (0.18 if page_like else 0.38)))
    stop = min(height - 36, int(height * 0.90))

    # Publisher PDF crops often contain no literal gap between the graphic and
    # the caption.  The reliable signal is instead a *run* of whitespace
    # followed by a lower text band.  Axes can produce one white row, but very
    # rarely 10+ consecutive blank rows followed by several typography bands.
    separator_rows = white_fraction >= 0.985
    minimum_separator_rows = max(12, int(height * 0.020))
    run_start: int | None = None
    best_boundary: int | None = None
    for row in range(start, stop + 1):
        if separator_rows[row]:
            if run_start is None:
                run_start = row
            continue
        if run_start is None:
            continue
        run_end = row
        run_length = run_end - run_start
        candidate = run_start
        run_start = None
        if run_length < minimum_separator_rows:
            continue
        lower = dark_fraction[run_end:]
        lower_white = white_fraction[run_end:]
        if len(lower) < max(42, int(height * 0.08)):
            continue
        minimum_lower_ink = 0.001 if page_like else 0.012
        if (
            float(lower_white.mean()) < 0.72
            or float(lower.mean()) < minimum_lower_ink
        ):
            continue
        # Dilate ink rows slightly: a publisher caption has several visual
        # text bands even when bold glyphs make a few scanlines unusually dark.
        ink_rows = lower > 0.0015
        dilated = np.convolve(ink_rows.astype("int8"), np.ones(5, dtype="int8"), mode="same") > 0
        bands = 0
        inside = False
        for active in dilated:
            if active and not inside:
                bands += 1
                inside = True
            elif not active:
                inside = False
        # Multi-column captions can form one visually continuous dense block
        # after dilation.  Several high-ink scanlines are an equivalent signal
        # once a long separator has already isolated the lower band.
        dense_text_rows = int((lower > 0.10).sum())
        if bands >= 2 or dense_text_rows >= 4:
            if page_like:
                return candidate
            # Multi-panel source figures may have an earlier inter-panel gap.
            # The final qualifying gap is the one immediately above the
            # publisher caption, while an earlier gap is part of the graphic.
            best_boundary = candidate
    return best_boundary


def _publication_asset_quality(image: Any) -> tuple[bool, str]:
    """Reject obvious text strips after conservative publication cropping."""

    width, height = int(image.width), int(image.height)
    aspect_ratio = max(width / height, height / width)
    if aspect_ratio > 4.5 and min(width, height) < 240:
        return False, "publication_crop_extreme_aspect_ratio"
    return True, ""


def prepare_publication_figure(
    source: Path,
    destination: Path,
    item: dict[str, Any],
) -> dict[str, Any]:
    """Copy a source figure, applying only auditable caption removal."""

    result: dict[str, Any] = {
        "source_path": str(source),
        "publication_path": str(destination),
        "caption_crop_status": "not_applicable",
        "caption_crop_boundary": None,
        "caption": clean_public_caption(
            item.get("caption_en") or item.get("caption_preview") or ""
        ),
    }
    suffix = source.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        result["caption_crop_status"] = "non_raster_copied_without_crop"
        return result
    try:
        from PIL import Image

        image = Image.open(source).convert("RGB")
        box = _explicit_crop_box(item, image.width, image.height)
        status = "explicit_crop" if box else "preserved_no_caption_signal"
        # Article-owned conceptual diagrams carry a deliberate disclosure
        # footer ("AI-assisted explanatory diagram").  The generic caption
        # detector cannot distinguish that footer from a publisher caption
        # reliably; on a tall graph it may mistake an internal lower label or
        # edge for the caption boundary and amputate the outcome node.  Keep
        # the complete generated visual unless the caller supplied an
        # explicit crop.  Source-derived figures retain the conservative
        # heuristic below.
        generated_conceptual = (
            str(item.get("source_route") or "").strip().lower()
            == "conceptual_generated"
            or (
                str(item.get("generated_or_source") or "")
                .strip()
                .lower()
                == "generated"
                and str(item.get("figure_type") or "")
                .strip()
                .lower()
                == "structured_explanatory_diagram"
            )
        )
        if box is None and not generated_conceptual:
            boundary = _caption_crop_boundary(image)
            if boundary is not None:
                box = (0, 0, image.width, boundary)
                status = "heuristic_embedded_caption_crop"
        if box is not None:
            image = image.crop(box)
            result["caption_crop_boundary"] = list(box)
        eligible, rejection_reason = _publication_asset_quality(image)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        result["caption_crop_status"] = status
        result["rendered_size"] = [image.width, image.height]
        result["publication_asset_eligible"] = eligible
        result["publication_asset_rejection_reason"] = rejection_reason
        return result
    except Exception as exc:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        result["caption_crop_status"] = "processor_fallback_copied"
        result["processor_error"] = type(exc).__name__
        result["publication_asset_eligible"] = False
        result["publication_asset_rejection_reason"] = (
            "publication_figure_processor_failed"
        )
        return result
