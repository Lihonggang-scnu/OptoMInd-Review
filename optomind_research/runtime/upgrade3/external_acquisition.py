"""The external channel (SM10 reopen, owner directive).

The local semantic cache is a historical asset.  It is deep on what earlier
generations happened to collect and silent on everything published since, so a
role it cannot answer is not evidence that the literature cannot answer it.  The
owner's instruction is explicit: the external channel matters more than the cache,
and the cache is temporary.

This module opens that channel without relaxing a single rule:

* the policy is open access only.  A paywalled candidate is refused, never worked
  around, and no credential of any kind is used to reach a document;
* a candidate is material only when its DOI resolves to bytes that really are a
  PDF of a usable size.  A publisher landing page that says "sign in to continue"
  is refused, not parsed;
* the document is written under the name the run's own DOI lookup already
  understands, so everything downstream -- scope judgement, atoms, cards,
  binding -- consumes it through the existing path with no special case;
* the route, the OA status, the licence and the sha256 are recorded, so a reader
  can always tell where a document came from and under what terms.

Nothing here decides domain, permission or role.  It fetches bytes and says
truthfully where they came from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

EXTERNAL_CHANNEL = "external_open_access"
DEFAULT_PROVIDER = "semantic_scholar"
UA = ("Mozilla/5.0 (compatible; OptoMind-Review/1.0; "
      "+https://example.invalid/optomind)")

#: a PDF shorter than this is a cover page or an error page, not a document
MIN_DOCUMENT_BYTES = 4096

#: keyword intent per role.  The external engine is a keyword search and it
#: matches the whole query, so length is fatal: the same role measured 0 results
#: as a 12-word query and 831 as a 5-word one.  Two keywords per role, and the
#: topic is truncated to make room rather than the keywords.
ROLE_KEYWORDS = {
    "controversy": ("limitations", "misalignment"),
    "foundation": ("diffraction", "propagation"),
    "mechanism": ("mechanism", "modulation"),
    "method": ("training", "backpropagation"),
    "comparison": ("benchmark", "accuracy"),
    "application": ("imaging", "classification"),
    "trend": ("review", "advances"),
    "frontier": ("challenges", "directions"),
    "background": ("overview", "introduction"),
}
_KEYWORD_FALLBACK = ("review",)

#: the longest query the external engine still answers
MAX_QUERY_WORDS = 6


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def external_query_text(
    role: str,
    *,
    topic: str,
    max_words: int = MAX_QUERY_WORDS,
) -> str:
    """A keyword query an external search engine will actually answer.

    The role keywords are the point of the query, so they are kept whole and the
    topic is truncated to fit the budget.
    """

    keywords = ROLE_KEYWORDS.get(_text(role).casefold(), _KEYWORD_FALLBACK)
    budget = max(len(keywords) + 1, int(max_words))
    topic_words = _text(topic).split()[: max(1, budget - len(keywords))]
    return " ".join([*topic_words, *keywords])


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #

def accept_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """May this candidate be fetched at all?  OA only, and only with an identity."""

    doi = _text(candidate.get("doi"))
    if not doi:
        # Identity is not negotiable: a document with no DOI cannot be matched to
        # a served record, so it can never become evidence however good it is.
        return {"accepted": False, "reason": "candidate_without_doi"}
    route = _text(candidate.get("oa_pdf"))
    if not route and not candidate.get("is_oa"):
        return {"accepted": False, "reason": "candidate_without_open_access_route"}
    if not route:
        return {"accepted": False, "reason": "candidate_without_open_access_route"}
    return {"accepted": True, "reason": ""}


def normalise_candidates(
    papers: Iterable[Mapping[str, Any]],
    *,
    known_dois: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Provider records to one shape, deduplicated by DOI, minus what we hold."""

    known = {_text(item).casefold() for item in (known_dois or ()) if _text(item)}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for paper in papers:
        doi = _text(paper.get("doi"))
        key = doi.casefold()
        if not doi or key in seen:
            continue
        seen.add(key)
        if key in known:
            continue
        rows.append({
            "doi": doi,
            "title": _text(paper.get("title")),
            "year": paper.get("year"),
            "abstract": _text(paper.get("abstract")),
            "is_oa": bool(paper.get("is_oa")),
            "oa_status": _text(paper.get("oa_status")),
            "oa_license": _text(paper.get("oa_license")),
            "oa_pdf": _text(paper.get("oa_pdf")),
            "provider": _text(paper.get("provider")) or DEFAULT_PROVIDER,
            "provider_paper_id": _text(paper.get("provider_paper_id")),
        })
    return rows


def _paper_to_mapping(paper: Any) -> dict[str, Any]:
    """A provider record, whether it arrives as an object or as a mapping."""

    if isinstance(paper, Mapping):
        return dict(paper)
    return {
        "doi": _text(getattr(paper, "doi", "")),
        "title": _text(getattr(paper, "title", "")),
        "year": getattr(paper, "year", None),
        "abstract": _text(getattr(paper, "abstract", "")),
        "is_oa": getattr(paper, "is_oa", None),
        "oa_status": _text(getattr(paper, "s2_oa_status", "")),
        "oa_license": _text(getattr(paper, "s2_oa_license", "")),
        "oa_pdf": _text(getattr(paper, "s2_open_access_candidate_url", "")),
        "provider": DEFAULT_PROVIDER,
        "provider_paper_id": _text(getattr(paper, "paper_id", "")),
    }


def search_candidates(
    gateway: Any,
    role: str,
    *,
    topic: str,
    limit: int = 20,
    known_dois: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Ask the external engine for material that could carry this role."""

    query = external_query_text(role, topic=topic)
    papers, response = gateway.search_papers(
        query, limit=int(limit), open_access_pdf=True)
    rows = normalise_candidates(
        (_paper_to_mapping(paper) for paper in papers or ()),
        known_dois=known_dois,
    )
    audit = {
        "channel": EXTERNAL_CHANNEL,
        "provider": DEFAULT_PROVIDER,
        "query": query,
        "requested": int(limit),
        "returned": len(papers or ()),
        "candidates": len(rows),
        "status": _text(getattr(response, "status_category", "")),
        "status_code": getattr(response, "status_code", None),
        "ok": bool(getattr(response, "ok", False)),
        "error": _text(getattr(response, "error", "")),
        "provider_total": (getattr(response, "audit", {}) or {}).get("provider_total"),
    }
    return rows, audit


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

def _document_name(doi: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", _text(doi)).strip("-.")
    return (slug or "document") + ".pdf"


def _default_opener(url: str, headers: Mapping[str, str] | None = None):
    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return (getattr(response, "status", 200),
                dict(response.headers.items()),
                response.read())


def download_document(
    candidate: Mapping[str, Any],
    downloads_dir: str | os.PathLike[str],
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch one candidate's open-access document, or say why it was not used."""

    verdict = accept_candidate(candidate)
    base = {
        "schema_version": "optomind.upgrade3.external_document.v1",
        "channel": EXTERNAL_CHANNEL,
        "doi": _text(candidate.get("doi")),
        "title": _text(candidate.get("title")),
        "provider": _text(candidate.get("provider")) or DEFAULT_PROVIDER,
        "provider_paper_id": _text(candidate.get("provider_paper_id")),
        "source_url": _text(candidate.get("oa_pdf")),
        "oa_status": _text(candidate.get("oa_status")),
        "oa_license": _text(candidate.get("oa_license")),
        "downloaded": False,
        "reason": "",
        "path": "",
        "bytes": 0,
        "sha256": "",
    }
    if not verdict["accepted"]:
        return dict(base, reason=verdict["reason"])

    target_dir = Path(downloads_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _document_name(base["doi"])
    if target.is_file() and target.stat().st_size >= MIN_DOCUMENT_BYTES:
        # Already fetched in a previous round: the bytes are the material, and
        # re-downloading would only risk a different version under one identity.
        return dict(base, downloaded=True, reason="already_downloaded",
                    path=str(target), bytes=target.stat().st_size,
                    sha256=_sha_file(target))

    fetch = opener or _default_opener
    try:
        status, headers, body = fetch(base["source_url"], {"User-Agent": UA})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return dict(base, reason="transport_failure", detail=type(exc).__name__)
    status = int(status or 0)
    if status >= 400:
        return dict(base, reason="http_%d" % status)
    payload = bytes(body or b"")
    if not payload:
        return dict(base, reason="response_is_empty")
    content_type = ""
    for key, value in (headers or {}).items():
        if str(key).casefold() == "content-type":
            content_type = _text(value)
            break
    if not payload.startswith(b"%PDF"):
        # A landing page, a login wall or an error document: refusing on the
        # bytes rather than on the content type, because a server may label
        # anything "application/pdf".
        return dict(base, reason="response_is_not_a_pdf",
                    content_type=content_type, bytes=len(payload))
    if len(payload) < MIN_DOCUMENT_BYTES:
        return dict(base, reason="document_too_small", bytes=len(payload))
    temporary = target.with_name(target.name + ".tmp-%d" % os.getpid())
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return dict(base, downloaded=True, reason="downloaded", path=str(target),
                bytes=len(payload), sha256=_sha_file(target),
                content_type=content_type)


def write_external_manifest(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=1, sort_keys=True)
        + chr(10),
        encoding="utf-8",
    )
    return target

def build_chunks_from_document(
    *,
    pdf_path: str | os.PathLike[str] | None = None,
    pages: Sequence[str] | None = None,
    doi: str,
    paper_id: str,
    title: str = "",
    min_chars: int = 80,
    max_chars: int = 1600,
) -> list[dict[str, Any]]:
    """The knowledge-base rows one externally acquired document contributes.

    The document goes through the project's own parser and the project's own
    paragraph splitter, so an externally acquired paper is materialised exactly
    the way a downloaded paper already is -- real pages, real char ranges, real
    chunk identities.  Depth is fulltext and permission is factual_support
    because that is what an OA full text is; the scope verdict is deliberately
    left unreviewed so the run's own judge decides it.
    """

    from optomind_research.m3_kb_ingest import split_paragraphs
    from optomind_research.runtime.upgrade3.source_resolver import pdf_pages

    page_texts = [str(item) for item in (pages or ())]
    if pages is None:
        if pdf_path is None:
            return []
        try:
            page_texts = [str(item) for item in pdf_pages(str(pdf_path))]
        except Exception:
            return []
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", _text(doi)).strip("-") or "document"
    rows: list[dict[str, Any]] = []
    for page_number, page_text in enumerate(page_texts, start=1):
        cursor = 0
        for ordinal, paragraph in enumerate(
                split_paragraphs(page_text, min_chars=int(min_chars),
                                 max_chars=int(max_chars)), start=1):
            body = _text(paragraph)
            if not body:
                continue
            start = page_text.find(paragraph, cursor)
            if start < 0:
                start = cursor
            end = start + len(paragraph)
            cursor = max(cursor, end)
            chunk_id = "ext:%s:%04d:%04d" % (slug, page_number, ordinal)
            locator = {
                "doi": _text(doi),
                "paper_id": _text(paper_id),
                "chunk_id": chunk_id,
                "page_number": page_number,
                "section_path": "external_open_access",
                "ordinal": ordinal,
                "char_start": start,
                "char_end": end,
            }
            rows.append({
                "chunk_id": chunk_id,
                "paper_id": _text(paper_id),
                "doi": _text(doi),
                "title": _text(title),
                "text": body,
                "text_hash": _sha_text(body),
                "content_depth": "fulltext",
                "use_permission": "factual_support",
                "scope_fit": "unreviewed",
                "source_kind": "fulltext",
                "raw_json": json.dumps({"source_locator": locator},
                                        ensure_ascii=False, sort_keys=True),
            })
    return rows


def _sha_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

def ledger_source_for_candidate(
    candidate: Mapping[str, Any],
    *,
    role: str,
    section_id: str,
    document: Mapping[str, Any],
    chunk_ids: Sequence[str],
) -> dict[str, Any]:
    """The ledger row that registers an externally acquired paper under a role."""

    doi = _text(candidate.get("doi"))
    return {
        "paper_id": "doi:%s" % doi,
        "doi": doi,
        "title": _text(candidate.get("title")),
        "year": candidate.get("year"),
        "venue": "",
        "authors": [],
        "literature_role": _text(role),
        "scope_fit": "unreviewed",
        "retrieval_query": _text(candidate.get("query")),
        "retrieval_backend": "semantic_scholar_graph_search",
        "adoption_reason": (
            "external open-access search for the %s role of %s"
            % (_text(role), _text(section_id))
        ),
        "expected_section_use": _text(section_id),
        "canonical_chunk_ids": [_text(item) for item in chunk_ids if _text(item)],
        "local_prior": False,
        "new_this_run": True,
        "acquisition_status": "fulltext",
        "normalization_status": "",
        "section_id": _text(section_id),
        "not_usable_for": [],
        "discovery_route": "semantic_scholar_graph",
        "materialization_route": "external_open_access_pdf",
        "content_depth": "fulltext",
        "use_permission": "factual_support",
        "allowed_claim_kinds": ["application", "author_synthesis", "background",
                                 "comparison", "measurement", "mechanism", "method",
                                 "trend"],
        "route_events": [{
            "event": "external_open_access_download",
            "route": EXTERNAL_CHANNEL,
            "source_url": _text(document.get("source_url")),
            "sha256": _text(document.get("sha256")),
            "bytes": document.get("bytes"),
            "oa_status": _text(document.get("oa_status")),
            "oa_license": _text(document.get("oa_license")),
        }],
        "metadata_conflicts": [],
        "relation_roles": [],
        "role_provenance": {
            "method": "external_open_access_acquisition",
            "role": _text(role),
            "section_id": _text(section_id),
            "provider": _text(candidate.get("provider")) or DEFAULT_PROVIDER,
            "provider_paper_id": _text(candidate.get("provider_paper_id")),
            "source_url": _text(document.get("source_url")),
            "sha256": _text(document.get("sha256")),
        },
    }
