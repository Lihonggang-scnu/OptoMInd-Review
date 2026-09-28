"""Test-level visual asset extraction for local scientific PDFs.

The first version only extracted embedded raster images. That misses many real
paper figures because scientific PDFs often store figures as vector drawings,
groups of small images, or ordinary page drawing commands. This module now
keeps the embedded-image path but also renders figure/table regions from the
page by locating caption blocks and cropping the surrounding visual area.

This is still a test-level extractor: it prepares figure/table image regions and
captions for a later vision model, but it does not yet semantically understand
or score the figures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


CAPTION_MARKER_RE = re.compile(r"\b(Fig\.?|Figure|Table)\s*\.?\s*(\d+[A-Za-z]?)", flags=re.I)


def safe_stem(value: str) -> str:
    """Return a deliberately short stable stem for deeply nested Windows runs.

    Visual regression outputs can already have a long experiment/topic path.
    Keeping this component below 32 characters avoids otherwise valid images
    failing at the final write because the legacy Windows MAX_PATH limit is
    crossed.
    """
    raw = value or "paper"
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "paper"
    if len(cleaned) <= 24:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{cleaned[:13]}_{digest}"


def caption_candidates(page_text: str, limit: int = 8) -> list[str]:
    text = re.sub(r"\s+", " ", page_text or " ").strip()
    if not text:
        return []
    # Short candidate windows around figure/table markers.
    out: list[str] = []
    for marker in re.finditer(r"\b(?:Fig\.?|Figure|Table)\s*\d+[A-Za-z]?", text, flags=re.I):
        start = max(0, marker.start() - 120)
        end = min(len(text), marker.start() + 700)
        snippet = text[start:end].strip()
        if snippet and snippet not in out:
            out.append(snippet)
        if len(out) >= limit:
            break
    return out


def _normalize_rect_tuple(bbox: Any) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = [float(v) for v in bbox]
    except Exception:
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) <= 0 or (y1 - y0) <= 0:
        return None
    return x0, y0, x1, y1


def _rect_area(rect: Any) -> float:
    return max(0.0, float(rect.x1) - float(rect.x0)) * max(0.0, float(rect.y1) - float(rect.y0))


def _clip_rect(rect: Any, page_rect: Any, margin: float = 0.0) -> Any:
    import fitz

    clipped = fitz.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin)
    clipped.x0 = max(float(page_rect.x0), clipped.x0)
    clipped.y0 = max(float(page_rect.y0), clipped.y0)
    clipped.x1 = min(float(page_rect.x1), clipped.x1)
    clipped.y1 = min(float(page_rect.y1), clipped.y1)
    return clipped


def _caption_label(kind: str, number: str) -> str:
    if kind.lower().startswith("tab"):
        return f"Table {number}"
    return f"Fig. {number}"


def _is_caption_like(text: str, marker_start: int) -> bool:
    """Reject ordinary body references such as 'as shown in Fig. 1'."""

    prefix = text[:marker_start]
    # PDF text blocks often contain line numbers before captions. Those are OK.
    prefix_without_digits = re.sub(r"[\d\s:;,.()\[\]-]+", "", prefix)
    if prefix_without_digits:
        return False
    # A useful caption has explanatory words after the marker.
    marker = CAPTION_MARKER_RE.search(text, marker_start)
    if not marker:
        return False
    suffix = text[marker.end() :].strip(" .:-–—")
    first_word = re.match(r"[A-Za-z]+", suffix)
    if first_word and first_word.group(0).lower() in {
        "shows",
        "lists",
        "compares",
        "summarizes",
        "illustrates",
        "presents",
        "provides",
        "indicates",
        "demonstrates",
    }:
        return False
    return len(text[marker_start:].strip()) >= 18


def _caption_blocks(page: Any) -> list[dict[str, Any]]:
    blocks = page.get_text("blocks") or []
    captions: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for block in blocks:
        if len(block) < 5:
            continue
        bbox = _normalize_rect_tuple(block[:4])
        if not bbox:
            continue
        text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
        if not text:
            continue
        for match in CAPTION_MARKER_RE.finditer(text):
            if not _is_caption_like(text, match.start()):
                continue
            kind = match.group(1)
            number = match.group(2)
            label = _caption_label(kind, number)
            key = (label.lower(), int(round(bbox[1])), int(round(bbox[3])))
            if key in seen:
                continue
            seen.add(key)
            captions.append(
                {
                    "label": label,
                    "kind": "table" if kind.lower().startswith("tab") else "figure",
                    "caption_text": text[match.start() :].strip(),
                    "caption_bbox": bbox,
                }
            )
            break
    return captions


def _visual_rects(page: Any) -> list[Any]:
    import fitz

    rects: list[Any] = []

    # Raster images visible on the page.
    try:
        page_dict = page.get_text("dict") or {}
        for block in page_dict.get("blocks", []):
            if int(block.get("type", -1)) != 1:
                continue
            bbox = _normalize_rect_tuple(block.get("bbox"))
            if not bbox:
                continue
            rect = fitz.Rect(*bbox)
            if _rect_area(rect) >= 120:
                rects.append(rect)
    except Exception:
        pass

    # Vector drawings, lines, paths, and composed figure elements.
    try:
        for drawing in page.get_drawings() or []:
            rect = drawing.get("rect")
            if not rect:
                continue
            rect = fitz.Rect(rect)
            if _rect_area(rect) >= 30:
                rects.append(rect)
    except Exception:
        pass

    return rects


def _nearby_text(page: Any, clip: Any, *, max_chars: int = 2500) -> str:
    expanded = _clip_rect(clip, page.rect, margin=28)
    try:
        text = page.get_text("text", clip=expanded) or ""
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _region_for_caption(
    page: Any,
    caption: dict[str, Any],
    visual_rects: list[Any],
    *,
    min_visual_y: float | None = None,
) -> Any | None:
    """Return a crop rectangle for a caption's associated figure/table region.

    ``min_visual_y`` is the lower edge of the preceding caption on the same
    page.  It prevents a stacked figure from inheriting visual rectangles from
    the figure above it when the PDF exposes both figures as separate drawing
    groups.  The argument is optional so callers that only have one caption can
    retain the historical behavior.
    """

    import fitz

    page_rect = page.rect
    cap = fitz.Rect(*caption["caption_bbox"])
    kind = str(caption.get("kind") or "figure")
    page_width = float(page_rect.width)
    visual_y_floor = max(70.0, float(min_visual_y)) if min_visual_y is not None else 70.0

    if kind == "figure":
        candidates = [
            rect
            for rect in visual_rects
            if rect.y1 <= cap.y0 + 18
            and rect.y1 >= cap.y0 - 380
            and rect.y0 >= visual_y_floor
            and rect.x1 >= 40
            and rect.x0 <= page_width - 40
        ]
        if candidates:
            region = fitz.Rect(candidates[0])
            for rect in candidates[1:]:
                region |= rect
            region.y1 = max(region.y1, cap.y1)
            region.x0 = min(region.x0, cap.x0)
            region.x1 = max(region.x1, cap.x1)
            return _clip_rect(region, page_rect, margin=14)

        # Fallback: caption below figure, crop a conservative area above it.
        fallback = fitz.Rect(45, max(70, cap.y0 - 280), min(page_width - 45, 555), cap.y1 + 8)
        if fallback.height >= 70:
            return _clip_rect(fallback, page_rect)
        return None

    # Tables are often text objects rather than drawings. Capture caption and
    # nearby table body below it. If there are strong drawing candidates below,
    # union them; otherwise use a conservative text/table window.
    below = [
        rect
        for rect in visual_rects
        if rect.y0 >= cap.y0 - 15
        and rect.y0 <= cap.y1 + 300
        and rect.y0 >= 70
        and rect.x1 >= 40
        and rect.x0 <= page_width - 40
    ]
    if below:
        region = fitz.Rect(below[0])
        for rect in below[1:]:
            region |= rect
        region.y0 = min(region.y0, cap.y0)
        region.x0 = min(region.x0, cap.x0)
        region.x1 = max(region.x1, cap.x1)
        return _clip_rect(region, page_rect, margin=14)

    fallback = fitz.Rect(45, max(70, cap.y0 - 8), min(page_width - 45, 555), min(page_rect.height - 45, cap.y1 + 260))
    if fallback.height >= 70:
        return _clip_rect(fallback, page_rect)
    return None


def extract_pdf_visual_assets(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    max_pages: int = 20,
    skip_first_pages: int = 1,
    min_width: int = 160,
    min_height: int = 120,
    render_scale: float = 2.5,
) -> dict[str, Any]:
    import fitz

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    stem = safe_stem(pdf_path.stem)
    image_dir = output_dir / "images" / stem
    region_dir = output_dir / "regions" / stem
    image_dir.mkdir(parents=True, exist_ok=True)
    region_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    with fitz.open(str(pdf_path)) as doc:
        for page_index in range(min(len(doc), int(max_pages))):
            page_number = page_index + 1
            if page_number <= int(skip_first_pages):
                continue
            page = doc[page_index]
            page_text = page.get_text("text")
            captions = caption_candidates(page_text)

            visual_rects = _visual_rects(page)
            page_captions = _caption_blocks(page)
            for region_index, caption in enumerate(page_captions, start=1):
                # Captions for vertically stacked figures are separated by the
                # previous caption's text block.  Use that boundary only when
                # the captions are genuinely on different vertical rows; this
                # keeps side-by-side captions from suppressing each other.
                cap_y0 = float(caption["caption_bbox"][1])
                previous_caption_bottoms = [
                    float(previous["caption_bbox"][3])
                    for previous in page_captions[: region_index - 1]
                    if float(previous["caption_bbox"][3]) < cap_y0 - 8.0
                ]
                min_visual_y = max(previous_caption_bottoms, default=70.0)
                region = _region_for_caption(
                    page,
                    caption,
                    visual_rects,
                    min_visual_y=min_visual_y,
                )
                if region is None or region.width < 100 or region.height < 60:
                    continue
                label_safe = safe_stem(str(caption.get("label") or f"region{region_index}"))
                region_path = region_dir / f"page{page_number:03d}_{label_safe}_{region_index:02d}.png"
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), clip=region, alpha=False)
                    pix.save(str(region_path))
                except Exception:
                    continue
                records.append(
                    {
                        "source_pdf": str(pdf_path),
                        "asset_id": f"{stem}:p{page_number}:{label_safe}:{region_index}",
                        "asset_type": str(caption.get("kind") or "figure"),
                        "label": caption.get("label"),
                        "page": page_number,
                        "image_path": str(region_path),
                        "extraction_method": "rendered_region_from_caption",
                        "bbox_pdf": [round(float(region.x0), 2), round(float(region.y0), 2), round(float(region.x1), 2), round(float(region.y1), 2)],
                        "caption_text": caption.get("caption_text") or "",
                        "caption_bbox": [round(float(v), 2) for v in caption.get("caption_bbox", [])],
                        "nearby_text": _nearby_text(page, region),
                        "status": "extracted_needs_visual_tagging",
                    }
                )

            seen_xrefs: set[int] = set()
            for img_index, img in enumerate(page.get_images(full=True)):
                xref = int(img[0])
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                width = int(info.get("width") or 0)
                height = int(info.get("height") or 0)
                if width < min_width or height < min_height:
                    continue
                ext = str(info.get("ext") or "png").lower()
                image_bytes = info.get("image") or b""
                if not image_bytes:
                    continue
                image_path = image_dir / f"page{page_number:03d}_img{img_index + 1:02d}_xref{xref}.{ext}"
                try:
                    # Re-create the parent defensively: one failed image must
                    # not discard figure regions already extracted from the
                    # same paper.
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(image_bytes)
                except OSError:
                    continue
                records.append(
                    {
                        "source_pdf": str(pdf_path),
                        "asset_id": f"{stem}:p{page_number}:xref{xref}",
                        "asset_type": "embedded_image",
                        "page": page_number,
                        "image_index": img_index + 1,
                        "xref": xref,
                        "image_path": str(image_path),
                        "width": width,
                        "height": height,
                        "caption_candidates": captions,
                        "extraction_method": "embedded_raster_image",
                        "status": "extracted_needs_visual_tagging",
                    }
                )
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl = output_dir / f"{safe_stem(pdf_path.stem)}.visual_assets.jsonl"
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    summary = {
        "source_pdf": str(pdf_path),
        "output_dir": str(output_dir),
        "image_dir": str(image_dir),
        "region_dir": str(region_dir),
        "asset_count": len(records),
        "rendered_region_count": sum(1 for r in records if r.get("extraction_method") == "rendered_region_from_caption"),
        "embedded_image_count": sum(1 for r in records if r.get("extraction_method") == "embedded_raster_image"),
        "visual_assets_jsonl": str(jsonl),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (output_dir / f"{safe_stem(pdf_path.stem)}.visual_assets.summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _html_base_url(soup: Any, fallback: str = "") -> str:
    for selector in (
        ("link", {"rel": "canonical"}),
        ("meta", {"property": "og:url"}),
        ("meta", {"name": "citation_public_url"}),
    ):
        tag = soup.find(*selector)
        if not tag:
            continue
        value = tag.get("href") or tag.get("content")
        if value:
            return str(value)
    return fallback


def _first_src_from_srcset(srcset: str) -> str:
    parts = [p.strip() for p in (srcset or "").split(",") if p.strip()]
    if not parts:
        return ""
    # Prefer the last candidate; publisher srcsets usually progress from small
    # to large variants.
    return parts[-1].split()[0].strip()


def _image_url_from_tag(img: Any, base_url: str) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = img.get(attr)
        if value:
            return urllib.parse.urljoin(base_url or "https:", str(value))
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        return urllib.parse.urljoin(base_url or "https:", _first_src_from_srcset(str(srcset)))
    return ""


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _looks_like_article_image(img: Any, url: str) -> bool:
    text = " ".join(
        str(v or "")
        for v in (
            url,
            img.get("alt"),
            img.get("aria-label"),
            img.get("title"),
        )
    )
    if re.search(r"\b(Fig|Figure|Table)\s*\d+", text, flags=re.I):
        return True
    if re.search(r"[_-]fig(?:ure)?\d+|/figures?/\d+", text, flags=re.I):
        return True
    return False


def _download_url(url: str, dst: Path, *, timeout: float = 20.0) -> tuple[bool, str]:
    if not url or url.startswith("data:"):
        return False, "unsupported_data_or_empty_url"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("content-type", "")
            data = resp.read()
        if not data:
            return False, "empty_response"
        head = data[:80].lstrip().lower()
        if "text/html" in content_type.lower() or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            return False, "server_returned_html_not_image"
        is_svg = "svg" in content_type.lower() or head.startswith(b"<svg")
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = data.startswith(b"\xff\xd8\xff")
        is_gif = data.startswith((b"GIF87a", b"GIF89a"))
        is_webp = data[:12].startswith(b"RIFF") and data[8:12] == b"WEBP"
        if not (is_svg or is_png or is_jpeg or is_gif or is_webp or content_type.lower().startswith("image/")):
            return False, f"unexpected_content_type:{content_type or 'unknown'}"
        suffix = dst.suffix.lower()
        if not suffix:
            if is_svg:
                dst = dst.with_suffix(".svg")
            elif is_jpeg:
                dst = dst.with_suffix(".jpg")
            elif is_webp:
                dst = dst.with_suffix(".webp")
            elif is_gif:
                dst = dst.with_suffix(".gif")
            else:
                dst = dst.with_suffix(".png")
        dst.write_bytes(data)
        return True, str(dst)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _html_caption_for_figure(fig: Any) -> str:
    for selector in ("figcaption", "[class*=caption]", "[class*=Caption]"):
        tag = fig.select_one(selector)
        if tag:
            text = _clean_html_text(tag.get_text(" ", strip=True))
            if text:
                return text
    return _clean_html_text(fig.get_text(" ", strip=True))[:1200]


def _html_nearby_section_text(node: Any, max_chars: int = 2500) -> str:
    parent = node
    for _ in range(6):
        if not parent:
            break
        classes = " ".join(parent.get("class", []) if hasattr(parent, "get") else [])
        if parent.name in {"section", "article"} or "article-section" in classes.lower():
            return _clean_html_text(parent.get_text(" ", strip=True))[:max_chars]
        parent = parent.parent
    return _clean_html_text(node.get_text(" ", strip=True))[:max_chars]


def _table_to_markdown(table: Any, max_rows: int = 80, max_cols: int = 12) -> str:
    rows: list[list[str]] = []
    for tr in table.find_all("tr")[:max_rows]:
        cells = [_clean_html_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])[:max_cols]]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = rows[0]
    body = rows[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in body)
    return "\n".join(lines)


def extract_html_visual_assets(
    html_path: str | Path,
    output_dir: str | Path,
    *,
    base_url: str = "",
    max_assets: int = 80,
    download_images: bool = False,
) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    html_path = Path(html_path)
    output_dir = Path(output_dir)
    stem = safe_stem(html_path.stem)
    html = html_path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    base_url = _html_base_url(soup, base_url)
    image_dir = output_dir / "html_images" / stem
    table_dir = output_dir / "html_tables" / stem
    image_dir.mkdir(parents=True, exist_ok=True)
    table_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for idx, fig in enumerate(soup.find_all("figure"), start=1):
        if len(records) >= max_assets:
            break
        image_urls: list[str] = []
        local_paths: list[str] = []
        image_download_errors: list[dict[str, str]] = []
        for img_index, img in enumerate(fig.find_all("img"), start=1):
            url = _image_url_from_tag(img, base_url)
            if not url or url in seen_urls:
                continue
            if not _looks_like_article_image(img, url) and idx > 3:
                # Keep the first few ambiguous figures but filter logos,
                # recommendation cards, and UI images later in the page.
                continue
            seen_urls.add(url)
            image_urls.append(url)
            if download_images:
                suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
                ok, result = _download_url(url, image_dir / f"figure{idx:03d}_img{img_index:02d}{suffix}")
                if ok:
                    local_paths.append(result)
                else:
                    image_download_errors.append({"url": url, "error": result})
        caption = _html_caption_for_figure(fig)
        if not image_urls and "table" not in caption.lower():
            continue
        label_match = CAPTION_MARKER_RE.search(caption)
        label = _caption_label(label_match.group(1), label_match.group(2)) if label_match else f"Figure {idx}"
        records.append(
            {
                "source_html": str(html_path),
                "asset_id": f"{stem}:htmlfig:{idx}",
                "asset_type": "figure",
                "label": label,
                "image_urls": image_urls,
                "local_image_paths": local_paths,
                "image_download_errors": image_download_errors,
                "caption_text": caption,
                "nearby_text": _html_nearby_section_text(fig),
                "html_snippet": str(fig)[:5000],
                "extraction_method": "html_figure_dom",
                "status": "extracted_needs_visual_tagging",
            }
        )

    for idx, table in enumerate(soup.find_all("table"), start=1):
        if len(records) >= max_assets:
            break
        caption_tag = table.find("caption")
        caption = _clean_html_text(caption_tag.get_text(" ", strip=True)) if caption_tag else ""
        if not caption:
            previous = table.find_previous(["h2", "h3", "p", "figcaption"])
            caption = _clean_html_text(previous.get_text(" ", strip=True))[:500] if previous else f"Table {idx}"
        label_match = CAPTION_MARKER_RE.search(caption)
        label = _caption_label(label_match.group(1), label_match.group(2)) if label_match else f"Table {idx}"
        table_html_path = table_dir / f"table{idx:03d}.html"
        table_md_path = table_dir / f"table{idx:03d}.md"
        table_html_path.write_text(str(table), encoding="utf-8", errors="replace")
        table_md_path.write_text(_table_to_markdown(table), encoding="utf-8", errors="replace")
        records.append(
            {
                "source_html": str(html_path),
                "asset_id": f"{stem}:htmltable:{idx}",
                "asset_type": "table",
                "label": label,
                "table_html_path": str(table_html_path),
                "table_markdown_path": str(table_md_path),
                "caption_text": caption,
                "nearby_text": _html_nearby_section_text(table),
                "html_snippet": str(table)[:5000],
                "extraction_method": "html_table_dom",
                "status": "extracted_needs_table_or_visual_tagging",
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl = output_dir / f"{stem}.visual_assets.jsonl"
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    summary = {
        "source_html": str(html_path),
        "base_url": base_url,
        "output_dir": str(output_dir),
        "image_dir": str(image_dir),
        "table_dir": str(table_dir),
        "asset_count": len(records),
        "figure_count": sum(1 for r in records if r.get("asset_type") == "figure"),
        "table_count": sum(1 for r in records if r.get("asset_type") == "table"),
        "downloaded_image_count": sum(len(r.get("local_image_paths") or []) for r in records),
        "visual_assets_jsonl": str(jsonl),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (output_dir / f"{stem}.visual_assets.summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract test-level visual assets from a local scientific PDF or publisher HTML.")
    parser.add_argument("--pdf", default="")
    parser.add_argument("--html", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--skip-first-pages", type=int, default=1)
    parser.add_argument("--min-width", type=int, default=160)
    parser.add_argument("--min-height", type=int, default=120)
    parser.add_argument("--render-scale", type=float, default=2.5)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--download-html-images", action="store_true")
    args = parser.parse_args()
    if bool(args.pdf) == bool(args.html):
        raise SystemExit("Pass exactly one of --pdf or --html.")
    if args.pdf:
        summary = extract_pdf_visual_assets(
            args.pdf,
            args.output_dir,
            max_pages=args.max_pages,
            skip_first_pages=args.skip_first_pages,
            min_width=args.min_width,
            min_height=args.min_height,
            render_scale=args.render_scale,
        )
    else:
        summary = extract_html_visual_assets(
            args.html,
            args.output_dir,
            base_url=args.base_url,
            max_assets=args.max_assets,
            download_images=bool(args.download_html_images),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
