"""Injectable OpenAlex adapter for final publication metadata enrichment.

This module keeps every live-network interaction behind a lazy import so the
publication metadata resolver remains offline by default.  The injectable
adapter accepts one request mapping with two string keys:

    {"kind": "doi", "value": "10.1234/example"}
    {"kind": "title", "value": "An Example Paper Title"}

and returns a resolver-shaped mapping (``title``, ``authors``, ``year``,
``venue``, ``doi``, ``url``, ``openalex_id``) or ``None`` when no usable
record is found.

DOI lookups are direct work lookups.  Title lookups are accepted only when
the normalized OpenAlex result title matches the requested title, so an
unrelated top search hit can never fabricate final citation metadata.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Mapping


OpenAlexProvider = Callable[[Mapping[str, str]], Mapping[str, Any] | None]

OPENALEX_LOOKUP_KINDS = ("doi", "title")


def _normalize_title_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def normalize_openalex_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map one shared-backend OpenAlex record to resolver bibliographic fields."""

    authors = [
        str(author).strip()
        for author in (record.get("authors") or [])
        if str(author).strip()
    ]
    year = record.get("year")
    doi = str(record.get("doi") or "").strip()
    for prefix in ("https://doi.org/", "http://dx.doi.org/"):
        if doi.casefold().startswith(prefix):
            doi = doi[len(prefix) :]
            break
    return {
        "title": str(record.get("title") or "").strip(),
        "authors": authors,
        "year": str(year or "").strip(),
        "venue": str(
            record.get("journal_or_venue") or record.get("venue") or ""
        ).strip(),
        "doi": doi,
        "url": str(
            record.get("url_or_doi")
            or record.get("source_url")
            or ""
        ).strip(),
        "openalex_id": str(record.get("openalex_id") or "").strip(),
    }


def make_default_openalex_provider() -> OpenAlexProvider:
    """Real OpenAlex DOI/title lookup via the existing backend (lazy import)."""

    def lookup(request: Mapping[str, str]) -> dict[str, Any] | None:
        kind = str(request.get("kind") or "").strip().casefold()
        value = str(request.get("value") or "").strip()
        if kind not in OPENALEX_LOOKUP_KINDS or not value:
            return None

        from tools.academic_backends.openalex_backend import OpenAlexBackend

        backend = OpenAlexBackend(rate_limit=0.25)
        if kind == "doi":
            record = backend.get_work(value)
        else:
            matches = backend.search(value, max_results=1)
            record = matches[0] if matches else None
        if not record:
            return None

        normalized = normalize_openalex_record(record)
        if kind == "title" and (
            not normalized["title"]
            or _normalize_title_key(value) != _normalize_title_key(
                normalized["title"]
            )
        ):
            return None
        return normalized

    return lookup


__all__ = [
    "OpenAlexProvider",
    "normalize_openalex_record",
    "make_default_openalex_provider",
]
