"""Opt-in Docling source adapter for bounded PDF layout evidence.

The native XML snapshot remains the primary material source.  This module only
normalizes a Docling layout result into immutable ``additional_sources``
artifacts.  It does not install Docling, download models, call a vision model,
or turn a parser candidate into a scientific answer.  A caller may provide a
precomputed canonical layout (the normal production path after a reviewed
trial) or an explicitly configured CLI command.

The canonical layout keeps parser version/config/source hashes beside every
region and crop.  That provenance is part of the snapshot identity, so a
changed parser or configuration creates a new snapshot instead of silently
reusing or replacing an older one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DOCLING_LAYOUT_SCHEMA_VERSION = "optomind.docling_layout.v1"
DOCLING_ADAPTER_SCHEMA_VERSION = "optomind.docling_adapter.v2"


class DoclingAdapterError(ValueError):
    """Raised when an opt-in Docling result cannot be source-bound."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _canonical(value: Any) -> bytes:
    def stable(item: Any, key: str = "") -> Any:
        if isinstance(item, Mapping):
            return {name: stable(child, name) for name, child in item.items()}
        if isinstance(item, (list, tuple)):
            values = [stable(child) for child in item]
            # Docling declares this field as Set[VlmEngineType]. Its dump
            # order varies between processes; all members remain fingerprinted.
            return sorted(values) if key == "supported_engines" else values
        return item
    return json.dumps(stable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def config_sha256(config: Mapping[str, Any] | None) -> str:
    return _sha256(_canonical(dict(config or {})))


def cache_key(pdf_sha256: str, parser_version: str, config: Mapping[str, Any] | None) -> str:
    return _sha256(_canonical({
        "schema_version": DOCLING_ADAPTER_SCHEMA_VERSION,
        "pdf_sha256": _text(pdf_sha256),
        "parser_version": _text(parser_version),
        "config": dict(config or {}),
    }))


def _bbox(value: Any) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)) or len(value) != 4:
        return []
    output: list[float] = []
    try:
        output = [float(item) for item in value]
    except (TypeError, ValueError):
        return []
    return output


def _page(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def _normalize_cell(cell: Mapping[str, Any], index: int) -> dict[str, Any]:
    row = dict(cell)
    row["row_index"] = int(row.get("row_index", row.get("row", 0)) or 0)
    row["col_index"] = int(row.get("col_index", row.get("col", 0)) or 0)
    row["row_span"] = max(1, int(row.get("row_span", row.get("rowspan", 1)) or 1))
    row["col_span"] = max(1, int(row.get("col_span", row.get("colspan", 1)) or 1))
    row["is_header"] = bool(row.get("is_header", row.get("header", False)))
    row["text"] = _text(row.get("text"))
    row.setdefault("cell_id", f"cell-{index + 1}")
    if row.get("bbox") is not None:
        row["bbox"] = _bbox(row.get("bbox"))
    return row


def _run_config(run: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: run.get(key) for key in ("packages", "pipeline_options", "page_range")}
    if run.get("configured_page_range") == "all":
        result["page_range"] = "all"
    if run.get("model_manifest") is not None:
        result["model_manifest"] = run["model_manifest"]
    return result


def _native_location(item: Mapping[str, Any], pages: Mapping[str, Any]) -> dict[str, Any]:
    provenance = list(item.get("prov") or ())
    first = provenance[0] if provenance else {}
    page = _page(first.get("page_no"))
    raw_bbox = dict(first.get("bbox") or {})
    height = float(((pages.get(str(page)) or {}).get("size") or {}).get("height") or 0)
    bbox: list[float] = []
    if raw_bbox:
        left, top, right, bottom = (float(raw_bbox[key]) for key in ("l", "t", "r", "b"))
        if raw_bbox.get("coord_origin") == "BOTTOMLEFT":
            if not height:
                raise DoclingAdapterError("docling_page_height_missing")
            top, bottom = height - top, height - bottom
        bbox = [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]
    return {"page": page, "bbox": bbox, "bbox_origin": "TOPLEFT", "docling_prov": provenance,
            "source_json_pointer": _text(item.get("self_ref")).removeprefix("#")}


def native_document_to_layout(
    document: Mapping[str, Any], run: Mapping[str, Any], *, pdf_sha256: str,
    source_document_id: str, parser_version: str, config: Mapping[str, Any],
    cache_key_value: str = "",
) -> dict[str, Any]:
    """Map real DoclingDocument refs/cells without duplicating its expanded grid."""
    if document.get("schema_name") != "DoclingDocument":
        raise DoclingAdapterError("docling_document_schema_invalid")
    pages = document.get("pages") or {}
    by_ref: dict[str, Mapping[str, Any]] = {}
    for key in ("texts", "tables", "pictures", "groups"):
        for item in document.get(key) or ():
            by_ref[_text(item.get("self_ref"))] = item
    ordered: list[Mapping[str, Any]] = []
    seen: set[str] = set()

    def visit(item: Mapping[str, Any]) -> None:
        ref = _text(item.get("self_ref"))
        if ref in seen:
            return
        seen.add(ref)
        ordered.append(item)
        for child in item.get("children") or ():
            target = by_ref.get(_text(child.get("$ref")))
            if target is None:
                raise DoclingAdapterError("docling_child_ref_missing")
            visit(target)

    visit(document.get("body") or {})

    def linked_texts(item: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
        result = []
        for link in item.get(key) or ():
            target = by_ref.get(_text(link.get("$ref")))
            if target is None:
                raise DoclingAdapterError(f"docling_{key}_ref_missing")
            result.append({"text": _text(target.get("text") or target.get("orig")),
                           "self_ref": _text(target.get("self_ref")), **_native_location(target, pages)})
        return result

    regions = []
    gaps: list[dict[str, Any]] = []
    for item in ordered:
        label = _text(item.get("label"))
        if label not in {"table", "picture", "formula"}:
            continue
        region = {"region_id": _text(item.get("self_ref")), "type": "figure" if label == "picture" else label,
                  **_native_location(item, pages), "captions": linked_texts(item, "captions"),
                  "footnotes": linked_texts(item, "footnotes"),
                  "fidelity_status": "parser_candidate_requires_visual_or_source_review"}
        if label == "table":
            data = item.get("data") or {}
            region.update({"num_rows": data.get("num_rows"), "num_cols": data.get("num_cols"), "cells": []})
            for index, cell in enumerate(data.get("table_cells") or ()):
                cell_location = _native_location({"self_ref": f"{item['self_ref']}/data/table_cells/{index}",
                    "prov": [{"page_no": region["page"], "bbox": cell.get("bbox") or {}}]}, pages)
                region["cells"].append({**dict(cell), **cell_location,
                    "row_index": int(cell["start_row_offset_idx"]), "col_index": int(cell["start_col_offset_idx"]),
                    "is_header": bool(cell.get("column_header")), "is_row_header": bool(cell.get("row_header"))})
            if not region["cells"]:
                region["recognition_status"] = "missing"
                gaps.append({"kind": "table", "status": "recognition_required", "region_id": region["region_id"],
                             "reason": "Docling detected a table region without recognized cells"})
        elif label == "formula":
            recognized = _text(item.get("text")).strip()
            region.update({"text": recognized, "raw_source": recognized, "parser_text": _text(item.get("orig")),
                           "representation": "docling_latex" if recognized else "unrecognized_formula_region",
                           "recognition_status": "parser_candidate" if recognized else "missing"})
            if not recognized:
                gaps.append({"kind": "formula", "status": "recognition_required", "region_id": region["region_id"],
                             "reason": "formula region has no recognized formula; flattened orig text is not a faithful formula"})
        regions.append(region)
    recognized_body = sum(bool(_text(item.get("text")).strip()) for item in ordered
                          if item.get("label") in {"text", "list_item"})
    if not recognized_body:
        gaps.append({"kind": "coverage", "status": "recognition_required",
                     "reason": "no recognized body text; scanned or absent text remains unknown and OCR was not invoked"})
    if not any(region["type"] == "formula" for region in regions):
        gaps.append({"kind": "formula", "status": "unknown", "reason": "no formula regions detected; absence does not establish that the document has no formulas"})
    layout = canonical_layout(regions, pdf_sha256=pdf_sha256, source_document_id=source_document_id,
                              parser_version=parser_version, config=config, cache_key_value=cache_key_value)
    layout["coverage"] = {"page_range": run.get("page_range"), "pages": sorted(int(key) for key in pages),
                          "recognized_body_blocks": recognized_body, "scope": "layout_supplement"}
    layout["known_gaps"] = gaps
    if not run.get("model_manifest"):
        gaps.append({"kind": "parser_provenance", "status": "unknown", "reason": "local model weight hashes were not recorded; package versions, declared model/options and raw output hashes are retained"})
    return layout


def _normalize_region(region: Mapping[str, Any], index: int) -> dict[str, Any]:
    row = dict(region)
    region_id = _text(row.get("region_id") or row.get("id") or f"region-{index + 1}").strip()
    if not region_id:
        raise DoclingAdapterError("docling_region_id_missing")
    region_type = _text(row.get("type") or row.get("kind") or "region").casefold()
    row["region_id"] = region_id
    row["type"] = region_type
    row["page"] = _page(row.get("page"))
    row["bbox"] = _bbox(row.get("bbox"))
    if isinstance(row.get("cells"), Sequence) and not isinstance(row.get("cells"), (str, bytes, bytearray)):
        row["cells"] = [_normalize_cell(cell, cell_index) for cell_index, cell in enumerate(row["cells"]) if isinstance(cell, Mapping)]
    else:
        row["cells"] = []
    caption = row.get("caption")
    if isinstance(caption, Mapping):
        caption_row = dict(caption)
        caption_row["text"] = _text(caption_row.get("text"))
        if caption_row.get("bbox") is not None:
            caption_row["bbox"] = _bbox(caption_row.get("bbox"))
        if caption_row.get("page") is not None:
            caption_row["page"] = _page(caption_row.get("page"))
        row["caption"] = caption_row
    elif caption is not None:
        row["caption"] = {"text": _text(caption)}
    if row.get("raw_source") is not None:
        # Preserve parser output byte-for-byte as text; no LaTeX/XML repair is
        # attempted here because a regional recognizer may be needed later.
        row["raw_source"] = _text(row.get("raw_source"))
    return row


def canonical_layout(
    regions: Sequence[Mapping[str, Any]],
    *,
    pdf_sha256: str,
    source_document_id: str,
    parser_version: str = "2.138.0",
    config: Mapping[str, Any] | None = None,
    parser_name: str = "docling",
    cache_key_value: str = "",
) -> dict[str, Any]:
    """Build a deterministic, source-bound Docling layout payload."""

    parser_config = dict(config or {})
    normalized_regions = [_normalize_region(region, index) for index, region in enumerate(regions) if isinstance(region, Mapping)]
    seen: set[str] = set()
    for region in normalized_regions:
        if region["region_id"] in seen:
            raise DoclingAdapterError("docling_region_id_duplicate")
        seen.add(region["region_id"])
    return {
        "schema_version": DOCLING_LAYOUT_SCHEMA_VERSION,
        "adapter_schema_version": DOCLING_ADAPTER_SCHEMA_VERSION,
        "parser": {
            "name": _text(parser_name) or "docling",
            "version": _text(parser_version),
            "config": parser_config,
            "config_sha256": config_sha256(parser_config),
        },
        "source": {
            "source_document_id": _text(source_document_id),
            "sha256": _text(pdf_sha256),
        },
        "cache_key": _text(cache_key_value),
        "regions": normalized_regions,
    }


def _validate_layout(value: Mapping[str, Any]) -> dict[str, Any]:
    if _text(value.get("schema_version")) != DOCLING_LAYOUT_SCHEMA_VERSION:
        raise DoclingAdapterError("docling_layout_schema_invalid")
    parser = value.get("parser") if isinstance(value.get("parser"), Mapping) else {}
    source = value.get("source") if isinstance(value.get("source"), Mapping) else {}
    regions = value.get("regions")
    if not _text(parser.get("version")) or not _text(source.get("sha256")) or not isinstance(regions, Sequence) or isinstance(regions, (str, bytes, bytearray)):
        raise DoclingAdapterError("docling_layout_provenance_missing")
    normalized = canonical_layout(
        [row for row in regions if isinstance(row, Mapping)],
        pdf_sha256=_text(source.get("sha256")),
        source_document_id=_text(source.get("source_document_id")),
        parser_version=_text(parser.get("version")),
        config=parser.get("config") if isinstance(parser.get("config"), Mapping) else {},
        parser_name=_text(parser.get("name") or "docling"),
        cache_key_value=_text(value.get("cache_key")),
    )
    for key in ("coverage", "known_gaps", "raw_document_sha256", "raw_run_sha256", "recognition_run", "raw_document_source_document_id"):
        if key in value:
            normalized[key] = value[key]
    return normalized


def _provenance(layout: Mapping[str, Any], *, source_sha256: str, region_id: str = "", role: str = "") -> dict[str, Any]:
    parser = layout.get("parser") if isinstance(layout.get("parser"), Mapping) else {}
    source = layout.get("source") if isinstance(layout.get("source"), Mapping) else {}
    return {
        "schema_version": DOCLING_ADAPTER_SCHEMA_VERSION,
        "parser": _text(parser.get("name") or "docling"),
        "parser_version": _text(parser.get("version")),
        "parser_config_sha256": _text(parser.get("config_sha256")) or config_sha256(parser.get("config") if isinstance(parser.get("config"), Mapping) else {}),
        "parser_source_sha256": _text(source.get("sha256")),
        "layout_source_sha256": _text(source_sha256),
        "region_id": _text(region_id),
        "role": _text(role),
        "cache_key": _text(layout.get("cache_key")),
    }


def build_source_specs(
    layout: Mapping[str, Any] | bytes,
    *,
    source_document_id: str = "",
    source_name: str = "docling-layout.json",
    derived_from: str = "",
    source_uri: str = "",
    crops: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return JSON/crop artifacts suitable for snapshot ``additional_sources``."""

    if isinstance(layout, (bytes, bytearray, memoryview)):
        try:
            decoded = json.loads(bytes(layout).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DoclingAdapterError("docling_layout_json_invalid") from exc
    else:
        decoded = dict(layout)
    if not isinstance(decoded, Mapping):
        raise DoclingAdapterError("docling_layout_object_required")
    normalized = _validate_layout(decoded)
    layout_bytes = _canonical(normalized) + b"\n"
    layout_sha = _sha256(layout_bytes)
    parser = normalized.get("parser") if isinstance(normalized.get("parser"), Mapping) else {}
    sid = _text(source_document_id) or f"docling-layout-{layout_sha[:12]}"
    provenance = _provenance(normalized, source_sha256=layout_sha, role="pdf_layout_parser")
    specs: list[dict[str, Any]] = [{
        "source_document_id": sid,
        "role": "pdf_layout_parser",
        "source_uri": source_uri,
        "source_name": Path(source_name).name or "docling-layout.json",
        "format": "json",
        "mime_type": "application/json",
        "data": layout_bytes,
        "derived_from": derived_from,
        "route": "docling",
        "legal_use_status": "derived",
        "provenance": provenance,
    }]
    audit = {
        "schema_version": DOCLING_ADAPTER_SCHEMA_VERSION,
        "parser": _text(parser.get("name") or "docling"),
        "parser_version": _text(parser.get("version")),
        "parser_config_sha256": _text(parser.get("config_sha256")),
        "parser_source_sha256": _text((normalized.get("source") or {}).get("sha256")),
        "layout_sha256": layout_sha,
        "layout_source_document_id": sid,
        "region_count": len(normalized.get("regions") or ()),
        "crop_count": len(crops or ()),
        "cache_key": _text(normalized.get("cache_key")),
        "coverage": normalized.get("coverage") or {},
        "known_gaps": normalized.get("known_gaps") or [],
    }
    audit_bytes = _canonical(audit) + b"\n"
    audit_id = f"{sid}-audit"
    specs.append({
        "source_document_id": audit_id,
        "role": "parser_audit",
        "source_uri": source_uri,
        "source_name": "docling-parser-audit.json",
        "format": "json",
        "mime_type": "application/json",
        "data": audit_bytes,
        "derived_from": sid,
        "route": "docling",
        "legal_use_status": "derived",
        "provenance": _provenance(normalized, source_sha256=_sha256(audit_bytes), role="parser_audit"),
    })
    for index, crop in enumerate(crops or ()):
        if not isinstance(crop, Mapping):
            continue
        data = crop.get("data") if crop.get("data") is not None else crop.get("bytes")
        if not isinstance(data, (bytes, bytearray, memoryview)) or not bytes(data):
            raise DoclingAdapterError(f"docling_crop_empty:{index}")
        region_id = _text(crop.get("region_id") or crop.get("asset_id") or f"region-{index + 1}")
        crop_sha = _sha256(bytes(data))
        crop_id = _text(crop.get("source_document_id")) or f"docling-crop-{_sha256(region_id.encode())[:12]}-{crop_sha[:12]}"
        crop_name = Path(_text(crop.get("source_name") or crop.get("name") or f"{region_id}.png")).name
        specs.append({
            "source_document_id": crop_id,
            "role": "pdf_region_crop",
            "source_uri": source_uri,
            "source_name": crop_name,
            "format": _text(crop.get("format") or "image"),
            "mime_type": _text(crop.get("mime_type") or "image/png"),
            "data": bytes(data),
            "derived_from": sid,
            "route": "docling",
            "legal_use_status": "derived",
            "asset_member": region_id,
            "image_readable": bool(crop.get("image_readable", True)),
            "provenance": _provenance(normalized, source_sha256=crop_sha, region_id=region_id, role="pdf_region_crop") | {
                "crop": {key: value for key, value in crop.items() if key not in {"data", "bytes"}}},
        })
    return specs


@dataclass(frozen=True)
class DoclingAdapterConfig:
    """Explicit opt-in CLI/cache configuration; no default command is run."""

    parser_version: str = "2.138.0"
    cache_root: str | Path = "parser_repair/docling_cache"
    command: tuple[str, ...] = ()
    command_args: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 180.0
    document_path: str | Path = ""
    run_metadata_path: str | Path = ""
    output_filename: str = "document.json"
    crop_scale: float = 2.5
    max_processes: int = 1

    def __post_init__(self):
        if isinstance(self.max_processes, bool) or not isinstance(self.max_processes, int) or not 1 <= self.max_processes <= 8:
            raise DoclingAdapterError("docling_max_processes_invalid")

    @classmethod
    def from_saved_run(cls, document_path: str | Path, run_path: str | Path, **kwargs: Any) -> "DoclingAdapterConfig":
        run = json.loads(Path(run_path).read_text(encoding="utf-8"))
        version = _text((run.get("packages") or {}).get("docling"))
        if not version:
            raise DoclingAdapterError("docling_run_version_missing")
        return cls(parser_version=version, config=_run_config(run), document_path=document_path,
                   run_metadata_path=run_path, **kwargs)

    def fingerprint(self) -> dict[str, Any]:
        value = {"options": dict(self.config), "command": list(self.command), "command_args": list(self.command_args),
                 "crop_strategy": "region-and-linked-caption-footnote-v1",
                 "output_filename": self.output_filename, "crop_scale": float(self.crop_scale)}
        for key, path in (("raw_document_sha256", self.document_path), ("raw_run_sha256", self.run_metadata_path)):
            if path:
                value[key] = _sha256(Path(path).read_bytes())
        configured_files = {}
        for part in (*self.command, *self.command_args):
            path = Path(part)
            if "{" not in part and path.is_file() and path.stat().st_size <= 16 * 1024 * 1024:
                configured_files[str(path.resolve())] = _sha256(path.read_bytes())
        value["configured_files"] = configured_files
        return value

    def cache_path(self, pdf_sha256: str) -> Path:
        return Path(self.cache_root) / cache_key(pdf_sha256, self.parser_version, self.fingerprint())


class DoclingCliAdapter:
    """Run a caller-supplied Docling CLI and return immutable source specs.

    The command must be supplied explicitly.  Its arguments may use the
    placeholders ``{input}`` and ``{output}``; the adapter never chooses a
    global installation or mutates a shared model cache.
    """

    def __init__(self, config: DoclingAdapterConfig, *, runner: Callable[..., Any] | None = None, recognizer: Any = None):
        self.config = config
        self._runner = runner
        self.recognizer = recognizer
        self._process_slots = threading.BoundedSemaphore(config.max_processes)
        self._cache_locks_guard = threading.Lock()
        self._cache_locks = {}

    def _recognized(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.recognizer.prepare(specs) if self.recognizer is not None else specs

    def prepare(
        self,
        pdf_bytes: bytes,
        *,
        source_document_id: str = "original-pdf",
        derived_from: str = "",
        layout: Mapping[str, Any] | None = None,
        crops: Sequence[Mapping[str, Any]] = (),
        run_metadata: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        key = str(self.config.cache_path(_sha256(bytes(pdf_bytes))))
        with self._cache_locks_guard:
            cache_lock = self._cache_locks.setdefault(key, threading.Lock())
        cache_admitted = cache_lock.acquire() if deadline is None else cache_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not cache_admitted:
            raise DoclingAdapterError("docling_deadline_exhausted")
        try:
            admitted = self._process_slots.acquire() if deadline is None else self._process_slots.acquire(timeout=max(0.0, deadline - time.monotonic()))
            if not admitted:
                raise DoclingAdapterError("docling_deadline_exhausted")
            try:
                specs = self._prepare_local(pdf_bytes, source_document_id=source_document_id, derived_from=derived_from,
                    layout=layout, crops=crops, run_metadata=run_metadata, deadline=deadline)
            finally:
                self._process_slots.release()
        finally:
            cache_lock.release()
        # Local extraction slots never span paid recognition or HTTP waits.
        return self._recognized(specs)

    def _prepare_local(
        self,
        pdf_bytes: bytes,
        *,
        source_document_id: str = "original-pdf",
        derived_from: str = "",
        layout: Mapping[str, Any] | None = None,
        crops: Sequence[Mapping[str, Any]] = (),
        run_metadata: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        pdf_sha = _sha256(bytes(pdf_bytes))
        fingerprint = self.config.fingerprint()
        cache_dir = self.config.cache_path(pdf_sha)
        if layout is None and (cache_dir / "CACHE_MANIFEST.json").is_file():
            specs = self._load_cache(cache_dir, pdf_sha, fingerprint)
            return bind_pdf_source_specs(specs, source_document_id, pdf_sha)
        if deadline is not None and time.monotonic() >= deadline:
            raise DoclingAdapterError("docling_deadline_exhausted")
        raw_document = b""
        raw_run = b""
        if layout is None and self.config.document_path:
            raw_document = Path(self.config.document_path).read_bytes()
            raw_run = Path(self.config.run_metadata_path).read_bytes()
            layout, run_metadata = json.loads(raw_document), json.loads(raw_run)
        elif layout is None:
            raw_document, raw_run = self._run(pdf_bytes, deadline=deadline)
            layout, run_metadata = json.loads(raw_document), json.loads(raw_run)
        if not isinstance(layout, Mapping):
            raise DoclingAdapterError("docling_layout_object_required")
        if layout.get("schema_name") == "DoclingDocument":
            if not isinstance(run_metadata, Mapping):
                raise DoclingAdapterError("docling_run_metadata_required")
            self._validate_run(run_metadata, pdf_sha)
            raw_document = raw_document or _canonical(layout) + b"\n"
            raw_run = raw_run or _canonical(run_metadata) + b"\n"
            normalized = native_document_to_layout(layout, run_metadata, pdf_sha256=pdf_sha,
                source_document_id="pdf-content-" + pdf_sha[:12], parser_version=self.config.parser_version,
                config=fingerprint, cache_key_value=cache_dir.name)
            normalized["raw_document_sha256"] = _sha256(raw_document)
            normalized["raw_run_sha256"] = _sha256(raw_run)
            crops = self._render_crops(pdf_bytes, normalized, deadline=deadline)
        else:
            normalized = _validate_layout(layout)
            self._validate_binding(normalized, pdf_sha, fingerprint)
        specs = build_source_specs(normalized, source_document_id="docling-layout-" + cache_dir.name[:16],
                                   derived_from=normalized["source"]["source_document_id"], crops=crops)
        layout_id = specs[0]["source_document_id"]
        for role, name, data in (("pdf_layout_raw", "docling-document.json", raw_document),
                                 ("parser_run", "docling-run.json", raw_run)):
            if data:
                specs.append({"source_document_id": f"{layout_id}-{role}", "role": role, "source_name": name,
                    "format": "json", "mime_type": "application/json", "data": data, "route": "docling",
                    "derived_from": normalized["source"]["source_document_id"], "legal_use_status": "derived",
                    "provenance": _provenance(normalized, source_sha256=_sha256(data), role=role)})
        crop_manifest = [dict(spec["provenance"].get("crop") or {}, source_document_id=spec["source_document_id"],
                              sha256=_sha256(spec["data"])) for spec in specs if spec["role"] == "pdf_region_crop"]
        manifest_bytes = _canonical({"pdf_sha256": pdf_sha, "crops": crop_manifest}) + b"\n"
        specs.append({"source_document_id": layout_id + "-crops", "role": "pdf_crop_manifest",
            "source_name": "docling-crops.json", "format": "json", "mime_type": "application/json", "data": manifest_bytes,
            "derived_from": layout_id, "route": "docling", "legal_use_status": "derived",
            "provenance": _provenance(normalized, source_sha256=_sha256(manifest_bytes), role="pdf_crop_manifest")})
        self._save_cache(cache_dir, specs, pdf_sha, fingerprint)
        return bind_pdf_source_specs(specs, source_document_id, pdf_sha)

    def _validate_run(self, run: Mapping[str, Any], pdf_sha: str) -> None:
        if run.get("status") != "success":
            raise DoclingAdapterError("docling_run_not_successful")
        if run.get("source_sha256") != pdf_sha:
            raise DoclingAdapterError("docling_source_sha256_mismatch")
        if _text((run.get("packages") or {}).get("docling")) != self.config.parser_version:
            raise DoclingAdapterError("docling_parser_version_mismatch")
        if _canonical(_run_config(run)) != _canonical(dict(self.config.config)):
            raise DoclingAdapterError("docling_run_config_mismatch")

    def _validate_binding(self, layout: Mapping[str, Any], pdf_sha: str, fingerprint: Mapping[str, Any]) -> None:
        if (layout.get("source") or {}).get("sha256") != pdf_sha:
            raise DoclingAdapterError("docling_source_sha256_mismatch")
        parser = layout.get("parser") or {}
        if parser.get("version") != self.config.parser_version:
            raise DoclingAdapterError("docling_parser_version_mismatch")
        if _canonical(parser.get("config")) != _canonical(fingerprint):
            raise DoclingAdapterError("docling_run_config_mismatch")

    def _render_crops(self, pdf_bytes: bytes, layout: dict[str, Any], *, deadline: float | None) -> list[dict[str, Any]]:
        try:
            import fitz
        except ImportError as exc:
            raise DoclingAdapterError("docling_crop_renderer_unavailable") from exc
        scale = float(self.config.crop_scale)
        if not 1.0 <= scale <= 6.0:
            raise DoclingAdapterError("docling_crop_scale_invalid")
        result = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
            coverage = layout.setdefault("coverage", {})
            coverage["source_page_count"] = len(pdf)
            coverage["complete_page_coverage"] = coverage.get("pages") == list(range(1, len(pdf) + 1))
            if not coverage["complete_page_coverage"]:
                layout.setdefault("known_gaps", []).append({"kind": "coverage", "status": "partial",
                    "reason": "Docling layout covers only selected PDF pages", "pages": coverage.get("pages")})
            targets = []
            for region in layout.get("regions") or ():
                targets.append((region, region, region["region_id"], "region"))
                for key, role in (("captions", "caption"), ("footnotes", "footnote")):
                    for index, child in enumerate(region.get(key) or ()):
                        targets.append((region, child, f"{region['region_id']}:{role}:{index}", role))
            for region, target, crop_region_id, crop_role in targets:
                if deadline is not None and time.monotonic() >= deadline:
                    raise DoclingAdapterError("docling_deadline_exhausted")
                page_no = target.get("page")
                bbox = target.get("bbox") or []
                if not page_no or not bbox:
                    target["crop_gap"] = "region_without_page_or_bbox"
                    continue
                if not 1 <= page_no <= len(pdf):
                    raise DoclingAdapterError("docling_crop_page_out_of_range")
                page = pdf[page_no - 1]
                clip = fitz.Rect(bbox) & page.rect
                if clip.is_empty or clip.is_infinite:
                    raise DoclingAdapterError("docling_crop_bbox_invalid")
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
                data = pixmap.tobytes("png")
                ref_hash = _sha256(crop_region_id.encode())[:12]
                sid = f"docling-crop-{ref_hash}-{_sha256(data)[:12]}"
                target["crop_source_document_id"] = sid
                result.append({"region_id": crop_region_id, "owner_region_id": region["region_id"], "crop_role": crop_role,
                    "source_document_id": sid, "source_name": f"{region['type']}-{crop_role}-p{page_no}-{ref_hash}.png", "data": data,
                    "format": "png", "mime_type": "image/png", "image_readable": True,
                    "pdf_sha256": _sha256(pdf_bytes), "page": page_no, "bbox": list(bbox),
                    "render_bbox": list(clip), "bbox_origin": "TOPLEFT", "scale": scale,
                    "image_size": [pixmap.width, pixmap.height], "source_json_pointer": target.get("source_json_pointer")})
        return result

    def _save_cache(self, root: Path, specs: Sequence[Mapping[str, Any]], pdf_sha: str, fingerprint: Mapping[str, Any]) -> None:
        Path(self.config.cache_root).mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".docling-cache-", dir=str(Path(self.config.cache_root))))
        try:
            entries = []
            for index, spec in enumerate(specs):
                name = f"{index:03d}-{Path(spec['source_name']).name}"
                data = bytes(spec["data"])
                (staging / name).write_bytes(data)
                entries.append({"file": name, "sha256": _sha256(data),
                                "spec": {key: value for key, value in spec.items() if key != "data"}})
            manifest = {"schema_version": DOCLING_ADAPTER_SCHEMA_VERSION, "pdf_sha256": pdf_sha,
                        "parser_version": self.config.parser_version, "config": dict(fingerprint), "entries": entries}
            (staging / "CACHE_MANIFEST.json").write_bytes(_canonical(manifest) + b"\n")
            if root.exists():
                existing = self._load_cache(root, pdf_sha, fingerprint)
                if _canonical([{**spec, "data": _sha256(spec["data"])} for spec in existing]) != _canonical([{**spec, "data": _sha256(spec["data"])} for spec in specs]):
                    raise DoclingAdapterError("docling_cache_conflict")
            else:
                os.replace(staging, root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _load_cache(self, root: Path, pdf_sha: str, fingerprint: Mapping[str, Any]) -> list[dict[str, Any]]:
        manifest = json.loads((root / "CACHE_MANIFEST.json").read_bytes())
        if manifest.get("pdf_sha256") != pdf_sha:
            raise DoclingAdapterError("docling_source_sha256_mismatch")
        if manifest.get("schema_version") != DOCLING_ADAPTER_SCHEMA_VERSION or manifest.get("parser_version") != self.config.parser_version:
            raise DoclingAdapterError("docling_cache_version_mismatch")
        if _canonical(manifest.get("config")) != _canonical(fingerprint):
            raise DoclingAdapterError("docling_run_config_mismatch")
        result = []
        for entry in manifest.get("entries") or ():
            name = entry["file"]
            if Path(name).name != name:
                raise DoclingAdapterError("docling_cache_path_invalid")
            data = (root / name).read_bytes()
            if _sha256(data) != entry["sha256"]:
                raise DoclingAdapterError("docling_cache_content_mismatch")
            result.append(dict(entry["spec"], data=data))
        layout = next((json.loads(spec["data"]) for spec in result if spec.get("role") == "pdf_layout_parser"), None)
        if layout is None:
            raise DoclingAdapterError("docling_cached_layout_missing")
        self._validate_binding(layout, pdf_sha, fingerprint)
        return result

    def _run(self, pdf_bytes: bytes, *, deadline: float | None) -> tuple[bytes, bytes]:
        if not self.config.command:
            raise DoclingAdapterError("docling_cli_not_configured")
        if Path(self.config.output_filename).name != self.config.output_filename:
            raise DoclingAdapterError("docling_output_filename_invalid")
        Path(self.config.cache_root).mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="docling-run-", dir=str(Path(self.config.cache_root))))
        succeeded = False
        try:
            input_path = work_dir / "input.pdf"
            output_dir = work_dir / "output"
            output_dir.mkdir()
            input_path.write_bytes(bytes(pdf_bytes))
            command = [
                _text(part).format(input=str(input_path), output=str(output_dir), pdf_sha256=_sha256(pdf_bytes))
                for part in (*self.config.command, *self.config.command_args)
            ]
            runner = self._runner or subprocess.run
            (work_dir / "command.json").write_bytes(_canonical(command) + b"\n")
            timeout = float(self.config.timeout_seconds)
            if deadline is not None:
                timeout = min(timeout, max(0.01, deadline - time.monotonic()))
            result = runner(command, check=True, capture_output=True, timeout=timeout)
            for name in ("stdout", "stderr"):
                value = getattr(result, name, b"") or b""
                (work_dir / f"{name}.txt").write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
            document_path = output_dir / self.config.output_filename
            run_path = output_dir / "run.json"
            if not document_path.is_file() or not run_path.is_file():
                raise DoclingAdapterError("docling_cli_layout_missing")
            document, run = document_path.read_bytes(), run_path.read_bytes()
            self._validate_run(json.loads(run), _sha256(pdf_bytes))
            if json.loads(document).get("schema_name") != "DoclingDocument":
                raise DoclingAdapterError("docling_document_schema_invalid")
            succeeded = True
            return document, run
        except DoclingAdapterError as exc:
            raise DoclingAdapterError(f"{exc}; attempt={work_dir}") from exc
        except Exception as exc:
            for name in ("stdout", "stderr"):
                value = getattr(exc, name, b"") or b""
                (work_dir / f"{name}.txt").write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
            raise DoclingAdapterError(f"docling_cli_failed:{type(exc).__name__}; attempt={work_dir}") from exc
        finally:
            if succeeded:
                shutil.rmtree(work_dir, ignore_errors=True)


def bind_pdf_source_specs(specs: Sequence[Mapping[str, Any]], pdf_source_document_id: str, pdf_sha256: str) -> list[dict[str, Any]]:
    """Bind cached content to the PDF source actually retained by publication."""
    layout_spec = next(spec for spec in specs if spec.get("role") == "pdf_layout_parser")
    layout = json.loads(bytes(layout_spec["data"]))
    if (layout.get("source") or {}).get("sha256") != pdf_sha256:
        raise DoclingAdapterError("docling_source_sha256_mismatch")
    old_pdf_id = layout["source"]["source_document_id"]
    layout["source"]["source_document_id"] = pdf_source_document_id
    crops = []
    for spec in specs:
        if spec.get("role") == "pdf_region_crop":
            crops.append(dict((spec.get("provenance") or {}).get("crop") or {}, data=spec["data"],
                              region_id=spec.get("asset_member"), source_document_id=spec["source_document_id"],
                              source_name=spec["source_name"]))
    rebound = build_source_specs(layout, source_document_id=layout_spec["source_document_id"],
                                derived_from=pdf_source_document_id, crops=crops)
    for spec in specs:
        if spec.get("role") not in {"pdf_layout_parser", "parser_audit", "pdf_region_crop"}:
            item = dict(spec)
            if item.get("derived_from") == old_pdf_id:
                item["derived_from"] = pdf_source_document_id
            rebound.append(item)
    return rebound


__all__ = [
    "DOCLING_ADAPTER_SCHEMA_VERSION",
    "DOCLING_LAYOUT_SCHEMA_VERSION",
    "DoclingAdapterConfig",
    "DoclingAdapterError",
    "DoclingCliAdapter",
    "build_source_specs",
    "cache_key",
    "canonical_layout",
    "config_sha256",
    "native_document_to_layout",
    "bind_pdf_source_specs",
]
