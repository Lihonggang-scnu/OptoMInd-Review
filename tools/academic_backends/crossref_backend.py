"""Crossref public REST API backend — no API key required.

Uses the Crossref REST API (https://api.crossref.org/).
Optional CONTACT_EMAIL env var for polite pool access.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

CROSSREF_API_BASE = "https://api.crossref.org/works"
RATE_LIMIT_SECONDS = 1.0


def _contact_email() -> Optional[str]:
    return os.environ.get("CONTACT_EMAIL") or None


class CrossrefBackend:
    """Search Crossref and verify DOIs via the public REST API."""

    def __init__(self, rate_limit: float | None = None) -> None:
        self._last_request = 0.0
        self._rate_limit = rate_limit if rate_limit is not None else RATE_LIMIT_SECONDS
        self.stats: Dict[str, int] = {"requests": 0, "errors": 0}

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)

    def search(
        self,
        query: str,
        max_results: int = 10,
        rows: int | None = None,
        from_year: int | None = None,
        sort: str = "relevance",
    ) -> List[Dict[str, Any]]:
        """Search Crossref works and return normalized SourceRecord dicts."""
        rows_val = min(rows or max_results, 100)
        params: Dict[str, Any] = {
            "query.bibliographic": query,
            "rows": str(rows_val),
            "sort": sort,
            "order": "desc",
        }
        if from_year and int(from_year) > 0:
            params["filter"] = (
                f"from-pub-date:{int(from_year)}-01-01,"
                "type:journal-article"
            )
        email = _contact_email()
        if email:
            params["mailto"] = email

        url = CROSSREF_API_BASE + "?" + urllib.parse.urlencode(params)
        data = self._fetch_json(url)
        if data is None:
            return []

        results: List[Dict[str, Any]] = []
        items = data.get("message", {}).get("items", [])
        for item in items:
            source = self._parse_item(item, query)
            if source:
                results.append(normalize_crossref_result(source))
        return results[:max_results]

    def verify_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """Look up a single DOI and return normalized metadata."""
        clean_doi = doi.strip()
        url = f"{CROSSREF_API_BASE}/{urllib.parse.quote(clean_doi, safe='')}"
        email = _contact_email()
        if email:
            url += f"?mailto={urllib.parse.quote(email)}"

        data = self._fetch_json(url)
        if data is None:
            return None
        item = data.get("message", {})
        if not item:
            return None
        source = self._parse_item(item, clean_doi)
        return normalize_crossref_result(source) if source else None

    def _fetch_json(self, url: str) -> Optional[Dict[str, Any]]:
        self._respect_rate_limit()
        self.stats["requests"] += 1
        headers = {
            "User-Agent": "OptoMind/0.1 (mailto:anonymous@example.com); https://github.com/anonymous/optomind",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            self.stats["errors"] += 1
            return None

    def _parse_item(
        self, item: Dict[str, Any], query: str
    ) -> Optional[Dict[str, Any]]:
        try:
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""

            authors: List[str] = []
            for a in item.get("author", []):
                given = a.get("given", "")
                family = a.get("family", "")
                name = f"{given} {family}".strip()
                if name:
                    authors.append(name)

            year = None
            issued = item.get("issued", {})
            date_parts = issued.get("date-parts", [[]])
            if date_parts and date_parts[0]:
                year = date_parts[0][0]

            doi = item.get("DOI", "")
            url = f"https://doi.org/{doi}" if doi else ""

            abstract = item.get("abstract", "")
            abstract_or_snippet = abstract
            has_abstract = bool(abstract and len(abstract) > 30)
            evidence_extraction_ready = has_abstract

            container = item.get("container-title", [""])[0] if item.get("container-title") else ""
            publisher = item.get("publisher", "")
            pdf_url = ""
            for link in item.get("link", []) or []:
                content_type = str(link.get("content-type", "")).lower()
                if "pdf" in content_type and link.get("URL"):
                    pdf_url = str(link["URL"])
                    break

            source = {
                "source_id": f"crossref:{doi}" if doi else f"crossref:{title[:80]}",
                "title": title,
                "authors": authors,
                "year": year,
                "doi": doi,
                "url_or_doi": url,
                "source_url": url,
                "pdf_url": pdf_url,
                "journal_or_venue": container,
                "publisher": publisher,
                "abstract_or_snippet": abstract_or_snippet,
                "query": query,
                "retrieval_method": "crossref_api",
                "backend": "crossref",
                "verification_status": "verified" if doi else "unverified",
                "evidence_extraction_ready": evidence_extraction_ready,
                "relevance_score": 0.5,
                "raw_metadata": {
                    "type": item.get("type", ""),
                    "issn": item.get("ISSN", []),
                    "is_referenced_by_count": item.get(
                        "is-referenced-by-count",
                        0,
                    ),
                    "subject": item.get("subject", []),
                },
            }
            return source
        except Exception:
            return None


def normalize_crossref_result(source: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure all expected SourceRecord fields are present."""
    defaults = {
        "source_id": "",
        "title": "",
        "authors": [],
        "year": None,
        "doi": "",
        "url_or_doi": "",
        "arxiv_id": "",
        "semantic_scholar_paper_id": "",
        "openalex_id": "",
        "source_url": "",
        "pdf_url": "",
        "journal_or_venue": "",
        "publisher": "",
        "abstract_or_snippet": "",
        "query": "",
        "query_id": "",
        "retrieval_method": "",
        "backend": "crossref",
        "verification_status": "unverified",
        "evidence_extraction_ready": False,
        "relevance_score": 0.0,
        "backend_score": 0.0,
        "source_quality_score": 0.0,
        "raw_metadata": {},
        "local_pdf_path": "",
        "local_text_path": "",
        "local_chunks_path": "",
        "notes": "",
    }
    for k, v in defaults.items():
        source.setdefault(k, v)
    return source
