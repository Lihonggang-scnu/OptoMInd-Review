"""Local import backend — import PDFs, BibTeX, and metadata JSON.

No API keys required. Users manually export from CNKI/Wanfang/Google Scholar
and import via these tools.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class LocalImportBackend:
    """Import local academic data: BibTeX, metadata JSON, PDFs."""

    def __init__(self) -> None:
        self.stats: Dict[str, int] = {"bibtex_imported": 0, "json_imported": 0, "pdf_imported": 0, "errors": 0}

    def import_bibtex(self, path: str) -> List[Dict[str, Any]]:
        """Parse a .bib file and return normalized SourceRecord dicts."""
        bib_path = Path(path)
        if not bib_path.exists():
            self.stats["errors"] += 1
            return []

        raw = bib_path.read_text(encoding="utf-8", errors="replace")
        records: List[Dict[str, Any]] = []
        entries = re.findall(r'@\w+\s*\{[^@]*\}', raw, re.DOTALL | re.IGNORECASE)
        for entry in entries:
            source = self._parse_bibtex_entry(entry)
            if source:
                records.append(source)
                self.stats["bibtex_imported"] += 1
        return records

    def import_metadata_json(self, path: str) -> List[Dict[str, Any]]:
        """Import a metadata JSON file (list or single object)."""
        json_path = Path(path)
        if not json_path.exists():
            self.stats["errors"] += 1
            return []

        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.stats["errors"] += 1
            return []

        items = data if isinstance(data, list) else [data]
        records: List[Dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                source = self._normalize_json_metadata(item)
                records.append(source)
                self.stats["json_imported"] += 1
        return records

    def import_pdf(self, path: str) -> Optional[Dict[str, Any]]:
        """Register a local PDF. Returns a placeholder SourceRecord with local_pdf_path set."""
        pdf_path = Path(path).resolve()
        if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
            self.stats["errors"] += 1
            return None

        filename = pdf_path.stem
        source = {
            "source_id": f"local:{filename}",
            "title": filename.replace("_", " ").replace("-", " "),
            "authors": [],
            "year": None,
            "doi": "",
            "url_or_doi": "",
            "source_url": "",
            "pdf_url": "",
            "journal_or_venue": "",
            "publisher": "",
            "abstract_or_snippet": "",
            "query": "",
            "query_id": "",
            "retrieval_method": "local_import",
            "backend": "local_import",
            "verification_status": "unverified",
            "evidence_extraction_ready": False,
            "relevance_score": 0.0,
            "backend_score": 0.0,
            "source_quality_score": 0.0,
            "raw_metadata": {"file_size": pdf_path.stat().st_size, "filename": pdf_path.name},
            "local_pdf_path": str(pdf_path),
            "local_text_path": "",
            "local_chunks_path": "",
            "notes": "Imported from local PDF. Parse with PdfParserBackend to extract text.",
        }
        self.stats["pdf_imported"] += 1
        return normalize_local_result(source)

    def _parse_bibtex_entry(self, entry: str) -> Optional[Dict[str, Any]]:
        try:
            entry_type_match = re.match(r'@(\w+)\s*\{', entry)
            entry_type = entry_type_match.group(1) if entry_type_match else "misc"
            cite_key_match = re.match(r'@\w+\s*\{([^,]+),', entry)
            cite_key = cite_key_match.group(1) if cite_key_match else ""

            fields: Dict[str, str] = {}
            for match in re.finditer(r'(\w+)\s*=\s*[{"]([^}"]*)[}"]', entry):
                fields[match.group(1).lower()] = match.group(2)
            for match in re.finditer(r'(\w+)\s*=\s*\{([^}]*)\}', entry):
                fields[match.group(1).lower()] = match.group(2)

            title = fields.get("title", "")
            authors_raw = fields.get("author", "")
            authors = [a.strip() for a in authors_raw.split(" and ")] if authors_raw else []
            year = None
            try:
                year = int(fields.get("year", ""))
            except (ValueError, TypeError):
                pass
            doi = fields.get("doi", "")
            journal = fields.get("journal", fields.get("booktitle", ""))
            abstract = fields.get("abstract", fields.get("note", ""))
            url = fields.get("url", "")

            source = {
                "source_id": f"local:{cite_key}" if cite_key else f"local:{title[:80]}",
                "title": title,
                "authors": authors,
                "year": year,
                "doi": doi,
                "url_or_doi": f"https://doi.org/{doi}" if doi else url,
                "source_url": url,
                "journal_or_venue": journal,
                "publisher": fields.get("publisher", ""),
                "abstract_or_snippet": abstract,
                "query": "",
                "query_id": "",
                "retrieval_method": "local_import_bibtex",
                "backend": "local_import",
                "verification_status": "verified" if doi else "unverified",
                "evidence_extraction_ready": bool(abstract and len(abstract) > 30),
                "relevance_score": 0.0,
                "source_quality_score": 0.5,
                "raw_metadata": {"entry_type": entry_type, "cite_key": cite_key, "raw_fields": fields},
            }
            return normalize_local_result(source)
        except Exception:
            self.stats["errors"] += 1
            return None

    def _normalize_json_metadata(self, item: Dict[str, Any]) -> Dict[str, Any]:
        title = str(item.get("title", item.get("Title", item.get("name", ""))))
        doi = str(item.get("doi", item.get("DOI", "")))
        url = str(item.get("url", item.get("URL", item.get("link", ""))))
        abstract = str(item.get("abstract", item.get("Abstract", item.get("description", ""))))
        year = item.get("year", item.get("Year", item.get("publication_year", None)))
        journal = str(item.get("journal", item.get("Journal", item.get("venue", item.get("source", "")))))

        authors: List[str] = []
        authors_raw = item.get("authors", item.get("Authors", item.get("author", [])))
        if isinstance(authors_raw, list):
            for a in authors_raw:
                if isinstance(a, dict):
                    name = a.get("name", a.get("full_name", ""))
                elif isinstance(a, str):
                    name = a
                else:
                    continue
                if name:
                    authors.append(str(name))
        elif isinstance(authors_raw, str):
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]

        source = {
            "source_id": f"local:{title[:80]}" if title else f"local:{doi}",
            "title": title,
            "authors": authors,
            "year": int(year) if year else None,
            "doi": doi,
            "url_or_doi": f"https://doi.org/{doi}" if doi else url,
            "source_url": url,
            "journal_or_venue": journal,
            "publisher": str(item.get("publisher", item.get("Publisher", ""))),
            "abstract_or_snippet": abstract,
            "query": "",
            "query_id": "",
            "retrieval_method": "local_import_json",
            "backend": "local_import",
            "verification_status": "verified" if doi else "unverified",
            "evidence_extraction_ready": bool(abstract and len(abstract) > 30),
            "relevance_score": 0.0,
            "source_quality_score": 0.5,
            "raw_metadata": {"import_source": "metadata_json", "original_keys": list(item.keys())},
        }
        return normalize_local_result(source)


def normalize_local_result(source: Dict[str, Any]) -> Dict[str, Any]:
    defaults = {
        "source_id": "", "title": "", "authors": [], "year": None,
        "doi": "", "url_or_doi": "", "arxiv_id": "",
        "semantic_scholar_paper_id": "", "openalex_id": "",
        "source_url": "", "pdf_url": "", "journal_or_venue": "",
        "publisher": "", "abstract_or_snippet": "", "query": "",
        "query_id": "", "retrieval_method": "", "backend": "local_import",
        "verification_status": "unverified", "evidence_extraction_ready": False,
        "relevance_score": 0.0, "backend_score": 0.0,
        "source_quality_score": 0.0, "raw_metadata": {},
        "local_pdf_path": "", "local_text_path": "",
        "local_chunks_path": "", "notes": "",
    }
    for k, v in defaults.items():
        source.setdefault(k, v)
    return source
