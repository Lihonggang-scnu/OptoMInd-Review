"""Heuristic subfigure extraction and optional Qwen-VL tagging.

This module is deliberately small and local-tool friendly:
- it reads the existing visual_assets JSONL protocol;
- it crops likely subfigures with deterministic image heuristics;
- it can call a vision model only for a small subset of crops.

It is an experimental bridge toward treating subfigures as first-class review
materials. It does not delete or replace parent figures.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from llm.qwen_chat_client import call_qwen_chat
from optomind_research.common_io import clip, contains_cjk, write_json, write_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VISUAL_ASSETS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "visual_asset_pipeline"
    / "core58-v31-20260701"
    / "visual_assets.v1_1.all.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "subfigure_extraction"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Subfigure Visual Tagger.txt"


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def print_event(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def safe_id(value: Any, limit: int = 120) -> str:
    text = str(value or "item").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return (text or "item")[:limit]


def asset_identity(asset: dict[str, Any]) -> dict[str, Any]:
    return asset.get("asset_identity") if isinstance(asset.get("asset_identity"), dict) else {}


def asset_paper(asset: dict[str, Any]) -> dict[str, Any]:
    return asset.get("paper") if isinstance(asset.get("paper"), dict) else {}


def local_image_path(asset: dict[str, Any]) -> Path | None:
    resources = asset.get("local_resources") if isinstance(asset.get("local_resources"), dict) else {}
    candidates = [
        resources.get("local_image_path"),
        asset.get("local_image_path"),
        asset.get("path"),
    ]
    for item in candidates:
        if item:
            p = Path(str(item))
            if p.exists():
                return p
    return None


def labels_for_asset(asset: dict[str, Any]) -> list[str]:
    ident = asset_identity(asset)
    labels = ident.get("subpanel_labels") or asset.get("subpanel_labels") or []
    if not isinstance(labels, list):
        return []
    return [str(x).strip() for x in labels if str(x).strip()]


def choose_grid(n: int, width: int, height: int) -> tuple[int, int]:
    n = max(1, int(n))
    aspect = max(0.1, width / max(1, height))
    candidates: list[tuple[float, int, int]] = []
    for rows in range(1, n + 1):
        for cols in range(1, n + 1):
            if rows * cols < n:
                continue
            grid_aspect = cols / max(1, rows)
            unused = rows * cols - n
            score = abs(math.log((grid_aspect + 1e-6) / aspect)) + unused * 0.35
            candidates.append((score, rows, cols))
    _, rows, cols = min(candidates, key=lambda x: x[0])
    return rows, cols


def trim_white_border(image: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    rgb = image.convert("RGB")
    arr = np.asarray(rgb)
    mask = np.any(arr < 248, axis=2)
    if not mask.any():
        return rgb, (0, 0, rgb.width, rgb.height)
    ys, xs = np.where(mask)
    left, right = int(xs.min()), int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max()) + 1
    pad = 4
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(rgb.width, right + pad)
    bottom = min(rgb.height, bottom + pad)
    return rgb.crop((left, top, right, bottom)), (left, top, right, bottom)


def nearest_whitespace_cut(gray: np.ndarray, expected: int, axis: int, window_ratio: float = 0.12) -> int:
    if axis == 0:
        length = gray.shape[1]
        scores = (gray > 245).mean(axis=0)
    else:
        length = gray.shape[0]
        scores = (gray > 245).mean(axis=1)
    window = max(8, int(length * window_ratio))
    lo = max(1, expected - window)
    hi = min(length - 1, expected + window)
    if hi <= lo:
        return expected
    local = scores[lo:hi]
    best = int(np.argmax(local)) + lo
    if scores[best] >= 0.82:
        return best
    return expected


def grid_boundaries(width: int, height: int, rows: int, cols: int, gray: np.ndarray) -> tuple[list[int], list[int]]:
    xs = [0]
    for c in range(1, cols):
        xs.append(nearest_whitespace_cut(gray, round(width * c / cols), axis=0))
    xs.append(width)
    ys = [0]
    for r in range(1, rows):
        ys.append(nearest_whitespace_cut(gray, round(height * r / rows), axis=1))
    ys.append(height)
    xs = sorted(set(xs))
    ys = sorted(set(ys))
    if len(xs) != cols + 1:
        xs = [round(width * c / cols) for c in range(cols + 1)]
    if len(ys) != rows + 1:
        ys = [round(height * r / rows) for r in range(rows + 1)]
    return xs, ys


def available_marker_fonts() -> list[Path]:
    candidates = [
        Path("C:/Windows/Fonts/timesbd.ttf"),
        Path("C:/Windows/Fonts/times.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    return [p for p in candidates if p.exists()]


def marker_template(label: str, font_path: Path, size: int) -> np.ndarray:
    font = ImageFont.truetype(str(font_path), size)
    text = f"({label})"
    probe = Image.new("L", (1, 1), 255)
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font)
    w = max(12, bbox[2] - bbox[0] + 8)
    h = max(12, bbox[3] - bbox[1] + 8)
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    draw.text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=0)
    return 255 - np.asarray(img)


def detect_subpanel_markers(image: Image.Image, labels: list[str], threshold: float = 0.86) -> dict[str, dict[str, Any]]:
    fonts = available_marker_fonts()
    if not fonts or not labels:
        return {}
    gray = np.asarray(image.convert("L"))
    haystack = 255 - gray
    detections: dict[str, dict[str, Any]] = {}
    for label in labels:
        best: tuple[float, tuple[int, int], tuple[int, int]] | None = None
        for font_path in fonts:
            for size in range(14, 34, 2):
                template = marker_template(label, font_path, size)
                if template.shape[0] >= haystack.shape[0] or template.shape[1] >= haystack.shape[1]:
                    continue
                res = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                candidate = (float(max_val), (int(max_loc[0]), int(max_loc[1])), (int(template.shape[1]), int(template.shape[0])))
                if best is None or candidate[0] > best[0]:
                    best = candidate
        if best and best[0] >= threshold:
            score, loc, size = best
            detections[label] = {
                "x": loc[0],
                "y": loc[1],
                "w": size[0],
                "h": size[1],
                "score": round(score, 3),
            }
    if len(detections) != len(labels):
        return {}
    points = []
    for label in labels:
        d = detections[label]
        points.append((label, d["x"] + d["w"] / 2, d["y"] + d["h"] / 2))
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if abs(points[i][1] - points[j][1]) < 24 and abs(points[i][2] - points[j][2]) < 24:
                return {}
    return detections


def cluster_axis(values: list[float], tolerance: float) -> tuple[list[float], dict[int, int]]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    centers: list[float] = []
    assignment: dict[int, int] = {}
    for original_index, value in ordered:
        placed = False
        for idx, center in enumerate(centers):
            if abs(value - center) <= tolerance:
                centers[idx] = (center + value) / 2
                assignment[original_index] = idx
                placed = True
                break
        if not placed:
            assignment[original_index] = len(centers)
            centers.append(value)
    return centers, assignment


def marker_based_boxes(image: Image.Image, labels: list[str]) -> dict[str, tuple[int, int, int, int]] | None:
    detections = detect_subpanel_markers(image, labels)
    if not detections:
        return None
    centers_x = [detections[label]["x"] + detections[label]["w"] / 2 for label in labels]
    centers_y = [detections[label]["y"] + detections[label]["h"] / 2 for label in labels]
    x_clusters, x_assign = cluster_axis(centers_x, max(40, image.width * 0.10))
    y_clusters, y_assign = cluster_axis(centers_y, max(40, image.height * 0.10))
    if len(x_clusters) * len(y_clusters) < len(labels):
        return None
    x_sorted = sorted(x_clusters)
    y_sorted = sorted(y_clusters)
    x_bounds = [0]
    for a, b in zip(x_sorted, x_sorted[1:]):
        x_bounds.append(round((a + b) / 2))
    x_bounds.append(image.width)
    y_bounds = [0]
    for a, b in zip(y_sorted, y_sorted[1:]):
        y_bounds.append(round((a + b) / 2))
    y_bounds.append(image.height)
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for i, label in enumerate(labels):
        x_center = centers_x[i]
        y_center = centers_y[i]
        col = min(range(len(x_sorted)), key=lambda idx: abs(x_sorted[idx] - x_center))
        row = min(range(len(y_sorted)), key=lambda idx: abs(y_sorted[idx] - y_center))
        boxes[label] = (x_bounds[col], y_bounds[row], x_bounds[col + 1], y_bounds[row + 1])
    return boxes


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def parse_json_response(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            pass
    try:
        from json_repair import repair_json  # type: ignore

        repaired = repair_json(str(text or ""), return_objects=True)
        return repaired if isinstance(repaired, dict) else {}
    except Exception:
        return {}


def normalize_visual_tag(tag: dict[str, Any]) -> dict[str, Any]:
    allowed_roles = {
        "method",
        "mechanism",
        "result",
        "benchmark",
        "spectrum",
        "micrograph",
        "device_photo",
        "schematic",
        "workflow",
        "table_like",
        "unclear",
    }
    allowed_utility = {"high", "medium", "low", "exclude"}
    allowed_conf = {"high", "medium", "low"}

    def clean_list(value: Any, limit: int = 8) -> list[str]:
        out = []
        if isinstance(value, list):
            src = value
        else:
            src = []
        for item in src:
            s = re.sub(r"\s+", " ", str(item or "")).strip()
            if s:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    role = str(tag.get("subfigure_role") or "unclear")
    if role not in allowed_roles:
        role = "unclear"
    review_use = tag.get("review_use") if isinstance(tag.get("review_use"), dict) else {}
    utility = str(review_use.get("utility") or "medium")
    if utility not in allowed_utility:
        utility = "medium"
    confidence = str(tag.get("confidence") or "low")
    if confidence not in allowed_conf:
        confidence = "low"
    return {
        "schema_version": "subfigure_visual_tag.v1",
        "subfigure_role": role,
        "visual_content_type": str(tag.get("visual_content_type") or ""),
        "short_label": str(tag.get("short_label") or ""),
        "what_is_visible": str(tag.get("what_is_visible") or ""),
        "caption_link": str(tag.get("caption_link") or ""),
        "key_visual_elements": clean_list(tag.get("key_visual_elements")),
        "materials_or_structures_visible": clean_list(tag.get("materials_or_structures_visible")),
        "metrics_or_axes_visible": clean_list(tag.get("metrics_or_axes_visible")),
        "review_use": {
            "utility": utility,
            "likely_sections": clean_list(review_use.get("likely_sections")),
            "argument_function": str(review_use.get("argument_function") or ""),
            "cautions": clean_list(review_use.get("cautions")),
        },
        "warnings": clean_list(tag.get("warnings")),
        "confidence": confidence,
    }


def tag_subfigure_with_qwen_vl(
    *,
    subfigure: dict[str, Any],
    prompt_path: Path,
    model_tier: str,
    max_tokens: int = 1600,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = prompt_path.read_text(encoding="utf-8")
    image_path = Path(str(subfigure["local_image_path"]))
    payload = {
        "paper_title": subfigure.get("paper_title", ""),
        "paper_id": subfigure.get("paper_id", ""),
        "parent_asset_id": subfigure.get("parent_asset_id", ""),
        "parent_label": subfigure.get("parent_label", ""),
        "subfigure_label": subfigure.get("subfigure_label", ""),
        "caption": clip(str(subfigure.get("caption") or ""), 1600),
        "nearby_text": clip(str(subfigure.get("nearby_text") or ""), 1000),
    }
    result = call_qwen_chat(
        "SubfigureVisualTaggerAgent",
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
                ],
            },
        ],
        model_tier=model_tier,
        temperature=0,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        force_mock=False,
        max_retries=1,
    )
    raw = str(result.get("content") or "")
    parsed = parse_json_response(raw)
    usage = dict(result.get("_llm_usage") or {})
    usage["_tag_diagnostics"] = {
        "raw_chars": len(raw),
        "parse_ok": bool(parsed),
        "raw_preview": clip(raw, 1000),
    }
    usage["_raw_content"] = raw
    return normalize_visual_tag(parsed), usage


def extract_asset_subfigures(asset: dict[str, Any], output_dir: Path, max_subfigures: int = 0) -> list[dict[str, Any]]:
    labels = labels_for_asset(asset)
    image_path = local_image_path(asset)
    if not labels or image_path is None:
        return []
    paper = asset_paper(asset)
    ident = asset_identity(asset)
    parent_asset_id = str(ident.get("asset_id") or safe_id(image_path.stem))
    raw = Image.open(image_path).convert("RGB")
    trimmed, trim_box = trim_white_border(raw)
    marker_boxes = marker_based_boxes(trimmed, labels)
    gray = np.asarray(trimmed.convert("L"))
    rows, cols = choose_grid(len(labels), trimmed.width, trimmed.height)
    xs, ys = grid_boundaries(trimmed.width, trimmed.height, rows, cols, gray)
    out_dir = output_dir / "subfigures" / safe_id(parent_asset_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    subfigures: list[dict[str, Any]] = []
    if marker_boxes:
        box_items = [(label, marker_boxes[label]) for label in labels]
        split_method = "marker_template_grid"
        split_confidence = "medium"
    else:
        box_items = []
        label_index = 0
        for r in range(len(ys) - 1):
            for c in range(len(xs) - 1):
                if label_index >= len(labels):
                    break
                label = labels[label_index]
                box_items.append((label, (xs[c], ys[r], xs[c + 1], ys[r + 1])))
                label_index += 1
        split_method = f"grid_{rows}x{cols}_whitespace_adjusted"
        split_confidence = "medium" if rows * cols == len(labels) else "low"
    for label, box in box_items:
            if max_subfigures and len(subfigures) >= max_subfigures:
                break
            x0, y0, x1, y1 = box
            if x1 - x0 < 80 or y1 - y0 < 80:
                continue
            crop = trimmed.crop((x0, y0, x1, y1))
            sub_id = f"{parent_asset_id}-subfig-{safe_id(label, 20)}"
            crop_path = out_dir / f"{safe_id(sub_id)}.png"
            crop.save(crop_path)
            bbox = [x0 + trim_box[0], y0 + trim_box[1], x1 + trim_box[0], y1 + trim_box[1]]
            subfigures.append(
                {
                    "schema_version": "subfigure_asset.v1",
                    "subfigure_id": sub_id,
                    "parent_asset_id": parent_asset_id,
                    "paper_id": paper.get("paper_id", ""),
                    "paper_title": paper.get("title", ""),
                    "parent_label": ident.get("label", ""),
                    "subfigure_label": label,
                    "bbox_px": bbox,
                    "local_image_path": str(crop_path),
                    "parent_image_path": str(image_path),
                    "caption": ident.get("caption_clean") or ident.get("caption_original") or "",
                    "nearby_text": (asset.get("text_linkage") or {}).get("nearest_text", "") if isinstance(asset.get("text_linkage"), dict) else "",
                    "split_method": split_method,
                    "split_confidence": split_confidence,
                }
            )
    return subfigures


def make_contact_sheet(subfigures: list[dict[str, Any]], output_path: Path, thumb_w: int = 260) -> None:
    if not subfigures:
        return
    thumbs: list[tuple[Image.Image, str]] = []
    for item in subfigures:
        p = Path(str(item.get("local_image_path") or ""))
        if not p.exists():
            continue
        img = Image.open(p).convert("RGB")
        ratio = thumb_w / max(1, img.width)
        thumb_h = max(80, int(img.height * ratio))
        img = img.resize((thumb_w, thumb_h))
        label = f"{item.get('subfigure_label')} | {item.get('paper_id')}"
        thumbs.append((img, label))
    if not thumbs:
        return
    cols = 2
    pad = 18
    label_h = 34
    rows = math.ceil(len(thumbs) / cols)
    cell_h = max(img.height for img, _ in thumbs) + label_h
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (cell_h + pad) + pad), "white")
    draw = ImageDraw.Draw(sheet)
    for idx, (img, label) in enumerate(thumbs):
        r, c = divmod(idx, cols)
        x = pad + c * (thumb_w + pad)
        y = pad + r * (cell_h + pad)
        sheet.paste(img, (x, y))
        draw.text((x, y + img.height + 4), clip(label, 60), fill=(0, 0, 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT / utc_stamp()
    output_dir.mkdir(parents=True, exist_ok=True)
    print_event("start", output_dir=str(output_dir))
    assets = read_jsonl(Path(args.visual_assets_jsonl))
    eligible = []
    for asset in assets:
        labels = labels_for_asset(asset)
        p = local_image_path(asset)
        if p and len(labels) >= int(args.min_subpanels):
            eligible.append(asset)
    if int(args.sample_assets) > 0:
        eligible = eligible[: int(args.sample_assets)]
    print_event("assets_loaded", total=len(assets), eligible=len(eligible))

    subfigures: list[dict[str, Any]] = []
    for asset in eligible:
        remaining = max(0, int(args.max_subfigures) - len(subfigures)) if int(args.max_subfigures) > 0 else 0
        if int(args.max_subfigures) > 0 and remaining <= 0:
            break
        extracted = extract_asset_subfigures(asset, output_dir, max_subfigures=remaining)
        subfigures.extend(extracted)
        print_event("asset_done", parent_asset_id=(asset_identity(asset).get("asset_id") or ""), subfigures=len(extracted))

    tag_records: list[dict[str, Any]] = []
    llm_usage: list[dict[str, Any]] = []
    if args.real_vl and int(args.tag_limit) > 0:
        tag_candidates = []
        for item in subfigures:
            if str(item.get("split_confidence") or "low") == "low" and not bool(args.tag_low_confidence):
                continue
            tag_candidates.append(item)
        for item in tag_candidates[: int(args.tag_limit)]:
            tag, usage = tag_subfigure_with_qwen_vl(
                subfigure=item,
                prompt_path=Path(args.prompt_path),
                model_tier=str(args.vision_model_tier),
            )
            raw_tag = str(usage.pop("_raw_content", "") or "")
            merged = {**item, "visual_tag": tag}
            tag_records.append(merged)
            llm_usage.append({"subfigure_id": item["subfigure_id"], "usage": usage})
            write_text(output_dir / "raw_vl_tags" / f"{safe_id(item['subfigure_id'])}.txt", raw_tag)
            print_event("tag_done", subfigure_id=item["subfigure_id"], role=tag.get("subfigure_role"), utility=(tag.get("review_use") or {}).get("utility"))

    write_jsonl(output_dir / "subfigures.jsonl", subfigures)
    write_jsonl(output_dir / "subfigure_visual_tags.jsonl", tag_records)
    write_json(output_dir / "llm_usage_log.json", llm_usage)
    contact = output_dir / "subfigure_contact_sheet.png"
    make_contact_sheet(subfigures, contact)
    audit = {
        "schema_version": "subfigure_extraction_audit.v1",
        "visual_assets_jsonl": str(args.visual_assets_jsonl),
        "total_assets": len(assets),
        "eligible_assets": len(eligible),
        "subfigure_count": len(subfigures),
        "tagged_subfigure_count": len(tag_records),
        "tag_low_confidence": bool(args.tag_low_confidence),
        "contains_cjk_in_prompt": contains_cjk(Path(args.prompt_path).read_text(encoding="utf-8")),
        "contact_sheet": str(contact) if contact.exists() else "",
    }
    write_json(output_dir / "quality_audit.json", audit)
    lines = [
        "# Subfigure extraction sample",
        "",
        f"- Eligible parent figures: {len(eligible)}",
        f"- Extracted subfigures: {len(subfigures)}",
        f"- Qwen-VL tagged subfigures: {len(tag_records)}",
        f"- Contact sheet: {contact if contact.exists() else '-'}",
        "",
        "## Notes",
        "- This is heuristic grid-based cutting with whitespace-adjusted gutters.",
        "- Parent figures are preserved; subfigures are additional visual chunks.",
    ]
    write_text(output_dir / "report.md", "\n".join(lines).strip() + "\n")
    print_event("done", subfigures=len(subfigures), tagged=len(tag_records), output_dir=str(output_dir))
    return 0 if subfigures else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract likely subfigures from visual assets and optionally tag them with Qwen-VL.")
    parser.add_argument("--visual-assets-jsonl", default=str(DEFAULT_VISUAL_ASSETS_JSONL))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--sample-assets", type=int, default=3)
    parser.add_argument("--min-subpanels", type=int, default=2)
    parser.add_argument("--max-subfigures", type=int, default=8)
    parser.add_argument("--real-vl", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--tag-limit", type=int, default=4)
    parser.add_argument("--tag-low-confidence", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--vision-model-tier", default="vision_fast_model")
    parser.add_argument("--prompt-path", default=str(DEFAULT_PROMPT))
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
