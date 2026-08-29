"""Bounded, optional Qwen semantic crop-boundary advisor.

Qwen is deliberately an *advisor* only: it may return semantic JSON fields
about what a figure/table region is, where its content and caption live, and
how confident the model is.  It never writes pixels, never copies files, and
never overrides the local Pillow crop/hygiene authority.

The module is pure and mockable: parsing, JSON repair, coordinate
normalization, overlap checks, and confidence gating are all deterministic
local code.  A caller may optionally inject a Qwen client callable; any
call failure, malformed JSON, low confidence, or invalid geometry fails open
to ``needs_review=True`` so the original candidate is preserved and prose is
never blocked.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping


QWEN_ADVISOR_SCHEMA_VERSION = "optomind.visual_qwen_crop_advisor.v1"

QWEN_ASSET_KINDS = frozenset(
    {
        "figure",
        "table",
        "diagram",
        "photo",
        "equation",
        "page_region",
        "unknown",
    }
)

# Confidence below this threshold means the advisor did not provide usable
# crop-boundary information; the local pipeline keeps the original candidate
# and marks it for review.
LOW_CONFIDENCE_THRESHOLD = 0.35

MAX_PANEL_BOXES = 24
MAX_CAPTION_CHARS = 2000
MAX_NOTE_CHARS = 160
MAX_RESPONSE_CHARS = 200_000

_KEY_GROUPS = (
    ("x0", "xmin", "left"),
    ("y0", "ymin", "top"),
    ("x1", "xmax", "right"),
    ("y1", "ymax", "bottom"),
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _strings(value: Any, limit: int = 12) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    rows: list[str] = []
    for item in values:
        text = _text(item)
        if not text:
            continue
        text = text[:MAX_NOTE_CHARS]
        if text not in rows:
            rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def _extract_bbox_values(value: Any) -> tuple[float, float, float, float] | None:
    """Extract (x0,y0,x1,y1) from list or mapping forms."""

    if isinstance(value, (list, tuple)):
        if len(value) < 4:
            return None
        numbers = [_float(item) for item in value[:4]]
    elif isinstance(value, Mapping):
        numbers: list[float | None] = []
        for group in _KEY_GROUPS:
            found = None
            for key in group:
                if key in value:
                    found = _float(value[key])
                    break
            if found is None:
                return None
            numbers.append(found)
    else:
        return None
    if any(number is None for number in numbers):
        return None
    x0, y0, x1, y1 = (float(number) for number in numbers)  # type: ignore[misc]
    return x0, y0, x1, y1


def normalize_bbox(
    value: Any,
    *,
    image_width: int | float | None = None,
    image_height: int | float | None = None,
    coordinate_space: str = "",
) -> list[float] | None:
    """Return a clamped normalized ``[x0,y0,x1,y1]`` box or ``None``.

    Accepts list/tuple ``[x0,y0,x1,y1]`` or a mapping using ``x0/y0/x1/y1``,
    ``xmin/ymin/xmax/ymax``, or ``left/top/right/bottom`` keys.  Values that
    look normalized (all in ``[0,1]``) stay normalized; otherwise they are
    treated as pixels and divided by the supplied image size.  Boxes are
    order-normalized, clamped to ``[0,1]``, and rejected when degenerate.
    """

    box = _extract_bbox_values(value)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0

    width = _float(image_width)
    height = _float(image_height)
    normalized_hint = bool(
        coordinate_space == "normalized"
        or (
            coordinate_space != "pixel"
            and 0.0 <= x0 <= 1.0
            and 0.0 <= y0 <= 1.0
            and 0.0 <= x1 <= 1.0
            and 0.0 <= y1 <= 1.0
        )
    )
    if not normalized_hint:
        if width is None or height is None or width <= 0 or height <= 0:
            return None
        x0 /= width
        x1 /= width
        y0 /= height
        y1 /= height
    x0 = max(0.0, min(1.0, x0))
    y0 = max(0.0, min(1.0, y0))
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]


def boxes_overlap(
    left: list[float] | tuple[float, float, float, float],
    right: list[float] | tuple[float, float, float, float],
    *,
    tolerance: float = 0.0,
) -> bool:
    """Return whether two normalized boxes overlap by more than tolerance."""

    lx0, ly0, lx1, ly1 = (float(item) for item in left)
    rx0, ry0, rx1, ry1 = (float(item) for item in right)
    return bool(
        lx0 < rx1 - tolerance
        and rx0 < lx1 - tolerance
        and ly0 < ry1 - tolerance
        and ry0 < ly1 - tolerance
    )


def repair_advisor_json(text: str) -> dict[str, Any]:
    """Parse model JSON, repairing common fence/prose/trailing-comma issues."""

    text = str(text or "")
    if not text.strip():
        raise ValueError("empty_response")
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        repaired = _repair_common_json(candidate)
        try:
            parsed = json.loads(repaired)
        except (ValueError, json.JSONDecodeError):
            try:
                import json_repair

                parsed = json_repair.loads(candidate)
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        raise ValueError("parsed_json_not_object")
    return parsed


def _repair_common_json(text: str) -> str:
    """Small deterministic repairs before falling back to json-repair."""

    repaired = text.strip()
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(
        r"(['\"]?)([A-Za-z_][A-Za-z0-9_]*)(['\"]?)\s*:",
        lambda match: f'"{match.group(2)}":',
        repaired,
    )
    repaired = re.sub(r":\s*'([^']*)'", r':"\1"', repaired)
    repaired = re.sub(r"\\'", "'", repaired)
    return repaired


def _advice_failure(*errors: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": QWEN_ADVISOR_SCHEMA_VERSION,
        "ok": False,
        "needs_review": True,
        "asset_kind": "unknown",
        "content_bbox": None,
        "caption_bbox": None,
        "panel_boxes": [],
        "caption_text": "",
        "confidence": 0.0,
        "contamination_notes": [],
        "errors": list(errors),
        "advisor": {
            "model": _text(extra.get("advisor_model")),
            "called": bool(extra.get("called")),
        },
    }
    return payload


def parse_qwen_crop_advice(
    response_text: str,
    *,
    image_width: int | float | None = None,
    image_height: int | float | None = None,
    advisor_model: str = "",
    called: bool = True,
) -> dict[str, Any]:
    """Parse and validate one Qwen advisor response into a bounded record.

    ``ok=False`` / ``needs_review=True`` is the fail-open contract: callers
    keep the original candidate and mark it for review.  No pixels are read
    or written here; ``image_width``/``image_height`` are optional and only
    used to convert pixel coordinates supplied by the model.
    """

    try:
        parsed = repair_advisor_json(response_text)
    except Exception as exc:
        result = _advice_failure(
            f"malformed_json:{type(exc).__name__}:{exc}",
            advisor_model=advisor_model,
            called=called,
        )
        result["raw_response"] = str(response_text)[:4000]
        return result

    errors: list[str] = []
    notes: list[str] = _strings(parsed.get("contamination_notes"))
    raw_kind = _text(
        parsed.get("asset_kind")
        or parsed.get("kind")
        or parsed.get("asset_type")
    ).lower()
    if raw_kind not in QWEN_ASSET_KINDS:
        if raw_kind:
            errors.append(f"unsupported_asset_kind:{raw_kind}")
        else:
            errors.append("asset_kind_missing")
        raw_kind = "unknown"

    content_bbox = normalize_bbox(
        parsed.get("content_bbox") or parsed.get("bbox") or parsed.get("content_box"),
        image_width=image_width,
        image_height=image_height,
    )
    caption_bbox = normalize_bbox(
        parsed.get("caption_bbox") or parsed.get("caption_box"),
        image_width=image_width,
        image_height=image_height,
    )
    if content_bbox is None:
        errors.append("content_bbox_invalid_or_missing")
    if caption_bbox is None:
        errors.append("caption_bbox_invalid_or_missing")

    panel_values = (
        parsed.get("panel_boxes")
        or parsed.get("panels")
        or parsed.get("panel_box")
        or []
    )
    if not isinstance(panel_values, (list, tuple)):
        panel_values = []
    panel_boxes: list[list[float]] = []
    for item in panel_values[:MAX_PANEL_BOXES]:
        panel = normalize_bbox(
            item,
            image_width=image_width,
            image_height=image_height,
        )
        if panel is None:
            errors.append("panel_box_invalid")
            continue
        if content_bbox is not None and not boxes_overlap(
            panel, content_bbox
        ):
            errors.append("panel_box_outside_content_bbox")
        panel_boxes.append(panel)
    for left in range(len(panel_boxes)):
        for right in range(left + 1, len(panel_boxes)):
            if boxes_overlap(panel_boxes[left], panel_boxes[right]):
                errors.append("panel_boxes_overlap")
                break

    confidence = _float(parsed.get("confidence"))
    if confidence is None:
        errors.append("confidence_missing")
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        errors.append(
            f"confidence_low:{round(confidence, 4)}<{LOW_CONFIDENCE_THRESHOLD}"
        )

    if (
        content_bbox is not None
        and caption_bbox is not None
        and boxes_overlap(content_bbox, caption_bbox, tolerance=0.005)
    ):
        errors.append("caption_bbox_overlaps_content_bbox")
        if "caption_overlaps_content" not in notes:
            notes.append("caption_overlaps_content")

    caption_text = _text(
        parsed.get("caption_text")
        or parsed.get("caption")
        or parsed.get("text")
    )[:MAX_CAPTION_CHARS]
    if not caption_text:
        errors.append("caption_text_missing")

    needs_review = bool(errors)
    return {
        "schema_version": QWEN_ADVISOR_SCHEMA_VERSION,
        "ok": not needs_review,
        "needs_review": needs_review,
        "asset_kind": raw_kind,
        "content_bbox": content_bbox,
        "caption_bbox": caption_bbox,
        "panel_boxes": panel_boxes,
        "caption_text": caption_text,
        "confidence": round(confidence, 6),
        "contamination_notes": notes,
        "errors": errors[:16],
        "advisor": {
            "model": _text(advisor_model),
            "called": bool(called),
            "schema_version": QWEN_ADVISOR_SCHEMA_VERSION,
        },
        "raw_response": str(response_text)[:4000],
    }


def default_qwen_crop_prompt(asset_hint: str = "") -> str:
    """Prompt template for the optional Qwen crop advisor.

    The model is asked for semantic fields only.  Pixels are never produced
    or mutated by this module; local crop code remains authoritative.
    """

    hint = f" The asset is expected to be a {asset_hint}." if asset_hint else ""
    return (
        "You are a semantic crop-boundary advisor for scientific visual "
        "extraction. Inspect the attached page/visual and return JSON only "
        "with these fields: asset_kind (one of figure/table/diagram/photo/"
        "equation/page_region/unknown), content_bbox, caption_bbox, "
        "panel_boxes (list), caption_text, confidence (0..1), and "
        "contamination_notes (list). Use normalized [x0,y0,x1,y1] "
        "coordinates in [0,1]. Do not invent pixels, do not crop, and do not "
        "return anything outside JSON." + hint
    )


def advise_with_qwen(
    qwen_client: Callable[..., str] | None,
    image_path: Any,
    *,
    prompt: str = "",
    image_width: int | float | None = None,
    image_height: int | float | None = None,
    advisor_model: str = "",
    timeout_seconds: int | float = 30,
) -> dict[str, Any]:
    """Bounded optional advisor invocation; always fails open.

    ``qwen_client`` must be a callable accepting ``(prompt, image_path)``
    and returning text.  Any exception, timeout, malformed output, or low
    confidence yields ``needs_review=True`` and never blocks the caller.
    """

    if qwen_client is None:
        result = _advice_failure(
            "qwen_client_unavailable",
            advisor_model=advisor_model,
        )
        result["advisor"]["timeout_seconds"] = timeout_seconds
        return result
    try:
        response = qwen_client(
            prompt or default_qwen_crop_prompt(),
            image_path,
        )
    except Exception as exc:
        result = _advice_failure(
            f"qwen_call_failed:{type(exc).__name__}:{exc}",
            advisor_model=advisor_model,
            called=True,
        )
        result["advisor"]["timeout_seconds"] = timeout_seconds
        return result
    if not isinstance(response, str):
        result = _advice_failure(
            "qwen_call_returned_non_text",
            advisor_model=advisor_model,
            called=True,
        )
        result["advisor"]["timeout_seconds"] = timeout_seconds
        return result
    result = parse_qwen_crop_advice(
        response[:MAX_RESPONSE_CHARS],
        image_width=image_width,
        image_height=image_height,
        advisor_model=advisor_model,
        called=True,
    )
    result["advisor"]["timeout_seconds"] = timeout_seconds
    result["advisor"]["prompt_preview"] = (
        prompt or default_qwen_crop_prompt()
    )[:200]
    return result


__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "MAX_PANEL_BOXES",
    "QWEN_ADVISOR_SCHEMA_VERSION",
    "QWEN_ASSET_KINDS",
    "advise_with_qwen",
    "boxes_overlap",
    "default_qwen_crop_prompt",
    "normalize_bbox",
    "parse_qwen_crop_advice",
    "repair_advisor_json",
]
