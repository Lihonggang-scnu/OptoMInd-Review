"""Deterministic, source-preserving document snapshots for upgrade 3.

This module is deliberately a material layer.  It parses a local TEI or JATS
XML document into stable blocks and asset records; it does not select evidence,
answer a facet, or call a model.  The original input bytes remain in the
snapshot and every normalized block keeps an XML locator back to those bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import copy
import re
import shutil
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lxml import etree as LET

SNAPSHOT_SCHEMA_VERSION = "optomind.document_snapshot.v1"
# Bumped when auxiliary source exposure and derived-source provenance changed.
# Existing snapshot directories remain available for inspection, but a batch
# checkpoint must not resume one as if it had the new semantics.
NORMALIZER_VERSION = "optomind.xml_material_normalizer.v5"
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_WS_RE = re.compile(r"\s+")


class SnapshotError(ValueError):
    """Raised when an input cannot be made into a trustworthy snapshot."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_json(value: Any) -> str:
    return _sha256(_json_bytes(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _normalized(text: str) -> str:
    # NFKC is intentionally not applied: it can change scientific source
    # symbols.  Whitespace folding is the only text normalization here.
    return _WS_RE.sub(" ", text).strip()


def _attr(element: ET.Element, name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = element.attrib.get(key)
        if value is not None:
            return str(value)
    for key, value in element.attrib.items():
        if _local_name(key) == name.lower():
            return str(value)
    return ""


def _safe_filename(value: str, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value or fallback


def _parse_xml(data: bytes) -> ET.Element:
    if not data:
        raise SnapshotError("empty_xml_source")
    try:
        # lxml's hardened parser handles UTF-8/16 declarations correctly while
        # forbidding network access and entity expansion.  Harmless DTDs are
        # tolerated; actual entity nodes are rejected below.
        parser = LET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, recover=False, huge_tree=False)
        root = LET.fromstring(data, parser=parser)
        for node in root.iter():
            if isinstance(node, LET._Entity):
                raise SnapshotError("external_entities_and_dtd_are_not_allowed")
        # Convert the hardened tree to the stdlib tree used below.  lxml may
        # create a fresh Python proxy for the same node on each iteration,
        # which would make id-based source locators unstable.
        return ET.fromstring(LET.tostring(root, encoding="utf-8"))
    except (ET.ParseError, LET.XMLSyntaxError, SnapshotError) as exc:
        raise SnapshotError(f"invalid_xml:{exc}") from exc


def _element_paths(root: ET.Element) -> dict[int, str]:
    paths: dict[int, str] = {}

    def walk(element: ET.Element, path: str) -> None:
        paths[id(element)] = path
        counts: dict[str, int] = defaultdict(int)
        for child in list(element):
            name = _local_name(child.tag) or "node"
            counts[name] += 1
            walk(child, f"{path}/{name}[{counts[name]}]")

    walk(root, f"/{_local_name(root.tag) or 'document'}")
    return paths


def _is_ref(element: ET.Element) -> bool:
    name = _local_name(element.tag)
    return name in {"ref", "xref"} and bool(_attr(element, "refid", "rid", "target"))


def _render_with_refs(element: ET.Element) -> tuple[str, list[dict[str, Any]]]:
    """Render mixed content while retaining citation/cross-reference spans."""

    pieces: list[str] = []
    refs: list[dict[str, Any]] = []

    def append(value: str) -> None:
        if value:
            pieces.append(value)

    append(element.text or "")
    for child in list(element):
        name = _local_name(child.tag)
        start = len("".join(pieces))
        child_text, child_refs = _render_with_refs(child)
        prefix = ""
        rend = _attr(child, "rend", "style").lower()
        if name == "sup" or rend in {"sup", "superscript"}:
            prefix = "^{"
            child_text = prefix + child_text + "}"
        elif name == "sub" or rend in {"sub", "subscript"}:
            prefix = "_{"
            child_text = prefix + child_text + "}"
        elif name in {"break", "lb", "br"}:
            child_text = "\n"
        elif name in {"msup", "msub"} and len(list(child)) >= 2:
            base_text, base_refs = _render_with_refs(list(child)[0])
            power_text, power_refs = _render_with_refs(list(child)[1])
            marker = "^" if name == "msup" else "_"
            child_text = base_text + marker + "{" + power_text + "}"
            child_refs = base_refs + power_refs
        append(child_text)
        end = len("".join(pieces))
        for item in child_refs:
            item["start"] += start + len(prefix)
            item["end"] += start + len(prefix)
            refs.append(item)
        if _is_ref(child):
            ref_type = _attr(child, "ref-type", "type") or "cross_reference"
            target_value = _attr(child, "refid", "rid", "target").strip()
            targets = [item.lstrip("#") for item in re.split(r"[\s,]+", target_value) if item]
            refs.append(
                {
                    "target": target_value,
                    "targets": targets,
                    "marker": child_text,
                    "start": start,
                    "end": end,
                    "kind": "bibliography" if ref_type.lower() in {"bibr", "bib", "citation"} else ref_type,
                }
            )
        append(child.tail or "")
    return "".join(pieces), refs


def _normalized_span(raw: str, normalized: str, start: int, end: int, cursor: int = 0) -> tuple[int, int, int]:
    marker = _normalized(raw[start:end])
    if not marker:
        return start, end, cursor
    found = normalized.find(marker, cursor)
    if found < 0:
        found = normalized.find(marker)
    if found < 0:
        return start, end, cursor
    return found, found + len(marker), found + len(marker)


def _render(element: ET.Element) -> tuple[str, str, list[dict[str, Any]]]:
    raw, refs = _render_with_refs(element)
    normalized = _normalized(raw)
    cursor = 0
    for item in refs:
        start, end, cursor = _normalized_span(raw, normalized, int(item["start"]), int(item["end"]), cursor)
        item["start"], item["end"] = start, end
    return raw, normalized, refs


def _source_identity(root: ET.Element) -> dict[str, str]:
    """Read identity only from article metadata, never from citations."""

    title = ""
    doi = ""
    year = ""
    parents = _find_parent_map(root)
    headers = [element for element in root.iter() if _local_name(element.tag) in {"teiheader", "article-meta"}]
    for header in headers:
        for element in header.iter():
            name = _local_name(element.tag)
            if not title and name in {"article-title", "doc-title"}:
                candidate = _normalized("".join(element.itertext()))
                if candidate:
                    title = candidate
            if not title and name == "title" and any(_local_name(parent.tag) == "titlestmt" for parent in _ancestors(element, parents)):
                candidate = _normalized("".join(element.itertext()))
                if candidate:
                    title = candidate
            if not doi and name in {"article-id", "pub-id", "idno"}:
                kind = (_attr(element, "pub-id-type", "type") or "").lower()
                value = _normalized("".join(element.itertext()))
                if kind in {"doi", "doi-access", "doi.org"} or re.match(r"^10\.\d{4,9}/\S+", value, re.I):
                    doi = value.lower().removeprefix("https://doi.org/").removeprefix("doi:")
            if not year and name in {"year", "date"}:
                candidate = _normalized("".join(element.itertext()))
                match = re.search(r"\b(19|20)\d{2}\b", candidate)
                if match:
                    year = match.group(0)
    return {"title": title, "doi": doi, "year": year}


def _doi(value: Any) -> str:
    text = _text(value).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip()


def _identity_check(canonical_paper_id: str, observed: Mapping[str, str], metadata: Mapping[str, Any]) -> dict[str, Any]:
    provided = {"canonical_paper_id": canonical_paper_id, **{k: _text(metadata.get(k)) for k in ("title", "doi", "year") if metadata.get(k) is not None}}
    conflicts: list[dict[str, str]] = []
    candidate_doi = _doi(metadata.get("doi"))
    if not candidate_doi and re.match(r"^10\.\d{4,9}/\S+", _text(canonical_paper_id), re.I):
        candidate_doi = _doi(canonical_paper_id)
    expected_doi = candidate_doi
    observed_doi = _doi(observed.get("doi"))
    if expected_doi and observed_doi and expected_doi != observed_doi:
        conflicts.append({"field": "doi", "provided": expected_doi, "observed": observed_doi})
    comparable_title = lambda value: re.sub(r"[^0-9a-z]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()
    if metadata.get("title") and observed.get("title") and comparable_title(str(metadata["title"])) != comparable_title(observed["title"]):
        conflicts.append({"field": "title", "provided": _normalized(str(metadata["title"])), "observed": observed["title"]})
    if metadata.get("year") and observed.get("year"):
        try:
            if abs(int(_text(metadata["year"])) - int(observed["year"])) > 1:
                conflicts.append({"field": "year", "provided": _text(metadata["year"]), "observed": observed["year"]})
        except ValueError:
            pass
    # An exact DOI is the strongest identity signal. Keep other discrepancies
    # for audit, but do not downgrade an exact DOI match.
    if _text(metadata.get("identity_basis")).casefold() in {"input_attributed_projection", "synthetic_input"} or bool(metadata.get("synthetic_identity")):
        status = "input_attributed"
    else:
        status = "verified" if expected_doi and observed_doi and expected_doi == observed_doi else ("conflict" if conflicts or (expected_doi and observed_doi and expected_doi != observed_doi) else "provisional")
    return {"status": status, "provided": provided, "observed": dict(observed), "conflicts": conflicts}


@dataclass(frozen=True)
class _BlockDraft:
    element: ET.Element | None
    block_type: str
    section_path: tuple[str, ...]
    parent_element: ET.Element | None
    locator_extra: Mapping[str, Any]
    orphan_text: str = ""
    order_index: int = 0


_BLOCK_NAMES = {
    "head": "heading",
    "title": "heading",
    "p": "paragraph",
    "paragraph": "paragraph",
    "caption": "table_caption",
    "figdesc": "figure_caption",
    "table-cell": "table_cell",
    "td": "table_cell",
    "th": "table_cell",
    "cell": "table_cell",
    "fig": "figure",
    "figure": "figure",
    "formula": "formula",
    "disp-formula": "formula",
    "inline-formula": "formula",
    "note": "footnote",
    "footnote": "footnote",
    "fn": "footnote",
    "table-wrap-foot": "footnote",
    "ref": "reference",
    "bibl": "reference",
    "biblstruct": "reference",
}


def _ancestors(element: ET.Element, parents: Mapping[int, ET.Element]) -> Iterable[ET.Element]:
    current = parents.get(id(element))
    while current is not None:
        yield current
        current = parents.get(id(current))


def _is_inside(element: ET.Element, parent: ET.Element, parents: Mapping[int, ET.Element]) -> bool:
    return any(candidate is parent for candidate in _ancestors(element, parents))


def _find_parent_map(root: ET.Element) -> dict[int, ET.Element]:
    parents: dict[int, ET.Element] = {}
    for parent in root.iter():
        for child in list(parent):
            parents[id(child)] = parent
    return parents


def _is_bibliography_entry(element: ET.Element, parents: Mapping[int, ET.Element]) -> bool:
    if _local_name(element.tag) not in {"biblstruct", "bibl", "ref"}:
        return False
    return any(_local_name(parent.tag) in {"listbibl", "ref-list", "bibliography", "refs"} for parent in _ancestors(element, parents))


def _is_excluded_header(element: ET.Element, parents: Mapping[int, ET.Element]) -> bool:
    if _local_name(element.tag) == "abstract" or any(_local_name(parent.tag) == "abstract" for parent in _ancestors(element, parents)):
        return False
    return any(_local_name(parent.tag) in {"teiheader", "front", "journal-meta", "article-meta"} for parent in _ancestors(element, parents))


def _section_elements(root: ET.Element, parents: Mapping[int, ET.Element]) -> set[int]:
    return {id(item) for item in root.iter() if _local_name(item.tag) in {"div", "sec", "section", "appendix"} and not _is_excluded_header(item, parents)}


def _heading_for_section(section: ET.Element) -> ET.Element | None:
    for child in list(section):
        if _local_name(child.tag) in {"head", "title"}:
            return child
    return None


def _section_path(element: ET.Element, parents: Mapping[int, ET.Element]) -> tuple[str, ...]:
    path: list[str] = []
    for parent in reversed(list(_ancestors(element, parents))):
        if _local_name(parent.tag) in {"div", "sec", "section", "appendix", "abstract"}:
            heading = _heading_for_section(parent)
            value = _normalized("".join(heading.itertext())) if heading is not None else ""
            if not value and _local_name(parent.tag) == "abstract":
                value = "Abstract"
            if value:
                path.append(value)
    return tuple(path)


def _ancestor_candidate(element: ET.Element, parents: Mapping[int, ET.Element], candidates: set[int]) -> ET.Element | None:
    for parent in _ancestors(element, parents):
        if id(parent) in candidates:
            return parent
    return None


def _nearest_table_owner(element: ET.Element, parents: Mapping[int, ET.Element]) -> ET.Element | None:
    """Return the nearest table/table-wrap that owns an element."""

    current: ET.Element | None = element
    while current is not None:
        if _local_name(current.tag) in {"table", "table-wrap"}:
            return current
        current = parents.get(id(current))
    return None


def _research_elements(root: ET.Element, parents: Mapping[int, ET.Element]) -> tuple[list[_BlockDraft], list[dict[str, Any]]]:
    candidates: set[int] = set()
    drafts: list[_BlockDraft] = []
    for traversal_index, element in enumerate(root.iter()):
        name = _local_name(element.tag)
        if _is_excluded_header(element, parents) or _is_bibliography_entry(element, parents) and name not in {"ref", "bibl", "biblstruct"}:
            continue
        block_type = _BLOCK_NAMES.get(name)
        if block_type is None:
            continue
        if block_type == "reference" and not _is_bibliography_entry(element, parents):
            # Inline ref/xref is represented inside its containing block.
            continue
        if block_type == "heading" and name == "title" and _is_bibliography_entry(element, parents):
            continue
        if block_type == "figure":
            # Figures become assets; their captions are separate blocks.
            continue
        ancestor = _ancestor_candidate(element, parents, candidates)
        # A nested table cell is a real source block of the inner table even
        # when its outer cell was selected first.  Other nested known nodes
        # remain covered by their enclosing block to avoid duplicate model
        # projections.
        nested_table_cell = (
            block_type == "table_cell"
            and ancestor is not None
            and _nearest_table_owner(element, parents) is not None
            and _nearest_table_owner(element, parents) is not _nearest_table_owner(ancestor, parents)
        )
        nested_table_context = (
            block_type in {"table_caption", "figure_caption", "footnote"}
            and ancestor is not None
            and _nearest_table_owner(element, parents) is not None
            and _nearest_table_owner(element, parents) is not _nearest_table_owner(ancestor, parents)
        )
        if ancestor is not None and not (nested_table_cell or nested_table_context):
            continue
        if block_type == "table_caption" and any(_local_name(parent.tag) in {"fig", "figure"} for parent in _ancestors(element, parents)):
            block_type = "figure_caption"
        candidates.add(id(element))
        drafts.append(_BlockDraft(element, block_type, _section_path(element, parents), None, {}, order_index=traversal_index))

    # XML text that lives directly in a section/container is easy to lose when
    # only known paragraph nodes are selected.  Retain each non-whitespace
    # direct fragment as an explicit ``other`` block.
    for traversal_index, element in enumerate(root.iter()):
        if _is_excluded_header(element, parents) or id(element) in candidates or _ancestor_candidate(element, parents, candidates) is not None:
            continue
        fragments: list[str] = []
        if element.text and element.text.strip():
            fragments.append(element.text)
        for child in list(element):
            if child.tail and child.tail.strip():
                fragments.append(child.tail)
        if fragments and _local_name(element.tag) not in {"table", "tr", "row", "fig", "figure", "formula", "disp-formula"}:
            drafts.append(_BlockDraft(None, "other", _section_path(element, parents), None, {"container_xml_path": element}, "".join(fragments), order_index=traversal_index))
    return drafts, []


def _table_geometry(table: ET.Element, parents: Mapping[int, ET.Element]) -> dict[int, dict[str, Any]]:
    # A table may contain a nested table in one of its cells.  Walking all
    # descendants here makes the inner rows consume outer row indexes and
    # projects their cells into the outer table.  Use the nearest table owner
    # so each row is assigned to exactly one geometry map.  ``table-wrap`` is
    # retained for small TEI/HTML-like inputs that put rows directly under the
    # wrapper; normal JATS rows are owned by the nested ``table`` element.
    def nearest_table_owner(element: ET.Element) -> ET.Element | None:
        current = element
        while current is not None:
            if _local_name(current.tag) in {"table", "table-wrap"}:
                return current
            current = parents.get(id(current))
        return None

    rows = [
        element
        for element in table.iter()
        if _local_name(element.tag) in {"tr", "row"}
        and nearest_table_owner(element) is table
    ]
    occupancy: dict[tuple[int, int], bool] = {}
    result: dict[int, dict[str, Any]] = {}
    for row_index, row in enumerate(rows):
        col = 0
        cells = [c for c in list(row) if _local_name(c.tag) in {"td", "th", "entry", "table-cell", "cell"}]
        for cell in cells:
            while occupancy.get((row_index, col)):
                col += 1
            colspan = max(1, int(_attr(cell, "colspan", "colspan") or "1"))
            rowspan_raw = _attr(cell, "rowspan")
            if rowspan_raw:
                rowspan = max(1, int(rowspan_raw))
            else:
                # TEI ``morerows`` counts additional rows; HTML/JATS
                # ``rowspan`` counts the current row as well.
                rowspan = max(1, int(_attr(cell, "morerows") or "0") + 1)
            is_header = _local_name(cell.tag) == "th" or _attr(cell, "scope", "role").lower() in {"row", "col", "column", "header"}
            result[id(cell)] = {"row_index": row_index, "col_index": col, "row_span": rowspan, "col_span": colspan, "is_header": is_header}
            for rr in range(row_index, row_index + rowspan):
                for cc in range(col, col + colspan):
                    occupancy[(rr, cc)] = True
            col += colspan
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid_snapshot_json:{path.name}") from exc


def _source_relpath(source_document_id: str, source_name: str) -> str:
    suffix = Path(source_name).suffix.lower() or ".xml"
    return f"sources/{_safe_filename(source_document_id, 'source')}{suffix}"


def _safe_source_uri(value: Any) -> str:
    """Keep provenance useful without persisting credential query values."""

    text = _text(value).strip()
    if not text:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(text)
        if not parts.scheme or not parts.netloc:
            return text[:500]
        secret_names = {"api_key", "apikey", "key", "token", "access_token", "email"}
        query = [(key, "<redacted>" if key.casefold() in secret_names else val) for key, val in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))[:1000]
    except Exception:
        return text[:500]


def _additional_source_specs(additional_sources: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Validate and normalize immutable byte artifacts before staging begins."""

    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(additional_sources or ()):
        if not isinstance(raw, Mapping):
            raise SnapshotError(f"invalid_additional_source:{index}")
        source_id = _text(raw.get("source_document_id") or raw.get("id")) or f"artifact-{index + 1}"
        if source_id in seen or source_id == "main":
            raise SnapshotError(f"duplicate_additional_source:{source_id}")
        payload = raw.get("data") if raw.get("data") is not None else raw.get("bytes")
        if not isinstance(payload, (bytes, bytearray, memoryview)) or not bytes(payload):
            raise SnapshotError(f"empty_additional_source:{source_id}")
        source_name = Path(_text(raw.get("source_name") or raw.get("name") or source_id)).name or source_id
        specs.append({
            "source_document_id": source_id,
            "role": _text(raw.get("role") or "artifact"),
            "source_uri": _safe_source_uri(raw.get("source_uri") or raw.get("uri")),
            "source_name": source_name,
            "format": _text(raw.get("format") or raw.get("mime_type") or "binary"),
            "mime_type": _text(raw.get("mime_type") or ""),
            "data": bytes(payload),
            "derived_from": _text(raw.get("derived_from") or raw.get("parent_source_document_id") or ""),
            "route": _text(raw.get("route") or ""),
            "compression": _text(raw.get("compression") or ""),
            "legal_use_status": _text(raw.get("legal_use_status") or "unknown"),
            "asset_href": _text(raw.get("asset_href") or ""),
            "asset_member": _text(raw.get("asset_member") or ""),
            "image_readable": bool(raw.get("image_readable", False)),
            "provenance": dict(raw.get("provenance") or {}) if isinstance(raw.get("provenance"), Mapping) else {},
        })
        seen.add(source_id)
    return specs


def _asset_id(source_document_id: str, kind: str, locator: str) -> str:
    return f"asset-{_sha256(_json_bytes([source_document_id, kind, locator]))[:20]}"


def _block_id(source_document_id: str, block_type: str, locator: str) -> str:
    return f"block-{_sha256(_json_bytes([source_document_id, block_type, locator]))[:20]}"


def _docling_primary_locator(element: ET.Element | None) -> dict[str, Any]:
    if element is None or not element.get("data-docling-location"):
        return {}
    value = json.loads(element.get("data-docling-location"))
    if not isinstance(value, dict) or value.get("projection_version") != "optomind.docling_body.v1":
        raise SnapshotError("invalid_docling_primary_locator")
    return value


def _docling_layout_payloads(extra_specs: Sequence[Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Read opt-in canonical Docling layout artifacts without trusting them as XML."""

    payloads: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for spec in extra_specs:
        role = _text(spec.get("role")).casefold()
        if role not in {"pdf_layout_parser", "docling_layout", "layout_parser"}:
            continue
        if _text(spec.get("format")).casefold() not in {"json", "application/json"}:
            continue
        try:
            value = json.loads(bytes(spec.get("data") or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, Mapping) or _text(value.get("schema_version")) != "optomind.docling_layout.v1":
            continue
        if not isinstance(value.get("regions"), Sequence) or isinstance(value.get("regions"), (str, bytes, bytearray)):
            continue
        payloads.append((spec, value))
    return payloads


def _docling_projection(
    extra_specs: Sequence[Mapping[str, Any]],
    extra_relpaths: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project source-bound Docling regions into the shared block/asset index.

    This projection carries parser candidates as material with explicit parser
    provenance.  It never rewrites the native XML source and never claims that
    a parser-rendered formula/table is scientifically faithful.
    """

    assets: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    specs_by_id = {_text(item.get("source_document_id")): item for item in extra_specs}
    for spec, payload in _docling_layout_payloads(extra_specs):
        source_id = _text(spec.get("source_document_id"))
        source_path = _text(extra_relpaths.get(source_id))
        parser = payload.get("parser") if isinstance(payload.get("parser"), Mapping) else {}
        parser_version = _text(parser.get("version"))
        parser_name = _text(parser.get("name") or "docling")
        regions = payload.get("regions") or ()
        for region_index, raw_region in enumerate(regions, start=1):
            if not isinstance(raw_region, Mapping):
                continue
            region = dict(raw_region)
            region_id = _text(region.get("region_id") or region.get("id") or f"region-{region_index}")
            kind = _text(region.get("type") or region.get("kind") or "region").casefold()
            page = region.get("page")
            bbox = list(region.get("bbox") or []) if isinstance(region.get("bbox"), Sequence) and not isinstance(region.get("bbox"), (str, bytes, bytearray)) else []
            base_locator = {
                "parser": parser_name,
                "parser_version": parser_version,
                "region_id": region_id,
                "page": page,
                "bbox": bbox,
                "raw_path": source_path,
                "source_json_pointer": f"/regions/{region_index - 1}",
                "xml_path": f"/body[docling]/region[{region_index}]",
                "bbox_origin": _text(region.get("bbox_origin") or "TOPLEFT"),
                "docling_prov": list(region.get("docling_prov") or ()),
                "raw_document_json_pointer": _text(region.get("source_json_pointer")),
                "raw_document_source_document_id": _text(payload.get("raw_document_source_document_id") or source_id + "-pdf_layout_raw"),
                "raw_document_raw_path": _text(extra_relpaths.get(_text(payload.get("raw_document_source_document_id") or source_id + "-pdf_layout_raw"))),
                "fidelity_status": _text(region.get("fidelity_status") or "parser_candidate_requires_visual_or_source_review"),
                "recognition_status": _text(region.get("recognition_status") or "parser_candidate"),
            }
            if kind in {"table", "table_candidate"}:
                asset_type = "table"
            elif kind in {"formula", "formula_candidate", "equation"}:
                asset_type = "formula"
            elif kind in {"figure", "figure_candidate", "picture"}:
                asset_type = "figure"
            else:
                asset_type = ""

            if asset_type:
                asset_id = _asset_id(source_id, asset_type, f"docling:{region_id}")
                parser_provenance = dict(spec.get("provenance") or {})
                parser_provenance.update({
                    "parser": parser_name,
                    "parser_version": parser_version,
                    "region_id": region_id,
                    "source_json_pointer": base_locator["source_json_pointer"],
                    "original_pdf_source_document_id": _text((payload.get("source") or {}).get("source_document_id")),
                    "raw_document_sha256": _text(payload.get("raw_document_sha256")),
                })
                entry: dict[str, Any] = {
                    "asset_id": asset_id,
                    "type": asset_type,
                    "source_document_id": source_id,
                    "locator": dict(base_locator),
                    "caption_block_ids": [],
                    "content_ref": {
                        "source_form": "docling-json",
                        "raw_path": source_path,
                        "json_pointer": base_locator["source_json_pointer"],
                        "content_status": "available",
                    },
                    "content_status": "available",
                    "image_readable": None,
                    "provided_to_multimodal": bool((region.get("recognition") or {}).get("provided_to_multimodal")),
                    "parsed_successfully": True,
                    "parser": parser_name,
                    "parser_version": parser_version,
                    "parser_provenance": parser_provenance,
                    "fidelity_status": base_locator["fidelity_status"],
                    "recognition_status": base_locator["recognition_status"],
                }
                if region.get("recognition"):
                    entry["recognition_provenance"] = dict(region["recognition"])
                    entry["recognition_content"] = dict(region.get("recognition_content") or {})
                    if base_locator["recognition_status"] != "recognized":
                        entry["content_status"] = "recognition_" + base_locator["recognition_status"]
                if asset_type == "formula":
                    raw_source = _text(region.get("raw_source") or region.get("text"))
                    entry["raw_source"] = raw_source
                    entry["content_ref"]["raw_source"] = raw_source
                    entry["content_ref"]["source_form"] = _text(region.get("representation") or "docling_parser_text")
                    if region.get("recognition"):
                        entry["normalized_latex"] = _text(region.get("text"))
                        entry["equation_label"] = _text(region.get("label"))
                    entry["recognition_status"] = base_locator["recognition_status"]
                    if not raw_source:
                        entry.update({"content_status": "recognition_required", "parsed_successfully": False,
                                      "known_gap": "formula region has no recognized formula; flattened parser text is not faithful formula source"})
                elif asset_type == "table":
                    entry["table_structure"] = {"rows": [], "columns": [], "footnotes": []}
                    if not region.get("cells"):
                        entry.update({"content_status": "recognition_required", "parsed_successfully": False,
                                      "known_gap": "table region has no recognized cells"})
                crop_id = _text(region.get("crop_source_document_id"))
                crop_spec = specs_by_id.get(crop_id)
                crop_path = _text(extra_relpaths.get(crop_id)) if crop_spec else ""
                if crop_path:
                    crop_ref = {"raw_path": crop_path, "source_document_id": crop_id,
                                "sha256": _sha256(bytes(crop_spec["data"])), "bytes": len(crop_spec["data"]),
                                **dict((crop_spec.get("provenance") or {}).get("crop") or {})}
                    entry["crop_ref"] = crop_ref
                    entry["image_readable"] = bool(crop_spec.get("image_readable"))
                    if asset_type == "figure":
                        entry["content_ref"].update(crop_ref)
                        entry["parsed_successfully"] = entry["image_readable"]
                elif asset_type == "figure":
                    entry.update({"content_status": "caption_only", "image_readable": False,
                                  "parsed_successfully": False, "known_gap": "figure region has no retained readable crop"})
                entry["content_ref"]["content_status"] = entry["content_status"]
                assets.append(entry)
            else:
                asset_id = ""

            def add_parser_block(
                block_type: str,
                text: Any,
                locator: Mapping[str, Any],
                refs: Sequence[str] = (),
            ) -> str:
                value = _text(text)
                block_locator = dict(locator)
                xml_path = _text(block_locator.get("xml_path"))
                block_id = _block_id(source_id, block_type, xml_path)
                blocks.append({
                    "block_id": block_id,
                    "source_document_id": source_id,
                    "block_type": block_type,
                    "section_path": [
                        _text(item)
                        for item in (region.get("section_path") or region.get("section") or ())
                        if _text(item)
                    ] if isinstance(region.get("section_path") or region.get("section") or (), Sequence) and not isinstance(region.get("section_path") or region.get("section") or (), (str, bytes, bytearray)) else [],
                    "parent_id": None,
                    "order_index": 0,
                    "text_raw": value,
                    "text_normalized": _normalized(value),
                    "locator": block_locator,
                    "inline_references": [],
                    "cross_references": [],
                    "asset_refs": [ref for ref in refs if ref],
                    "text_hash": _sha256(_normalized(value).encode("utf-8")),
                    "research_content": bool(_normalized(value)),
                    "normalization_warnings": ["docling_parser_candidate"],
                })
                return block_id

            def child_locator(item: Mapping[str, Any], suffix: str, pointer: str) -> dict[str, Any]:
                locator = dict(base_locator)
                for field in ("page", "bbox", "bbox_origin", "docling_prov"):
                    if field in item:
                        locator[field] = item[field]
                locator.update({"xml_path": base_locator["xml_path"] + suffix,
                                "source_json_pointer": base_locator["source_json_pointer"] + pointer,
                                "raw_document_json_pointer": _text(item.get("source_json_pointer"))})
                crop_id = _text(item.get("crop_source_document_id"))
                crop_spec = specs_by_id.get(crop_id)
                if crop_spec:
                    locator.update({"crop_source_document_id": crop_id, "crop_raw_path": _text(extra_relpaths.get(crop_id)),
                                    "crop_sha256": _sha256(bytes(crop_spec["data"])),
                                    "crop_image_size": ((crop_spec.get("provenance") or {}).get("crop") or {}).get("image_size")})
                return locator

            captions = list(region.get("captions") or ())
            if region.get("recognized_caption"):
                captions = [dict(captions[0] if captions else {}, text=region["recognized_caption"])]
            if not captions and region.get("caption"):
                caption = region["caption"]
                captions = [dict(caption) if isinstance(caption, Mapping) else {"text": _text(caption)}]
            caption_text = "\n".join(_text(item.get("text")) for item in captions)
            for caption_index, caption in enumerate(captions, start=1):
                if asset_id and _text(caption.get("text")):
                    caption_locator = child_locator(caption, f"/caption[{caption_index}]", f"/captions/{caption_index - 1}")
                    add_parser_block("table_caption" if asset_type == "table" else "figure_caption", caption["text"], caption_locator, [asset_id])
            footnotes = list(region.get("footnotes") or ())
            if region.get("recognized_footnotes"):
                footnotes = [dict(footnotes[index] if index < len(footnotes) else {}, text=text)
                             for index, text in enumerate(region["recognized_footnotes"])]
            for footnote_index, footnote in enumerate(footnotes, start=1):
                footnote_locator = child_locator(footnote, f"/footnote[{footnote_index}]", f"/footnotes/{footnote_index - 1}")
                add_parser_block("footnote", footnote.get("text"), footnote_locator, [asset_id])

            if asset_type == "table":
                cells = region.get("cells") if isinstance(region.get("cells"), Sequence) and not isinstance(region.get("cells"), (str, bytes, bytearray)) else ()
                for cell_index, raw_cell in enumerate(cells, start=1):
                    if not isinstance(raw_cell, Mapping):
                        continue
                    cell = dict(raw_cell)
                    try:
                        row_index = int(cell.get("row_index", cell.get("row", 0)) or 0)
                        col_index = int(cell.get("col_index", cell.get("col", 0)) or 0)
                    except (TypeError, ValueError):
                        row_index, col_index = 0, cell_index - 1
                    try:
                        row_span = max(1, int(cell.get("row_span", cell.get("rowspan", 1)) or 1))
                        col_span = max(1, int(cell.get("col_span", cell.get("colspan", 1)) or 1))
                    except (TypeError, ValueError):
                        row_span, col_span = 1, 1
                    cell_locator = child_locator(cell, f"/table-cell[{cell_index}]", f"/cells/{cell_index - 1}")
                    cell_locator.update({
                        "xml_path": f"{base_locator['xml_path']}/table-cell[{cell_index}]",
                        "row_index": row_index,
                        "col_index": col_index,
                        "row_span": row_span,
                        "col_span": col_span,
                        "is_header": bool(cell.get("is_header", cell.get("header", False))),
                        "is_row_header": bool(cell.get("is_row_header", False)),
                    })
                    if cell.get("bbox") is not None:
                        cell_locator["bbox"] = list(cell.get("bbox") or [])
                    add_parser_block("table_cell", cell.get("text"), cell_locator, [asset_id])
            elif asset_type == "formula":
                formula_text = _text(region.get("text") or region.get("raw_source"))
                formula_locator = dict(base_locator)
                formula_locator["xml_path"] = f"{base_locator['xml_path']}/formula[1]"
                if formula_text:
                    add_parser_block("formula", formula_text, formula_locator, [asset_id])
                else:
                    add_parser_block("other", f"[parser gap: formula {region_id} on PDF page {page}; no recognized formula; recognition_required]", formula_locator, [asset_id])
            elif asset_type == "figure" and not caption_text:
                # Preserve a source-bound picture region even when the parser
                # did not provide a caption; it remains a gap for scientific
                # interpretation and is never emitted as a finding.
                pass
            elif not asset_type and _text(region.get("text")):
                add_parser_block("other", region.get("text"), base_locator)
    return assets, blocks


def _source_fragment(element: ET.Element) -> str:
    try:
        fragment = copy.deepcopy(element)
        fragment.tail = None
        return ET.tostring(fragment, encoding="unicode", short_empty_elements=True)
    except (TypeError, ValueError):
        return ""


def _contains(element: ET.Element, wanted: str) -> bool:
    return any(_local_name(node.tag) == wanted for node in element.iter())


def classify_source_material_roles(
    manifest: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Identify supplementary documents from explicit roles or opening declarations.

    Parser roles and filenames do not establish article coverage.  Only the
    opening body heading/paragraph is inspected, so a later SI citation or an
    appended supplement does not relabel the main article.
    """

    supplementary_roles = {"supplementary", "supplement", "supplementary_material", "supplementary_information", "supporting_information", "supplemental_material", "supplemental_information"}
    declaration = re.compile(r"^(supporting\s+information|supplement(?:ary|al)\s+(?:information|materials?))(?:\s+(?:for|to)\b|\s*[:：]|[\s.\-–—]*$)", re.I)
    body_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for block in blocks:
        path = str((block.get("locator") or {}).get("xml_path") or "")
        if "/body[" not in path or not block.get("research_content", True) or block.get("block_type") == "reference" or not _normalized(block.get("text_normalized")):
            continue
        body_by_source[_text(block.get("source_document_id"))].append(block)
    sources = {_text(row.get("source_document_id")): row for row in manifest.get("source_documents") or () if isinstance(row, Mapping)}
    evidence: list[dict[str, Any]] = []
    for source_id, rows in body_by_source.items():
        source = sources.get(source_id, {})
        explicit_role = _text(source.get("document_role") or source.get("role")).casefold()
        if explicit_role in supplementary_roles:
            evidence.append({"source_document_id": source_id, "basis": "explicit_source_role", "role": explicit_role})
            continue
        # Page labels are generated by PDF projections, not document titles.
        opening = next((row for row in rows if not (row.get("block_type") == "heading" and re.fullmatch(r"(?:page\s*)?\d+", _normalized(row.get("text_normalized")), re.I))), None)
        if opening is None:
            continue
        match = declaration.match(_normalized(opening.get("text_normalized")))
        if match:
            evidence.append({"source_document_id": source_id, "basis": "opening_document_declaration", "block_id": _text(opening.get("block_id")), "declaration": match.group(1)})
    supplementary_ids = sorted(row["source_document_id"] for row in evidence)
    return {"version": "optomind.source_material_role.v1", "body_source_ids": sorted(body_by_source), "supplementary_source_ids": supplementary_ids, "supplementary_only": bool(body_by_source) and set(body_by_source) == set(supplementary_ids), "evidence": evidence}


def build_snapshot(
    input_path: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    *,
    canonical_paper_id: str = "",
    publication_version: str = "",
    acquisition_revision: str = "",
    source_document_id: str = "main",
    source_role: str = "main",
    source_uri: str = "",
    metadata: Mapping[str, Any] | None = None,
    additional_sources: Sequence[Mapping[str, Any]] | None = None,
    material_depth_override: str = "",
    producer_fulltext_claim_override: Mapping[str, Any] | None = None,
    known_gaps_extra: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and atomically publish one immutable XML material snapshot."""

    source_path = Path(input_path)
    try:
        raw_bytes = source_path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"source_read_failed:{source_path}") from exc
    return build_snapshot_from_bytes(
        raw_bytes,
        output_root,
        canonical_paper_id=canonical_paper_id,
        publication_version=publication_version,
        acquisition_revision=acquisition_revision,
        source_document_id=source_document_id,
        source_role=source_role,
        source_uri=source_uri,
        source_name=source_path.name,
        metadata=metadata,
        additional_sources=additional_sources,
        material_depth_override=material_depth_override,
        producer_fulltext_claim_override=producer_fulltext_claim_override,
        known_gaps_extra=known_gaps_extra,
    )


def build_snapshot_from_bytes(
    raw_bytes: bytes,
    output_root: str | os.PathLike[str],
    *,
    canonical_paper_id: str = "",
    publication_version: str = "",
    acquisition_revision: str = "",
    source_document_id: str = "main",
    source_role: str = "main",
    source_uri: str = "",
    source_name: str = "source.xml",
    metadata: Mapping[str, Any] | None = None,
    additional_sources: Sequence[Mapping[str, Any]] | None = None,
    material_depth_override: str = "",
    producer_fulltext_claim_override: Mapping[str, Any] | None = None,
    known_gaps_extra: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    root = _parse_xml(raw_bytes)
    parents = _find_parent_map(root)
    paths = _element_paths(root)
    metadata = dict(metadata or {})
    extra_specs = _additional_source_specs(additional_sources)
    source_uri = _safe_source_uri(source_uri)
    observed_identity = _source_identity(root)
    identity_check = _identity_check(canonical_paper_id, observed_identity, metadata)

    # First identify a stable snapshot identity from semantic metadata and raw
    # content.  Time, output roots, and generated temporary names do not enter.
    source_entries = [{
        "source_document_id": source_document_id,
        "role": source_role,
        "source_uri": source_uri,
        "source_name": Path(source_name).name,
        "format": "xml",
        "raw_sha256": _sha256(raw_bytes),
        "bytes": len(raw_bytes),
    }]
    source_entries.extend({
        key: value for key, value in spec.items() if key != "data"
    } | {"raw_sha256": _sha256(spec["data"]), "bytes": len(spec["data"])} for spec in extra_specs)
    identity_seed = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "canonical_paper_id": canonical_paper_id,
        "publication_version": publication_version,
        "acquisition_revision": acquisition_revision,
        "source_document_id": source_document_id,
        "source_role": source_role,
        "source_name": Path(source_name).name,
        "source_sha256": _sha256(raw_bytes),
        "source_uri": source_uri,
        "source_artifacts": source_entries,
        "identity_check": identity_check,
    }
    snapshot_id = "snapshot-" + _sha256(_json_bytes(identity_seed))[:24]

    drafts, _ = _research_elements(root, parents)
    block_element_ids = {id(d.element) for d in drafts if d.element is not None}
    table_geometry: dict[int, dict[str, Any]] = {}
    for element in root.iter():
        if _local_name(element.tag) in {"table-wrap", "table"}:
            table_geometry.update(_table_geometry(element, parents))

    block_rows: list[dict[str, Any]] = []
    block_by_element: dict[int, str] = {}
    pending_assets: list[dict[str, Any]] = []
    pending_asset_by_element: dict[int, str] = {}
    references: list[dict[str, Any]] = []
    ref_by_target: dict[str, str] = {}
    extra_relpaths = {
        spec["source_document_id"]: _source_relpath(spec["source_document_id"], spec["source_name"])
        for spec in extra_specs
    }
    asset_specs_by_href: dict[str, dict[str, Any]] = {}
    for spec in extra_specs:
        href = _text(spec.get("asset_href"))
        if href:
            asset_specs_by_href[href] = spec
            asset_specs_by_href[Path(href).name] = spec

    # Include figure/table/formula assets in document order, with captions
    # linked after their caption blocks are assigned IDs.
    asset_elements: list[tuple[ET.Element, str]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if _is_excluded_header(element, parents):
            continue
        if name in {"fig", "figure"}:
            asset_elements.append((element, "figure"))
        elif name == "table-wrap":
            asset_elements.append((element, "table"))
        elif name == "table":
            # A normal JATS table is represented by its table-wrap asset.  A
            # genuinely nested table, however, owns distinct rows/cells and
            # needs its own source-bound asset rather than being attached to
            # the outer grid.
            has_table_ancestor = any(_local_name(parent.tag) == "table" for parent in _ancestors(element, parents))
            has_table_wrap_ancestor = any(_local_name(parent.tag) == "table-wrap" for parent in _ancestors(element, parents))
            if has_table_ancestor or not has_table_wrap_ancestor:
                asset_elements.append((element, "table"))
        elif name in {"formula", "disp-formula", "inline-formula"}:
            asset_elements.append((element, "formula"))
    for element, kind in asset_elements:
        locator = paths[id(element)]
        aid = _asset_id(source_document_id, kind, locator)
        pending_asset_by_element[id(element)] = aid
        entry: dict[str, Any] = {
            "asset_id": aid,
            "type": kind,
            "source_document_id": source_document_id,
            "locator": {"xml_path": locator, "xml_id": _attr(element, "id") or None},
            "caption_block_ids": [],
            "content_ref": None,
            "content_status": "available" if kind in {"table", "formula"} else "caption_only",
            "image_readable": False if kind == "figure" else None,
            "provided_to_multimodal": False if kind == "figure" else None,
            "parsed_successfully": kind in {"table", "formula"},
            "raw_source": _source_fragment(element) if kind == "formula" else None,
        }
        if _attr(element, "coords"):
            entry["locator"]["coords"] = _attr(element, "coords")
        primary_locator = _docling_primary_locator(element)
        if primary_locator:
            entry["locator"].update(primary_locator)
            entry.update(parser="docling", parser_version=primary_locator["parser_version"],
                         fidelity_status="parser_candidate_requires_visual_or_source_review",
                         recognition_status=element.get("data-recognition-status") or "parser_candidate",
                         parser_provenance={key: primary_locator[key] for key in (
                             "pdf_sha256", "projection_version", "parser_config_sha256", "region_id", "source_ref")})
        if kind == "figure":
            graphic = next((node for node in element.iter() if _local_name(node.tag) in {"graphic", "inline-graphic", "media"}), None)
            href = _attr(graphic, "href", "url") if graphic is not None else ""
            matched_asset = asset_specs_by_href.get(href) or asset_specs_by_href.get(Path(href).name)
            if matched_asset is not None:
                raw_path = extra_relpaths.get(matched_asset["source_document_id"])
                entry["content_ref"] = {
                    "href": href or None,
                    "raw_path": raw_path,
                    "sha256": _sha256(matched_asset["data"]),
                    "bytes": len(matched_asset["data"]),
                    "content_status": "available",
                }
                entry["content_status"] = "available"
                entry["image_readable"] = bool(matched_asset.get("image_readable"))
                entry["parsed_successfully"] = bool(matched_asset.get("image_readable"))
            else:
                entry["content_ref"] = {"href": href or None, "content_status": "caption_only"}
        elif kind == "formula":
            entry["content_ref"] = {"source_form": "mathml" if _contains(element, "math") else "xml", "raw_source": _source_fragment(element)}
        else:
            entry["content_ref"] = {"source_form": "xml", "xml_path": locator}
            entry["table_structure"] = {"rows": [], "columns": [], "footnotes": []}
        pending_assets.append(entry)

    # Build block rows in source order.  ``other`` drafts are sorted by their
    # container path after normal blocks so their text remains visible.
    drafts.sort(key=lambda draft: draft.order_index)
    for order_index, draft in enumerate(drafts):
        element = draft.element
        locator = paths[id(element)] if element is not None else paths[id(draft.locator_extra["container_xml_path"])]
        block_id = _block_id(source_document_id, draft.block_type, locator)
        parent_id = None
        if element is not None:
            for parent in _ancestors(element, parents):
                if id(parent) in block_by_element:
                    parent_id = block_by_element[id(parent)]
                    break
        if element is not None:
            raw, normalized, inline_refs = _render(element)
        else:
            raw, normalized, inline_refs = draft.orphan_text, _normalized(draft.orphan_text), []
        block: dict[str, Any] = {
            "block_id": block_id,
            "source_document_id": source_document_id,
            "block_type": draft.block_type,
            "section_path": list(draft.section_path),
            "parent_id": parent_id,
            "order_index": order_index,
            "text_raw": raw,
            "text_normalized": normalized,
            "locator": {"xml_path": locator, "xml_id": _attr(element, "id") if element is not None and _attr(element, "id") else None},
            "inline_references": inline_refs,
            "cross_references": [],
            "asset_refs": [],
            "text_hash": _sha256(normalized.encode("utf-8")),
            "research_content": draft.block_type not in {"reference", "heading"},
            "normalization_warnings": [],
        }
        if element is not None and _attr(element, "coords"):
            block["locator"]["coords"] = _attr(element, "coords")
        if element is not None and _attr(element, "data-page"):
            block["locator"]["page"] = int(_attr(element, "data-page")) if _attr(element, "data-page").isdigit() else _attr(element, "data-page")
        block["locator"].update(_docling_primary_locator(element))
        if element is not None and draft.block_type == "table_cell":
            block["locator"].update(table_geometry.get(id(element), {}))
            table = next((parent for parent in _ancestors(element, parents) if id(parent) in pending_asset_by_element), None)
            if table is not None:
                block["asset_refs"].append(pending_asset_by_element[id(table)])
        if element is not None and draft.block_type == "formula":
            nearest = next((candidate for candidate in [element, *list(_ancestors(element, parents))] if id(candidate) in pending_asset_by_element), None)
            if nearest is not None:
                block["asset_refs"].append(pending_asset_by_element[id(nearest)])
        if element is not None and draft.block_type in {"paragraph", "other"}:
            for child in element.iter():
                if id(child) in pending_asset_by_element and pending_asset_by_element[id(child)] not in block["asset_refs"]:
                    block["asset_refs"].append(pending_asset_by_element[id(child)])
        if element is not None and draft.block_type in {"table_cell", "footnote"}:
            # Inline/descendant formulas inside a cell or note do not get an
            # independent block when the enclosing source node owns the text.
            # Keep their immutable raw asset reachable from that same source
            # handle, while leaving nested tables owned by their own cells.
            for child in element.iter():
                child_asset_id = pending_asset_by_element.get(id(child))
                if not child_asset_id or child_asset_id in block["asset_refs"]:
                    continue
                child_asset = next((item for item in pending_assets if item["asset_id"] == child_asset_id), None)
                if child_asset is not None and child_asset.get("type") == "formula":
                    block["asset_refs"].append(child_asset_id)
        if element is not None and draft.block_type == "footnote":
            owner = next((parent for parent in _ancestors(element, parents) if id(parent) in pending_asset_by_element), None)
            if owner is not None:
                block["asset_refs"].append(pending_asset_by_element[id(owner)])
        if element is not None and draft.block_type in {"table_caption", "figure_caption"}:
            owner = next((parent for parent in _ancestors(element, parents) if _local_name(parent.tag) in {"table-wrap", "table", "fig", "figure"}), None)
            if owner is not None and id(owner) in pending_asset_by_element:
                block["asset_refs"].append(pending_asset_by_element[id(owner)])
            # Captions can contain inline/disp formulas (and, in some source
            # dialects, a small nested structured node).  The formula is a
            # first-class asset even though its formula block is suppressed by
            # the enclosing caption candidate.  Link every contained known
            # asset to the caption so downstream consumers can retain the raw
            # source and provenance without inventing a second block.
            for child in element.iter():
                child_asset_id = pending_asset_by_element.get(id(child))
                if child_asset_id and child_asset_id not in block["asset_refs"]:
                    block["asset_refs"].append(child_asset_id)
        block_by_element[id(element)] = block_id if element is not None else block_id
        block_rows.append(block)

        if draft.block_type == "reference" and element is not None:
            target = _attr(element, "id") or locator
            reference_id = f"ref-{_sha256(_json_bytes([source_document_id, target]))[:20]}"
            marker_node = next((child for child in list(element) if _local_name(child.tag) == "label"), None)
            marker = _normalized("".join(marker_node.itertext())) if marker_node is not None else ""
            ref = {
                "reference_id": reference_id,
                "source_document_id": source_document_id,
                "marker": marker,
                "xml_id": _attr(element, "id") or None,
                "text": normalized,
                "raw_source": _source_fragment(element),
                "identifiers": {},
                "block_id": block_id,
                "locator": {"xml_path": locator, "xml_id": _attr(element, "id") or None},
            }
            for identifier in element.iter():
                identifier_name = _local_name(identifier.tag)
                identifier_type = (_attr(identifier, "pub-id-type", "type") or "").lower()
                identifier_value = _normalized("".join(identifier.itertext()))
                if not identifier_value:
                    continue
                if identifier_type in {"doi", "doi-access", "doi.org"}:
                    ref["identifiers"]["doi"] = identifier_value
                elif identifier_type in {"pmid", "pubmed"} or identifier_name == "pmid":
                    ref["identifiers"]["pmid"] = identifier_value
                elif identifier_type in {"pmcid", "pmc"} or identifier_name == "pmcid":
                    ref["identifiers"]["pmcid"] = identifier_value
            references.append(ref)
            if target:
                ref_by_target[target.lstrip("#")] = reference_id

    # Optional Docling layout artifacts are projected into the same immutable
    # block/asset indexes as native XML.  The native source stays authoritative;
    # parser regions carry their own source document, page/bbox, parser
    # version/config provenance, and candidate-fidelity flag.
    docling_assets, docling_blocks = _docling_projection(extra_specs, extra_relpaths)
    if docling_assets or docling_blocks:
        pending_assets.extend(docling_assets)
        block_rows.extend(docling_blocks)
        for order_index, block in enumerate(block_rows):
            block["order_index"] = order_index

    # Attach resolved reference IDs and update asset captions/rows.
    block_id_set = {row["block_id"] for row in block_rows}
    for block in block_rows:
        inline: list[dict[str, Any]] = []
        cross: list[dict[str, Any]] = []
        for item in block["inline_references"]:
            item = dict(item)
            targets = list(item.get("targets") or [_text(item.get("target")).lstrip("#")])
            resolved_targets = [ref_by_target[target] for target in targets if target in ref_by_target]
            item["target_reference_ids"] = resolved_targets if item.get("kind") == "bibliography" else []
            item["target_reference_id"] = resolved_targets[0] if len(resolved_targets) == 1 else None
            if item.get("kind") == "bibliography":
                inline.append(item)
            else:
                cross.append(item)
        block["inline_references"] = inline
        block["cross_references"] = cross
        for aid in block["asset_refs"]:
            asset = next((item for item in pending_assets if item["asset_id"] == aid), None)
            if asset is None:
                continue
            if block["block_type"] in {"table_caption", "figure_caption"}:
                asset["caption_block_ids"].append(block["block_id"])
            if block["block_type"] == "table_cell" and asset.get("type") == "table":
                structure = asset.setdefault("table_structure", {"rows": [], "columns": [], "footnotes": []})
                cell = {"block_id": block["block_id"], "locator": dict(block["locator"]), "text": block["text_normalized"]}
                structure["rows"].append(cell)
                col_start = int(block["locator"].get("col_index", 0))
                col_span = max(1, int(block["locator"].get("col_span", 1)))
                for col_index in range(col_start, col_start + col_span):
                    column = next((item for item in structure["columns"] if item.get("col_index") == col_index), None)
                    if column is None:
                        column = {"col_index": col_index, "header_block_ids": [], "header_labels": []}
                        structure["columns"].append(column)
                    if block["locator"].get("is_header"):
                        column["header_block_ids"].append(block["block_id"])
                        column["header_labels"].append(block["text_normalized"])
            if block["block_type"] == "footnote":
                structure = asset.get("table_structure") if asset.get("type") == "table" else None
                if structure is not None:
                    structure.setdefault("footnotes", []).append({"block_id": block["block_id"], "text": block["text_normalized"], "locator": dict(block["locator"])})

    # Add all unreferenced known assets as explicit gaps; a caption never proves
    # image contents were available or understood.
    for asset in pending_assets:
        if asset["type"] == "figure" and asset.get("content_status") == "available":
            continue
        if asset["type"] == "figure" and asset["caption_block_ids"]:
            asset["content_status"] = "caption_only"
        elif asset["type"] == "figure":
            asset["content_status"] = "missing"
            asset["known_gap"] = "figure_without_caption_or_image"

    source_relpath = _source_relpath(source_document_id, source_name)
    body_nodes = [element for element in root.iter() if _local_name(element.tag) == "body" and not _is_excluded_header(element, parents)]
    body_paths = [paths[id(element)] for element in body_nodes]
    body_text_blocks = [
        row for row in block_rows
        if row["block_type"] in {"paragraph", "other", "table_cell", "formula", "figure_caption", "table_caption"}
        and row["text_normalized"]
        and any(str((row.get("locator") or {}).get("xml_path", "")).startswith(prefix + "/") for prefix in body_paths)
    ]
    fullbody = bool(body_nodes and body_text_blocks)
    material_roles = classify_source_material_roles({"source_documents": source_entries}, block_rows)
    inferred_material_depth = "fulltext" if fullbody else ("structured_partial" if block_rows else "metadata_only")
    material_depth = _text(material_depth_override).strip() or inferred_material_depth
    if material_roles["supplementary_source_ids"]:
        # Only affected sources receive a new immutable identity namespace.
        identity_seed["material_role_policy"] = material_roles
        snapshot_id = "snapshot-" + _sha256(_json_bytes(identity_seed))[:24]
    if material_roles["supplementary_only"]:
        material_depth = "supplementary"
    known_gaps: list[dict[str, Any]] = []
    for asset in pending_assets:
        if asset.get("parser") == "docling" and asset.get("known_gap"):
            known_gaps.append({"kind": "asset", "asset_id": asset["asset_id"], "status": asset["content_status"], "reason": asset["known_gap"]})
        if asset["content_status"] in {"caption_only", "missing"}:
            known_gaps.append({"kind": "asset", "asset_id": asset["asset_id"], "status": asset["content_status"], "reason": "caption_or_locator_without_readable_image"})
    if identity_check["status"] != "verified":
        known_gaps.append({"kind": "identity", "status": identity_check["status"], "reason": "source identity was not independently verified"})
    if not fullbody:
        known_gaps.append({"kind": "coverage", "status": "partial", "reason": "no complete research body was indexed"})
    if material_roles["supplementary_only"]:
        known_gaps.append({"kind": "material_scope", "status": "supplementary_only", "reason": "Supplied document is supporting/supplementary material; the main article is not supplied. Explicit methods, conditions and findings in the supplement remain usable.", "source_document_ids": material_roles["supplementary_source_ids"]})
    for gap in known_gaps_extra or ():
        if isinstance(gap, Mapping):
            known_gaps.append(dict(gap))
    semantic_manifest = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "snapshot_id": snapshot_id,
        "canonical_paper_id": canonical_paper_id,
        "publication_version": publication_version,
        "acquisition_revision": acquisition_revision,
        "source_documents": source_entries,
        "identity_check": identity_check,
    }
    if material_roles["supplementary_source_ids"]:
        semantic_manifest["material_role_policy"] = material_roles

    out_root = Path(output_root)
    out_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}-", dir=str(out_root)))
    target = out_root / snapshot_id
    try:
        (staging / "sources").mkdir(parents=True, exist_ok=True)
        (staging / source_relpath).write_bytes(raw_bytes)
        staged_source_entries: list[dict[str, Any]] = []
        for spec in extra_specs:
            relpath = _source_relpath(spec["source_document_id"], spec["source_name"])
            target_path = staging / relpath
            target_path.write_bytes(spec["data"])
            entry = {key: value for key, value in spec.items() if key != "data"}
            entry.update({"raw_path": relpath, "raw_sha256": _sha256(spec["data"]), "bytes": len(spec["data"]), "acquired_at": _now()})
            staged_source_entries.append(entry)
        blocks_path = staging / "DOCUMENT_BLOCKS.jsonl"
        blocks_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in block_rows), encoding="utf-8", newline="")
        assets_payload = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "assets": pending_assets}
        references_payload = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "references": references, "target_map": ref_by_target}
        _write_json(staging / "DOCUMENT_ASSETS.json", assets_payload)
        _write_json(staging / "REFERENCES.json", references_payload)
        auxiliary_sources = [
            {
                "source_document_id": entry.get("source_document_id"),
                "role": entry.get("role"),
                "raw_path": entry.get("raw_path"),
                "source_name": entry.get("source_name"),
            }
            for entry in staged_source_entries
            if str(entry.get("role") or "").casefold() in {
                "observed_metadata", "input_record", "abstract_metadata", "snippet_metadata"
            }
        ]
        reading_view = _reading_view(
            block_rows,
            snapshot_id,
            pending_assets,
            material_depth=material_depth,
            known_gaps=known_gaps,
            auxiliary_sources=auxiliary_sources,
            source_root=staging,
        )
        (staging / "READING_VIEW.md").write_text(reading_view, encoding="utf-8", newline="")
        manifest = {
            **semantic_manifest,
            "source_documents": [{
                **semantic_manifest["source_documents"][0],
                "raw_path": source_relpath,
                "tool": "optomind_local_xml_snapshot",
                "tool_version": NORMALIZER_VERSION,
                "acquired_at": _now(),
                "parsed_at": _now(),
                "legal_use_status": metadata.get("legal_use_status", "unknown"),
            }, *staged_source_entries],
            "auxiliary_sources": auxiliary_sources,
            "normalizer_version": NORMALIZER_VERSION,
            "block_index_sha256": _sha256(blocks_path.read_bytes()),
            "asset_index_sha256": _sha256((staging / "DOCUMENT_ASSETS.json").read_bytes()),
            "reference_index": {"path": "REFERENCES.json", "sha256": _sha256((staging / "REFERENCES.json").read_bytes()), "count": len(references)},
            "material_inventory": {
                "main_document": "available" if fullbody else ("partial" if block_rows else "missing"),
                "appendix": "available" if any("appendix" in row["section_path"] for row in block_rows) else "unknown",
                "supplementary_material": "unknown",
                "tables": "available" if any(asset["type"] == "table" for asset in pending_assets) else "unknown",
                "formulas": "available" if any(asset["type"] == "formula" for asset in pending_assets) else "unknown",
                "images": "partial" if any(asset["type"] == "figure" for asset in pending_assets) else "unknown",
            },
            "inventory_reasons": {
                "appendix": "no appendix section found" if not any("appendix" in row["section_path"] for row in block_rows) else "appendix blocks indexed",
                "supplementary_material": "not supplied to this snapshot",
                "tables": "no table node observed" if not any(asset["type"] == "table" for asset in pending_assets) else "table structure indexed",
                "formulas": "no formula node observed" if not any(asset["type"] == "formula" for asset in pending_assets) else "formula source indexed",
                "images": "no figure node observed" if not any(asset["type"] == "figure" for asset in pending_assets) else "figure locator/caption indexed; image pixels not provided",
            },
            "producer_fulltext_claim": {
                "claimed": fullbody,
                "scope": "structured_xml_source",
                "content_depth": material_depth,
                "basis": "source XML parsed without truncation and all selected research nodes indexed" if fullbody else "source XML did not contain a complete research body",
                "verification": "automatic structural audit; image pixels are not interpreted by this layer",
            },
            "known_gaps": known_gaps,
            "normalization_changes": [
                "whitespace runs are folded in text_normalized",
                "sup/sub content receives explicit ^{} or _{} delimiters",
                "inline references are indexed with normalized Unicode code-point offsets",
            ],
            "excluded_nonresearch_blocks": [
                {"kind": "tei_header_or_article_metadata", "reason": "metadata is retained in identity_check, not counted as body research blocks"},
                {"kind": "header_biblStruct", "reason": "only entries under listBibl/ref-list are bibliography references"},
            ],
            "unhandled_text_audit": {
                "count": sum(1 for row in block_rows if row["block_type"] == "other"),
                "block_ids": [row["block_id"] for row in block_rows if row["block_type"] == "other"],
                "meaning": "direct XML text retained as other blocks rather than silently dropped",
            },
            "integrity_checks": [
                {"name": "source_sha256", "passed": _sha256((staging / source_relpath).read_bytes()) == _sha256(raw_bytes)},
                {"name": "unique_block_ids", "passed": len(block_rows) == len({row["block_id"] for row in block_rows})},
                {"name": "asset_references_exist", "passed": all(aid in {a["asset_id"] for a in pending_assets} for row in block_rows for aid in row["asset_refs"])},
                {"name": "reference_targets_indexed", "passed": all(item.get("target_reference_id") in {r["reference_id"] for r in references} for row in block_rows for item in row["inline_references"] if item.get("target_reference_id"))},
                {"name": "reading_view_reconstructable", "passed": bool(reading_view.strip())},
            ],
            "files": {
                "DOCUMENT_MANIFEST.json": None,
                "DOCUMENT_BLOCKS.jsonl": {"sha256": _sha256(blocks_path.read_bytes())},
                "DOCUMENT_ASSETS.json": {"sha256": _sha256((staging / "DOCUMENT_ASSETS.json").read_bytes())},
                "REFERENCES.json": {"sha256": _sha256((staging / "REFERENCES.json").read_bytes())},
                "READING_VIEW.md": {"sha256": _sha256((staging / "READING_VIEW.md").read_bytes())},
            },
        }
        if producer_fulltext_claim_override:
            manifest["producer_fulltext_claim"].update(dict(producer_fulltext_claim_override))
        manifest["producer_fulltext_claim"]["content_depth"] = material_depth
        if material_roles["supplementary_source_ids"]:
            manifest["material_inventory"]["supplementary_material"] = "available"
            manifest["inventory_reasons"]["supplementary_material"] = "explicit source role or opening document declaration"
        if material_roles["supplementary_only"]:
            manifest["material_inventory"]["main_document"] = "missing"
            manifest["producer_fulltext_claim"].update(claimed=False, scope="supplementary_document", basis="supplementary document indexed without truncation; main article not supplied")
        for source in manifest["source_documents"]:
            manifest["files"][source["raw_path"]] = {"sha256": source["raw_sha256"]}
        # The manifest's own digest is intentionally excluded from the manifest.
        _write_json(staging / "DOCUMENT_MANIFEST.json", manifest)
        _write_jsonl(staging / "ACQUISITION_LOG.jsonl", [{"event": "snapshot_built", "snapshot_id": snapshot_id, "source_document_id": source_document_id, "source_sha256": _sha256(raw_bytes), "parser": NORMALIZER_VERSION}])
        if target.exists():
            validate_snapshot(target)
            existing_manifest = _read_json(target / "DOCUMENT_MANIFEST.json")
            if existing_manifest.get("snapshot_id") != snapshot_id:
                raise SnapshotError("immutable_snapshot_target_conflict")
            for name in ("DOCUMENT_BLOCKS.jsonl", "DOCUMENT_ASSETS.json", "REFERENCES.json", "READING_VIEW.md"):
                existing_bytes = (target / name).read_bytes() if (target / name).exists() else b""
                staged_bytes = (staging / name).read_bytes()
                if existing_bytes != staged_bytes:
                    raise SnapshotError("immutable_snapshot_content_conflict")
            shutil.rmtree(staging)
        else:
            os.replace(str(staging), str(target))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return _read_json(target / "DOCUMENT_MANIFEST.json")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8", newline="")


def _reading_view(
    blocks: Sequence[Mapping[str, Any]],
    snapshot_id: str,
    assets: Sequence[Mapping[str, Any]],
    *,
    material_depth: str = "unknown",
    known_gaps: Sequence[Mapping[str, Any]] = (),
    auxiliary_sources: Sequence[Mapping[str, Any]] = (),
    source_root: Path | None = None,
    normalizer_version: str = NORMALIZER_VERSION,
) -> str:
    # v4 snapshots are immutable artifacts produced with the previous raw
    # formula rendering. New v5 snapshots use the source-form-aware rendering
    # below, while validation of old snapshots must reproduce v4 exactly.
    legacy_formula_view = normalizer_version == "optomind.xml_material_normalizer.v4"
    lines = ["# Reading View", "", f"<!-- snapshot_id: {snapshot_id} -->", f"> Material depth: `{material_depth}`", "> This view is a structured material projection; asset captions do not prove image interpretation.", ""]
    if known_gaps:
        lines.extend(["> Known gaps:", *[f"> - {item.get('kind')}: {item.get('reason')} (status={item.get('status', 'unknown')})" for item in known_gaps], ""])
    if auxiliary_sources:
        lines.extend(
            [
                "> Auxiliary metadata retained separately from publisher body:",
                *[
                    f"> - {item.get('role', 'metadata')}: {item.get('raw_path') or item.get('source_name') or item.get('source_document_id')}; use only as supplied metadata/snippet evidence"
                    for item in auxiliary_sources
                ],
                "",
            ]
        )
    if source_root is not None:
        seen = {str(block.get("text_normalized") or "").strip() for block in blocks}
        auxiliary_lines: list[str] = []
        for source in auxiliary_sources:
            relative = str(source.get("raw_path") or "")
            path = (source_root / relative).resolve()
            if not path.is_relative_to(source_root.resolve()) or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            records = [payload.get("resolved_identity"), payload.get("record"), payload]
            for record in records:
                if not isinstance(record, dict):
                    continue
                abstract = str(record.get("abstract") or "").strip()
                if abstract and abstract not in seen:
                    seen.add(abstract)
                    auxiliary_lines.extend([f"### Metadata abstract — source: {relative}", "", abstract, ""])
                for snippet in record.get("snippets") or []:
                    if not isinstance(snippet, dict):
                        continue
                    text = str(snippet.get("text") or snippet.get("snippet") or snippet.get("snippet_text") or "").strip()
                    locator = snippet.get("snippet_locator") or {}
                    kind = str(snippet.get("snippet_kind") or snippet.get("kind") or (locator.get("snippet_kind") if isinstance(locator, dict) else "") or "unknown").lower()
                    if not text or text in seen or kind in {"title", "title_hit", "title_match"}:
                        continue
                    seen.add(text)
                    auxiliary_lines.extend([f"### Retrieved snippet — source: {relative}; hit: {snippet.get('hit_id', 'unknown')}; kind: {kind}", "", text, "", "Locator: " + json.dumps(locator, ensure_ascii=False, sort_keys=True), ""])
        if auxiliary_lines:
            lines.extend(["## Auxiliary evidence (separate from publisher body)", "", "> These are metadata abstracts and retrieved excerpts, not proof of complete body coverage. Their source files retain provenance. Publisher XML section labels may contain extraction errors; do not silently merge conflicting abstracts.", "", *auxiliary_lines])
    last_path: tuple[str, ...] = ()
    assets_by_id = {
        str(item.get("asset_id")): item
        for item in assets
        if isinstance(item, Mapping) and item.get("asset_id")
    }
    for block in blocks:
        section = tuple(str(item) for item in block.get("section_path", []))
        if section != last_path:
            for index in range(len(last_path), len(section)):
                lines.extend(["#" * min(6, index + 2) + " " + section[index], ""])
            last_path = section
        text = str(block.get("text_normalized") or "").strip()
        if not text and block.get("block_type") not in {"figure", "table"}:
            continue
        kind = block.get("block_type")
        raw_formula_assets: list[Mapping[str, Any]] = []
        if kind == "formula" and not legacy_formula_view:
            raw_formula_assets = [
                assets_by_id[str(aid)]
                for aid in block.get("asset_refs") or ()
                if str(aid) in assets_by_id
                and assets_by_id[str(aid)].get("type") == "formula"
                and assets_by_id[str(aid)].get("raw_source")
            ]
        if kind == "heading":
            lines.extend(["## " + f"[{block['block_id']}] " + text, ""])
        elif kind == "table_cell":
            loc = block.get("locator") or {}
            lines.append(f"[table cell {block['block_id']} row={loc.get('row_index')} col={loc.get('col_index')} rowspan={loc.get('row_span', 1)} colspan={loc.get('col_span', 1)} header={loc.get('is_header', False)}] {text}")
        elif kind == "formula":
            if raw_formula_assets:
                for asset in raw_formula_assets:
                    source_form = _text((asset.get("content_ref") or {}).get("source_form")) if isinstance(asset.get("content_ref"), Mapping) else ""
                    lines.extend([
                        f"> [raw formula asset {asset.get('asset_id')}; representation={source_form or 'preserved_source'}]",
                        "> ```text",
                        str(asset.get("raw_source")),
                        "> ```",
                        "",
                    ])
            else:
                lines.extend([f"$$ <!-- {block['block_id']} -->", text, "$$", ""])
        elif kind == "reference":
            lines.append(f"[reference {block['block_id']}] {text}")
        else:
            lines.extend([f"[{block['block_id']}] {text}", ""])
        if kind == "formula" and raw_formula_assets:
            continue
        for aid in block.get("asset_refs", []):
            asset = assets_by_id.get(str(aid))
            if asset and asset.get("content_status") in {"caption_only", "missing"}:
                lines.extend([f"> [asset gap: {aid}; status={asset.get('content_status')} ]", ""])
            elif asset and asset.get("type") == "formula" and asset.get("raw_source"):
                if legacy_formula_view:
                    lines.extend([f"> [raw formula asset {aid}; representation=XML/MathML]", "> ```xml", str(asset["raw_source"]), "> ```", ""])
                else:
                    source_form = _text((asset.get("content_ref") or {}).get("source_form")) if isinstance(asset.get("content_ref"), Mapping) else ""
                    lines.extend([f"> [raw formula asset {aid}; representation={source_form or 'preserved_source'}]", "> ```text", str(asset["raw_source"]), "> ```", ""])
    return "\n".join(lines).rstrip() + "\n"


def load_blocks(snapshot_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    path = Path(snapshot_dir) / "DOCUMENT_BLOCKS.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError
                rows.append(value)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotError("invalid_document_blocks") from exc
    return rows


def validate_snapshot(snapshot_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate referential integrity and source hashes of a prepared snapshot."""

    root = Path(snapshot_dir)
    root_real = root.resolve(strict=False)
    manifest = _read_json(root / "DOCUMENT_MANIFEST.json")
    try:
        blocks = load_blocks(root)
        assets_payload = _read_json(root / "DOCUMENT_ASSETS.json")
        refs_payload = _read_json(root / "REFERENCES.json")
    except SnapshotError as exc:
        raise SnapshotError("snapshot_validation_failed:" + str(exc)) from exc
    assets = list(assets_payload.get("assets") or [])
    refs = list(refs_payload.get("references") or [])
    failures: list[str] = []
    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    required_manifest = {
        "schema_version", "normalizer_version", "snapshot_id", "canonical_paper_id",
        "publication_version", "source_documents", "identity_check", "material_inventory",
        "producer_fulltext_claim", "known_gaps", "files", "block_index_sha256",
        "asset_index_sha256", "reference_index",
    }
    failures.extend(f"manifest_missing:{name}" for name in sorted(required_manifest - set(manifest)))
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        failures.append("manifest_schema_version")
    if not isinstance(manifest.get("source_documents"), list) or not manifest.get("source_documents"):
        failures.append("manifest_source_documents")
    source_docs = manifest.get("source_documents") or []
    source_ids = {item.get("source_document_id") for item in source_docs if isinstance(item, dict)}
    if None in source_ids or not source_ids:
        failures.append("manifest_source_document_ids")
    if not isinstance(assets_payload.get("assets"), list):
        failures.append("assets_not_list")
    if not isinstance(refs_payload.get("references"), list):
        failures.append("references_not_list")
    required_files = {"DOCUMENT_BLOCKS.jsonl", "DOCUMENT_ASSETS.json", "REFERENCES.json", "READING_VIEW.md"}
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) < required_files:
        failures.append("manifest_file_inventory")
    else:
        for name in required_files:
            item = files.get(name)
            if not isinstance(item, dict) or not sha_pattern.fullmatch(str(item.get("sha256", ""))):
                failures.append(f"manifest_file_hash:{name}")
    for field in ("block_index_sha256", "asset_index_sha256"):
        if not sha_pattern.fullmatch(str(manifest.get(field, ""))):
            failures.append(f"manifest_index_hash:{field}")
    reference_index = manifest.get("reference_index")
    if not isinstance(reference_index, dict) or reference_index.get("path") != "REFERENCES.json" or not sha_pattern.fullmatch(str(reference_index.get("sha256", ""))):
        failures.append("manifest_reference_index")
    block_ids = {row.get("block_id") for row in blocks}
    asset_ids = {row.get("asset_id") for row in assets}
    ref_ids = {row.get("reference_id") for row in refs}
    if len(block_ids) != len(blocks) or None in block_ids:
        failures.append("duplicate_or_missing_block_id")
    if len(asset_ids) != len(assets) or None in asset_ids:
        failures.append("duplicate_or_missing_asset_id")
    if len(ref_ids) != len(refs) or None in ref_ids:
        failures.append("duplicate_or_missing_reference_id")
    if manifest.get("snapshot_id"):
        source = source_docs[0] if source_docs and isinstance(source_docs[0], dict) else {}
        source_artifacts = []
        for index, item in enumerate(source_docs):
            if not isinstance(item, dict):
                continue
            # Manifest timestamps and legal status are operational fields on
            # the main source; additional artifact entries carry their full
            # immutable source specification in the identity seed.
            keys = (("source_document_id", "role", "source_uri", "source_name", "format", "raw_sha256", "bytes") if index == 0 else ("source_document_id", "role", "source_uri", "source_name", "format", "raw_sha256", "bytes", "mime_type", "derived_from", "route", "compression", "legal_use_status", "asset_href", "asset_member", "image_readable", "provenance"))
            source_artifacts.append({
                key: item.get(key, "")
                for key in keys
                if key in item
            })
        seed = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            # Validate an immutable snapshot against the normalizer recorded
            # when it was published.  New snapshots use NORMALIZER_VERSION;
            # old snapshots remain loadable for audit and are never rebound to
            # the current parser semantics.
            "normalizer_version": manifest.get("normalizer_version") or NORMALIZER_VERSION,
            "canonical_paper_id": manifest.get("canonical_paper_id", ""),
            "publication_version": manifest.get("publication_version", ""),
            "acquisition_revision": manifest.get("acquisition_revision", ""),
            "source_document_id": source.get("source_document_id", ""),
            "source_role": source.get("role", ""),
            "source_name": Path(str(source.get("source_name", "source.xml"))).name,
            "source_sha256": source.get("raw_sha256", ""),
            "source_uri": source.get("source_uri", ""),
            "source_artifacts": source_artifacts,
            "identity_check": manifest.get("identity_check", {}),
        }
        if manifest.get("material_role_policy"):
            seed["material_role_policy"] = manifest["material_role_policy"]
        if "snapshot-" + _sha256(_json_bytes(seed))[:24] != manifest.get("snapshot_id"):
            failures.append("snapshot_id_mismatch")
    expected_order = list(range(len(blocks)))
    if [row.get("order_index") for row in blocks] != expected_order:
        failures.append("block_order_index")
    for row in blocks:
        required_block = {"block_id", "source_document_id", "block_type", "section_path", "order_index", "text_raw", "text_normalized", "locator", "inline_references", "cross_references", "asset_refs", "text_hash"}
        failures.extend(f"block_missing:{row.get('block_id')}:{name}" for name in sorted(required_block - set(row)))
        if row.get("source_document_id") not in source_ids:
            failures.append(f"unknown_block_source:{row.get('block_id')}")
        if row.get("text_hash") != _sha256(str(row.get("text_normalized", "")).encode("utf-8")):
            failures.append(f"text_hash_mismatch:{row.get('block_id')}")
        if row.get("parent_id") and row["parent_id"] not in block_ids:
            failures.append(f"missing_parent:{row.get('block_id')}")
        failures.extend(f"missing_asset:{row.get('block_id')}:{aid}" for aid in row.get("asset_refs", []) if aid not in asset_ids)
        for item in row.get("inline_references", []):
            target_ids = list(item.get("target_reference_ids") or ([item.get("target_reference_id")] if item.get("target_reference_id") else []))
            for target in target_ids:
                if target not in ref_ids:
                    failures.append(f"missing_reference:{row.get('block_id')}:{target}")
            try:
                start, end = int(item.get("start")), int(item.get("end"))
                segment = str(row.get("text_normalized", ""))[start:end]
                if start < 0 or end < start or end > len(str(row.get("text_normalized", ""))) or _normalized(str(item.get("marker", ""))) != segment:
                    failures.append(f"inline_span_invalid:{row.get('block_id')}")
            except (TypeError, ValueError):
                failures.append(f"inline_span_invalid:{row.get('block_id')}")
    for asset in assets:
        required_asset = {"asset_id", "type", "source_document_id", "locator", "caption_block_ids", "content_status"}
        failures.extend(f"asset_missing:{asset.get('asset_id')}:{name}" for name in sorted(required_asset - set(asset)))
        if not isinstance(asset.get("type"), str) or not isinstance(asset.get("locator"), dict) or not isinstance(asset.get("caption_block_ids"), list):
            failures.append(f"asset_field_types:{asset.get('asset_id')}")
        if asset.get("source_document_id") not in source_ids:
            failures.append(f"unknown_asset_source:{asset.get('asset_id')}")
        failures.extend(f"missing_caption_block:{asset.get('asset_id')}:{bid}" for bid in asset.get("caption_block_ids", []) if bid not in block_ids)
        content_ref = asset.get("content_ref") or {}
        raw_path = content_ref.get("raw_path") if isinstance(content_ref, dict) else ""
        if raw_path:
            candidate = (root / str(raw_path)).resolve(strict=False)
            if not (candidate == root_real or root_real in candidate.parents) or not candidate.is_file():
                failures.append(f"missing_asset_bytes:{asset.get('asset_id')}")
            elif content_ref.get("sha256") and _sha256(candidate.read_bytes()) != content_ref.get("sha256"):
                failures.append(f"asset_bytes_hash_mismatch:{asset.get('asset_id')}")
    for ref in refs:
        required_ref = {"reference_id", "source_document_id", "text", "block_id", "locator"}
        failures.extend(f"reference_missing:{ref.get('reference_id')}:{name}" for name in sorted(required_ref - set(ref)))
        if ref.get("source_document_id") not in source_ids:
            failures.append(f"unknown_reference_source:{ref.get('reference_id')}")
        if ref.get("block_id") not in block_ids:
            failures.append(f"missing_reference_block:{ref.get('reference_id')}")
    for source in source_docs:
        rel = source.get("raw_path")
        if not sha_pattern.fullmatch(str(source.get("raw_sha256", ""))):
            failures.append(f"source_hash_field:{rel}")
        if not isinstance(source.get("bytes"), int) or source.get("bytes", -1) < 0:
            failures.append(f"source_bytes_field:{rel}")
        rel_path = Path(str(rel or ""))
        source_candidate = (root / rel_path).resolve(strict=False)
        confined = source_candidate == root_real or root_real in source_candidate.parents
        if not rel or rel_path.is_absolute() or ".." in rel_path.parts or not rel_path.as_posix().startswith("sources/") or not confined or not source_candidate.is_file():
            failures.append(f"missing_source:{rel}")
        elif source.get("raw_sha256") and _sha256(source_candidate.read_bytes()) != source["raw_sha256"]:
            failures.append(f"source_hash_mismatch:{rel}")
        elif source.get("bytes") is not None and source_candidate.stat().st_size != int(source.get("bytes") or 0):
            failures.append(f"source_size_mismatch:{rel}")
    expected = manifest.get("files", {})
    for name, item in expected.items():
        if name == "DOCUMENT_MANIFEST.json":
            continue
        if not isinstance(item, dict) or not sha_pattern.fullmatch(str(item.get("sha256", ""))):
            failures.append(f"file_hash_field:{name}")
            continue
        rel_path = Path(str(name))
        if rel_path.is_absolute() or ".." in rel_path.parts:
            failures.append(f"unsafe_file_path:{name}")
            continue
        path = (root / rel_path).resolve(strict=False)
        if not (path == root_real or root_real in path.parents):
            failures.append(f"unsafe_file_path:{name}")
            continue
        if not path.is_file() or _sha256(path.read_bytes()) != item["sha256"]:
            failures.append(f"file_hash_mismatch:{name}")
    actual_blocks_hash = _sha256((root / "DOCUMENT_BLOCKS.jsonl").read_bytes()) if (root / "DOCUMENT_BLOCKS.jsonl").is_file() else ""
    actual_assets_hash = _sha256((root / "DOCUMENT_ASSETS.json").read_bytes()) if (root / "DOCUMENT_ASSETS.json").is_file() else ""
    actual_refs_hash = _sha256((root / "REFERENCES.json").read_bytes()) if (root / "REFERENCES.json").is_file() else ""
    if manifest.get("block_index_sha256") != actual_blocks_hash:
        failures.append("block_index_hash_mismatch")
    if manifest.get("asset_index_sha256") != actual_assets_hash:
        failures.append("asset_index_hash_mismatch")
    if isinstance(reference_index, dict) and reference_index.get("sha256") != actual_refs_hash:
        failures.append("reference_index_hash_mismatch")
    claim = manifest.get("producer_fulltext_claim") or {}
    material_depth = str(claim.get("content_depth") or ("fulltext" if claim.get("claimed") else "unknown"))
    expected_view = _reading_view(
        blocks,
        str(manifest.get("snapshot_id") or ""),
        assets,
        material_depth=material_depth,
        known_gaps=manifest.get("known_gaps") or (),
        auxiliary_sources=manifest.get("auxiliary_sources") or (),
        source_root=root,
        normalizer_version=str(manifest.get("normalizer_version") or NORMALIZER_VERSION),
    )
    view_path = root / "READING_VIEW.md"
    if not view_path.is_file() or view_path.read_bytes() != expected_view.encode("utf-8"):
        failures.append("reading_view_not_reconstructable")
    if failures:
        raise SnapshotError("snapshot_validation_failed:" + ",".join(failures))
    return {"snapshot_id": manifest.get("snapshot_id"), "blocks": len(blocks), "assets": len(assets), "references": len(refs), "valid": True}


__all__ = [
    "NORMALIZER_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotError",
    "build_snapshot",
    "build_snapshot_from_bytes",
    "load_blocks",
    "validate_snapshot",
]
