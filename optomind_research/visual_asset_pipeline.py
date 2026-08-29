"""Visual asset extraction pipeline for v3.1-v3.5.

The pipeline reads a fulltext index, resolves local fulltext files from the
literature database, extracts figures/tables using local tools, normalizes them
to the visual_asset.v1.1 protocol, links body callouts back to existing chunks,
and optionally creates a small number of Qwen-VL visual cards.

This module is intentionally file-output first. It does not mutate the
literature database until the user approves the protocol and results.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from optomind_research.literature_resource_builder import DEFAULT_LIBRARY_DB, normalize_doi, normalize_space, safe_filename
from optomind_research.visual_asset_extractor import extract_html_visual_assets, extract_pdf_visual_assets


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "Figure Table Card Tagger.txt"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "visual_asset_pipeline"
DEFAULT_CORE58_INDEX = (
    PROJECT_ROOT
    / "outputs"
    / "literature_resource_builder"
    / "web_jobs"
    / "20260701-152234-51de78"
    / "artifacts"
    / "core58_fulltext_index.json"
)


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_asset_stem(value: str) -> str:
    return safe_filename(value or "paper")[:90] or "paper"


def short_paper_dir_name(item: dict[str, Any]) -> str:
    """Return a Windows-safe, stable, short output directory name."""
    raw = str(item.get("paper_id") or item.get("doi") or item.get("title") or "paper")
    prefix = str(item.get("index") or "x").zfill(3)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    doi = normalize_doi(item.get("doi") or "")
    if doi:
        label = re.sub(r"[^A-Za-z0-9]+", "-", doi).strip("-")[:36]
    else:
        label = safe_asset_stem(item.get("title") or "paper")[:36]
    return f"{prefix}-{label}-{digest}"


def load_core_items(index_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("core_fulltexts"), list):
        return data, list(data["core_fulltexts"])
    if isinstance(data, list):
        return {"source": str(index_path), "core_fulltext_count": len(data)}, data
    raise ValueError(f"Unsupported core index shape: {index_path}")


def db_records_by_id(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM fulltext_records").fetchall()
    try:
        abstract_rows = con.execute("SELECT * FROM abstract_papers").fetchall()
    except Exception:
        abstract_rows = []
    con.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        if d.get("paper_id"):
            out[str(d["paper_id"])] = d
        doi = normalize_doi(d.get("doi") or "")
        if doi:
            out[f"doi:{doi}"] = d
            out[doi] = d
    for row in abstract_rows:
        d = dict(row)
        doi = normalize_doi(d.get("doi") or "")
        keys = []
        if d.get("paper_id"):
            keys.append(str(d["paper_id"]))
        if doi:
            keys.extend([doi, f"doi:{doi}"])
        for key in keys:
            current = out.setdefault(key, {})
            for src_key, dst_key in [
                ("open_access", "open_access"),
                ("pdf_url", "pdf_url"),
                ("landing_page_url", "landing_page_url"),
                ("abstract", "abstract"),
                ("source_apis_json", "source_apis_json"),
            ]:
                value = d.get(src_key)
                if value not in (None, "", [], {}) and current.get(dst_key) in (None, "", [], {}):
                    current[dst_key] = value
    return out


def merge_db_record(item: dict[str, Any], records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    merged = dict(item)
    keys = [str(item.get("paper_id") or ""), normalize_doi(item.get("doi") or "")]
    if keys[1]:
        keys.append(f"doi:{keys[1]}")
    for key in keys:
        rec = records.get(key)
        if rec:
            for k, v in rec.items():
                if merged.get(k) in (None, "", [], {}):
                    merged[k] = v
            break
    return merged


def load_chunks(chunk_path: str | Path) -> list[dict[str, Any]]:
    p = Path(chunk_path) if chunk_path else Path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except Exception:
            continue
    return rows


def read_text(path: str | Path) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_label(label: str, caption: str = "") -> str:
    label = normalize_space(str(label or ""))
    if label:
        m = re.search(r"\b(Fig\.?|Figure|Table)\s*\.?\s*(\d+[A-Za-z]?)", label, flags=re.I)
        if m:
            kind = "Table" if m.group(1).lower().startswith("tab") else "Figure"
            return f"{kind} {m.group(2)}"
    m = re.search(r"\b(Fig\.?|Figure|Table)\s*\.?\s*(\d+[A-Za-z]?)", caption or "", flags=re.I)
    if m:
        kind = "Table" if m.group(1).lower().startswith("tab") else "Figure"
        return f"{kind} {m.group(2)}"
    return label


def subpanel_labels(text: str) -> list[str]:
    labels = []
    for m in re.finditer(r"(?:^|\s|\()([a-h])\)", text or "", flags=re.I):
        val = m.group(1).lower()
        if val not in labels:
            labels.append(val)
    return labels[:12]


def infer_domain_hints(caption: str, context: str) -> dict[str, Any]:
    text = f"{caption} {context}".lower()
    quantities = []
    for key, patterns in {
        "reflectance": ["reflectance", "reflection"],
        "emittance": ["emittance", "emissivity", "emission"],
        "transmittance": ["transmittance", "transmission"],
        "absorptance": ["absorptance", "absorption"],
        "cooling_power": ["cooling power", "cooling performance"],
        "temperature_drop": ["temperature drop", "sub-ambient", "below ambient", "delta t", "Δt"],
        "thermal_conductivity": ["thermal conductivity"],
        "psnr_ssim": ["psnr", "ssim"],
    }.items():
        if any(p in text for p in patterns):
            quantities.append(key)
    ranges = []
    if any(p in text for p in ["visible", "vis", "color"]):
        ranges.append("visible")
    if any(p in text for p in ["solar", "sunlight", "250", "2.5"]):
        ranges.append("solar")
    if any(p in text for p in ["nir", "near-infrared", "near infrared"]):
        ranges.append("NIR")
    if any(p in text for p in ["mid-infrared", "mid infrared", "mir", "8-13", "8–13", "atmospheric window"]):
        ranges.append("MIR_8_13um")
    role = "other"
    if any(p in text for p in ["spectrum", "spectra", "wavelength", "reflectance", "emittance", "transmittance"]):
        role = "spectrum"
    if any(p in text for p in ["sem", "tem", "microscopy", "micrograph", "morphology"]):
        role = "material_structure"
    if any(p in text for p in ["schematic", "diagram", "device", "structure", "route"]):
        role = "device_schematic"
    if any(p in text for p in ["thermal image", "infrared image", "temperature map"]):
        role = "thermal_image"
    if any(p in text for p in ["comparison", "benchmark", "versus", "vs."]):
        role = "benchmark_curve"
    sim = "unclear"
    if any(p in text for p in ["simulation", "calculated", "theoretical", "modeling"]):
        sim = "simulation"
    if any(p in text for p in ["experiment", "measured", "fabricated", "photograph", "field test"]):
        sim = "both" if sim == "simulation" else "experiment"
    return {
        "optical_asset_role": role,
        "physical_quantities": quantities,
        "wavelength_ranges": ranges,
        "materials_or_stack": [],
        "angle_or_polarization": "polarization" if "polarization" in text or "polarized" in text else "",
        "experiment_or_simulation": sim,
    }


def find_chunk_by_context(context: str, chunks: list[dict[str, Any]]) -> str | None:
    probe = compact_text(context)
    probes = []
    if len(probe) > 300:
        probes.extend([probe[:300], probe[120:420], probe[-300:]])
    elif len(probe) > 90:
        probes.append(probe)
    for snippet in probes:
        if len(snippet) < 80:
            continue
        for chunk in chunks:
            text = compact_text(chunk.get("text") or "")
            if snippet in text:
                return str(chunk.get("chunk_id") or "")
    return None


def context_window(full_text: str, pos: int, radius: int = 520) -> str:
    start = max(0, pos - radius)
    end = min(len(full_text), pos + radius)
    ps = full_text.rfind("\n\n", 0, pos)
    pe = full_text.find("\n\n", pos)
    if ps >= 0 and pos - ps < radius:
        start = ps + 2
    if pe >= 0 and pe - pos < radius:
        end = pe
    return compact_text(full_text[start:end])


def link_callouts(label: str, caption: str, full_text: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    label = normalize_label(label, caption)
    m = re.search(r"\b(?:Fig\.?|Figure|Table)\s*\.?\s*(\d+[A-Za-z]?)", label, flags=re.I)
    if not m or not full_text:
        return {"body_callouts": [], "linked_chunk_ids": [], "same_page_chunk_ids": []}
    num = re.escape(m.group(1))
    is_table = label.lower().startswith("table")
    if is_table:
        pattern = re.compile(rf"\bTable\s*\.?\s*{num}\b", flags=re.I)
    else:
        pattern = re.compile(rf"\b(?:Fig\.?|Figure)\s*\.?\s*{num}\b", flags=re.I)
    callouts = []
    linked: list[str] = []
    for mt in pattern.finditer(full_text):
        ctx = context_window(full_text, mt.start())
        line_start = full_text.rfind("\n", 0, mt.start()) + 1
        line_end = full_text.find("\n", mt.start())
        if line_end < 0:
            line_end = len(full_text)
        line = full_text[line_start:line_end]
        is_caption = bool(re.match(r"\s*#+\s*(?:Figure|Fig\.?|Table)\s*\.?\s*" + num + r"\b", line, flags=re.I))
        role = "caption_heading_or_caption" if is_caption else "body_callout"
        chunk_id = find_chunk_by_context(ctx, chunks)
        if chunk_id and chunk_id not in linked:
            linked.append(chunk_id)
        callouts.append(
            {
                "callout_text": mt.group(0),
                "chunk_id": chunk_id or "",
                "paragraph_text": ctx[:1600],
                "char_start": mt.start(),
                "char_end": mt.end(),
                "role": role,
            }
        )
    return {"body_callouts": callouts, "linked_chunk_ids": linked, "same_page_chunk_ids": []}


def asset_id_for(item: dict[str, Any], label: str, index: int) -> str:
    base = str(item.get("paper_id") or item.get("doi") or item.get("title") or "paper")
    return safe_filename(f"{base}-{label or 'asset'}-{index}")[:160]


def build_asset(
    item: dict[str, Any],
    *,
    index: int,
    asset_type: str,
    label: str,
    caption: str,
    source_format: str,
    parser: str,
    source_file: str,
    source_url: str = "",
    page: int | None = None,
    bbox: list[Any] | None = None,
    image_path: str = "",
    table_html_path: str = "",
    table_markdown_path: str = "",
    remote_image_url: str = "",
    nearby_text: str = "",
    full_text: str = "",
    chunks: list[dict[str, Any]] | None = None,
    docling_ref: str = "",
    extraction_run_id: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    caption_clean = compact_text(caption)
    label_norm = normalize_label(label, caption_clean)
    image_width = None
    image_height = None
    mime_type = ""
    checksum = ""
    if image_path and Path(image_path).exists():
        try:
            from PIL import Image

            with Image.open(image_path) as img:
                image_width, image_height = img.size
                mime_type = Image.MIME.get(img.format or "", "") or mimetypes.guess_type(image_path)[0] or ""
        except Exception:
            mime_type = mimetypes.guess_type(image_path)[0] or ""
        try:
            checksum = sha256_file(Path(image_path))
        except Exception:
            checksum = ""
    link = link_callouts(label_norm, caption_clean, full_text, chunks or [])
    context = nearby_text or " ".join([c.get("paragraph_text", "") for c in link.get("body_callouts", [])[:2]])
    confidence = "high" if caption_clean and (image_path or table_markdown_path or table_html_path) else "medium"
    if warnings:
        confidence = "medium" if confidence == "high" else "low"
    return {
        "schema_version": "visual_asset.v1.1",
        "paper": {
            "paper_id": item.get("paper_id", ""),
            "doi": normalize_doi(item.get("doi") or ""),
            "title": item.get("title", ""),
            "year": item.get("year"),
            "venue": item.get("venue", ""),
        },
        "asset_identity": {
            "asset_id": asset_id_for(item, label_norm, index),
            "asset_type": asset_type,
            "label": label_norm,
            "subpanel_labels": subpanel_labels(caption_clean),
            "caption_original": caption,
            "caption_clean": caption_clean,
            "caption_confidence": "high" if caption_clean else "low",
        },
        "source_provenance": {
            "source_format": source_format,
            "source_file": source_file,
            "source_url": source_url or item.get("source_url", ""),
            "parser": parser,
            "parser_version": "",
            "extraction_run_id": extraction_run_id,
            "page": page,
            "bbox": bbox or [],
            "docling_ref": docling_ref,
            "html_dom_path": "",
            "checksum": checksum,
        },
        "local_resources": {
            "local_image_path": image_path,
            "local_table_html_path": table_html_path,
            "local_table_markdown_path": table_markdown_path,
            "local_table_csv_path": "",
            "remote_image_url": remote_image_url,
            "mime_type": mime_type,
            "width": image_width,
            "height": image_height,
        },
        "document_context": {
            "section_path": [],
            "section_role": infer_section_role(caption_clean + " " + context),
            "reading_order_index": index,
            "nearby_text": compact_text(nearby_text)[:2400],
            "caption_neighbor_text": "",
        },
        "text_linkage": link,
        "domain_hints": infer_domain_hints(caption_clean, context),
        "quality": {
            "extraction_confidence": confidence,
            "is_duplicate": False,
            "failure_reason": "",
            "warnings": warnings or [],
        },
    }


def infer_section_role(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ["method", "fabrication", "preparation", "synthesis", "setup"]):
        return "method"
    if any(x in t for x in ["result", "performance", "comparison", "measured", "temperature", "cooling"]):
        return "result"
    if any(x in t for x in ["discussion", "mechanism"]):
        return "discussion"
    if any(x in t for x in ["supplementary", "supporting information"]):
        return "supplementary"
    if any(x in t for x in ["introduction", "background"]):
        return "background"
    return "unclear"


def convert_html_outputs(item: dict[str, Any], raw_jsonl: Path, full_text: str, chunks: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    assets = []
    for idx, line in enumerate(raw_jsonl.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        local_paths = row.get("local_image_paths") or []
        image_path = str(local_paths[0]) if local_paths else ""
        remote_urls = row.get("image_urls") or []
        assets.append(
            build_asset(
                item,
                index=idx,
                asset_type=row.get("asset_type") or "figure",
                label=row.get("label") or "",
                caption=row.get("caption_text") or "",
                source_format="publisher_html",
                parser="html_dom",
                source_file=row.get("source_html") or item.get("local_file_path") or "",
                source_url=item.get("source_url") or "",
                image_path=image_path,
                table_html_path=row.get("table_html_path") or "",
                table_markdown_path=row.get("table_markdown_path") or "",
                remote_image_url=str(remote_urls[0]) if remote_urls else "",
                nearby_text=row.get("nearby_text") or "",
                full_text=full_text,
                chunks=chunks,
                extraction_run_id=run_id,
                warnings=[e.get("error", "") for e in row.get("image_download_errors", []) if e.get("error")],
            )
        )
    return assets


def resolve_ref(data: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref or not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    cur: Any = data
    for part in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur if isinstance(cur, dict) else None


def caption_from_docling(data: dict[str, Any], node: dict[str, Any]) -> str:
    texts = []
    for ref in node.get("captions") or []:
        if isinstance(ref, dict):
            row = resolve_ref(data, str(ref.get("$ref") or ""))
            if row:
                texts.append(str(row.get("text") or row.get("orig") or ""))
    if texts:
        return compact_text(" ".join(texts))
    for child in node.get("children") or []:
        if isinstance(child, dict):
            row = resolve_ref(data, str(child.get("$ref") or ""))
            if row and str(row.get("label") or "").lower() == "caption":
                texts.append(str(row.get("text") or row.get("orig") or ""))
    return compact_text(" ".join(texts))


def valid_scientific_caption(caption: str, asset_kind: str = "figure") -> bool:
    caption = compact_text(caption)
    if not caption:
        return False
    if asset_kind == "table":
        return bool(re.search(r"\bTable\s*\.?\s*\d+", caption, flags=re.I))
    return bool(re.search(r"\b(Fig\.?|Figure)\s*\.?\s*\d+", caption, flags=re.I))


def bbox_size_from_prov(prov: dict[str, Any]) -> tuple[float, float]:
    bbox = prov.get("bbox") if isinstance(prov, dict) else None
    if not isinstance(bbox, dict):
        return 0.0, 0.0
    try:
        return abs(float(bbox.get("r", 0)) - float(bbox.get("l", 0))), abs(float(bbox.get("t", 0)) - float(bbox.get("b", 0)))
    except Exception:
        return 0.0, 0.0


def convert_docling_outputs(
    item: dict[str, Any],
    docling_json: Path,
    full_text: str,
    chunks: list[dict[str, Any]],
    run_id: str,
) -> list[dict[str, Any]]:
    data = json.loads(docling_json.read_text(encoding="utf-8", errors="replace"))
    assets = []
    idx = 0
    for pic in data.get("pictures") or []:
        image = pic.get("image") or {}
        prov = (pic.get("prov") or [{}])[0]
        caption = caption_from_docling(data, pic)
        if not valid_scientific_caption(caption, "figure"):
            # Docling also exports publisher logos, cover art, icons, and
            # decorative page images. They are useful for layout rendering but
            # must not enter the scientific visual evidence library.
            continue
        box_w, box_h = bbox_size_from_prov(prov)
        if box_w < 120 or box_h < 80:
            # Common Docling failure on publisher PDFs: a small page logo is
            # associated with a nearby figure caption. Let the PyMuPDF fallback
            # recover the actual caption region instead of storing the logo.
            continue
        idx += 1
        label = normalize_label("", caption) or f"Figure {idx}"
        bbox = []
        if isinstance(prov.get("bbox"), dict):
            b = prov["bbox"]
            bbox = [b.get("l"), b.get("t"), b.get("r"), b.get("b")]
        assets.append(
            build_asset(
                item,
                index=idx,
                asset_type="figure",
                label=label,
                caption=caption,
                source_format="pdf_docling",
                parser="docling",
                source_file=item.get("local_file_path") or "",
                source_url=item.get("source_url") or "",
                page=prov.get("page_no"),
                bbox=bbox,
                image_path=str(image.get("uri") or ""),
                nearby_text=caption,
                full_text=full_text,
                chunks=chunks,
                docling_ref=pic.get("self_ref") or "",
                extraction_run_id=run_id,
                warnings=[] if caption else ["docling_picture_without_caption"],
            )
        )
    for table in data.get("tables") or []:
        prov = (table.get("prov") or [{}])[0]
        caption = caption_from_docling(data, table)
        if caption and not valid_scientific_caption(caption, "table"):
            # Keep captionless tables if Docling extracted a table object, but
            # reject non-table captions.
            continue
        idx += 1
        label = normalize_label("", caption) or f"Table {idx}"
        bbox = []
        if isinstance(prov.get("bbox"), dict):
            b = prov["bbox"]
            bbox = [b.get("l"), b.get("t"), b.get("r"), b.get("b")]
        assets.append(
            build_asset(
                item,
                index=idx,
                asset_type="table",
                label=label,
                caption=caption,
                source_format="pdf_docling",
                parser="docling",
                source_file=item.get("local_file_path") or "",
                source_url=item.get("source_url") or "",
                page=prov.get("page_no"),
                bbox=bbox,
                nearby_text=caption,
                full_text=full_text,
                chunks=chunks,
                docling_ref=table.get("self_ref") or "",
                extraction_run_id=run_id,
                warnings=[] if caption else ["docling_table_without_caption"],
            )
        )
    return assets


def run_docling_pdf(pdf_path: Path, out_dir: Path, timeout_seconds: int = 240) -> dict[str, Any]:
    tmp = out_dir / "_docling_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    short_pdf = tmp / "sample.pdf"
    shutil.copy2(pdf_path, short_pdf)
    docling_out = out_dir / "docling"
    if docling_out.exists():
        shutil.rmtree(docling_out)
    docling_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docling",
        "convert",
        str(short_pdf),
        "--to",
        "json",
        "--to",
        "md",
        "--image-export-mode",
        "referenced",
        "--output",
        str(docling_out),
        "--device",
        "cpu",
        "--no-ocr",
        "--pdf-backend",
        "pypdfium2",
        "--page-batch-size",
        "1",
    ]
    start = time.time()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": f"docling_timeout:{timeout_seconds}", "stdout": exc.stdout or "", "stderr": exc.stderr or "", "elapsed": time.time() - start}
    jsons = list(docling_out.glob("*.json"))
    return {
        "ok": completed.returncode == 0 and bool(jsons),
        "returncode": completed.returncode,
        "json_path": str(jsons[0]) if jsons else "",
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
        "elapsed": time.time() - start,
    }


def convert_pymupdf_outputs(item: dict[str, Any], raw_jsonl: Path, full_text: str, chunks: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    assets = []
    for idx, line in enumerate(raw_jsonl.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("extraction_method") != "rendered_region_from_caption":
            continue
        assets.append(
            build_asset(
                item,
                index=idx,
                asset_type=row.get("asset_type") or "figure",
                label=row.get("label") or "",
                caption=row.get("caption_text") or "",
                source_format="pdf_pymupdf",
                parser="pymupdf_caption_crop",
                source_file=row.get("source_pdf") or item.get("local_file_path") or "",
                source_url=item.get("source_url") or "",
                page=row.get("page"),
                bbox=row.get("bbox_pdf") or [],
                image_path=row.get("image_path") or "",
                nearby_text=row.get("nearby_text") or "",
                full_text=full_text,
                chunks=chunks,
                extraction_run_id=run_id,
                warnings=["fallback_parser"],
            )
        )
    return assets


def recall_urls(urls: list[tuple[str, str]], out_dir: Path, stem_raw: str, timeout_seconds: int = 40) -> tuple[Path | None, str]:
    recall_dir = out_dir / "recalled_source"
    recall_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{hashlib.sha1(stem_raw.encode('utf-8', errors='ignore')).hexdigest()[:12]}"
    failures: list[str] = []
    for key, url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/pdf,text/html,*/*"},
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                content_type = resp.headers.get("content-type", "").lower()
                data = resp.read(80 * 1024 * 1024)
                final_url = resp.geturl()
        except Exception as exc:
            failures.append(f"{key}:{type(exc).__name__}")
            continue
        head = data[:200].lstrip().lower()
        if data.startswith(b"%PDF") or "application/pdf" in content_type or final_url.lower().endswith(".pdf"):
            path = recall_dir / f"{stem}.pdf"
            path.write_bytes(data)
            return path, f"recalled_pdf:{key}"
        if head.startswith(b"<!doctype html") or head.startswith(b"<html") or "text/html" in content_type:
            path = recall_dir / f"{stem}.html"
            path.write_bytes(data)
            return path, f"recalled_html:{key}"
        failures.append(f"{key}:unsupported:{content_type or 'unknown'}")
    return None, "all_recall_failed:" + ",".join(failures[:8])


def direct_recall_source(item: dict[str, Any], out_dir: Path, timeout_seconds: int = 40) -> tuple[Path | None, str]:
    urls: list[tuple[str, str]] = []
    for key in ("pdf_url", "abstract_pdf_url", "source_url", "landing_page_url"):
        url = str(item.get(key) or "").strip()
        if url and "***" not in url and all(url != seen_url for _, seen_url in urls):
            urls.append((key, url))
    if not urls:
        return None, "no_direct_source_url"
    stem_raw = str(item.get("paper_id") or item.get("doi") or item.get("title") or "source")
    return recall_urls(urls, out_dir, stem_raw, timeout_seconds=timeout_seconds)


def direct_pdf_link_recall_from_html(item: dict[str, Any], html_path: Path, out_dir: Path, timeout_seconds: int = 40) -> tuple[Path | None, str]:
    if not html_path.exists():
        return None, "html_missing"
    html = html_path.read_text(encoding="utf-8", errors="replace")
    hrefs = re.findall(r"""href\s*=\s*["']([^"']+)["']""", html, flags=re.I)
    base = str(item.get("source_url") or item.get("landing_page_url") or item.get("pdf_url") or "")
    ranked: list[tuple[int, str, str]] = []
    for href in hrefs:
        low = href.lower()
        if ".pdf" in low:
            rank = 0
        elif "/pdf" in low or "/epdf" in low:
            rank = 1
        elif "download" in low:
            rank = 2
        else:
            continue
        url = urllib.parse.urljoin(base, href)
        if url and "***" not in url and all(url != seen_url for _, _, seen_url in ranked):
            ranked.append((rank, "html_pdf_link", url))
    ranked.sort(key=lambda x: x[0])
    urls = [(key, url) for _, key, url in ranked[:8]]
    if not urls:
        return None, "no_pdf_like_link_in_html"
    stem_raw = str(item.get("paper_id") or item.get("doi") or item.get("title") or "source") + ":html_pdf_link"
    return recall_urls(urls, out_dir, stem_raw, timeout_seconds=timeout_seconds)


def process_one(
    item: dict[str, Any],
    *,
    out_dir: Path,
    run_id: str,
    allow_recall: bool,
    docling_timeout: int,
    max_assets: int,
) -> dict[str, Any]:
    paper_dir = out_dir / "papers" / short_paper_dir_name(item)
    paper_dir.mkdir(parents=True, exist_ok=True)
    full_text = read_text(item.get("parsed_text_path") or "")
    chunks = load_chunks(item.get("chunk_index_path") or "")
    fulltext_type = str(item.get("fulltext_type") or "").lower()
    local_file = Path(str(item.get("local_file_path") or "")) if item.get("local_file_path") else None
    assets: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    def try_html(path: Path, parser_note: str = "html_dom") -> None:
        html_out = paper_dir / "html_dom"
        short_input = paper_dir / "_input.html"
        source_path = path
        try:
            if path.resolve() != short_input.resolve():
                shutil.copy2(path, short_input)
                source_path = short_input
        except Exception:
            source_path = path
        summary = extract_html_visual_assets(source_path, html_out, max_assets=max_assets, download_images=True)
        raw = Path(summary["visual_assets_jsonl"])
        attempts.append({"tool": parser_note, "ok": True, "summary": summary})
        assets.extend(convert_html_outputs(item, raw, full_text, chunks, run_id))

    def try_pdf(path: Path, parser_note: str = "docling") -> bool:
        result = run_docling_pdf(path, paper_dir / "pdf_docling", timeout_seconds=docling_timeout)
        attempts.append({"tool": parser_note, **result})
        if result.get("ok") and result.get("json_path"):
            doc_assets = convert_docling_outputs(item, Path(result["json_path"]), full_text, chunks, run_id)
            assets.extend(doc_assets)
            return bool(doc_assets)
        return False

    def try_recalled_path(recalled: Path, note_prefix: str, allow_pdf_link_from_html: bool = True) -> None:
        if recalled.suffix.lower() in {".html", ".htm"}:
            try:
                try_html(recalled, parser_note=f"html_dom_{note_prefix}")
            except Exception as exc:
                attempts.append({"tool": f"html_dom_{note_prefix}", "ok": False, "error": f"{type(exc).__name__}:{exc}"})
            if not assets and allow_pdf_link_from_html:
                pdf_recalled, pdf_reason = direct_pdf_link_recall_from_html(item, recalled, paper_dir)
                attempts.append({"tool": f"html_pdf_link_recall_{note_prefix}", "ok": bool(pdf_recalled), "reason": pdf_reason, "path": str(pdf_recalled) if pdf_recalled else ""})
                if pdf_recalled:
                    try_recalled_path(pdf_recalled, f"{note_prefix}_pdf_link", allow_pdf_link_from_html=False)
        elif recalled.suffix.lower() == ".pdf":
            ok = try_pdf(recalled, parser_note=f"docling_{note_prefix}")
            if not ok:
                try:
                    summary = extract_pdf_visual_assets(recalled, paper_dir / f"pymupdf_{note_prefix}", max_pages=40, skip_first_pages=1, render_scale=2.2)
                    attempts.append({"tool": f"pymupdf_{note_prefix}", "ok": True, "summary": summary})
                    assets.extend(convert_pymupdf_outputs(item, Path(summary["visual_assets_jsonl"]), full_text, chunks, run_id))
                except Exception as exc:
                    attempts.append({"tool": f"pymupdf_{note_prefix}", "ok": False, "error": f"{type(exc).__name__}:{exc}"})

    if fulltext_type == "publisher_html" and local_file and local_file.exists():
        try:
            try_html(local_file)
        except Exception as exc:
            attempts.append({"tool": "html_dom", "ok": False, "error": f"{type(exc).__name__}:{exc}"})
        if not assets and allow_recall:
            recalled, reason = direct_recall_source(item, paper_dir)
            attempts.append({"tool": "direct_source_recall_after_html", "ok": bool(recalled), "reason": reason, "path": str(recalled) if recalled else ""})
            if recalled:
                try_recalled_path(recalled, "recalled_after_html")
        if not assets:
            pdf_recalled, pdf_reason = direct_pdf_link_recall_from_html(item, local_file, paper_dir)
            attempts.append({"tool": "html_pdf_link_recall_local", "ok": bool(pdf_recalled), "reason": pdf_reason, "path": str(pdf_recalled) if pdf_recalled else ""})
            if pdf_recalled:
                try_recalled_path(pdf_recalled, "local_pdf_link")
    elif fulltext_type.startswith("pdf") and local_file and local_file.exists():
        ok = try_pdf(local_file)
        if not ok:
            try:
                fb_out = paper_dir / "pymupdf_fallback"
                summary = extract_pdf_visual_assets(local_file, fb_out, max_pages=40, skip_first_pages=1, render_scale=2.2)
                attempts.append({"tool": "pymupdf_fallback", "ok": True, "summary": summary})
                assets.extend(convert_pymupdf_outputs(item, Path(summary["visual_assets_jsonl"]), full_text, chunks, run_id))
            except Exception as exc:
                attempts.append({"tool": "pymupdf_fallback", "ok": False, "error": f"{type(exc).__name__}:{exc}"})
    elif fulltext_type == "html_markdown":
        attempts.append({"tool": "html_markdown", "ok": False, "error": "html_markdown_has_no_embedded_images"})
        if allow_recall:
            recalled, reason = direct_recall_source(item, paper_dir)
            attempts.append({"tool": "direct_source_recall", "ok": bool(recalled), "reason": reason, "path": str(recalled) if recalled else ""})
            if recalled:
                try_recalled_path(recalled, "recalled")
    else:
        attempts.append({"tool": "format_router", "ok": False, "error": f"unsupported_or_missing_local_file:{fulltext_type}"})

    # Deduplicate by local image path or label.
    seen = set()
    deduped = []
    for asset in assets:
        key = (
            asset.get("local_resources", {}).get("local_image_path")
            or asset.get("local_resources", {}).get("local_table_markdown_path")
            or asset.get("asset_identity", {}).get("label")
            or asset.get("asset_identity", {}).get("asset_id")
        )
        if key in seen:
            asset["quality"]["is_duplicate"] = True
            continue
        seen.add(key)
        deduped.append(asset)
    assets = deduped
    out_jsonl = paper_dir / "visual_assets.v1_1.jsonl"
    out_jsonl.write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in assets), encoding="utf-8")
    summary = {
        "paper_id": item.get("paper_id"),
        "doi": item.get("doi"),
        "title": item.get("title"),
        "fulltext_type": item.get("fulltext_type"),
        "local_file_path": str(local_file) if local_file else "",
        "parsed_text_path": item.get("parsed_text_path", ""),
        "chunk_index_path": item.get("chunk_index_path", ""),
        "asset_count": len(assets),
        "asset_jsonl": str(out_jsonl),
        "attempts": attempts,
    }
    (paper_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary | {"assets": assets}


def make_contact_sheet(all_assets: list[dict[str, Any]], out_path: Path, limit: int = 80) -> None:
    from PIL import Image, ImageDraw

    items = []
    for asset in all_assets:
        p = asset.get("local_resources", {}).get("local_image_path") or ""
        if p and Path(p).exists():
            items.append((Path(p), asset.get("asset_identity", {}).get("label") or "", asset.get("paper", {}).get("title") or ""))
        if len(items) >= limit:
            break
    if not items:
        return
    thumb_w, thumb_h = 260, 190
    pad = 14
    label_h = 46
    cols = 4
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, rows * (thumb_h + label_h + pad) + pad), "white")
    draw = ImageDraw.Draw(sheet)
    for idx, (path, label, title) in enumerate(items):
        x = pad + (idx % cols) * (thumb_w + pad)
        y = pad + (idx // cols) * (thumb_h + label_h + pad)
        draw.text((x, y), f"{label} | {title[:34]}", fill="black")
        try:
            img = Image.open(path).convert("RGB")
            img.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(img, (x, y + label_h))
        except Exception as exc:
            draw.text((x, y + label_h), f"cannot open: {type(exc).__name__}", fill="red")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)


def qwen_vl_card(asset: dict[str, Any], prompt: str, model: str = "qwen-vl-plus") -> tuple[dict[str, Any] | None, str]:
    from config.qwen_config import DASHSCOPE_COMPATIBLE_BASE_URL, get_qwen_api_key_candidates

    image_path = asset.get("local_resources", {}).get("local_image_path") or ""
    if not image_path or not Path(image_path).exists():
        return None, "no_local_image"
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    data_url = f"data:{mime};base64," + base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    user_payload = {
        "paper_title": asset.get("paper", {}).get("title", ""),
        "asset_label": asset.get("asset_identity", {}).get("label", ""),
        "caption": asset.get("asset_identity", {}).get("caption_clean", ""),
        "nearby_text": asset.get("document_context", {}).get("nearby_text", ""),
        "body_callouts": asset.get("text_linkage", {}).get("body_callouts", [])[:3],
    }
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }
    keys = get_qwen_api_key_candidates()
    if not keys:
        return None, "no_qwen_key"
    last = ""
    for key in keys[:3]:
        try:
            req = urllib.request.Request(
                DASHSCOPE_COMPATIBLE_BASE_URL.rstrip("/") + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": "Bearer " + key["api_key"], "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=160) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            content = raw["choices"][0]["message"]["content"]
            return normalize_visual_card(clean_llm_json(json.loads(strip_json_fence(content)))), ""
        except Exception as exc:
            last = f"{type(exc).__name__}:{exc}"
            continue
    return None, last


def strip_json_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def clean_llm_json(obj: Any) -> Any:
    replacements = {
        "渭m": "um",
        "碌m": "um",
        "μm": "um",
        "掳C": "°C",
        "°C": "degC",
        "鈭?": "−",
        "鈥?": "–",
        "鈦宦?": "−2",
        "m虏": "m²",
        "蟿_avg": "tau_avg",
        "蟿avg": "tau_avg",
        "蟿": "tau",
        "蔚_avg": "epsilon_avg",
        "蔚avg": "epsilon_avg",
        "蔚": "epsilon",
        "胃": "theta",
        "位": "lambda",
        "鈧?": "2",
    }
    if isinstance(obj, str):
        out = obj
        for a, b in replacements.items():
            out = out.replace(a, b)
        return out
    if isinstance(obj, list):
        return [clean_llm_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: clean_llm_json(v) for k, v in obj.items()}
    return obj


def normalize_visual_card(card: dict[str, Any]) -> dict[str, Any]:
    if "result_clclaims_supported" in card and "result_claims_supported" not in card:
        card["result_claims_supported"] = card.pop("result_clclaims_supported")
    expected_defaults = {
        "asset_type": "unknown",
        "visual_type": "",
        "paper_role": "unclear",
        "short_title": "",
        "what_the_visual_shows": "",
        "what_the_caption_says": "",
        "what_the_linked_text_says": "",
        "combined_interpretation": "",
        "key_variables_or_metrics": [],
        "materials_or_structures": [],
        "method_details_visible": [],
        "result_claims_supported": [],
        "important_numbers_visible": [],
        "recommended_use_in_review": [],
        "warnings": [],
        "confidence": "low",
    }
    for key, default in expected_defaults.items():
        card.setdefault(key, default)
    return {key: card.get(key) for key in expected_defaults}


def run(args: argparse.Namespace) -> int:
    run_id = f"visual-v31-{utc_stamp()}"
    output_dir = Path(args.output_dir or (DEFAULT_OUTPUT_ROOT / run_id))
    output_dir.mkdir(parents=True, exist_ok=True)
    meta, items = load_core_items(Path(args.index_path))
    records = db_records_by_id(Path(args.db_path))
    selected = items[int(args.offset) :]
    if int(args.limit) > 0:
        selected = selected[: int(args.limit)]
    all_assets: list[dict[str, Any]] = []
    paper_summaries: list[dict[str, Any]] = []
    start = time.time()
    for idx, item0 in enumerate(selected, 1):
        item = merge_db_record(item0, records)
        print(json.dumps({"event": "paper_start", "i": idx, "n": len(selected), "doi": item.get("doi"), "type": item.get("fulltext_type"), "title": str(item.get("title") or "")[:120]}, ensure_ascii=False), flush=True)
        if bool(getattr(args, "resume_existing", False)):
            paper_dir = output_dir / "papers" / short_paper_dir_name(item)
            summary_path = paper_dir / "summary.json"
            asset_path = paper_dir / "visual_assets.v1_1.jsonl"
            if summary_path.exists() and asset_path.exists():
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    paper_assets = [
                        json.loads(line)
                        for line in asset_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    all_assets.extend(paper_assets)
                    paper_summaries.append(summary)
                    print(
                        json.dumps(
                            {
                                "event": "paper_reused",
                                "doi": summary.get("doi"),
                                "asset_count": len(paper_assets),
                                "elapsed_total": round(time.time() - start, 1),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    continue
                except Exception as exc:
                    print(
                        json.dumps(
                            {"event": "resume_read_failed", "doi": item.get("doi"), "error": f"{type(exc).__name__}:{exc}"},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        try:
            summary = process_one(
                item,
                out_dir=output_dir,
                run_id=run_id,
                allow_recall=bool(args.allow_recall),
                docling_timeout=int(args.docling_timeout),
                max_assets=int(args.max_assets),
            )
        except Exception as exc:
            summary = {"paper_id": item.get("paper_id"), "doi": item.get("doi"), "title": item.get("title"), "fulltext_type": item.get("fulltext_type"), "asset_count": 0, "error": f"{type(exc).__name__}:{exc}", "assets": []}
        paper_assets = list(summary.pop("assets", []))
        all_assets.extend(paper_assets)
        paper_summaries.append(summary)
        print(json.dumps({"event": "paper_done", "doi": summary.get("doi"), "asset_count": len(paper_assets), "elapsed_total": round(time.time() - start, 1)}, ensure_ascii=False), flush=True)
    combined_jsonl = output_dir / "visual_assets.v1_1.all.jsonl"
    combined_jsonl.write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in all_assets), encoding="utf-8")
    card_results = []
    if int(args.qwen_vl_limit) > 0:
        prompt = Path(args.qwen_vl_prompt).read_text(encoding="utf-8", errors="replace")
        card_dir = output_dir / "visual_cards"
        card_dir.mkdir(exist_ok=True)
        candidates = [a for a in all_assets if a.get("local_resources", {}).get("local_image_path")]
        for i, asset in enumerate(candidates[: int(args.qwen_vl_limit)], 1):
            card, error = qwen_vl_card(asset, prompt, model=str(args.qwen_vl_model))
            asset_id = asset.get("asset_identity", {}).get("asset_id", f"asset-{i}")
            out = card_dir / f"{safe_asset_stem(asset_id)}.visual_card.json"
            if card:
                payload = {"schema_version": "visual_card.v1", "asset_id": asset_id, "card": card, "created_at": now_iso()}
                out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                card_results.append({"asset_id": asset_id, "ok": True, "path": str(out), "confidence": card.get("confidence")})
            else:
                card_results.append({"asset_id": asset_id, "ok": False, "error": error})
            print(json.dumps({"event": "qwen_vl_card", **card_results[-1]}, ensure_ascii=False), flush=True)
    contact = output_dir / "visual_assets_contact_sheet.png"
    make_contact_sheet(all_assets, contact)
    summary = {
        "run_id": run_id,
        "created_at": now_iso(),
        "source_index": str(args.index_path),
        "source_meta": meta,
        "output_dir": str(output_dir),
        "paper_count": len(paper_summaries),
        "asset_count": len(all_assets),
        "combined_visual_assets_jsonl": str(combined_jsonl),
        "contact_sheet": str(contact) if contact.exists() else "",
        "paper_summaries": paper_summaries,
        "qwen_vl_cards": card_results,
        "elapsed_seconds": round(time.time() - start, 2),
    }
    (output_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"event": "summary", "summary_path": str(output_dir / "run_summary.json"), "asset_count": len(all_assets), "elapsed_seconds": summary["elapsed_seconds"]}, ensure_ascii=False), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract and normalize visual assets for core fulltexts.")
    parser.add_argument("--index-path", default=str(DEFAULT_CORE58_INDEX))
    parser.add_argument("--db-path", default=str(DEFAULT_LIBRARY_DB))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--allow-recall", action="store_true")
    parser.add_argument("--docling-timeout", type=int, default=240)
    parser.add_argument("--max-assets", type=int, default=80)
    parser.add_argument("--qwen-vl-limit", type=int, default=0)
    parser.add_argument("--qwen-vl-model", default="qwen-vl-plus")
    parser.add_argument("--qwen-vl-prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument(
        "--resume-existing",
        action="store_true",
        help="Reuse completed per-paper outputs in output-dir and rebuild the combined index.",
    )
    return parser


def main() -> int:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
