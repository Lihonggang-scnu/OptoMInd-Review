"""Authorized local acquisition for upgrade-3 material snapshots.

This module owns identity recovery, open-access route selection, byte sniffing,
and the hand-off to the immutable XML snapshot builder. It does not call a
model and it never treats a provider claim, a PDF filename, or a response
header as proof that a body was retrieved.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from .local_materials import PreparedSnapshot
from .local_materials import LocalTeiMaterialProvider
from .document_snapshot import SnapshotError
from .document_snapshot import _parse_xml as _secure_snapshot_xml
from .public_fulltext_rescue import collect_public_candidates, provider_status

try:  # Optional at import time; fitz is only needed for PDF fallback.
    import fitz  # type: ignore
except Exception:  # pragma: no cover - exercised on installations without fitz
    fitz = None


ACQUISITION_SCHEMA_VERSION = "optomind.material_acquisition.v1"
DEFAULT_DEADLINE_SECONDS = 180.0
DEFAULT_NETWORK_WORKERS = 4
MAX_NETWORK_WORKERS = 10
PDF_PARSE_CONCURRENCY = 1
MAX_PMC_ASSET_BYTES = 25 * 1024 * 1024
MAX_PMC_ASSET_MEMBERS = 200
MAX_PMC_ASSET_UNCOMPRESSED = 100 * 1024 * 1024
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SECRET_QUERY_NAMES = {"api_key", "apikey", "key", "token", "access_token", "email"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_doi(value: Any) -> str:
    text = _text(value).casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip().rstrip(".") if _DOI_RE.match(text.strip()) else ""


def normalize_title(value: Any) -> str:
    value = re.sub(r"\s+", " ", _text(value).casefold())
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", value).strip()


def _authors(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = re.split(r"[,;]|\s+and\s+", value, flags=re.I)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = []
        for item in value:
            if isinstance(item, Mapping):
                family = _text(item.get("lastName") or item.get("last_name") or item.get("family") or item.get("surname"))
                given = _text(item.get("firstName") or item.get("first_name") or item.get("given"))
                if family:
                    # Structured family names are stronger than provider
                    # fullName strings such as ``Liu W``.
                    values.append(" ".join(filter(None, (given, family))))
                else:
                    values.append(_text(item.get("name") or item.get("display_name") or item.get("fullName") or item.get("author")))
            else:
                values.append(_text(item))
    else:
        values = []
    return tuple(value for value in (normalize_title(item) for item in values) if value)[:24]


def _year(value: Any) -> str:
    match = _YEAR_RE.search(_text(value))
    return match.group(0) if match else ""


def normalize_arxiv_id(value: Any) -> str:
    """Normalize arXiv URLs and version suffixes to a stable paper ID."""

    text = _text(value).strip()
    if not text:
        return ""
    text = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^arxiv:", "", text, flags=re.I)
    text = text.rsplit("/", 1)[-1].removesuffix(".pdf")
    return re.sub(r"v\d+$", "", text, flags=re.I).strip()


def _last_name(value: str) -> str:
    words = normalize_title(value).split()
    return words[-1] if words else ""


def title_similarity(left: Any, right: Any) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    overlap = len(at & bt) / max(1, len(at | bt))
    return max(ratio, overlap)


def match_identity(expected: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compare metadata without allowing a generic title prefix to pass."""

    expected_doi = normalize_doi(expected.get("doi"))
    candidate_doi = normalize_doi(candidate.get("doi") or (candidate.get("externalIds") or {}).get("DOI"))
    expected_arxiv = normalize_arxiv_id(expected.get("arxiv_id") or expected.get("arxivId"))
    candidate_arxiv = normalize_arxiv_id(candidate.get("arxiv_id") or candidate.get("arxivId") or (candidate.get("externalIds") or {}).get("ArXiv"))
    expected_openalex = _text(expected.get("openalex_id") or expected.get("openalexId"))
    candidate_openalex = _text(candidate.get("openalex_id") or candidate.get("openalexId"))
    if expected_openalex.startswith("https://openalex.org/"):
        expected_openalex = expected_openalex.rsplit("/", 1)[-1]
    if candidate_openalex.startswith("https://openalex.org/"):
        candidate_openalex = candidate_openalex.rsplit("/", 1)[-1]
    conflicts: list[str] = []
    if expected_doi and candidate_doi and expected_doi != candidate_doi:
        conflicts.append("doi")
    if expected_arxiv and candidate_arxiv and expected_arxiv != candidate_arxiv:
        conflicts.append("arxiv_id")
    if expected_openalex and candidate_openalex and expected_openalex != candidate_openalex:
        conflicts.append("openalex_id")
    expected_title, candidate_title = normalize_title(expected.get("title")), normalize_title(candidate.get("title"))
    similarity = title_similarity(expected_title, candidate_title)
    if expected_title and candidate_title and similarity < 0.84:
        conflicts.append("title")
    expected_year, candidate_year = _year(expected.get("year")), _year(candidate.get("year") or candidate.get("yearPublished"))
    if expected_year and candidate_year and abs(int(expected_year) - int(candidate_year)) > 1:
        conflicts.append("year")
    expected_authors = {_last_name(item) for item in _authors(expected.get("authors")) if _last_name(item)}
    candidate_authors = {_last_name(item) for item in _authors(candidate.get("authors") or candidate.get("author")) if _last_name(item)}
    author_overlap = len(expected_authors & candidate_authors) / max(1, min(len(expected_authors), len(candidate_authors))) if expected_authors and candidate_authors else 0.0
    if expected_authors and candidate_authors and not expected_authors.intersection(candidate_authors):
        conflicts.append("authors")
    if conflicts:
        return {"verified": False, "state": "identity_mismatch", "basis": "conflict", "conflicts": conflicts, "title_similarity": similarity, "author_overlap": author_overlap}
    if expected_doi and candidate_doi and expected_doi == candidate_doi:
        return {"verified": True, "state": "verified", "basis": "doi", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}
    if expected_arxiv and candidate_arxiv and expected_arxiv == candidate_arxiv:
        return {"verified": True, "state": "verified", "basis": "arxiv_id", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}
    if expected_openalex and candidate_openalex and expected_openalex == candidate_openalex:
        return {"verified": True, "state": "verified", "basis": "openalex_id", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}
    exact_ids = ((expected.get("corpus_id") and candidate.get("corpus_id") and str(expected.get("corpus_id")) == str(candidate.get("corpus_id"))) or
                 (expected.get("semantic_scholar_paper_id") and candidate.get("semantic_scholar_paper_id") and str(expected.get("semantic_scholar_paper_id")) == str(candidate.get("semantic_scholar_paper_id"))) or
                 (expected_arxiv and candidate_arxiv and expected_arxiv == candidate_arxiv) or
                 (expected_openalex and candidate_openalex and expected_openalex == candidate_openalex))
    if exact_ids:
        return {"verified": True, "state": "verified", "basis": "provider_id", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}
    if expected_title and candidate_title and similarity >= 0.84 and (not expected_year or not candidate_year or abs(int(expected_year) - int(candidate_year)) <= 1) and (not expected_authors or not candidate_authors or author_overlap > 0):
        return {"verified": True, "state": "provisional", "basis": "title_author_year", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}
    return {"verified": False, "state": "unresolved", "basis": "insufficient_identity", "conflicts": [], "title_similarity": similarity, "author_overlap": author_overlap}


def _match_version_variant(expected: Mapping[str, Any], candidate: Mapping[str, Any], comparison: Mapping[str, Any]) -> dict[str, Any] | None:
    """Allow a public author/preprint version only with strong shared identity."""

    conflicts = list(comparison.get("conflicts") or ())
    if conflicts != ["doi"] or not expected.get("title") or not candidate.get("title"):
        return None
    if float(comparison.get("title_similarity") or 0.0) < 0.92:
        return None
    expected_year, candidate_year = _year(expected.get("year")), _year(candidate.get("year"))
    if expected_year and candidate_year and abs(int(expected_year) - int(candidate_year)) > 1:
        return None
    expected_authors = {_last_name(item) for item in _authors(expected.get("authors")) if _last_name(item)}
    candidate_authors = {_last_name(item) for item in _authors(candidate.get("authors")) if _last_name(item)}
    if not expected_authors or not candidate_authors or not expected_authors.intersection(candidate_authors):
        return None
    return {
        **dict(comparison),
        "verified": True,
        "state": "version_variant",
        "basis": "title_author_year_variant",
        "version_conflict": "doi",
    }


def redact_url(url: Any) -> str:
    text = _text(url)
    try:
        parsed = urllib.parse.urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text[:500]
        query = [(key, "<redacted>" if key.casefold() in _SECRET_QUERY_NAMES else value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), ""))[:1000]
    except Exception:
        return text[:500]


_SENSITIVE_FIELD_RE = re.compile(r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|authorization|bearer|secret)", re.I)


def _safe_observed(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Keep observed metadata useful while bounding size and removing secrets."""

    if _SENSITIVE_FIELD_RE.search(key):
        return "<redacted>"
    if depth > 5:
        return "<depth_limit>"
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value), "sha256": _sha256(bytes(value))}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:200]:
            name = _text(raw_key)[:120]
            out[name] = _safe_observed(raw_value, key=name, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_safe_observed(item, key=key, depth=depth + 1) for item in list(value)[:200]]
    if isinstance(value, str):
        if "url" in key.casefold() or key.casefold() in {"uri", "source"}:
            return redact_url(value)
        return value[:20000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _text(value)[:2000]


def _iter_mappings(value: Any, depth: int = 3) -> Iterable[Mapping[str, Any]]:
    if depth < 0 or not isinstance(value, Mapping):
        return
    yield value
    for child in value.values():
        if isinstance(child, Mapping):
            yield from _iter_mappings(child, depth - 1)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, Mapping):
                    yield from _iter_mappings(item, depth - 1)


def provider_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    external = record.get("externalIds") if isinstance(record.get("externalIds"), Mapping) else {}
    openalex = record.get("openalex_id") or record.get("openalexId") or ""
    if not openalex and _text(record.get("id")).startswith("https://openalex.org/"):
        openalex = _text(record.get("id")).rsplit("/", 1)[-1]
    openalex = _text(openalex)
    if openalex.startswith("https://openalex.org/"):
        openalex = openalex.rsplit("/", 1)[-1]
    s2_id = record.get("semantic_scholar_paper_id") or record.get("paperId") or record.get("s2_id") or ""
    corpus_id = record.get("corpus_id") or record.get("corpusId") or ""
    return {
        "doi": normalize_doi(record.get("doi") or external.get("DOI")),
        "title": _text(record.get("title")),
        "authors": list(_authors(record.get("authors") or record.get("author"))),
        "year": _year(record.get("year") or record.get("yearPublished") or external.get("year")),
        "arxiv_id": normalize_arxiv_id(record.get("arxiv_id") or record.get("arxivId") or external.get("ArXiv")),
        "openalex_id": _text(openalex),
        "semantic_scholar_paper_id": _text(s2_id),
        "corpus_id": _text(corpus_id),
        "abstract": _text(record.get("abstract") or record.get("abstract_or_snippet")),
        "raw": dict(record),
    }


@dataclass
class AcquisitionConfig:
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    network_workers: int = DEFAULT_NETWORK_WORKERS
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    cache_root: str | Path = "data/local_material_acquisition_cache"
    refresh: bool = False
    max_body_bytes: int = 80 * 1024 * 1024
    rescue_enabled: bool = True
    rescue_max_candidates: int = 8
    rescue_max_search_calls: int = 2
    rescue_max_extract_calls: int = 4

    def __post_init__(self) -> None:
        self.deadline_seconds = max(1.0, float(self.deadline_seconds))
        self.network_workers = min(MAX_NETWORK_WORKERS, max(1, int(self.network_workers)))
        self.max_retries = max(0, int(self.max_retries))
        self.rescue_enabled = bool(self.rescue_enabled)
        self.rescue_max_candidates = min(64, max(0, int(self.rescue_max_candidates)))
        self.rescue_max_search_calls = max(0, int(self.rescue_max_search_calls))
        self.rescue_max_extract_calls = max(0, int(self.rescue_max_extract_calls))


@dataclass
class IdentityResolution:
    canonical_paper_id: str
    sample_id: str = ""
    doi: str = ""
    corpus_id: str = ""
    semantic_scholar_paper_id: str = ""
    openalex_id: str = ""
    arxiv_id: str = ""
    title: str = ""
    authors: tuple[str, ...] = ()
    year: str = ""
    abstract: str = ""
    snippets: tuple[Mapping[str, Any], ...] = ()
    status: str = "provisional"
    sources: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    provider_records: tuple[Mapping[str, Any], ...] = ()
    provider_audit: tuple[Mapping[str, Any], ...] = ()

    def as_mapping(self) -> dict[str, Any]:
        return {
            "canonical_paper_id": self.canonical_paper_id,
            "sample_id": self.sample_id,
            "doi": self.doi,
            "corpus_id": self.corpus_id,
            "semantic_scholar_paper_id": self.semantic_scholar_paper_id,
            "openalex_id": self.openalex_id,
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "abstract": self.abstract,
            "snippets": [dict(item) for item in self.snippets],
            "status": self.status,
            "sources": list(self.sources),
            "conflicts": list(self.conflicts),
            "provider_audit": [dict(item) for item in self.provider_audit],
        }


@dataclass
class Artifact:
    kind: str
    data: bytes
    route: str
    source_url: str = ""
    content_type: str = ""
    wire_bytes: bytes = b""
    derived_from: str = ""
    source_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def source_spec(self, source_document_id: str) -> dict[str, Any]:
        spec = {
            "source_document_id": source_document_id,
            "role": self.metadata.get("source_role") or self.kind,
            "source_uri": redact_url(self.source_url),
            "source_name": self.source_name or f"{source_document_id}.bin",
            "format": self.kind,
            "mime_type": self.content_type,
            "data": self.data,
            "derived_from": self.derived_from,
            "route": self.route,
            "compression": "gzip" if self.metadata.get("compressed") else "",
            "legal_use_status": self.metadata.get("legal_use_status", "unknown"),
        }
        for key in ("asset_href", "asset_member", "image_readable"):
            if key in self.metadata:
                spec[key] = self.metadata[key]
        return spec


@dataclass
class AcquisitionResult:
    identity: IdentityResolution
    status: str
    material_depth: str
    selected_route: str = ""
    parser: str = ""
    snapshot: PreparedSnapshot | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    known_gaps: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    stage_timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACQUISITION_SCHEMA_VERSION,
            "identity": self.identity.as_mapping(),
            "status": self.status,
            "material_depth": self.material_depth,
            "selected_route": self.selected_route,
            "parser": self.parser,
            "snapshot_id": self.snapshot.snapshot_id if self.snapshot else "",
            "snapshot_path": str(self.snapshot.root) if self.snapshot else "",
            "attempts": list(self.attempts),
            "artifacts": [{"kind": item.kind, "route": item.route, "source_url": redact_url(item.source_url), "bytes": len(item.data), "sha256": _sha256(item.data), "wire_bytes": len(item.wire_bytes), "wire_sha256": _sha256(item.wire_bytes) if item.wire_bytes else ""} for item in self.artifacts],
            "known_gaps": list(self.known_gaps),
            "errors": list(self.errors),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stage_timings": {key: round(float(value), 3) for key, value in self.stage_timings.items()},
        }


@dataclass
class _Route:
    name: str
    url: str
    kind: str
    source: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 100
    direct: bool = False


def _looks_like_login(text: str) -> bool:
    low = text.casefold()
    return any(token in low for token in (
        "sign in to access", "login to access", "institutional access",
        "subscribe to read", "purchase access", "access denied",
        "verify you are human", "checking your browser", "just a moment",
        "enable javascript and cookies", "captcha", "anubis",
    ))


def _reset_provider_status(provider: Any) -> None:
    """Prevent an older call's error from classifying a fresh no-hit."""

    if provider is None:
        return
    for name, value in (("last_error", ""), ("last_status", "ready")):
        try:
            setattr(provider, name, value)
        except Exception:
            pass


def _html_discovered_links(data: bytes, base_url: str) -> list[dict[str, str]]:
    """Return a small, explicit set of article XML/PDF links from one page."""

    class LinkCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.links: list[dict[str, str]] = []
            self._anchor: dict[str, str] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = {str(key).casefold(): _text(value) for key, value in attrs}
            tag = tag.casefold()
            if tag == "meta":
                name = values.get("name", "").casefold()
                if name in {"citation_pdf_url", "citation_fulltext_xml_url", "citation_fulltext_html_url"} and values.get("content"):
                    self.links.append({"url": values["content"], "label": name})
            elif tag == "link" and values.get("href"):
                self.links.append({"url": values["href"], "label": " ".join((values.get("rel", ""), values.get("type", ""), values.get("title", "")))})
            elif tag == "a" and values.get("href"):
                self._anchor = {"url": values["href"], "label": ""}

        def handle_data(self, value: str) -> None:
            if self._anchor is not None:
                self._anchor["label"] = (self._anchor.get("label", "") + " " + value).strip()

        def handle_endtag(self, tag: str) -> None:
            if tag.casefold() == "a" and self._anchor is not None:
                self.links.append(dict(self._anchor))
                self._anchor = None

    parser = LinkCollector()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
    except Exception:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.links:
        href = urllib.parse.urljoin(base_url, _text(item.get("url")))
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            continue
        label = _text(item.get("label")).casefold()
        lower_url = href.casefold()
        if not any(token in (label + " " + lower_url) for token in ("pdf", "xml", "fulltext", "full-text", "download")):
            continue
        if href in seen:
            continue
        seen.add(href)
        kind = "jats" if any(token in (label + " " + lower_url) for token in ("xml", "fulltextxml", "full-text xml")) else "pdf" if "pdf" in (label + " " + lower_url) else "html"
        if kind == "html":
            continue
        out.append({"url": href, "kind": kind, "label": label[:160]})
        if len(out) >= 12:
            break
    return out


def _parse_xml_identity(data: bytes) -> dict[str, Any]:
    try:
        root = _secure_snapshot_xml(data)
    except Exception:
        return {}
    scope = next((node for node in root.iter() if str(node.tag).rsplit("}", 1)[-1].casefold() in {"article-meta", "teiheader"}), None)
    # A document without an article-meta/teiHeader has no source identity
    # evidence. Never scan the body or bibliography for a DOI/title.
    if scope is None:
        return {}
    title = ""
    doi = ""
    year = ""
    authors: list[str] = []
    for node in scope.iter():
        local = str(node.tag).rsplit("}", 1)[-1].casefold() if isinstance(node.tag, str) else ""
        text = " ".join("".join(node.itertext()).split())
        if not title and local in {"article-title", "doc-title", "title"}:
            title = text
        if not doi and local in {"article-id", "pub-id", "idno"}:
            kind = str(node.attrib.get("pub-id-type") or node.attrib.get("type") or "").casefold()
            if kind in {"doi", "doi-access", "doi.org"} or _DOI_RE.match(text):
                doi = normalize_doi(text)
        if not year and local in {"year", "date", "dateissued"}:
            year = _year(text)
        if local in {"surname", "persname"} and text:
            authors.append(text)
    return {"title": title, "doi": doi, "year": year, "authors": authors}


def _xml_kind(data: bytes) -> str:
    try:
        root = _secure_snapshot_xml(data)
        local = str(root.tag).rsplit("}", 1)[-1].casefold()
        if local == "article":
            return "jats"
        if local in {"tei", "teidoc"}:
            return "tei"
    except Exception:
        pass
    return ""


def _xml_has_research_body(data: bytes) -> bool:
    try:
        root = _secure_snapshot_xml(data)
    except Exception:
        return False
    for node in root.iter():
        local = str(node.tag).rsplit("}", 1)[-1].casefold() if isinstance(node.tag, str) else ""
        if local not in {"body", "text"}:
            continue
        text = " ".join("".join(node.itertext()).split())
        # A body containing only a heading/caption is metadata-only. Require
        # a research paragraph or equivalent text-bearing element.
        has_text_element = any(
            str(child.tag).rsplit("}", 1)[-1].casefold() in {"p", "paragraph", "ab", "div"}
            and " ".join("".join(child.itertext()).split())
            for child in node.iter()
        )
        if has_text_element and text:
            return True
    return False


def _pdf_is_readable(data: bytes) -> bool:
    if not data.startswith(b"%PDF"):
        return False
    if fitz is None:
        return True
    try:
        document = fitz.open(stream=data, filetype="pdf")
        page_count = int(document.page_count)
        document.close()
        return page_count > 0
    except Exception:
        return False


def _html_to_jats(data: bytes, identity: IdentityResolution, base_url: str = "") -> tuple[bytes | None, dict[str, Any]]:
    class Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.in_body = 0
            self.skip = 0
            self.parts: list[str] = []
            self.title = ""
            self.heading_title = ""
            self.meta: dict[str, str] = {}
            self.in_title = False
            self.in_heading = False
            self.current_heading = ""
            self.headings: list[str] = []
            self.has_article = False
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            tag = tag.casefold()
            values = {str(key).casefold(): _text(value) for key, value in attrs}
            if tag == "meta":
                name = values.get("name") or values.get("property") or values.get("itemprop")
                content = values.get("content")
                if name and content and name.casefold() in {
                    "citation_title", "citation_doi", "citation_author", "citation_publication_date",
                    "dc.title", "dc.identifier", "dc.date", "og:title",
                }:
                    self.meta[name.casefold()] = (self.meta.get(name.casefold(), "") + " " + content).strip()
            if tag == "title": self.in_title = True
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.in_body and not self.skip:
                self.in_heading = True
                self.current_heading = ""
            if tag == "body": self.in_body += 1
            if tag in {"article", "main"}: self.has_article = True
            if tag in {"script", "style", "nav", "footer", "header", "aside"}: self.skip += 1
            if tag in {"p", "h1", "h2", "h3", "section"} and self.in_body and not self.skip: self.parts.append("\n")
        def handle_endtag(self, tag: str) -> None:
            tag = tag.casefold()
            if tag == "title": self.in_title = False
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                if self.in_heading:
                    self.headings.append(self.current_heading.strip())
                    if tag == "h1" and not self.heading_title:
                        self.heading_title = self.current_heading.strip()
                self.in_heading = False
            if tag == "body": self.in_body = max(0, self.in_body - 1)
            if tag in {"script", "style", "nav", "footer", "header", "aside"}: self.skip = max(0, self.skip - 1)
            if tag in {"p", "h1", "h2", "h3", "section"} and self.in_body and not self.skip: self.parts.append("\n")
        def handle_data(self, value: str) -> None:
            if self.in_title: self.title += value
            if self.in_heading: self.current_heading += value
            if self.in_body and not self.skip and value.strip(): self.parts.append(value)
    parser = Collector()
    try:
        parser.feed(data.decode("utf-8", errors="replace"))
    except Exception:
        return None, {"reason": "html_parse_failed"}
    raw_text = "".join(parser.parts)
    text = re.sub(r"\s+", " ", raw_text).strip()
    discovered = _html_discovered_links(data, base_url)
    if not text:
        return None, {"reason": "html_empty", "chars": 0, "has_article": parser.has_article, "discovered_links": discovered}
    if _looks_like_login(text):
        return None, {"reason": "html_challenge_or_login", "chars": len(text), "has_article": parser.has_article, "discovered_links": discovered}
    paragraphs = [part.strip() for part in re.split(r"\n+", raw_text) if len(part.strip()) >= 30]
    research_signals = len(re.findall(r"\b(?:abstract|introduction|methods?|results?|discussion|conclusion|references?|doi|figure|table|experiment|measured|analysis)\b", text, flags=re.I))
    body_section_signals = len(re.findall(r"\b(?:introduction|methods?|results?|discussion|conclusion|materials?|experiments?)\b", text, flags=re.I))
    actual_sections = {re.sub(r"^[\d.\s]+", "", h).strip().lower() for h in parser.headings}
    scholarly_sections = [h for h in actual_sections if re.match(r"^(introduction|background|methods?|materials?|experimental|results?|discussion|conclusions?)\b", h)]
    positive_body = bool(parser.has_article and len(text) >= 1200 and len(paragraphs) >= 3 and len(scholarly_sections) >= 2)
    if not positive_body:
        return None, {"reason": "html_not_scholarly_body", "chars": len(text), "paragraphs": len(paragraphs), "research_signals": research_signals, "body_section_signals": body_section_signals, "has_article": parser.has_article, "discovered_links": discovered}
    body = "".join(f"<p>{xml_escape(part)}</p>" for part in paragraphs if len(part) > 30)
    if len(body) < 500:
        body = f"<p>{xml_escape(text)}</p>"
    source_title = _text(
        parser.meta.get("citation_title")
        or parser.meta.get("dc.title")
        or parser.meta.get("og:title")
        or parser.heading_title
        or parser.title
    )
    source_doi = normalize_doi(parser.meta.get("citation_doi") or parser.meta.get("dc.identifier"))
    source_year = _year(parser.meta.get("citation_publication_date") or parser.meta.get("dc.date"))
    # Multiple citation_author meta tags are intentionally collapsed above;
    # retain no guessed author tokenization here.  DOI/title evidence remains
    # available for the source identity comparison.
    source_authors: tuple[str, ...] = ()
    source_identity = {
        "title": source_title,
        "doi": source_doi,
        "year": source_year,
        "authors": list(source_authors),
    }
    # A source page must contribute identity evidence of its own.  Reusing the
    # requested title here would create a self-certifying synthetic document.
    if not source_title and not source_doi:
        return None, {
            "reason": "html_source_identity_missing",
            "chars": len(text),
            "paragraphs": len(paragraphs),
            "discovered_links": discovered,
        }
    title = xml_escape(source_title or identity.title or "Untitled")
    if source_doi:
        source_id = f"<article-id pub-id-type=\"doi\">{xml_escape(source_doi)}</article-id>"
    else:
        source_id = ""
    authors = "".join(f"<name><surname>{xml_escape(item)}</surname></name>" for item in source_authors)
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><article><front><article-meta>{source_id}<title-group><article-title>{title}</article-title></title-group><contrib-group>{authors}</contrib-group></article-meta></front><body><sec><title>Retrieved article body</title>{body}</sec></body></article>".encode()
    return xml, {
        "chars": len(text),
        "paragraphs": len(paragraphs),
        "research_signals": research_signals,
        "body_section_signals": body_section_signals,
        "html_article": parser.has_article,
        "source_identity": source_identity,
        "identity_basis": "html_source_metadata_or_heading",
        "discovered_links": discovered,
        "known_losses": ["headings/tables/math/image pixels are not preserved by this plain HTML projection"],
    }


def _plain_text_to_jats(data: bytes, identity: IdentityResolution) -> tuple[bytes | None, dict[str, Any]]:
    """Project provider-supplied fullText while retaining its raw wire bytes."""

    text = data.decode("utf-8", errors="replace")
    paragraphs = [part.strip() for part in re.split(r"\n{2,}|\r\n", text) if part.strip()]
    if not paragraphs:
        return None, {"reason": "provider_fulltext_empty"}
    body = "".join(f"<p>{xml_escape(part)}</p>" for part in paragraphs)
    if len(body) < 500 and len(text.strip()) < 400:
        return None, {"reason": "provider_fulltext_too_short", "chars": len(text.strip())}
    title = xml_escape(identity.title or "Untitled")
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><article><front><article-meta><title-group><article-title>{title}</article-title></title-group></article-meta></front><body><sec><title>Provider full text</title>{body}</sec></body></article>".encode()
    return xml, {"chars": len(text), "paragraphs": len(paragraphs)}


_MARKDOWN_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\]\)<>\"'#?]+", re.I)


def _reader_heading(line: str) -> str:
    value = line.strip().replace(r"\.", ".")
    if not value or len(value) > 150:
        return ""
    if value.startswith("#"):
        return re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", re.sub(r"^#+\s*", "", value))
    numbered = re.match(r"^\d+(?:\.\d+)*\.?(?:\s+)([A-Z][A-Za-z].*)$", value)
    if numbered:
        return numbered.group(1)
    if re.fullmatch(r"(?:References(?: From the Supporting Information)?|Acknowledg(?:e)?ments?|Conclusions?|Introduction|Methods|Results and Discussion|Supporting Information)", value, re.I):
        return value
    return ""


def _markdown_source_identity(markdown: str, identity: IdentityResolution) -> dict[str, Any]:
    """Read conservative identity/version signals from a reader response."""

    text = _text(markdown)
    lines = text.splitlines()
    identity_end = len(lines)
    for index, line in enumerate(lines[:500]):
        heading = re.sub(r"^\s{0,3}#+\s*", "", line).strip().casefold()
        if heading and re.match(r"^(references?|citing literature|related content|supporting information)\b", heading):
            identity_end = index
            break
    # Body citations are not paper identity. Use front matter and repeated
    # standalone DOI running headers from a publicly uploaded article.
    first_body = next((i for i, line in enumerate(lines) if re.match(r"^(introduction|background)\b", _reader_heading(line), re.I)), identity_end)
    identity_text = "\n".join(lines[:min(first_body, identity_end)])[:60000]
    identity_text = re.sub(r"Digital Object Identifier\s*\(DOI\)", " ", identity_text, flags=re.I)
    running_dois = [line.strip() for line in lines if _MARKDOWN_DOI_RE.fullmatch(line.strip())]
    identity_text += "\n" + "\n".join(doi for doi in dict.fromkeys(running_dois) if running_dois.count(doi) >= 2)
    head = text[:12000]
    dois: list[str] = []
    for raw in _MARKDOWN_DOI_RE.findall(identity_text):
        doi = normalize_doi(raw.rstrip(".,;:)]}"))
        if doi and doi not in dois:
            dois.append(doi)
    expected_doi = normalize_doi(identity.doi)
    source_doi = expected_doi if expected_doi and expected_doi in dois else (dois[0] if dois else "")
    title = ""
    for line in text.splitlines()[:160]:
        candidate = re.sub(r"^\s{0,3}#+\s*", "", line).strip()
        candidate = re.sub(r"!?(?:\[([^\]]+)\])\([^)]*\)", r"\1", candidate)
        is_section = re.match(r"^(abstract|introduction|background|methods?|materials?|experimental|results?|discussion|conclusions?|references?)\b", candidate, re.I)
        if line.lstrip().startswith("#") and len(candidate) >= 12 and not is_section:
            title = candidate
            break
    if not title:
        for line in lines[:min(first_body, 80)]:
            candidate = re.sub(r"!?(?:\[([^\]]+)\])\([^)]*\)", r"\1", line).strip(" -*")
            if 24 <= len(candidate) <= 300 and not candidate.casefold().startswith(("home", "menu", "doi:", "http", "#", "authors:", "abstract")):
                title = candidate
                break
    version_evidence = " ".join(text.splitlines()[:80]).casefold()
    version_label = ""
    if re.search(r"\bpreprint(?:\b|PDF)|author\s+(?:manuscript|upload)", version_evidence, re.I):
        version_label = "public_preprint_or_author_upload"
    elif source_doi:
        version_label = "public_source_version"
    restrictions = ""
    restriction_match = re.search(r"[^.]{0,100}(?:all rights reserved|no reuse|without permission|permission required|reuse is not permitted)[^.]{0,180}", text[:25000], flags=re.I)
    if restriction_match:
        restrictions = " ".join(restriction_match.group(0).split())[:280]
    if version_label == "public_preprint_or_author_upload" and len(dois) > 1 and running_dois:
        version_label = "author_upload_version_conflict_unknown"
    return {
        "title": title,
        "doi": source_doi,
        "year": next((_year(line) for line in lines[:500] if re.match(r"^(first published|published|publication date)\s*:", line.strip(), re.I)), ""),
        "authors": [],
        "source_dois": dois[:24],
        "version_label": version_label,
        "restriction": restrictions,
    }


def _markdown_to_jats(data: bytes, identity: IdentityResolution) -> tuple[bytes | None, dict[str, Any]]:
    """Project a reader's Markdown body while dropping page chrome and links."""

    markdown = data.decode("utf-8", errors="replace")
    source = _markdown_source_identity(markdown, identity)
    if not source.get("title") and not source.get("doi"):
        return None, {"reason": "reader_source_identity_missing", "chars": len(markdown)}
    lines = markdown.splitlines()
    body_section_re = re.compile(r"^(?:introduction|background|methods?|materials?(?: and methods?)?|experimental(?: procedures?)?|results?|discussion|conclusions?|limitations?)\b", re.I)
    start = next((i for i, line in enumerate(lines) if body_section_re.match(_reader_heading(line))), len(lines))
    stop = next((i for i in range(start + 1, len(lines)) if re.match(r"^(references?|citing literature|related content|recommended publications)\b", _reader_heading(lines[i]), re.I)), len(lines))
    numbered_layout = any(re.match(r"^\d+\\?\.\s+(Introduction|Methods)\b", line.strip(), re.I) for line in lines[start:stop])
    section_names: set[str] = set()
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            paragraph = " ".join(pending).strip()
            if len(paragraph) >= 20:
                current.append(paragraph)
            pending.clear()

    for line in lines[start:stop]:
        stripped = line.strip()
        heading = _reader_heading(line)
        if heading:
            flush()
            current = []
            sections.append((heading, current))
            match = body_section_re.match(heading)
            if match:
                section_names.add(match.group(0).lower())
            continue
        if not stripped:
            if not numbered_layout:
                flush()
            continue
        if (re.fullmatch(r"\d+ of \d+", stripped, re.I)
                or re.fullmatch(r"[\w .-]+ ET AL\.", stripped)
                or _MARKDOWN_DOI_RE.fullmatch(stripped)):
            continue
        if re.match(r"^[-*]?\s*!?\[[^\]]*\]\([^)]*\)$", stripped):
            continue
        clean = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", stripped)
        clean = re.sub(r"<?https?://[^\s>]+>?", "", clean).strip()
        if not clean:
            continue
        if re.fullmatch(r"\(\d+[a-z]?\)", clean):
            clean = "[Equation " + clean + " has no readable expression in this extraction]"
        pending.append(clean)
        if numbered_layout and (re.search(r"[.!?;:]$", clean) or sum(map(len, pending)) > 1000):
            flush()
    flush()
    paragraphs = [paragraph for _, items in sections for paragraph in items]
    body_text = "\n\n".join(paragraphs)
    section_signal_count = len(section_names)
    prose_paragraphs = [item for item in paragraphs if len(item.split()) >= 45]
    if len(body_text) < 1200 or len(prose_paragraphs) < 3 or section_signal_count < 2:
        return None, {"reason": "reader_body_not_sectioned", "chars": len(body_text), "paragraphs": len(paragraphs), "prose_paragraphs": len(prose_paragraphs), "section_signals": section_signal_count}
    title = xml_escape(_text(source.get("title") or identity.title or "Untitled"))
    doi = normalize_doi(source.get("doi"))
    source_id = f"<article-id pub-id-type=\"doi\">{xml_escape(doi)}</article-id>" if doi else ""
    body = "".join(f"<sec><title>{xml_escape(heading)}</title>" + "".join(f"<p>{xml_escape(item)}</p>" for item in items) + "</sec>" for heading, items in sections)
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><article><front><article-meta>{source_id}<title-group><article-title>{title}</article-title></title-group></article-meta></front><body>{body}</body></article>".encode()
    return xml, {
        "chars": len(body_text),
        "paragraphs": len(paragraphs),
        "source_identity": source,
        "identity_basis": "reader_markdown_source_signals",
        "section_count": len(sections),
        "known_losses": ["Reader text only: figure pixels, display equations, table relationships and supplemental file contents may be missing or damaged", "References and page wrappers are retained in the raw source, not parsed into the reading body", "PDF line wraps may have been joined; original reader bytes remain available"],
    }


def _abstract_snippet_jats(identity: IdentityResolution) -> tuple[bytes, str]:
    abstract = identity.abstract.strip()
    snippets: list[str] = []
    for item in identity.snippets:
        text = _text(item.get("text"))
        locator = item.get("snippet_locator") if isinstance(item.get("snippet_locator"), Mapping) else {}
        kind = _text(item.get("snippet_kind") or item.get("kind") or locator.get("snippet_kind") or locator.get("kind")).casefold()
        if text and kind not in {"title", "title_hit", "title_match"}:
            hit = xml_escape(_text(item.get("hit_id") or locator.get("hit_id") or locator.get("id")))
            origin = xml_escape(_text(item.get("retrieval_source") or item.get("source") or locator.get("retrieval_source") or "snippet"))
            locator_json = html.escape(json.dumps(dict(locator), ensure_ascii=False, sort_keys=True), quote=True) if locator else ""
            snippets.append(f"<p data-origin=\"{origin}\" data-hit-id=\"{hit}\" data-snippet-locator=\"{locator_json}\">{xml_escape(text)}</p>")
    body = "".join(snippets)
    abstract_xml = f"<abstract><p>{xml_escape(abstract)}</p></abstract>" if abstract else ""
    title = xml_escape(identity.title or "Untitled")
    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><article><front><article-meta><title-group><article-title>{title}</article-title></title-group>{abstract_xml}</article-meta></front>{'<body><sec><title>Retrieved snippets</title>'+body+'</sec></body>' if body else ''}</article>".encode()
    return xml, "abstract" if abstract else ("snippet" if snippets else "metadata_only")


def _safe_zip_assets(data: bytes, hrefs: Sequence[str]) -> list[dict[str, Any]]:
    import io
    import zipfile
    wanted = {Path(_text(item)).name.casefold() for item in hrefs if _text(item)}
    if not wanted or len(data) > MAX_PMC_ASSET_BYTES:
        return []
    out: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_PMC_ASSET_MEMBERS:
                return []
            total = 0
            for info in infos:
                member_name = info.filename
                safe_name = Path(member_name)
                if safe_name.is_absolute() or ".." in safe_name.parts:
                    continue
                total += int(info.file_size)
                if total > MAX_PMC_ASSET_UNCOMPRESSED:
                    break
                if safe_name.name.casefold() not in wanted or info.is_dir():
                    continue
                payload = bundle.read(info)
                if not payload:
                    continue
                # Decode the image before marking it readable. Magic bytes
                # alone would accept a truncated or non-image payload.
                readable = False
                try:
                    from PIL import Image
                    with Image.open(io.BytesIO(payload)) as image:
                        image.verify()
                    with Image.open(io.BytesIO(payload)) as image:
                        image.load()
                    readable = True
                except Exception:
                    readable = False
                out.append({"member": member_name, "name": safe_name.name, "data": payload, "image_readable": readable})
    except Exception:
        return []
    return out


class MaterialAcquirer:
    def __init__(
        self,
        output_root: str | Path,
        *,
        config: AcquisitionConfig | None = None,
        providers: Mapping[str, Any] | None = None,
        transport: Callable[..., tuple[int, Mapping[str, str], bytes]] | None = None,
        reader: Any | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.config = config or AcquisitionConfig()
        self.cache_root = Path(self.config.cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._transport = transport or self._default_transport
        self.providers = dict(providers or {})
        self.reader = reader
        self._grobid_init_lock = threading.Lock()
        self._load_default_providers()

    def _load_default_providers(self) -> None:
        factories = {
            "openalex": ("tools.academic_backends.openalex_backend", "OpenAlexBackend"),
            "semantic_scholar": ("tools.academic_backends.semantic_scholar_backend", "SemanticScholarBackend"),
            "crossref": ("tools.academic_backends.crossref_backend", "CrossrefBackend"),
            "unpaywall": ("tools.academic_backends.unpaywall_backend", "UnpaywallBackend"),
            "core": ("tools.academic_backends.core_backend", "CoreBackend"),
            "arxiv": ("tools.academic_backends.arxiv_backend", "ArxivBackend"),
        }
        for name, (module_name, class_name) in factories.items():
            if name in self.providers:
                continue
            try:
                module = __import__(module_name, fromlist=[class_name])
                self.providers[name] = getattr(module, class_name)()
            except Exception:
                self.providers[name] = None

    def _ensure_grobid(self) -> Any:
        """Create at most one parser backend for this acquirer."""

        if "grobid" in self.providers:
            return self.providers.get("grobid")
        with self._grobid_init_lock:
            if "grobid" not in self.providers:
                try:
                    from tools.academic_backends.grobid_backend import GrobidBackend
                    self.providers["grobid"] = GrobidBackend()
                except Exception:
                    self.providers["grobid"] = None
        return self.providers.get("grobid")

    @staticmethod
    def _default_transport(url: str, *, timeout: float = 20.0) -> tuple[int, Mapping[str, str], bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": "OptoMind-Local-Materials/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                chunks: list[bytes] = []
                total = 0
                cap = 80 * 1024 * 1024 + 1
                while total <= cap:
                    chunk = response.read(min(1024 * 1024, cap - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                headers = dict(response.headers.items())
                headers["x-optomind-resolved-url"] = redact_url(response.geturl())
                return int(getattr(response, "status", 200) or 200), headers, b"".join(chunks)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(min(1024 * 1024, 80 * 1024 * 1024 + 1))
            except Exception:
                body = b""
            headers = dict(exc.headers or {})
            headers["x-optomind-resolved-url"] = redact_url(exc.geturl())
            return int(exc.code), headers, body

    def _record_identity(self, record: Mapping[str, Any]) -> IdentityResolution:
        fields = provider_fields(record)
        canonical = _text(record.get("canonical_paper_id") or record.get("paper_id") or record.get("paperId") or fields.get("doi") or fields.get("title"))
        return IdentityResolution(
            canonical_paper_id=canonical,
            sample_id=_text(record.get("sample_id")),
            doi=fields["doi"], corpus_id=fields["corpus_id"], semantic_scholar_paper_id=fields["semantic_scholar_paper_id"], openalex_id=fields["openalex_id"], arxiv_id=fields["arxiv_id"], title=fields["title"], authors=tuple(fields["authors"]), year=fields["year"], abstract=fields["abstract"], snippets=tuple(item for item in (record.get("snippets") or ()) if isinstance(item, Mapping)), sources=("input",), provider_records=(fields["raw"],), status="verified" if (fields["doi"] or fields["arxiv_id"] or fields["openalex_id"] or fields["corpus_id"]) else "provisional",
        )

    def resolve_identity(self, record: Mapping[str, Any], *, deadline: float | None = None) -> IdentityResolution:
        current = self._record_identity(record)
        expected = current.as_mapping()
        records: list[Mapping[str, Any]] = [dict(record)]
        sources = list(current.sources)
        conflicts: list[str] = []
        provider_audit: list[dict[str, Any]] = []

        def consider(provider_name: str, candidate: Mapping[str, Any]) -> bool:
            nonlocal current, expected
            fields = provider_fields(candidate)
            comparison = match_identity(expected, fields)
            if not comparison["verified"]:
                conflicts.extend(f"{provider_name}:{item}" for item in comparison.get("conflicts", []))
                return False
            merged_sources = list(dict.fromkeys([*sources, provider_name]))
            current = IdentityResolution(
                canonical_paper_id=current.canonical_paper_id,
                sample_id=current.sample_id,
                doi=current.doi or fields["doi"], corpus_id=current.corpus_id or fields["corpus_id"], semantic_scholar_paper_id=current.semantic_scholar_paper_id or fields["semantic_scholar_paper_id"], openalex_id=current.openalex_id or fields["openalex_id"], arxiv_id=current.arxiv_id or fields["arxiv_id"], title=current.title or fields["title"], authors=current.authors or tuple(_authors(fields.get("authors"))), year=current.year or fields["year"], abstract=current.abstract or fields["abstract"], snippets=current.snippets, status="verified" if comparison["basis"] in {"doi", "provider_id", "arxiv_id", "openalex_id"} else "provisional", sources=tuple(merged_sources), conflicts=tuple(conflicts), provider_records=tuple([*records, dict(candidate)]), provider_audit=current.provider_audit,
            )
            expected = current.as_mapping()
            records.append(dict(candidate))
            sources[:] = merged_sources
            return True

        s2 = self.providers.get("semantic_scholar")
        handles: list[str] = []
        if current.corpus_id and current.corpus_id.isdigit(): handles.append("CorpusId:" + current.corpus_id)
        if current.semantic_scholar_paper_id and not current.semantic_scholar_paper_id.startswith("s2:"): handles.append(current.semantic_scholar_paper_id)
        if current.doi: handles.append("DOI:" + current.doi)
        if current.arxiv_id: handles.append("ARXIV:" + current.arxiv_id)
        for handle in handles:
            if deadline and time.monotonic() >= deadline: break
            audit = {"provider": "semantic_scholar", "operation": "get_paper", "handle_sha256": _sha256(handle.encode("utf-8"))[:16]}
            _reset_provider_status(s2)
            try:
                candidate = getattr(s2, "get_paper", lambda *_: None)(handle) if s2 is not None else None
            except Exception as exc:
                candidate = None
                audit.update({"status": "error", "error": type(exc).__name__})
            if isinstance(candidate, Mapping):
                # S2's normalized response exposes DOI/arXiv/PDF at the top
                # level but may omit the CorpusId used for the lookup. Carry
                # the queried handle only for this comparison; it is an
                # observed provider lookup key, not a formatter-generated
                # document identity.
                comparable = dict(candidate)
                if handle.casefold().startswith("corpusid:") and current.corpus_id:
                    comparable["corpus_id"] = current.corpus_id
                matched = consider("semantic_scholar", comparable)
                audit.update({"status": "matched" if matched else "identity_rejected", "has_record": True, "has_pdf": bool(comparable.get("pdf_url")), "has_arxiv": bool(provider_fields(comparable).get("arxiv_id"))})
                provider_audit.append(audit)
                if matched:
                    break
            else:
                audit.update(provider_status(s2, result=candidate))
                audit["has_record"] = False
                provider_audit.append(audit)

        openalex = self.providers.get("openalex")
        if openalex is not None and (current.openalex_id or current.doi):
            if not deadline or time.monotonic() < deadline:
                lookup = current.openalex_id or current.doi
                audit = {"provider": "openalex", "operation": "get_work", "lookup_sha256": _sha256(lookup.encode("utf-8"))[:16]}
                _reset_provider_status(openalex)
                try:
                    candidate = getattr(openalex, "get_work", lambda *_: None)(lookup)
                except Exception as exc:
                    candidate = None
                    audit.update({"status": "error", "error": type(exc).__name__})
                if isinstance(candidate, Mapping):
                    matched = consider("openalex", candidate)
                    audit.update({"status": "matched" if matched else "identity_rejected", "has_record": True, "has_content": bool(provider_fields(candidate).get("raw", {}).get("content_urls"))})
                    provider_audit.append(audit)
                else:
                    audit.update(provider_status(openalex, result=candidate))
                    audit["has_record"] = False
                    provider_audit.append(audit)

        # An exact provider handle already gives us a direct route (especially
        # arXiv without a DOI); do not spend the identity budget on broad title
        # searches before trying that handle's content.
        if not current.doi and current.title and not (current.arxiv_id or current.openalex_id or current.corpus_id or current.semantic_scholar_paper_id):
            for name in ("openalex", "crossref", "core", "arxiv"):
                provider = self.providers.get(name)
                if provider is None or (deadline and time.monotonic() >= deadline): continue
                search = getattr(provider, "search", None)
                if not callable(search): continue
                audit = {"provider": name, "operation": "search", "status": "started"}
                _reset_provider_status(provider)
                try:
                    rows = search(current.title, max_results=5)
                except Exception as exc:
                    rows = []
                    audit.update({"status": "error", "error": type(exc).__name__})
                for candidate in rows or []:
                    if isinstance(candidate, Mapping) and consider(name, candidate):
                        audit.update({"status": "matched", "rows": len(rows or [])})
                        break
                else:
                    if audit.get("status") != "error":
                        audit.update(provider_status(provider, result=rows))
                    audit["rows"] = len(rows or [])
                provider_audit.append(audit)
                if current.doi: break

        current.conflicts = tuple(conflicts)
        current.provider_audit = tuple(provider_audit)
        if conflicts and current.status == "provisional":
            current.status = "conflict"
        return current

    def _cache_key(self, url: str) -> str:
        return _sha256(redact_url(url).encode("utf-8"))[:32]

    def _fetch(self, url: str, *, route: str, deadline: float) -> tuple[bytes | None, bytes, dict[str, str], str]:
        if time.monotonic() >= deadline:
            return None, b"", {}, "deadline"
        cache_dir = self.cache_root / "blobs"
        meta_dir = self.cache_root / "meta"
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)
        cache_key = self._cache_key(url)
        meta_path, blob_path = meta_dir / f"{cache_key}.json", cache_dir / f"{cache_key}.bin"
        if not self.config.refresh and meta_path.is_file() and blob_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                wire = blob_path.read_bytes()
                if len(wire) == int(meta.get("bytes", -1)) and _sha256(wire) == meta.get("wire_sha256"):
                    return wire, wire, {"content-type": _text(meta.get("content_type")), "x-optomind-resolved-url": _text(meta.get("resolved_url"))}, "cache"
            except Exception:
                pass
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.casefold() == "https" and parsed.netloc.casefold() == "content.openalex.org":
            try:
                from tools.academic_backends.openalex_content import fetch_openalex_content, is_openalex_content_url
                if not is_openalex_content_url(url): return None, b"", {}, "openalex_url_rejected"
                body, error = fetch_openalex_content(url, timeout=min(self.config.request_timeout_seconds, max(0.5, deadline - time.monotonic())))
                if body is None: return None, b"", {}, error or "openalex_content_failed"
                raw = bytes(body); headers = {"content-type": ""}
            except Exception as exc:
                return None, b"", {}, f"openalex_content_{type(exc).__name__}"
        else:
            status = 0
            headers_raw: Mapping[str, str] = {}
            raw = b""
            last_reason = "transport_failed"
            for attempt in range(self.config.max_retries + 1):
                if time.monotonic() >= deadline:
                    return None, b"", {}, "deadline"
                try:
                    status, headers_raw, raw = self._transport(url, timeout=min(self.config.request_timeout_seconds, max(0.5, deadline - time.monotonic())))
                except TypeError:
                    status, headers_raw, raw = self._transport(url)
                except Exception as exc:
                    last_reason = f"transport_{type(exc).__name__}"
                    if attempt >= self.config.max_retries:
                        return None, b"", {}, last_reason
                    time.sleep(min(0.5 * (2 ** attempt), max(0.0, deadline - time.monotonic())))
                    continue
                normalized_headers = {str(k).lower(): str(v) for k, v in (headers_raw or {}).items()}
                if int(status or 0) == 200 and raw:
                    break
                last_reason = f"http_{int(status or 0)}"
                if int(status or 0) not in {408, 425, 429, 500, 502, 503, 504} or attempt >= self.config.max_retries:
                    return None, bytes(raw or b""), normalized_headers, last_reason
                time.sleep(min(0.5 * (2 ** attempt), max(0.0, deadline - time.monotonic())))
            if int(status or 0) != 200 or not raw:
                return None, bytes(raw or b""), {str(k).lower(): str(v) for k, v in (headers_raw or {}).items()}, last_reason
            headers = {str(k).lower(): str(v) for k, v in (headers_raw or {}).items()}
        if len(raw) > self.config.max_body_bytes:
            return None, raw, headers, "body_too_large"
        try:
            cache_meta = {"route": route, "url": redact_url(url), "resolved_url": headers.get("x-optomind-resolved-url", ""), "url_sha256": _sha256(redact_url(url).encode()), "bytes": len(raw), "wire_sha256": _sha256(raw), "content_type": headers.get("content-type", ""), "saved_at": time.time()}
            temporary = blob_path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}")
            temporary.write_bytes(raw); os.replace(temporary, blob_path)
            meta_path.write_text(json.dumps(cache_meta, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        return raw, raw, headers, "downloaded"

    def _initial_routes(self, record: Mapping[str, Any], identity: IdentityResolution) -> list[_Route]:
        routes: list[_Route] = []
        seen: set[str] = set()
        def add(name: str, url: Any, kind: str, source: str, priority: int, metadata: Mapping[str, Any] | None = None, direct: bool = False) -> None:
            text = _text(url)
            if not text or text in seen or not urllib.parse.urlsplit(text).scheme.casefold() in {"http", "https"}: return
            if kind == "pdf":
                source_key = source.casefold()
                oa_evidence = any(bool((metadata or {}).get(key)) for key in ("is_oa", "isOpenAccess", "open_access", "open_access_status", "oa_status", "license", "oa"))
                trusted_oa_source = any(token in source_key for token in ("openalex", "semantic_scholar", "unpaywall", "europepmc", "core", "arxiv", "local"))
                if not oa_evidence and not trusted_oa_source:
                    return
            seen.add(text); routes.append(_Route(name, text, kind, source, dict(metadata or {}), priority, direct))
        provider_mappings = [item for item in identity.provider_records if isinstance(item, Mapping)]
        for mapping in [record, *provider_mappings, *list(_iter_mappings(record))]:
            for key in ("jats_url", "fulltext_xml_url", "fullTextXML", "tei_url", "grobid_xml"):
                route_metadata = {**mapping, "identity_source_required": True}
                add(key, mapping.get(key), "jats" if "jats" in key.lower() or "fulltext" in key.lower() else "tei", "record", 10, route_metadata, True)
            for key in ("html_url", "landing_page_url", "source_url"):
                url = mapping.get(key)
                if url and not str(url).lower().endswith(".pdf"): add("html", url, "html", "record", 90, {**mapping, "identity_source_required": True}, True)
            for key in ("pdf_url", "open_access_url", "open_access_pdf", "best_oa_url", "url_for_pdf", "download_url", "downloadUrl"):
                add(key, mapping.get(key), "pdf", str(mapping.get("backend") or mapping.get("source") or "record"), 50, {**mapping, "identity_source_required": True}, True)
            content = mapping.get("content_urls") if isinstance(mapping.get("content_urls"), Mapping) else {}
            add("openalex_content_jats", content.get("jats") or content.get("jats_xml"), "jats", "openalex_content", 5, mapping)
            add("openalex_content_tei", content.get("grobid_xml") or content.get("tei"), "tei", "openalex_content", 12, mapping)
            add("openalex_content_pdf", content.get("pdf"), "pdf", "openalex_content", 48, mapping)
            best = mapping.get("best_oa_location") if isinstance(mapping.get("best_oa_location"), Mapping) else {}
            add("openalex_best_oa_pdf", best.get("pdf_url") or best.get("url_for_pdf"), "pdf", "openalex", 50, mapping)
            for loc in mapping.get("locations") or mapping.get("oa_locations") or ():
                if isinstance(loc, Mapping):
                    add("openalex_location_pdf", loc.get("pdf_url") or loc.get("url_for_pdf"), "pdf", "openalex", 55, {**mapping, **loc})
                    add("openalex_location_landing", loc.get("landing_page_url") or loc.get("url"), "html", "openalex", 65, {**mapping, **loc})
        # Provider adapters may expose additional repositories or publisher
        # landing pages.  Keep this list exact and small; the route attempt
        # still has to validate the returned source identity and body.
        if self.config.rescue_enabled and self.config.rescue_max_candidates:
            for candidate in collect_public_candidates(
                identity.as_mapping(), identity.provider_records,
                max_candidates=self.config.rescue_max_candidates,
            ):
                metadata = dict(identity.as_mapping())
                metadata.update(candidate.as_route_metadata())
                kind = "jats" if candidate.kind in {"jats", "tei"} else candidate.kind
                add(
                    "public_rescue_" + candidate.kind,
                    candidate.url,
                    kind,
                    "public_rescue:" + candidate.provider,
                    candidate.priority,
                    metadata,
                )
        if identity.arxiv_id:
            add("arxiv_pdf", f"https://arxiv.org/pdf/{identity.arxiv_id}.pdf", "pdf", "arxiv", 75, {"arxiv_id": identity.arxiv_id})
        routes.sort(key=lambda route: (route.priority, route.name, route.url))
        return routes

    def _html_link_routes(self, parent: _Route, audit: Mapping[str, Any], identity: IdentityResolution) -> list[_Route]:
        discovered = audit.get("discovered_links") if isinstance(audit, Mapping) else ()
        if not isinstance(discovered, Sequence) or isinstance(discovered, (str, bytes, bytearray)):
            return []
        routes: list[_Route] = []
        for item in discovered:
            if not isinstance(item, Mapping):
                continue
            url = _text(item.get("url"))
            kind = _text(item.get("kind")).casefold()
            if not url or kind not in {"jats", "pdf"}:
                continue
            metadata = identity.as_mapping()
            metadata.update({"is_oa": True, "discovered_from": redact_url(parent.url), "discovered_label": _text(item.get("label"))})
            routes.append(_Route(
                "html_link_jats" if kind == "jats" else "html_link_pdf",
                url,
                kind,
                "html_link",
                metadata,
                14 if kind == "jats" else 38,
                True,
            ))
        return routes

    def _europepmc_routes(self, identity: IdentityResolution, deadline: float) -> tuple[list[_Route], str]:
        if not identity.title and not identity.doi: return [], ""
        term = f'DOI:"{identity.doi}"' if identity.doi else f'TITLE:"{identity.title[:160]}"'
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode({"query": term, "format": "json", "resultType": "core", "pageSize": 5})
        raw, _, _, reason = self._fetch(url, route="europepmc_search", deadline=deadline)
        if not raw: return [], reason
        try: payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception: return [], "invalid_json"
        routes: list[_Route] = []
        for row in (((payload.get("resultList") or {}).get("result")) or []):
            if not isinstance(row, Mapping): continue
            candidate = {"doi": row.get("doi"), "title": row.get("title"), "authors": row.get("authorList", {}).get("author", []) if isinstance(row.get("authorList"), Mapping) else [], "year": row.get("pubYear"), "pmcid": row.get("pmcid")}
            comparison = match_identity(identity.as_mapping(), candidate)
            if not comparison["verified"]: continue
            pmcid = _text(row.get("pmcid"))
            if pmcid:
                xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                routes.append(_Route("europepmc_jats", xml_url, "jats", "europepmc", {**candidate, "pmcid": pmcid}, 8, True))
                routes.append(_Route("europepmc_assets", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles", "pmc_assets", "europepmc", {**candidate, "pmcid": pmcid}, 9, True))
            for item in ((row.get("fullTextUrlList") or {}).get("fullTextUrl") or []) if isinstance(row.get("fullTextUrlList"), Mapping) else []:
                if isinstance(item, Mapping) and _text(item.get("url")) and _text(item.get("documentStyle")).casefold() == "pdf":
                    routes.append(_Route("europepmc_pdf", _text(item.get("url")), "pdf", "europepmc", candidate, 58, True))
            if routes: return routes, ""
        return [], "europepmc_identity_not_found"

    def _provider_rescue_routes(self, identity: IdentityResolution, deadline: float, *, only: str | None = None) -> list[_Route]:
        routes: list[_Route] = []
        if identity.doi and (only is None or only == "unpaywall"):
            provider = self.providers.get("unpaywall")
            if provider is not None and time.monotonic() < deadline:
                _reset_provider_status(provider)
                try:
                    data = provider.lookup(identity.doi) or {}
                except Exception as exc:
                    data = {}
                    try:
                        provider.last_error = type(exc).__name__
                        provider.last_status = "provider_error"
                    except Exception:
                        pass
                if isinstance(data, Mapping):
                    for item in [data, *(data.get("oa_locations") or [])]:
                        if isinstance(item, Mapping):
                            metadata = {**identity.as_mapping(), **data, **item, "identity_source_required": True}
                            pdf_url = item.get("url_for_pdf") or item.get("best_oa_url")
                            landing_url = item.get("landing_page_url") or item.get("url")
                            if pdf_url:
                                routes.append(_Route("unpaywall_pdf", _text(pdf_url), "pdf", "unpaywall", metadata, 60))
                            if landing_url and _text(landing_url) != _text(pdf_url):
                                routes.append(_Route("unpaywall_landing", _text(landing_url), "html", "unpaywall", metadata, 66))
        core = self.providers.get("core")
        if (only is None or only == "core") and core is not None and identity.title and time.monotonic() < deadline:
            _reset_provider_status(core)
            try:
                rows = core.search(identity.title, max_results=8) or []
            except Exception as exc:
                rows = []
                try:
                    core.last_error = type(exc).__name__
                    core.last_status = "provider_error"
                except Exception:
                    pass
            for row in rows:
                if not isinstance(row, Mapping) or not match_identity(identity.as_mapping(), row)["verified"]: continue
                # Newer CORE adapters retain the verified inline fullText and
                # structured fulltext_routes fields. Consume those before a
                # download URL so content is not lost by generic normalization.
                inline = row.get("fullText") or row.get("fulltext") or row.get("full_text")
                if isinstance(inline, (str, bytes)) and inline:
                    routes.append(_Route("core_fulltext", "", "core_text", "core", {**row, "_fulltext": inline}, 62))
                route_rows = row.get("fulltext_routes") or row.get("fullTextRoutes") or ()
                if isinstance(route_rows, Mapping): route_rows = [route_rows]
                for route_row in route_rows:
                    if not isinstance(route_row, Mapping): continue
                    url = route_row.get("url") or route_row.get("download_url") or route_row.get("downloadUrl")
                    if url: routes.append(_Route("core_pdf", _text(url), "pdf", "core", {**identity.as_mapping(), **row, **route_row, "is_oa": True, "identity_source_required": True}, 65))
                for key in ("download_url", "downloadUrl", "fulltext_url", "source_url", "url_or_doi"):
                    if row.get(key): routes.append(_Route("core_pdf", _text(row[key]), "pdf", "core", {**identity.as_mapping(), **row, "is_oa": True, "identity_source_required": True}, 65)); break
        arxiv = self.providers.get("arxiv")
        if (only is None or only == "arxiv") and arxiv is not None and identity.title and not identity.arxiv_id and time.monotonic() < deadline:
            _reset_provider_status(arxiv)
            try:
                rows = arxiv.search(identity.title, max_results=5) or []
            except Exception as exc:
                rows = []
                try:
                    arxiv.last_error = type(exc).__name__
                    arxiv.last_status = "provider_error"
                except Exception:
                    pass
            for row in rows:
                if isinstance(row, Mapping) and match_identity(identity.as_mapping(), row)["verified"]:
                    aid = _text(row.get("arxiv_id")); url = _text(row.get("pdf_url")) or (f"https://arxiv.org/pdf/{aid}.pdf" if aid else "")
                    if url: routes.append(_Route("arxiv_pdf", url, "pdf", "arxiv", {**identity.as_mapping(), **row, "identity_source_required": True}, 75))
        return routes

    def _reader_rescue_routes(
        self,
        identity: IdentityResolution,
        deadline: float,
        *,
        tried_urls: set[str] | None = None,
        redirect_records: Sequence[Mapping[str, Any]] = (),
        validate_route: Callable[[_Route], tuple[Artifact | None, dict[str, Any], list[dict[str, Any]]]] | None = None,
    ) -> tuple[list[_Route], list[dict[str, Any]], list[tuple[_Route, Artifact | None, dict[str, Any], list[dict[str, Any]]]]]:
        """Use an injected public reader under explicit per-paper budgets."""

        reader = self.reader
        audits: list[dict[str, Any]] = []
        validated: list[tuple[_Route, Artifact | None, dict[str, Any], list[dict[str, Any]]]] = []
        if reader is None or time.monotonic() >= deadline:
            return [], audits, validated
        search_rows: list[Mapping[str, Any]] = []
        if self.config.rescue_max_search_calls > 0 and time.monotonic() < deadline:
            search = getattr(reader, "search", None)
            queries = [f'"{identity.title[:180]}"'] if identity.title else []
            if identity.doi:
                queries.append(f'DOI:"{identity.doi}"')
            for query in queries[: self.config.rescue_max_search_calls]:
                if time.monotonic() >= deadline:
                    break
                _reset_provider_status(reader)
                try:
                    if callable(search):
                        rows = list(search(query, max_results=min(8, self.config.rescue_max_candidates), deadline=deadline) or [])
                    else:
                        rows = []
                    search_rows.extend(item for item in rows if isinstance(item, Mapping))
                    status = "ok" if rows else ("provider_error" if getattr(reader, "last_error", "") else "nohit")
                    audits.append({"route": "reader_search", "provider": "firecrawl", "status": status, "rows": len(rows), "reason": _text(getattr(reader, "last_error", "")), "query_sha256": _sha256(query.encode("utf-8"))[:16]})
                    if rows:
                        break
                except Exception as exc:
                    audits.append({"route": "reader_search", "provider": "firecrawl", "status": "provider_error", "error": type(exc).__name__, "query_sha256": _sha256(query.encode("utf-8"))[:16]})

        records = [dict(item) for item in search_rows if isinstance(item, Mapping)]
        if records:
            search_dir = self.cache_root / "reader_searches"
            search_dir.mkdir(parents=True, exist_ok=True)
            search_bytes = json.dumps(_safe_observed(records), ensure_ascii=False).encode("utf-8")
            search_path = search_dir / (_sha256(search_bytes) + ".json")
            search_path.write_bytes(search_bytes)
            audits.append({"route": "reader_search_saved", "path": str(search_path), "sha256": _sha256(search_bytes)})
        candidates = collect_public_candidates(identity.as_mapping(), [*redirect_records, *records, *identity.provider_records], max_candidates=self.config.rescue_max_candidates)
        routes: list[_Route] = []
        extracted = 0
        fetch = getattr(reader, "fetch_fulltext", None)
        for candidate in candidates:
            if extracted >= self.config.rescue_max_extract_calls or time.monotonic() >= deadline:
                break
            if candidate.kind not in {"html", "jats", "tei"} or not callable(fetch):
                continue
            try:
                markdown = fetch(candidate.url, method="firecrawl", deadline=deadline)
            except Exception as exc:
                audits.append({"route": "reader_extract", "provider": "firecrawl", "status": "provider_error", "error": type(exc).__name__, "url_sha256": candidate.url_sha256})
                extracted += 1
                continue
            extracted += 1
            if not isinstance(markdown, str) or not markdown.strip():
                errors = getattr(reader, "last_fulltext_errors", {}) or {}
                reason = _text(errors.get(candidate.url))[:240] if isinstance(errors, Mapping) else ""
                reason = reason or _text(getattr(reader, "last_error", ""))[:240]
                provider_failed = bool(reason) or _text(getattr(reader, "last_status", "")).casefold() in {"error", "failed", "provider_error"}
                audits.append({"route": "reader_extract", "provider": "firecrawl", "status": "provider_error" if provider_failed else "nohit", "reason": reason or "empty_response", "url_sha256": candidate.url_sha256})
                continue
            metadata = identity.as_mapping()
            metadata.update(candidate.as_route_metadata())
            metadata.update({
                "reader_provider": "firecrawl",
                "_fulltext": markdown,
                "reader_response_sha256": _sha256(markdown.encode("utf-8")),
            })
            route = _Route("reader_firecrawl", candidate.url, "reader_text", "firecrawl", metadata, 18)
            raw_bytes = markdown.encode("utf-8")
            response_dir = self.cache_root / "reader_responses"
            response_dir.mkdir(parents=True, exist_ok=True)
            response_path = response_dir / (_sha256(raw_bytes) + ".md")
            response_path.write_bytes(raw_bytes)
            audits.append({"route": "reader_response_saved", "source_url": redact_url(candidate.url), "path": str(response_path), "sha256": _sha256(raw_bytes)})
            audits.append({"route": "reader_extract", "provider": "firecrawl", "status": "fetched", "bytes": len(markdown.encode("utf-8")), "content_sha256": _sha256(markdown.encode("utf-8")), "url_sha256": candidate.url_sha256})
            if validate_route is None:
                routes.append(route)
                continue
            try:
                artifact, route_audit, gaps = validate_route(route)
            except Exception as exc:
                artifact, route_audit, gaps = None, {"route": route.name, "status": "failed", "reason": f"validation_{type(exc).__name__}"}, []
            validated.append((route, artifact, route_audit, gaps))
            # Validate immediately so an unusable shell does not consume the
            # remaining reader extraction budget before a usable candidate.
            if artifact is not None:
                break
        return routes, audits, validated

    def _attach_pmc_assets(self, artifact: Artifact, pmcid: str, result: AcquisitionResult, deadline: float) -> None:
        if not pmcid or artifact.kind != "jats" or time.monotonic() >= deadline:
            return
        hrefs: list[str] = []
        try:
            root = _secure_snapshot_xml(artifact.data)
            for node in root.iter():
                local = str(node.tag).rsplit("}", 1)[-1].casefold() if isinstance(node.tag, str) else ""
                if local in {"graphic", "inline-graphic", "media"}:
                    href = _text(node.attrib.get("{http://www.w3.org/1999/xlink}href") or node.attrib.get("href") or node.attrib.get("url"))
                    if href: hrefs.append(href)
        except Exception:
            return
        if not hrefs:
            return
        url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid, safe='')}/supplementaryFiles"
        wire, _, headers, reason = self._fetch(url, route="europepmc_assets", deadline=deadline)
        if not wire:
            result.known_gaps.append({"kind": "assets", "status": "missing", "reason": "supplementaryFiles_fetch_" + reason})
            return
        if not wire.startswith(b"PK"):
            result.known_gaps.append({"kind": "assets", "status": "unparsed", "reason": "supplementaryFiles_not_zip"})
            return
        members = _safe_zip_assets(wire, hrefs)
        result.artifacts.append(Artifact(kind="pmc_assets_zip", data=wire, route="europepmc_assets", source_url=url, content_type=headers.get("content-type", "application/zip"), source_name=f"{pmcid}.supplementary.zip", metadata={"legal_use_status": "oa"}))
        matched = {item["name"].casefold() for item in members}
        for member in members:
            result.artifacts.append(Artifact(kind="figure_image", data=member["data"], route="europepmc_assets", source_url=url, content_type="image/*", source_name=member["name"], metadata={"asset_href": member["name"], "asset_member": member["member"], "image_readable": bool(member["image_readable"]), "legal_use_status": "oa"}))
        missing = [Path(href).name for href in hrefs if Path(href).name.casefold() not in matched]
        if missing:
            result.known_gaps.append({"kind": "assets", "status": "partial", "reason": "graphic_members_unmatched", "missing": missing[:20]})
        if not members:
            result.known_gaps.append({"kind": "assets", "status": "unparsed", "reason": "supplementary_bundle_had_no_safe_matching_images"})

    @staticmethod
    def _attach_route_provenance(artifact: Artifact, route: _Route) -> None:
        """Carry provider/version facts into the immutable source manifest."""

        metadata = route.metadata if isinstance(route.metadata, Mapping) else {}
        provenance = {
            "provider": _text(metadata.get("candidate_provider") or metadata.get("provider") or route.source),
            "discovered_from": _text(metadata.get("discovered_from") or metadata.get("location_field")),
            "publication_version": _text(
                metadata.get("publication_version")
                or metadata.get("source_version")
                or metadata.get("version_id")
            ),
            "version_label": _text(metadata.get("version_label") or metadata.get("version")),
            "version_kind": _text(metadata.get("version_kind") or metadata.get("host_type")),
            "source_url": redact_url(route.url),
        }
        source_dois = metadata.get("source_dois")
        if isinstance(source_dois, (list, tuple)):
            provenance["source_dois"] = [normalize_doi(item) for item in source_dois if normalize_doi(item)][:24]
        if metadata.get("source_restriction"):
            provenance["source_restriction"] = _text(metadata.get("source_restriction"))[:280]
        artifact.metadata["source_provenance"] = {key: value for key, value in provenance.items() if value}
        if provenance.get("publication_version"):
            artifact.metadata["publication_version"] = provenance["publication_version"]
        elif provenance.get("version_label"):
            artifact.metadata["publication_version"] = provenance["version_label"]

    def _route_attempt(self, route: _Route, identity: IdentityResolution, deadline: float) -> tuple[Artifact | None, dict[str, Any], list[dict[str, Any]]]:
        audit = {"route": route.name, "source": route.source, "url_sha256": _sha256(redact_url(route.url).encode())[:16], "kind": route.kind}
        comparison = match_identity(identity.as_mapping(), provider_fields(route.metadata)) if route.metadata else {"verified": True, "state": "provisional", "basis": "input"}
        if not comparison.get("verified") and route.kind != "reader_text":
            audit.update({"status": "rejected", "reason": "identity_mismatch", "identity": comparison}); return None, audit, []
        if route.kind in {"core_text", "reader_text"}:
            supplied = route.metadata.get("_fulltext")
            wire = supplied.encode("utf-8") if isinstance(supplied, str) else bytes(supplied or b"")
            headers = {"content-type": "text/xml" if wire.lstrip().startswith(b"<") else "text/plain"}
            reason = "provider_fulltext" if route.kind == "core_text" else "reader_response"
        else:
            wire, _, headers, reason = self._fetch(route.url, route=route.name, deadline=deadline)
        audit.update({"status": "failed" if wire is None else "fetched", "reason": reason, "bytes": len(wire or b""), "content_type": headers.get("content-type", "")})
        resolved_url = headers.get("x-optomind-resolved-url", "")
        if resolved_url and resolved_url != redact_url(route.url):
            audit["resolved_url"] = resolved_url
        if wire is None: return None, audit, []
        decoded = wire; compressed = False
        if wire[:2] == b"\x1f\x8b" or "gzip" in headers.get("content-type", "").casefold():
            try:
                import gzip
                decoded = gzip.decompress(wire); compressed = True
            except Exception:
                audit.update({"status": "rejected", "reason": "gzip_invalid"}); return None, audit, []
        kind = _xml_kind(decoded)
        if route.kind in {"jats", "tei"} and not kind:
            audit.update({"status": "rejected", "reason": "structured_xml_invalid"}); return None, audit, []
        if kind:
            if not _xml_has_research_body(decoded):
                audit.update({"status": "partial", "reason": "structured_xml_without_research_body"})
                return None, audit, [{"kind": "coverage", "status": "partial", "reason": "structured source contained metadata or abstract without a research body"}]
            observed = _parse_xml_identity(decoded)
            if route.metadata.get("identity_source_required") and not observed:
                audit.update({"status": "rejected", "reason": "source_identity_missing"})
                return None, audit, [{"kind": "identity", "status": "missing", "reason": "public source did not expose independently readable identity metadata"}]
            xml_match = match_identity(identity.as_mapping(), observed)
            if not xml_match["verified"] and observed:
                if route.metadata.get("allow_version_variant"):
                    xml_match = _match_version_variant(identity.as_mapping(), observed, xml_match) or xml_match
            if not xml_match["verified"] and observed:
                audit.update({"status": "rejected", "reason": "xml_identity_mismatch", "identity": xml_match}); return None, audit, []
            artifact = Artifact(kind=kind, data=decoded, route=route.name, source_url=route.url, content_type=headers.get("content-type", "application/xml"), wire_bytes=wire, source_name=f"{route.name}.{'jats.xml' if kind == 'jats' else 'tei.xml'}", metadata={"compressed": compressed, "identity": xml_match, "identity_verified": bool(xml_match.get("verified")), "legal_use_status": "oa"})
            self._attach_route_provenance(artifact, route)
            return artifact, audit | {"status": "usable", "content_kind": kind}, []
        if decoded.startswith(b"%PDF"):
            if not _pdf_is_readable(decoded):
                audit.update({"status": "rejected", "reason": "pdf_unreadable"})
                return None, audit, [{"kind": "pdf", "status": "partial", "reason": "PDF format or page structure was unreadable"}]
            artifact, gaps = self._parse_pdf(decoded, route, identity, deadline, headers)
            if artifact is None:
                audit.update({"status": "partial", "reason": "pdf_parse_unavailable"})
                return None, audit, gaps
            observed = _parse_xml_identity(artifact.data) if artifact.kind in {"tei", "fitz_xml"} else {}
            if route.metadata.get("identity_source_required"):
                if artifact.metadata.get("synthetic_identity") or not observed:
                    audit.update({"status": "rejected", "reason": "source_identity_missing"})
                    return None, audit, [{"kind": "identity", "status": "missing", "reason": "public PDF parser did not expose independently readable identity metadata"}]
                pdf_match = match_identity(identity.as_mapping(), observed)
                if not pdf_match.get("verified") and route.metadata.get("allow_version_variant"):
                    pdf_match = _match_version_variant(identity.as_mapping(), observed, pdf_match) or pdf_match
                if not pdf_match.get("verified"):
                    audit.update({"status": "rejected", "reason": "pdf_identity_mismatch", "identity": pdf_match})
                    return None, audit, []
                artifact.metadata["identity"] = pdf_match
                artifact.metadata["identity_verified"] = True
            artifact.wire_bytes = wire
            self._attach_route_provenance(artifact, route)
            audit.update({"status": "usable", "content_kind": artifact.kind, "parser": artifact.metadata.get("parser", "")})
            return artifact, audit, gaps
        if b"<html" in decoded[:8192].lower() or b"<!doctype html" in decoded[:8192].lower():
            generated, info = _html_to_jats(decoded, identity, route.url)
            if info.get("discovered_links"):
                audit["discovered_links"] = list(info["discovered_links"])
            if generated is None:
                audit.update({"status": "rejected", "reason": info.get("reason", "html_not_full_article")}); return None, audit, [{"kind": "html", "status": "partial", "reason": info.get("reason", "html_not_full_article")}]
            source_identity = info.get("source_identity") if isinstance(info.get("source_identity"), Mapping) else {}
            html_match = match_identity(identity.as_mapping(), source_identity) if source_identity else {"verified": False, "state": "unresolved", "basis": "missing_source_identity"}
            if source_identity and not html_match.get("verified"):
                audit.update({"status": "rejected", "reason": "html_identity_mismatch", "identity": html_match})
                return None, audit, [{"kind": "identity", "status": "mismatch", "reason": "public HTML identity did not match the requested paper"}]
            artifact = Artifact(kind="html_jats", data=generated, route=route.name, source_url=route.url, content_type="application/xml", wire_bytes=wire, derived_from=route.name, source_name="html-derived.xml", metadata={"html_info": info, "identity": html_match, "identity_verified": bool(html_match.get("verified")), "identity_basis": "html_source_metadata", "legal_use_status": "oa", "synthetic_identity": False})
            self._attach_route_provenance(artifact, route)
            route_audit = audit | {"status": "usable", "content_kind": "html_jats"}
            if info.get("discovered_links"):
                route_audit["defer_artifact"] = True
            return artifact, route_audit, [{"kind": "html", "status": "derived", "reason": "body converted to structured material; headings/tables/math/image pixels may be lost", "known_losses": info.get("known_losses", [])}]
        if route.kind in {"core_text", "reader_text"}:
            if route.kind == "reader_text":
                generated, info = _markdown_to_jats(decoded, identity)
                source_identity = info.get("source_identity") if isinstance(info.get("source_identity"), Mapping) else {}
                reader_match = match_identity(identity.as_mapping(), source_identity) if source_identity else {"verified": False, "state": "unresolved", "basis": "missing_source_identity"}
                if generated is not None and not reader_match.get("verified"):
                    if route.metadata.get("allow_version_variant"):
                        reader_match = _match_version_variant(identity.as_mapping(), source_identity, reader_match) or reader_match
                if generated is None:
                    audit.update({"status": "rejected", "reason": info.get("reason", "reader_body_invalid")})
                    return None, audit, [{"kind": "reader", "status": "partial", "reason": info.get("reason", "reader_body_invalid")}]
                if not reader_match.get("verified"):
                    audit.update({"status": "rejected", "reason": "reader_identity_mismatch", "identity": reader_match})
                    return None, audit, [{"kind": "identity", "status": "mismatch", "reason": "reader response did not match requested paper"}]
            else:
                generated, info = _plain_text_to_jats(decoded, identity)
                reader_match = {"verified": True, "basis": "provider_record"}
            if generated is not None:
                kind = "reader_text_jats" if route.kind == "reader_text" else "core_text_jats"
                artifact = Artifact(kind=kind, data=generated, route=route.name, source_url=route.url, content_type="application/xml", wire_bytes=wire, derived_from=route.name, source_name=("reader-response.md.xml" if route.kind == "reader_text" else "core-fulltext-derived.xml"), metadata={"core_info": info, "reader_info": info if route.kind == "reader_text" else {}, "identity": reader_match, "identity_verified": bool(reader_match.get("verified")), "identity_basis": info.get("identity_basis", "reader_markdown_source_signals" if route.kind == "reader_text" else "provider_fulltext"), "legal_use_status": "oa", "synthetic_identity": route.kind != "reader_text"})
                self._attach_route_provenance(artifact, route)
                if route.kind == "reader_text":
                    artifact.metadata["legal_use_status"] = "unknown"
                    source = info.get("source_identity") if isinstance(info.get("source_identity"), Mapping) else {}
                    provenance = artifact.metadata.setdefault("source_provenance", {})
                    provenance["source_dois"] = list(source.get("source_dois") or ())[:24]
                    if source.get("version_label"):
                        provenance["version_label"] = _text(source.get("version_label"))
                        artifact.metadata.setdefault("publication_version", _text(source.get("version_label")))
                    if source.get("restriction"):
                        provenance["source_restriction"] = _text(source.get("restriction"))[:280]
                return artifact, audit | {"status": "usable", "content_kind": kind}, [{"kind": route.kind, "status": "derived", "reason": "public reader response projected to structured material" if route.kind == "reader_text" else "provider fullText projected to structured material", "known_losses": info.get("known_losses", [])}]
        audit.update({"status": "rejected", "reason": "unrecognised_content"})
        return None, audit, []

    def _parse_pdf(self, pdf_bytes: bytes, route: _Route, identity: IdentityResolution, deadline: float, headers: Mapping[str, str]) -> tuple[Artifact | None, list[dict[str, Any]]]:
        if not _pdf_is_readable(pdf_bytes):
            return None, [{"kind": "pdf", "status": "partial", "reason": "PDF format or page structure was unreadable"}]
        pdf_artifact = Artifact(kind="pdf", data=pdf_bytes, route=route.name, source_url=route.url, content_type=headers.get("content-type", "application/pdf"), source_name=f"{route.name}.pdf", metadata={"legal_use_status": "oa"})
        pdf_path = self.cache_root / "pdf_work" / f"{_sha256(pdf_bytes)}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        if not pdf_path.exists(): pdf_path.write_bytes(pdf_bytes)
        grobid = self._ensure_grobid()
        if grobid is not None and time.monotonic() < deadline:
            parser = getattr(grobid, "parse_pdf", None)
            try:
                parsed = parser(str(pdf_path), deadline=deadline) if callable(parser) else None
            except TypeError:
                # Older backend implementations did not accept a deadline.
                try:
                    parsed = parser(str(pdf_path)) if callable(parser) else None
                except Exception as exc:
                    parsed = None
                    return_gap = {"kind": "grobid", "status": "failed", "reason": f"parser_{type(exc).__name__}"}
            except Exception as exc:
                parsed = None
                return_gap = {"kind": "grobid", "status": "failed", "reason": f"parser_{type(exc).__name__}"}
            if isinstance(parsed, Mapping):
                tei_bytes = bytes(parsed.get("tei_bytes") or _text(parsed.get("tei_xml")).encode("utf-8"))
                if _xml_kind(tei_bytes) and _xml_has_research_body(tei_bytes):
                    tei = Artifact(kind="tei", data=tei_bytes, route="grobid", source_url=route.url, content_type="application/xml", derived_from=route.name, source_name="grobid-derived.tei.xml", metadata={"parser": "grobid", "parser_version": parsed.get("parser_version", ""), "legal_use_status": "oa", "identity_basis": "pdf_parser_observation", "source_role": "pdf_parser_observation"})
                    tei.metadata["pdf_artifact"] = pdf_artifact
                    return tei, [
                        {"kind": "pdf", "status": "grobid", "reason": "structured TEI derived from original PDF"},
                        {"kind": "grobid", "status": "known_gap", "reason": "TEI may flatten superscript notation, represent figures as captions/coordinates, and split or mislabel bibliography contributors; original PDF is retained"},
                    ]
                if _xml_kind(tei_bytes):
                    # A parseable header-only/empty TEI is not fulltext. Keep
                    # the PDF and continue to the local fitz fallback.
                    return_gap = {"kind": "grobid", "status": "partial", "reason": "GROBID returned TEI without a research body; fitz fallback considered"}
                else:
                    return_gap = {"kind": "grobid", "status": "failed", "reason": "GROBID returned non-TEI output"}
            else:
                return_gap = {"kind": "grobid", "status": "failed", "reason": "GROBID returned no parse result"}
        else:
            return_gap = None
        if fitz is not None and time.monotonic() < deadline:
            try:
                document = fitz.open(stream=pdf_bytes, filetype="pdf")
                pages: list[str] = []
                positions: list[dict[str, Any]] = []
                for page_number, page in enumerate(document, start=1):
                    text = page.get_text("text") or ""
                    pages.append(text)
                    positions.extend({"page": page_number, "block": list(block[:4]) if len(block) >= 4 else [], "text": _text(block[4])} for block in page.get_text("blocks") if len(block) >= 5 and _text(block[4]))
                document.close()
                joined = "\n".join(page for page in pages if page.strip())
                if len(joined.strip()) >= 200:
                    # PDF font maps can emit control characters that XML 1.0
                    # forbids. Keep original PDF/positions; sanitize only the
                    # derived XML instead of relaxing the XML parser.
                    invalid_xml = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
                    removed = sum(len(invalid_xml.findall(text)) for text in pages)
                    title = xml_escape(invalid_xml.sub("", identity.title or "Untitled"))
                    body = "".join(f"<p data-page=\"{index}\">{xml_escape(invalid_xml.sub('', text.strip()))}</p>" for index, text in enumerate(pages, start=1) if text.strip())
                    xml = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><article><front><article-meta><title-group><article-title>{title}</article-title></title-group></article-meta></front><body><sec><title>PDF text</title>{body}</sec></body></article>".encode()
                    tei = Artifact(kind="fitz_xml", data=xml, route="fitz", source_url=route.url, content_type="application/xml", derived_from=route.name, source_name="fitz-derived.xml", metadata={"parser": "fitz", "page_count": len(pages), "positions": positions, "legal_use_status": "oa", "synthetic_identity": True})
                    tei.metadata["pdf_artifact"] = pdf_artifact
                    gaps = [
                        {"kind": "pdf", "status": "fitz", "reason": "full-page text extracted; page positions retained"},
                        {"kind": "fitz", "status": "known_gap", "reason": "layout, table relations, formulas, figure pixels, and scanned text may be lost"},
                    ]
                    if removed:
                        gaps.append({"kind": "xml_text_cleanup", "status": "normalized", "reason": f"removed {removed} XML 1.0 forbidden characters from derived PDF text; original PDF and page positions retained"})
                    return tei, gaps
            except Exception:
                pass
        gaps = [{"kind": "pdf", "status": "partial", "reason": "fulltext parser unavailable or unreadable"}]
        if return_gap is not None:
            gaps.insert(0, return_gap)
        return None, gaps

    def _publish(self, result: AcquisitionResult, record: Mapping[str, Any], main: Artifact, *, material_depth: str, deadline: float, source_document_id: str = "main", source_role: str = "", source_uri: str = "", publication_version: str = "") -> PreparedSnapshot | None:
        # The deadline bounds network and parser work. Once bytes are in hand,
        # local normalization and atomic publication must still happen so an
        # interrupted paper is resumable with an honest depth claim.
        extras: list[Mapping[str, Any]] = []
        seen_extra_hashes: set[str] = set()
        def add_extra(spec: Mapping[str, Any], data: bytes) -> None:
            digest = _sha256(data)
            if digest in seen_extra_hashes or not data:
                return
            seen_extra_hashes.add(digest)
            extras.append(dict(spec, data=data))
        source_ids: dict[str, str] = {}
        for index, artifact in enumerate(result.artifacts):
            if artifact is main: continue
            sid = f"artifact-{index + 1}-{_sha256(artifact.data)[:12]}"
            source_ids[artifact.kind + str(index)] = sid
            add_extra(artifact.source_spec(sid), artifact.data)
        if main.metadata.get("pdf_artifact") is not None:
            pdf_artifact = main.metadata["pdf_artifact"]
            sid = f"original-pdf-{_sha256(pdf_artifact.data)[:12]}"
            add_extra(pdf_artifact.source_spec(sid), pdf_artifact.data)
        if main.wire_bytes:
            sid = f"wire-{_sha256(main.wire_bytes)[:12]}"
            add_extra({"source_document_id": sid, "role": "wire_bytes", "source_uri": redact_url(main.source_url), "source_name": f"{main.route}.wire", "format": "wire", "mime_type": "application/octet-stream", "derived_from": main.route, "route": main.route, "compression": "gzip" if main.wire_bytes[:2] == b"\x1f\x8b" else ""}, main.wire_bytes)
        if main.metadata.get("positions"):
            payload = json.dumps(main.metadata["positions"], ensure_ascii=False, sort_keys=True).encode()
            add_extra({"source_document_id": f"fitz-positions-{_sha256(payload)[:12]}", "role": "page_positions", "source_name": "page-positions.json", "format": "json", "mime_type": "application/json", "derived_from": main.route, "route": "fitz"}, payload)
        observed_payload = {
            "record": _safe_observed(dict(record)),
            "resolved_identity": _safe_observed(result.identity.as_mapping()),
            "provider_records": _safe_observed(list(result.identity.provider_records)),
            "provider_audit": _safe_observed(list(result.identity.provider_audit)),
            "attempts": _safe_observed(result.attempts),
            "available_abstract": bool(result.identity.abstract),
            "available_snippets": len(result.identity.snippets),
        }
        observed_bytes = json.dumps(observed_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        add_extra({"source_document_id": f"observed-metadata-{_sha256(observed_bytes)[:12]}", "role": "observed_metadata", "source_uri": "", "source_name": "observed-metadata.json", "format": "json", "mime_type": "application/json", "derived_from": main.route, "route": "identity_resolution", "legal_use_status": "metadata"}, observed_bytes)
        identity_basis = main.metadata.get("identity_basis") or ("input_attributed_projection" if main.metadata.get("synthetic_identity") else "provider_or_source_xml")
        selected_publication_version = _text(
            publication_version
            or main.metadata.get("publication_version")
            or (main.metadata.get("source_provenance") or {}).get("version_label")
        )
        metadata = {
            "title": result.identity.title,
            "doi": result.identity.doi,
            "year": result.identity.year,
            "identity_basis": identity_basis,
            "synthetic_identity": bool(main.metadata.get("synthetic_identity")),
            "source_provenance": _safe_observed(main.metadata.get("source_provenance") or {}),
            "legal_use_status": "oa" if any(item.metadata.get("legal_use_status") == "oa" for item in result.artifacts) else "unknown",
        }
        claim = {"claimed": material_depth == "fulltext", "scope": main.route, "content_depth": material_depth, "basis": "acquisition route validated and parser audit recorded", "verification": "automatic structural and byte checks; figures are not interpreted by this layer", "identity_basis": identity_basis}
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", result.identity.canonical_paper_id or result.identity.doi or "paper")[:120]
        provider = LocalTeiMaterialProvider(self.output_root / slug)
        try:
            snapshot = provider.build_from_bytes(main.data, canonical_paper_id=result.identity.canonical_paper_id, publication_version=selected_publication_version, acquisition_revision=("refresh-" + str(int(time.time())) if self.config.refresh else "acquisition-v1"), source_document_id=source_document_id, source_role=source_role or main.metadata.get("source_role") or main.kind, source_uri=source_uri or main.source_url, source_name=main.source_name or "source.xml", metadata=metadata, additional_sources=extras, material_depth_override=material_depth, producer_fulltext_claim_override=claim, known_gaps_extra=result.known_gaps)
            return snapshot
        except (SnapshotError, OSError, ValueError) as exc:
            result.errors.append(f"snapshot_{type(exc).__name__}")
            return None

    def acquire(self, record: Mapping[str, Any]) -> AcquisitionResult:
        started = time.monotonic()
        deadline = started + self.config.deadline_seconds
        stage_started = time.monotonic()
        has_structured_route = any(
            _text(mapping.get(key))
            for mapping in [record, *list(_iter_mappings(record))]
            for key in ("jats_url", "fulltext_xml_url", "fullTextXML", "tei_url", "grobid_xml")
        )
        identity = self._record_identity(record) if has_structured_route else self.resolve_identity(record, deadline=deadline)
        result = AcquisitionResult(identity=identity, status="failed", material_depth="unknown")
        result.stage_timings["identity"] = time.monotonic() - stage_started

        routes = self._initial_routes(record, identity)
        # A TEI route must not suppress the inexpensive EuropePMC JATS lookup.
        # Skip only when a native JATS route is already available, and retain
        # the one discovery result so rescue cannot issue a duplicate search.
        europe_routes: list[_Route] = []
        if not any(route.kind == "jats" for route in routes) and time.monotonic() < deadline:
            search_started = time.monotonic()
            europe_routes, reason = self._europepmc_routes(identity, deadline)
            result.stage_timings["europepmc_discovery"] = time.monotonic() - search_started
            routes.extend(europe_routes)
            if reason:
                result.attempts.append({"route": "europepmc_search", "status": "failed", "reason": reason})
        routes.sort(key=lambda item: (item.priority, item.name, item.url))

        tried: set[str] = set()
        main: Artifact | None = None
        selected_route: _Route | None = None
        html_candidate: tuple[Artifact, _Route] | None = None

        def accept(artifact: Artifact, route: _Route) -> None:
            nonlocal main, selected_route
            main = artifact
            selected_route = route
            result.artifacts.append(artifact)
            result.selected_route = route.name
            result.parser = _text(artifact.metadata.get("parser")) or artifact.kind
            if artifact.metadata.get("pdf_artifact") is not None:
                result.artifacts.append(artifact.metadata["pdf_artifact"])
            if artifact.kind == "jats":
                pmcid = _text(route.metadata.get("pmcid") or record.get("pmcid") or record.get("pmc_id"))
                self._attach_pmc_assets(artifact, pmcid, result, deadline)
            body_kinds = {"jats", "tei", "fitz_xml", "core_text_jats"}
            result.material_depth = "fulltext" if artifact.kind in body_kinds and artifact.metadata.get("identity_verified", True) else "structured_partial"
            if not artifact.metadata.get("identity_verified", True):
                result.known_gaps.append({"kind": "identity", "status": "partial", "reason": "body source was readable but independent identity evidence was incomplete"})

        # Work as a priority queue because an HTML landing page can discover a
        # better native XML/PDF route after the initial route list is built.
        while routes and time.monotonic() < deadline and main is None:
            routes.sort(key=lambda item: (item.priority, item.name, item.url))
            route = routes.pop(0)
            if route.url in tried:
                continue
            tried.add(route.url)
            artifact, audit, gaps = self._route_attempt(route, identity, deadline)
            result.attempts.append(audit)
            result.known_gaps.extend(gaps)
            discovered = self._html_link_routes(route, audit, identity)
            for discovered_route in discovered:
                if discovered_route.url not in tried and all(item.url != discovered_route.url for item in routes):
                    routes.append(discovered_route)
            if artifact is None:
                continue
            # HTML is always a candidate only.  Continue explicit native links
            # and provider rescue so a landing page cannot shadow better text.
            if artifact.kind == "html_jats":
                if html_candidate is None:
                    html_candidate = (artifact, route)
                continue
            accept(artifact, route)

        # Provider-specific rescue remains lazy and only runs after all known
        # structured/native routes (including HTML-discovered routes) fail.
        if main is None:
            rescue_started = time.monotonic()
            for provider_name in ("unpaywall", "core", "arxiv"):
                if time.monotonic() >= deadline:
                    break
                rescue = self._provider_rescue_routes(identity, deadline, only=provider_name)
                if not rescue:
                    status = provider_status(self.providers.get(provider_name), result=rescue)
                    result.attempts.append({"route": provider_name, **status, "reason": status.get("reason", "no_verified_route")})
                for route in sorted(rescue, key=lambda item: (item.priority, item.name, item.url)):
                    if route.url in tried or time.monotonic() >= deadline:
                        continue
                    tried.add(route.url)
                    artifact, audit, gaps = self._route_attempt(route, identity, deadline)
                    result.attempts.append(audit)
                    result.known_gaps.extend(gaps)
                    if artifact is None:
                        for discovered_route in self._html_link_routes(route, audit, identity):
                            if discovered_route.url in tried or time.monotonic() >= deadline:
                                continue
                            tried.add(discovered_route.url)
                            linked_artifact, linked_audit, linked_gaps = self._route_attempt(discovered_route, identity, deadline)
                            result.attempts.append(linked_audit)
                            result.known_gaps.extend(linked_gaps)
                            if linked_artifact is not None and linked_artifact.kind != "html_jats":
                                accept(linked_artifact, discovered_route)
                                break
                        continue
                    if artifact.kind == "html_jats":
                        if html_candidate is None:
                            html_candidate = (artifact, route)
                        for discovered_route in self._html_link_routes(route, audit, identity):
                            if discovered_route.url in tried or time.monotonic() >= deadline:
                                continue
                            tried.add(discovered_route.url)
                            linked_artifact, linked_audit, linked_gaps = self._route_attempt(discovered_route, identity, deadline)
                            result.attempts.append(linked_audit)
                            result.known_gaps.extend(linked_gaps)
                            if linked_artifact is not None and linked_artifact.kind != "html_jats":
                                accept(linked_artifact, discovered_route)
                                break
                        if main is None:
                            continue
                        break
                    accept(artifact, route)
                    break
                if main is not None:
                    break
            result.stage_timings["provider_rescue"] = time.monotonic() - rescue_started

        if main is None and html_candidate is None and self.config.rescue_enabled and self.reader is not None and time.monotonic() < deadline:
            reader_started = time.monotonic()
            reader_routes, reader_audits, reader_validated = self._reader_rescue_routes(
                identity,
                deadline,
                tried_urls=tried,
                redirect_records=[{"source_url": item["resolved_url"], "backend": "doi_redirect"} for item in result.attempts if item.get("resolved_url")],
                validate_route=lambda route: self._route_attempt(route, identity, deadline),
            )
            result.attempts.extend(reader_audits)
            for route, artifact, audit, gaps in reader_validated:
                result.attempts.append(audit)
                result.known_gaps.extend(gaps)
                if artifact is not None:
                    accept(artifact, route)
                    break
            for route in (reader_routes if main is None else ()):
                # A direct publisher URL can have failed with 403/HTML shell;
                # the bounded reader is specifically allowed to retry that
                # exact URL through its own public extraction protocol.
                if (route.url in tried and route.kind != "reader_text") or time.monotonic() >= deadline:
                    continue
                tried.add(route.url)
                artifact, audit, gaps = self._route_attempt(route, identity, deadline)
                result.attempts.append(audit)
                result.known_gaps.extend(gaps)
                if artifact is not None:
                    accept(artifact, route)
                    break
            result.stage_timings["reader_rescue"] = time.monotonic() - reader_started

        if main is None and html_candidate is not None:
            accept(*html_candidate)

        if main is None:
            xml, depth = _abstract_snippet_jats(identity)
            main = Artifact(kind="abstract_xml" if depth == "abstract" else "snippet_xml", data=xml, route="abstract_snippet_fallback", source_url="", content_type="application/xml", source_name="abstract-snippet.xml", metadata={"legal_use_status": "unknown", "synthetic_identity": True})
            result.artifacts.append(main)
            result.artifacts.append(Artifact(kind="input_record", data=json.dumps(dict(record), ensure_ascii=False, sort_keys=True).encode("utf-8"), route="input_record", source_name="input-record.json", content_type="application/json", metadata={"legal_use_status": "input-attributed"}))
            result.selected_route = main.route
            result.parser = "fallback"
            result.material_depth = depth
            result.known_gaps.append({"kind": "coverage", "status": "missing" if depth == "metadata_only" else "partial", "reason": "no readable abstract or non-title snippets were available" if depth == "metadata_only" else "only abstract or non-title snippets were available"})

        result.snapshot = self._publish(result, record, main, material_depth=result.material_depth, deadline=deadline, publication_version=_text(record.get("publication_version") or record.get("version")))
        result.status = "published" if result.snapshot is not None else "failed"
        result.elapsed_seconds = time.monotonic() - started
        result.stage_timings["total"] = result.elapsed_seconds
        return result

    def acquire_local(self, record: Mapping[str, Any], *, xml_bytes: bytes | None = None, pdf_bytes: bytes | None = None, source_name: str = "source.xml", source_document_id: str = "main", source_role: str = "", source_uri: str = "", publication_version: str = "") -> AcquisitionResult:
        """Materialize caller-supplied XML/PDF without making a network request."""

        started = time.monotonic()
        deadline = started + self.config.deadline_seconds
        identity = self._record_identity(record)
        result = AcquisitionResult(identity=identity, status="failed", material_depth="unknown")
        if pdf_bytes:
            self._ensure_grobid()
        main: Artifact | None = None
        if xml_bytes:
            kind = _xml_kind(bytes(xml_bytes))
            if kind:
                main = Artifact(kind=kind, data=bytes(xml_bytes), route="local_xml", source_url="", content_type="application/xml", source_name=source_name, metadata={"legal_use_status": "local"})
                result.material_depth = "fulltext" if _xml_has_research_body(bytes(xml_bytes)) else "structured_partial"
                result.selected_route = "local_xml"
                result.parser = kind
                result.artifacts.append(main)
            else:
                result.errors.append("local_xml_invalid")
        if main is None and pdf_bytes:
            route = _Route("local_pdf", "", "pdf", "local", {"title": identity.title, "doi": identity.doi, "year": identity.year}, 0, True)
            main, gaps = self._parse_pdf(bytes(pdf_bytes), route, identity, deadline, {"content-type": "application/pdf"})
            result.known_gaps.extend(gaps)
            if main is not None:
                result.artifacts.append(main)
                result.artifacts.append(main.metadata["pdf_artifact"])
                result.material_depth = "fulltext"
                result.selected_route = main.route
                result.parser = _text(main.metadata.get("parser")) or main.kind
        if main is None:
            xml, depth = _abstract_snippet_jats(identity)
            main = Artifact(kind="abstract_xml" if depth == "abstract" else "snippet_xml", data=xml, route="abstract_snippet_fallback", source_url="", content_type="application/xml", source_name="abstract-snippet.xml", metadata={"legal_use_status": "unknown", "synthetic_identity": True})
            result.artifacts.append(main)
            result.artifacts.append(Artifact(kind="input_record", data=json.dumps(dict(record), ensure_ascii=False, sort_keys=True).encode("utf-8"), route="input_record", source_name="input-record.json", content_type="application/json", metadata={"legal_use_status": "input-attributed"}))
            result.material_depth = depth
            result.selected_route = main.route
            result.parser = "fallback"
            result.known_gaps.append({"kind": "coverage", "status": "partial", "reason": "local source was absent or unreadable"})
        result.snapshot = self._publish(result, record, main, material_depth=result.material_depth, deadline=deadline, source_document_id=source_document_id, source_role=source_role, source_uri=source_uri, publication_version=publication_version)
        result.status = "published" if result.snapshot is not None else "failed"
        result.elapsed_seconds = time.monotonic() - started
        return result

    def acquire_batch(self, records: Sequence[Mapping[str, Any]], *, on_result: Callable[[AcquisitionResult], None] | None = None) -> list[AcquisitionResult]:
        results: list[AcquisitionResult | None] = [None] * len(records)
        self._ensure_grobid()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.network_workers) as pool:
            future_map = {pool.submit(self.acquire, record): index for index, record in enumerate(records)}
            for future in concurrent.futures.as_completed(future_map):
                index = future_map[future]
                try: results[index] = future.result()
                except Exception as exc:
                    identity = self._record_identity(records[index])
                    results[index] = AcquisitionResult(identity=identity, status="failed", material_depth="unknown", errors=[type(exc).__name__])
                if on_result is not None and results[index] is not None:
                    try:
                        on_result(results[index])
                    except Exception:
                        pass
        return [item for item in results if item is not None]


__all__ = [
    "ACQUISITION_SCHEMA_VERSION", "AcquisitionConfig", "AcquisitionResult", "IdentityResolution", "MaterialAcquirer", "match_identity", "normalize_arxiv_id", "normalize_doi", "normalize_title", "provider_fields", "redact_url", "title_similarity",
]
