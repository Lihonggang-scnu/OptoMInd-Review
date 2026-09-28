# Aggregating full-text fetcher for the local tool layer.
#
# Goal: given a paper record of unknown shape (DOI or not, from any provider),
# find a usable full-text artifact with as many independent routes as the repo
# already has, never throw material away silently, and leave a per-attempt audit
# trail so an unattended run can be diagnosed afterwards.
#
# Design rules taken from the current round:
#   - identity is resolved by a ladder (DOI -> S2 CorpusId -> arXiv id ->
#     title+authors+year via Crossref/S2), because a missing DOI is a missing
#     label, not a missing paper;
#   - routes are tried in cheap-first order (OpenAlex cached TEI/PDF, then
#     publisher OA links, then other locations, then S2/Unpaywall/CORE/arXiv);
#   - every attempt is recorded with its own reason, so a failure is auditable;
#   - OpenAlex content responses may be gzip-compressed: decompression is part of
#     the contract, not an afterthought;
#   - PDFs are parsed by the local GROBID service when it is up, otherwise by the
#     local PDF parser; the parser actually used is recorded on the artifact;
#   - no paid model is involved anywhere in this module.

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

MAGIC_PDF = b"%PDF-"
GZIP_MAGIC = bytes([31, 139])
DEFAULT_TIMEOUT = 60.0
MIN_PDF_BYTES = 20000
# Statuses that mean "come back later" rather than "this paper is not here".
# Measured on a real unseen run: 15 of 30 route attempts answered 429, so treating
# them as terminal would have thrown away obtainable papers.
RETRY_STATUSES = frozenset({202, 429, 500, 502, 503, 504})

ROUTE_ORDER = (
    "openalex_content_grobid_xml",
    "openalex_content_pdf",
    "openalex_best_oa_pdf",
    "openalex_location_pdf",
    "s2_open_access_pdf",
    "unpaywall_pdf",
    "core_download",
    "arxiv_pdf",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip()


def title_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def looks_like_pdf(data: bytes) -> bool:
    return bool(data) and data[:5] == MAGIC_PDF


def looks_like_html(data: bytes) -> bool:
    head = bytes(data[:2048]).lower()
    return b"<html" in head or b"<!doctype html" in head


def looks_like_tei(data: bytes) -> bool:
    head = bytes(data[:4096]).lower()
    return b"<tei" in head or b"tei-c.org" in head


def maybe_gunzip(data: bytes, content_type: str = "") -> tuple[bytes, bool]:
    """OpenAlex serves cached GROBID TEI as application/gzip."""

    if not data:
        return data, False
    compressed = data[:2] == GZIP_MAGIC or "gzip" in str(content_type or "").lower()
    if not compressed:
        return data, False
    try:
        return gzip.decompress(data), True
    except Exception:
        return data, False


@dataclass
class Identity:
    doi: str = ""
    corpus_id: str = ""
    arxiv_id: str = ""
    title: str = ""
    authors: tuple[str, ...] = ()
    year: str = ""
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "doi": self.doi,
            "corpus_id": self.corpus_id,
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "sources": list(self.sources),
            "notes": list(self.notes),
        }


@dataclass
class Route:
    name: str
    url: str
    source: str
    expects: str = "any"
    needs_provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "expects": self.expects,
                "needs_provider": self.needs_provider, "url_sha256": _sha256(self.url.encode())[:16]}


@dataclass
class Attempt:
    route: str = ""
    source: str = ""
    status: str = ""
    reason: str = ""
    bytes: int = 0
    content_type: str = ""
    decompressed: bool = False
    usable: bool = False
    tries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route, "source": self.source, "status": self.status,
            "reason": self.reason, "bytes": self.bytes, "tries": self.tries,
            "content_type": self.content_type, "decompressed": self.decompressed,
            "usable": self.usable,
        }


@dataclass
class Body:
    kind: str
    data: bytes
    route: str
    source: str
    sha256: str
    decompressed: bool = False


@dataclass
class Acquisition:
    identity: Identity
    body: Body | None = None
    parsed: dict[str, Any] | None = None
    attempts: list[Attempt] = field(default_factory=list)
    outcome: str = "failed"
    outcome_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "attempts": [a.to_dict() for a in self.attempts],
            "body": None if self.body is None else {
                "kind": self.body.kind, "route": self.body.route,
                "source": self.body.source, "sha256": self.body.sha256,
                "bytes": len(self.body.data), "decompressed": self.body.decompressed,
            },
            "parsed": self.parsed,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_transport(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "OptoMind/0.1 (tool layer)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return int(exc.code), dict(exc.headers or {}), body or b""


class AggregateFulltextFetcher:
    """Try every configured route for one paper and keep the audit trail."""

    def __init__(
        self,
        *,
        material_root: str | Path = "data/tool_layer_materials",
        transport: Callable[..., tuple[int, dict[str, str], bytes]] | None = None,
        openalex: Any = None,
        semantic_scholar: Any = None,
        unpaywall: Any = None,
        core: Any = None,
        arxiv: Any = None,
        grobid: Any = None,
        pdf_parser: Any = None,
        sleep_seconds: float = 0.2,
        parse_pdf: bool = True,
        max_retries: int = 3,
        retry_cap_seconds: float = 30.0,
        host_interval_seconds: float = 1.0,
        per_paper_seconds: float = 90.0,
        per_route_seconds: float = 30.0,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self.material_root = Path(material_root)
        self.material_root.mkdir(parents=True, exist_ok=True)
        self._transport = transport or _default_transport
        self._openalex = openalex
        self._s2 = semantic_scholar
        self._unpaywall = unpaywall
        self._core = core
        self._arxiv = arxiv
        self._grobid = grobid
        self._pdf_parser = pdf_parser
        self._sleep = sleep_seconds
        self._parse_pdf = parse_pdf
        self._max_retries = max(0, int(max_retries))
        self._retry_cap = float(retry_cap_seconds)
        self._host_interval = max(0.0, float(host_interval_seconds))
        self._last_call: dict[str, float] = {}
        self._per_paper_seconds = float(per_paper_seconds)
        self._per_route_seconds = float(per_route_seconds)
        self._request_timeout = float(request_timeout_seconds)

    # ---------------------------------------------------------------- identity

    def resolve_identity(self, record: Mapping[str, Any]) -> Identity:
        """Identity only; route-bearing enrichments go through enrich_record()."""

        return self.enrich_record(record)[0]

    def enrich_record(self, record: Mapping[str, Any]) -> tuple[Identity, dict[str, Any]]:
        doi = normalize_doi(record.get("doi"))
        corpus_id = str(record.get("corpus_id") or record.get("paperId") or "").strip()
        arxiv_id = str(record.get("arxiv_id") or record.get("arxivId") or "").strip()
        title = str(record.get("title") or "").strip()
        authors = _as_author_tuple(record.get("authors"))
        year = str(record.get("year") or "").strip()
        sources: list[str] = []
        notes: list[str] = []
        if doi:
            sources.append("input.doi")
        if corpus_id:
            sources.append("input.corpus_id")
        if arxiv_id:
            sources.append("input.arxiv_id")

        # Second and third identity sources: never discard a paper just because the
        # discovery layer only gave it an internal id.
        if self._s2 is not None and (not doi or not arxiv_id):
            for handle in _s2_handles(doi, corpus_id, arxiv_id):
                paper = _safe_call(self._s2, "get_paper", handle)
                if not paper:
                    notes.append("s2_no_record:" + handle.split(":")[0])
                    continue
                if not _provider_record_matches(doi, title, year, paper):
                    # A provider handle can point at a different paper (a PMID is not a
                    # CorpusId).  An unrelated record must never contribute a PDF.
                    notes.append("s2_identity_mismatch:" + handle.split(":")[0])
                    continue
                found = _s2_identity(paper)
                if found.get("doi") and not doi:
                    doi = found["doi"]
                    sources.append("s2.doi")
                if found.get("arxiv_id") and not arxiv_id:
                    arxiv_id = found["arxiv_id"]
                    sources.append("s2.arxiv_id")
                if found.get("title") and not title:
                    title = found["title"]
                    sources.append("s2.title")
                record = _merge_extra(record, paper)
                break

        if not doi and self._openalex is not None and title:
            work = _openalex_title_lookup(self._openalex, title, year)
            if work:
                found = normalize_doi(work.get("doi"))
                if found:
                    doi = found
                    sources.append("openalex.title_lookup")
                record = _merge_extra(record, work)

        identity = Identity(doi=doi, corpus_id=corpus_id, arxiv_id=arxiv_id, title=title,
                            authors=authors, year=year, sources=tuple(sources),
                            notes=tuple(notes))
        return identity, dict(record)

    # ------------------------------------------------------------------ routes

    def plan_routes(self, record: Mapping[str, Any], identity: Identity) -> list[Route]:
        routes: list[Route] = []
        seen: set[str] = set()

        def add(name: str, url: Any, source: str, expects: str, provider: str = "") -> None:
            url = str(url or "").strip()
            if not url or url in seen:
                return
            seen.add(url)
            routes.append(Route(name=name, url=url, source=source, expects=expects,
                                needs_provider=provider))

        # Providers hand back several different record shapes: raw OpenAlex works,
        # the repo's normalised records and the nested raw_metadata copies.  Route
        # extraction is shape tolerant on purpose, so a new provider does not need a
        # new branch here.
        for content_urls in _collect_dicts(record, "content_urls"):
            add("openalex_content_grobid_xml", content_urls.get("grobid_xml"), "openalex_content", "tei", "openalex")
            add("openalex_content_pdf", content_urls.get("pdf"), "openalex_content", "pdf", "openalex")
        for best in _collect_dicts(record, "best_oa_location"):
            add("openalex_best_oa_pdf", best.get("pdf_url") or best.get("url_for_pdf"), "openalex_best_oa", "pdf")
        for locations in _collect_lists(record, "locations", "oa_locations"):
            for location in locations[:12]:
                if isinstance(location, dict):
                    add("openalex_location_pdf", location.get("pdf_url") or location.get("url_for_pdf"),
                        "openalex_location", "pdf")
        for key in ("pdf_url", "open_access_url", "open_access_pdf", "s2_open_access_pdf"):
            add("s2_open_access_pdf", record.get(key), "record.pdf_url", "pdf")
        add("s2_open_access_pdf", record.get("s2_open_access_pdf") or record.get("pdf_url"), "semantic_scholar", "pdf")
        unpaywall = record.get("unpaywall") if isinstance(record.get("unpaywall"), dict) else {}
        add("unpaywall_pdf", unpaywall.get("best_oa_url"), "unpaywall", "pdf")
        for location in (unpaywall.get("locations") or [])[:8]:
            if isinstance(location, dict):
                add("unpaywall_pdf", location.get("url_for_pdf"), "unpaywall", "pdf")
        add("core_download", record.get("core_download_url") or record.get("url_or_doi"), "core", "any")
        if identity.arxiv_id:
            add("arxiv_pdf", "https://arxiv.org/pdf/" + identity.arxiv_id, "arxiv", "pdf")
        routes.sort(key=lambda r: ROUTE_ORDER.index(r.name) if r.name in ROUTE_ORDER else len(ROUTE_ORDER))
        return routes

    def escalate(self, identity: Identity, tried_urls: set[str]) -> list[Route]:
        """Ask the remaining providers only after the cheap routes came up empty."""

        extra: list[Route] = []
        for route in self._europepmc_routes(identity):
            if route.url not in tried_urls:
                extra.append(route)
        for route in self._core_routes(identity):
            if route.url not in tried_urls:
                extra.append(route)
        return extra

    def _europepmc_routes(self, identity: Identity) -> list[Route]:
        term = 'DOI:"' + identity.doi + '"' if identity.doi else ('TITLE:"' + identity.title[:120] + '"')
        if not identity.doi and not identity.title:
            return []
        url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
               + urllib.parse.quote(term) + "&format=json&resultType=core&pageSize=1")
        try:
            status, _headers, raw = self._transport(url)
        except Exception:
            return []
        if status != 200 or not raw:
            return []
        try:
            payload = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return []
        results = (((payload.get("resultList") or {}).get("result")) or [])
        if not results:
            return []
        record = results[0] if isinstance(results[0], Mapping) else {}
        found_title = str(record.get("title") or "")
        if identity.title and title_key(found_title) and not _titles_agree(identity.title, found_title):
            return []
        routes: list[Route] = []
        urls = ((record.get("fullTextUrlList") or {}).get("fullTextUrl")) or []
        for item in urls:
            if not isinstance(item, Mapping):
                continue
            style = str(item.get("documentStyle") or "").lower()
            link = str(item.get("url") or "")
            if style == "pdf" and link:
                routes.append(Route(name="europepmc_pdf", url=link, source="europepmc", expects="pdf"))
        return routes

    def _core_routes(self, identity: Identity) -> list[Route]:
        if self._core is None or not identity.title:
            return []
        rows = _safe_call(self._core, "search", identity.title, 5) or []
        routes: list[Route] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if not _titles_agree(identity.title, str(row.get("title") or "")):
                continue
            for key in ("download_url", "downloadUrl", "fulltext_url", "url_or_doi"):
                link = str(row.get(key) or "")
                if link.startswith("http"):
                    routes.append(Route(name="core_download", url=link, source="core", expects="any"))
                    break
        return routes

    # ------------------------------------------------------------------- fetch

    def _pace(self, url: str) -> None:
        """Keep a minimum interval per host: bulk unattended runs get rate limited."""

        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            return
        if not host:
            return
        last = self._last_call.get(host)
        now = time.monotonic()
        if last is not None:
            wait = self._host_interval - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def _fetch_route(self, route: Route) -> tuple[Attempt, Body | None]:
        attempt = Attempt(route=route.name, source=route.source)
        route_deadline = time.monotonic() + self._per_route_seconds
        status, headers, raw = 0, {}, b""
        for tries in range(self._max_retries + 1):
            if time.monotonic() > route_deadline:
                attempt.status = "deadline"
                attempt.reason = "per_route_budget_exceeded"
                return attempt, None
            attempt.tries = tries + 1
            self._pace(route.url)
            try:
                status, headers, raw = self._transport(route.url, timeout=self._request_timeout)
            except TypeError:
                status, headers, raw = self._transport(route.url)
            except Exception as exc:  # transport failures are recorded, never raised
                attempt.status = "transport_error"
                attempt.reason = type(exc).__name__ + ": " + str(exc)[:120]
                if tries < self._max_retries and self._sleep:
                    time.sleep(min(self._retry_cap, self._sleep * (2 ** tries)))
                    continue
                return attempt, None
            if status in RETRY_STATUSES and tries < self._max_retries:
                delay = _retry_after_seconds(headers) or (max(self._sleep, 1.0) * (2 ** tries))
                time.sleep(min(self._retry_cap, delay))
                continue
            break
        self._pace(route.url)
        content_type = str((headers or {}).get("content-type") or (headers or {}).get("Content-Type") or "")
        attempt.status = str(status)
        attempt.content_type = content_type
        attempt.bytes = len(raw or b"")
        if status != 200 or not raw:
            attempt.reason = "http_" + str(status)
            return attempt, None
        data, decompressed = maybe_gunzip(raw, content_type)
        attempt.decompressed = decompressed
        if looks_like_pdf(data):
            if len(data) < MIN_PDF_BYTES:
                attempt.reason = "pdf_too_small"
                return attempt, None
            attempt.usable = True
            return attempt, Body(kind="pdf", data=data, route=route.name, source=route.source,
                                 sha256=_sha256(data), decompressed=decompressed)
        if looks_like_tei(data):
            attempt.usable = True
            return attempt, Body(kind="tei", data=data, route=route.name, source=route.source,
                                 sha256=_sha256(data), decompressed=decompressed)
        if looks_like_html(data):
            attempt.reason = "html_not_document"
            return attempt, None
        attempt.reason = "unrecognised_content"
        return attempt, None

    def obtain(self, record: Mapping[str, Any]) -> Acquisition:
        identity, merged = self.enrich_record(record)
        if self._openalex is not None and identity.doi and not merged.get("openalex_work"):
            work = _openalex_doi_lookup(self._openalex, identity.doi)
            if work:
                merged["openalex_work"] = work
        if self._unpaywall is not None and identity.doi and not merged.get("unpaywall"):
            merged["unpaywall"] = _safe_call(self._unpaywall, "lookup", identity.doi) or {}

        result = Acquisition(identity=identity)
        routes = self.plan_routes(merged, identity)
        if not routes and not (identity.title or identity.doi):
            result.outcome = "no_identity"
            result.outcome_reason = "neither a DOI nor a title was available to look anything up"
            return result
        tried: set[str] = set()
        deadline = time.monotonic() + self._per_paper_seconds
        for phase in (routes, None):
            if time.monotonic() > deadline:
                result.attempts.append(Attempt(route="", source="", status="deadline",
                                                reason="per_paper_budget_exceeded"))
                break
            if phase is None:
                # Only now do we spend the slower providers; a paper is never dropped
                # while an untried provider might still have it.
                phase = self.escalate(identity, tried)
            for route in phase:
                if time.monotonic() > deadline:
                    result.attempts.append(Attempt(route=route.name, source=route.source,
                                                    status="deadline",
                                                    reason="per_paper_budget_exceeded"))
                    break
                tried.add(route.url)
                attempt, body = self._fetch_route(route)
                result.attempts.append(attempt)
                if body is not None:
                    result.body = body
                    result.outcome = "obtained"
                    result.outcome_reason = route.name
                    if body.kind == "tei" or self._parse_pdf:
                        result.parsed = self.parse_body(body)
                    return result
                if self._sleep:
                    time.sleep(self._sleep)
        result.outcome = "exhausted" if result.attempts else "no_route"
        result.outcome_reason = (("all routes failed after escalation (attempts=" + str(len(result.attempts)) + ")")
                                if result.attempts else "no provider produced a candidate route")
        return result

    # ------------------------------------------------------------------- parse

    def parse_body(self, body: Body) -> dict[str, Any]:
        if body.kind == "tei":
            return _parse_tei(body.data)
        return self._parse_pdf_body(body)

    def _parse_pdf_body(self, body: Body) -> dict[str, Any]:
        saved = self.material_root / (body.sha256[:16] + ".pdf")
        if not saved.exists():
            saved.write_bytes(body.data)
        parser = "local_pdf_parser"
        result: dict[str, Any] | None = None
        if self._grobid is not None and _safe_call(self._grobid, "available"):
            try:
                result = self._grobid.parse_pdf(str(saved))
                parser = "grobid"
            except Exception as exc:
                result = None
                parser = "grobid_failed:" + type(exc).__name__
        if result is None and self._pdf_parser is not None:
            try:
                result = self._pdf_parser.parse_pdf(str(saved))
            except Exception as exc:
                return {"parser": "pdf_parser_failed:" + type(exc).__name__, "path": str(saved)}
        if result is None:
            return {"parser": parser, "path": str(saved), "text_chars": 0}
        payload = dict(result) if isinstance(result, Mapping) else {"value": result}
        payload.setdefault("path", str(saved))
        payload["parser_used"] = parser
        return payload


def _as_author_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = re.split(r"[;,]", value)
        return tuple(p.strip() for p in parts if p.strip())[:12]
    if isinstance(value, Iterable):
        out = []
        for item in value:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("author") or ""
                out.append(str(name).strip())
            else:
                out.append(str(item).strip())
        return tuple(n for n in out if n)[:12]
    return ()


def _safe_call(target: Any, name: str, *args: Any) -> Any:
    func = getattr(target, name, None)
    if not callable(func):
        return None
    try:
        return func(*args)
    except Exception:
        return None


def _s2_handles(doi: str, corpus_id: str, arxiv_id: str) -> list[str]:
    handles = []
    corpus = str(corpus_id or "").strip()
    if corpus.startswith("CorpusId:"):
        corpus = corpus.split(":", 1)[1]
    if corpus.isdigit():
        handles.append("CorpusId:" + corpus)
    if doi:
        handles.append("DOI:" + doi)
    if arxiv_id:
        handles.append("ARXIV:" + arxiv_id)
    return handles


def _s2_identity(paper: Mapping[str, Any]) -> dict[str, str]:
    external = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    oa_pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    paper = dict(paper)
    paper["s2_open_access_pdf"] = oa_pdf.get("url", "")
    return {
        "doi": normalize_doi(external.get("DOI")),
        "arxiv_id": str(external.get("ArXiv") or ""),
        "title": str(paper.get("title") or ""),
        "pdf_url": str(oa_pdf.get("url") or ""),
    }


def _merge_extra(record: Mapping[str, Any], provider_record: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    if isinstance(provider_record, Mapping):
        for key in ("content_urls", "best_oa_location", "locations", "open_access", "has_content"):
            if provider_record.get(key) and not merged.get(key):
                merged[key] = provider_record[key]
        oa_pdf = provider_record.get("openAccessPdf") if isinstance(provider_record.get("openAccessPdf"), dict) else {}
        if oa_pdf.get("url"):
            merged.setdefault("s2_open_access_pdf", oa_pdf.get("url"))
        if provider_record.get("pdf_url"):
            merged.setdefault("s2_open_access_pdf", provider_record.get("pdf_url"))
        if provider_record.get("id") and not merged.get("openalex_work"):
            merged["_openalex_id"] = provider_record.get("id")
    return merged


def _openalex_doi_lookup(provider: Any, doi: str) -> dict[str, Any] | None:
    work = _safe_call(provider, "get_work", doi)
    return work if isinstance(work, dict) else None


def _openalex_title_lookup(provider: Any, title: str, year: str) -> dict[str, Any] | None:
    search = getattr(provider, "search", None)
    if not callable(search):
        return None
    try:
        rows = search(title, max_results=5)
    except Exception:
        return None
    wanted = title_key(title)
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        if title_key(row.get("title")) == wanted:
            return dict(row)
        if wanted and title_key(row.get("title")).startswith(wanted[:60]):
            return dict(row)
    return None


def _iter_mappings(record: Mapping[str, Any], depth: int = 3) -> Iterable[Mapping[str, Any]]:
    """Every mapping reachable from a record, so nested provider shapes are not missed."""

    if depth < 0 or not isinstance(record, Mapping):
        return
    yield record
    for value in record.values():
        if isinstance(value, Mapping):
            for item in _iter_mappings(value, depth - 1):
                yield item
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, Mapping):
                    for item in _iter_mappings(entry, depth - 1):
                        yield item


def _collect_dicts(record: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    out = []
    for mapping in _iter_mappings(record):
        value = mapping.get(key)
        if isinstance(value, Mapping) and value not in out:
            out.append(value)
    return out


def _collect_lists(record: Mapping[str, Any], *keys: str) -> list[list[Any]]:
    out = []
    for mapping in _iter_mappings(record):
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, list) and value and value not in out:
                out.append(value)
    return out


def _provider_record_matches(doi: str, title: str, year: str, record: Mapping[str, Any]) -> bool:
    """A provider record may only contribute material if it is the same paper."""

    if not isinstance(record, Mapping):
        return False
    external = record.get("externalIds") if isinstance(record.get("externalIds"), dict) else {}
    other_doi = normalize_doi(external.get("DOI") or record.get("doi"))
    if doi and other_doi:
        return doi == other_doi
    other_title = str(record.get("title") or "")
    if title and other_title:
        if not _titles_agree(title, other_title):
            return False
        other_year = str(record.get("year") or external.get("year") or "").strip()[:4]
        if year and other_year and other_year.isdigit() and abs(int(year[:4] or 0) - int(other_year)) > 1:
            return False
        return True
    return False


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            try:
                return float(str(value).strip())
            except Exception:
                return None
    return None


def _titles_agree(left: str, right: str) -> bool:
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 30 and (a.startswith(b[:60]) or b.startswith(a[:60])):
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 30 and shorter[:80] in longer


def _parse_tei(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8", "ignore")
    payload: dict[str, Any] = {
        "parser": "tei",
        "text_chars": len(text),
        "sections": [s.strip() for s in re.findall(r"<head[^>]*>(.*?)</head>", text, flags=re.S)][:60],
        "paragraphs": text.count("<p"),
        "references": text.count("<biblStruct"),
        "figures": text.count("<figure"),
        "formulas": text.count("<formula"),
    }
    payload["sections"] = [re.sub(r"<[^>]+>", "", s).strip()[:80] for s in payload["sections"]]
    return payload

