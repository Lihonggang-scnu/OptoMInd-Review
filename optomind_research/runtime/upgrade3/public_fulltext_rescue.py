"""Bounded public full-text candidate handling for local material acquisition.

This module deliberately stops at candidate discovery.  It never turns a
provider URL, search result, or snippet into document content.  The caller
must fetch the candidate and validate the returned source identity before it
can be published as material.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


RESCUE_SCHEMA_VERSION = "optomind.public_fulltext_rescue.v1"
_SECRET_QUERY_NAMES = {"api_key", "apikey", "key", "token", "access_token", "email"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _redact_url(value: Any) -> str:
    text = _text(value)
    try:
        parsed = urllib.parse.urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return text[:1000]
        query = [
            (key, "<redacted>" if key.casefold() in _SECRET_QUERY_NAMES else val)
            for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
        )[:1000]
    except Exception:
        return text[:1000]


def _url_key(value: Any) -> str:
    text = _redact_url(value)
    try:
        parsed = urllib.parse.urlsplit(text)
        # Hosts and schemes are case-insensitive; URL paths may be
        # case-sensitive and must remain exact for acquisition/cache identity.
        return urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, parsed.query, ""))
    except Exception:
        return text


def _is_metadata_endpoint(value: Any) -> bool:
    parsed = urllib.parse.urlsplit(_text(value))
    host = parsed.netloc.casefold()
    path = parsed.path.casefold()
    if host in {"api.openalex.org", "api.unpaywall.org", "api.semanticscholar.org", "api.crossref.org", "export.arxiv.org", "api.firecrawl.dev", "r.jina.ai"}:
        return True
    if host in {"doi.org", "dx.doi.org"}:
        return True
    if host.endswith("openalex.org") and path.startswith("/works/"):
        return True
    return False


def _is_http_url(value: Any) -> bool:
    parsed = urllib.parse.urlsplit(_text(value))
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _iter_mappings(value: Any, depth: int = 4) -> Iterable[Mapping[str, Any]]:
    if depth < 0 or not isinstance(value, Mapping):
        return
    yield value
    for child in value.values():
        if isinstance(child, Mapping):
            yield from _iter_mappings(child, depth - 1)
        elif isinstance(child, (list, tuple)):
            for item in child:
                if isinstance(item, Mapping):
                    yield from _iter_mappings(item, depth - 1)


def _version_fields(mapping: Mapping[str, Any]) -> dict[str, str]:
    """Copy provider version information without inventing a version."""

    values: dict[str, str] = {}
    for target, keys in {
        "publication_version": ("publication_version", "publicationVersion", "version_id", "versionId"),
        "version_label": ("version", "version_label", "versionLabel", "source_version"),
        "version_kind": ("version_kind", "versionKind", "host_type", "type"),
        "license": ("license", "oa_license", "best_oa_license"),
    }.items():
        for key in keys:
            value = _text(mapping.get(key))
            if value:
                values[target] = value[:200]
                break
    return values


@dataclass(frozen=True)
class PublicFulltextCandidate:
    """One exact public URL candidate retained for later source validation."""

    url: str
    kind: str
    provider: str
    discovered_from: str
    priority: int = 100
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def url_sha256(self) -> str:
        return hashlib.sha256(_redact_url(self.url).encode("utf-8")).hexdigest()

    def as_route_metadata(self) -> dict[str, Any]:
        metadata = dict(self.metadata)
        metadata.update(
            {
                "rescue_schema_version": RESCUE_SCHEMA_VERSION,
                "candidate_provider": self.provider,
                "candidate_kind": self.kind,
                "discovered_from": self.discovered_from,
                "candidate_url_sha256": self.url_sha256,
                "identity_source_required": True,
                "allow_version_variant": True,
            }
        )
        return metadata


def _candidate_kind(key: str, url: str, label: str = "") -> str:
    haystack = " ".join((key, url, label)).casefold()
    path = urllib.parse.urlsplit(url).path.casefold()
    if ".pdf" in path or "pdf" in haystack or "download" in haystack:
        return "pdf"
    if any(token in haystack for token in ("xml", "jats", "tei", "fulltext")):
        return "jats"
    return "html"


def _candidate_urls(mapping: Mapping[str, Any]) -> Iterable[tuple[str, str, str]]:
    """Yield (field, URL, label) from one provider mapping."""

    direct_fields = (
        "pdf_url", "url_for_pdf", "open_access_pdf", "open_access_url",
        "best_oa_url", "download_url", "downloadUrl", "fulltext_url",
        "fullTextURL", "jats_url", "fulltext_xml_url", "fullTextXML",
        "tei_url", "grobid_xml", "landing_page_url", "html_url", "source_url",
        "url", "url_or_doi",
    )
    for key in direct_fields:
        value = mapping.get(key)
        if isinstance(value, str) and _is_http_url(value) and not _is_metadata_endpoint(value):
            yield key, value, key


def _source_label(mapping: Mapping[str, Any]) -> str:
    return _text(
        mapping.get("backend")
        or mapping.get("provider")
        or mapping.get("source")
        or mapping.get("host_type")
        or "provider"
    )[:120]


def collect_public_candidates(
    identity: Mapping[str, Any],
    provider_records: Sequence[Mapping[str, Any]] = (),
    *,
    max_candidates: int = 8,
) -> list[PublicFulltextCandidate]:
    """Collect a small deterministic set of provider URLs.

    The function only accepts URLs explicitly returned by providers.  It does
    not construct publisher URLs from a DOI, use search snippets, or infer a
    repository host.  A caller may pass additional public search records later
    through ``provider_records``; exact URL de-duplication keeps the budget
    bounded.
    """

    limit = max(0, min(64, int(max_candidates)))
    if not limit:
        return []
    sources: list[Mapping[str, Any]] = [identity]
    sources.extend(item for item in provider_records if isinstance(item, Mapping))
    seen: set[str] = set()
    candidates: list[PublicFulltextCandidate] = []
    for mapping in sources:
        provider = _source_label(mapping)
        for nested in _iter_mappings(mapping):
            nested_provider = _source_label(nested)
            if nested_provider == "provider":
                nested_provider = provider
            locations = []
            for key in ("best_oa_location", "primary_location"):
                value = nested.get(key)
                if isinstance(value, Mapping):
                    locations.append((key, value))
            for key in ("locations", "oa_locations", "fulltext_routes"):
                value = nested.get(key)
                if isinstance(value, Mapping):
                    value = [value]
                if isinstance(value, (list, tuple)):
                    locations.extend((key, item) for item in value if isinstance(item, Mapping))
            location_mappings = [nested, *(item for _, item in locations)]
            for location_name, location in locations:
                for field, url, label in _candidate_urls(location):
                    metadata = dict(location)
                    metadata.update(_version_fields(nested))
                    metadata.update(_version_fields(location))
                    metadata.update(
                        {
                            "provider": nested_provider,
                            "location_field": location_name,
                            "source_record_title": _text(nested.get("title")),
                            "source_record_doi": _text(nested.get("doi")),
                        }
                    )
                    key = _url_key(url)
                    if key in seen:
                        continue
                    seen.add(key)
                    kind = _candidate_kind(field, url, label)
                    candidate_priority = 20 if nested_provider.casefold() in {"firecrawl", "tavily", "serper", "brave"} else (32 if kind in {"jats", "tei"} else (42 if kind == "pdf" else 52))
                    candidates.append(
                        PublicFulltextCandidate(
                            url=url,
                            kind=kind,
                            provider=nested_provider,
                            discovered_from=location_name,
                            priority=candidate_priority,
                            metadata=metadata,
                        )
                    )
            # Include direct fields once.  Nested location mappings above are
            # preferred because they retain repository/license/version data.
            if not locations:
                for field, url, label in _candidate_urls(nested):
                    key = _url_key(url)
                    if key in seen:
                        continue
                    seen.add(key)
                    kind = _candidate_kind(field, url, label)
                    metadata = dict(nested)
                    metadata.update(_version_fields(nested))
                    metadata["provider"] = nested_provider
                    metadata["location_field"] = field
                    candidate_priority = 20 if nested_provider.casefold() in {"firecrawl", "tavily", "serper", "brave"} else (32 if kind in {"jats", "tei"} else (42 if kind == "pdf" else 52))
                    candidates.append(
                        PublicFulltextCandidate(
                            url=url,
                            kind=kind,
                            provider=nested_provider,
                            discovered_from=field,
                            priority=candidate_priority,
                            metadata=metadata,
                        )
                    )
            if len(candidates) >= limit * 3:
                break
        if len(candidates) >= limit * 3:
            break
    candidates.sort(key=lambda item: (10 if item.provider == "doi_redirect" else item.priority, int(item.metadata.get("search_rank", 0) or 0), item.provider.casefold(), item.url))
    return candidates[:limit]


def provider_status(provider: Any, *, result: Any = None) -> dict[str, Any]:
    """Classify an empty provider result without turning errors into no-hit."""

    error = _text(getattr(provider, "last_error", "")) if provider is not None else "provider_unavailable"
    status = _text(getattr(provider, "last_status", "")) if provider is not None else ""
    if error or status.casefold() in {"error", "failed", "provider_error"}:
        return {"status": "provider_error", "error": error[:240] or status[:240] or "provider_error"}
    if result is None:
        return {"status": "nohit", "reason": "no_result"}
    if isinstance(result, (list, tuple, Mapping)) and not result:
        return {"status": "nohit", "reason": "empty_result"}
    return {"status": "ok"}


__all__ = [
    "PublicFulltextCandidate",
    "RESCUE_SCHEMA_VERSION",
    "collect_public_candidates",
    "provider_status",
]
