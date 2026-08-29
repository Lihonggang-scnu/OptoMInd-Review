"""arXiv public API backend — no API key required.

Uses the arXiv public API (https://info.arxiv.org/help/api/index.html).
Rate limit: 1 request per 3 seconds (polite).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

ARXIV_API_BASE = "http://export.arxiv.org/api/query"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
RATE_LIMIT_SECONDS = 3.0

_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


class ArxivBackend:
    """Search arXiv, fetch metadata, and download PDFs via the public API."""

    def __init__(self, rate_limit: float | None = None) -> None:
        self._last_request = 0.0
        self._rate_limit = rate_limit if rate_limit is not None else RATE_LIMIT_SECONDS
        self.stats: Dict[str, int] = {"requests": 0, "errors": 0, "pdfs_downloaded": 0}

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)

    def search(
        self,
        query: str,
        max_results: int = 10,
        categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search arXiv and return normalized SourceRecord dicts."""
        params: Dict[str, str] = {
            "search_query": query,
            "max_results": str(min(max_results, 100)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        if categories:
            cat_str = " OR ".join(f"cat:{c}" for c in categories)
            params["search_query"] = f"({query}) AND ({cat_str})"

        url = ARXIV_API_BASE + "?" + urllib.parse.urlencode(params)
        raw = self._fetch_xml(url)
        if raw is None:
            return []

        results: List[Dict[str, Any]] = []
        root = ET.fromstring(raw)
        for entry in root.findall("atom:entry", _NAMESPACES):
            source = self._parse_entry(entry, query)
            if source:
                results.append(normalize_arxiv_result(source))
        return results

    def fetch_metadata(self, arxiv_id: str) -> Optional[Dict[str, Any]]:
        """Fetch metadata for a single arXiv ID."""
        clean_id = arxiv_id.strip().replace("arXiv:", "").replace("arxiv:", "")
        url = f"{ARXIV_API_BASE}?id_list={clean_id}"
        raw = self._fetch_xml(url)
        if raw is None:
            return None
        root = ET.fromstring(raw)
        entry = root.find("atom:entry", _NAMESPACES)
        if entry is None:
            return None
        source = self._parse_entry(entry, clean_id)
        return normalize_arxiv_result(source) if source else None

    def download_pdf(self, arxiv_id: str, output_dir: str) -> Optional[str]:
        """Download the PDF for an arXiv ID. Returns the local file path or None."""
        clean_id = arxiv_id.strip().replace("arXiv:", "").replace("arxiv:", "")
        pdf_url = f"https://arxiv.org/pdf/{clean_id}.pdf"
        dest = Path(output_dir) / f"{clean_id.replace('/', '_')}.pdf"
        try:
            self._respect_rate_limit()
            req = urllib.request.Request(
                pdf_url,
                headers={"User-Agent": "OptoMind/0.1 (mailto:anonymous@example.com)"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.read())
            self.stats["pdfs_downloaded"] += 1
            return str(dest.resolve())
        except Exception:
            return None

    def _fetch_xml(self, url: str) -> Optional[str]:
        self._respect_rate_limit()
        self.stats["requests"] += 1
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OptoMind/0.1 (mailto:anonymous@example.com)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            self.stats["errors"] += 1
            return None

    def _parse_entry(
        self, entry: ET.Element, query: str
    ) -> Optional[Dict[str, Any]]:
        try:
            arxiv_id = _text(entry.find("atom:id", _NAMESPACES)).split("/abs/")[-1]
            title = _text(entry.find("atom:title", _NAMESPACES))
            abstract = _text(entry.find("atom:summary", _NAMESPACES))
            published = _text(entry.find("atom:published", _NAMESPACES))

            authors: List[str] = []
            for author_el in entry.findall("atom:author", _NAMESPACES):
                name = _text(author_el.find("atom:name", _NAMESPACES))
                if name:
                    authors.append(name)

            year = None
            if published:
                try:
                    year = int(published[:4])
                except ValueError:
                    pass

            doi = ""
            for link in entry.findall("atom:link", _NAMESPACES):
                href = link.attrib.get("href", "")
                if "doi.org" in href:
                    doi = href.split("doi.org/")[-1] if "doi.org/" in href else href

            abstract_or_snippet = abstract
            has_abstract = bool(abstract and len(abstract) > 50)
            evidence_extraction_ready = has_abstract

            categories = []
            for cat in entry.findall("atom:category", _NAMESPACES):
                cat_term = cat.attrib.get("term", "")
                if cat_term:
                    categories.append(cat_term)

            source = {
                "source_id": f"arxiv:{arxiv_id}",
                "title": title,
                "authors": authors,
                "year": year,
                "doi": doi,
                "url_or_doi": f"https://doi.org/{doi}" if doi else f"https://arxiv.org/abs/{arxiv_id}",
                "arxiv_id": arxiv_id,
                "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                "source_url": f"https://arxiv.org/abs/{arxiv_id}",
                "journal_or_venue": "arXiv preprint",
                "publisher": "arXiv",
                "abstract_or_snippet": abstract_or_snippet,
                "query": query,
                "retrieval_method": "arxiv_api",
                "backend": "arxiv",
                "verification_status": "verified" if doi else "verified_url",
                "evidence_extraction_ready": evidence_extraction_ready,
                "relevance_score": 0.5,
                "raw_metadata": {
                    "published": published,
                    "updated": _text(entry.find("atom:updated", _NAMESPACES)),
                    "comment": _text(entry.find("arxiv:comment", _NAMESPACES)),
                    "primary_category": categories[0] if categories else "",
                    "categories": categories,
                },
            }
            return source
        except Exception:
            return None


def normalize_arxiv_result(source: Dict[str, Any]) -> Dict[str, Any]:
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
        "backend": "arxiv",
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
