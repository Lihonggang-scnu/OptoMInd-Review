"""Unified paper metadata index for OptoMind literature resources.

This module builds a normalized metadata layer on top of the legacy
``abstract_papers`` table without deleting or mutating it.  The goal is to make
all academic backends speak one practical contract:

paper_id, title, authors, year, venue, doi, abstract, is_oa,
best_fulltext_url, best_fulltext_format, best_fulltext_route, sources.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP = Path.home() / "Desktop"
API_KEYS_DIR = PROJECT_ROOT / "api_keys"
DEFAULT_LIBRARY_DB = PROJECT_ROOT / "database" / "literature_resource_builder" / "literature_resources.sqlite"
DEFAULT_CORE_API_FILE = API_KEYS_DIR / "core_api.txt"
DEFAULT_UNPAYWALL_EMAIL_FILE = API_KEYS_DIR / "Unpaywall.txt"
DEFAULT_OPENALEX_CONTENT_KEYS_FILE = API_KEYS_DIR / "openalex.txt"
DEFAULT_SEMANTIC_SCHOLAR_API_FILE = API_KEYS_DIR / "semantic-scholar-api-key.txt"
LEGACY_CORE_API_FILE = DESKTOP / "core_api.txt"
LEGACY_UNPAYWALL_EMAIL_FILE = DESKTOP / "Unpaywall.txt"
LEGACY_OPENALEX_CONTENT_KEYS_FILE = DESKTOP / "openalex.txt"
LEGACY_SEMANTIC_SCHOLAR_API_FILE = DESKTOP / "semantic-scholar-api-key.txt"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip().lower()


def normalize_space(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_title_key(text: Any) -> str:
    text = normalize_space(text).casefold()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return normalize_space(text)


def title_match_score(left: Any, right: Any) -> float:
    a = normalize_title_key(left)
    b = normalize_title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def clean_list(items: Iterable[Any], *, limit: int = 100) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text = normalize_space(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def oa_status(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"yes", "true", "1", "oa", "open", "gold", "green", "bronze", "hybrid"}:
            return "yes"
        if lowered in {"no", "false", "0", "closed", "not_oa"}:
            return "no"
        return "unknown"
    return "yes" if bool(value) else "no"


def merge_oa_status(old: str, new: str) -> str:
    old = oa_status(old)
    new = oa_status(new)
    if "yes" in {old, new}:
        return "yes"
    if old == "unknown":
        return new
    if new == "unknown":
        return old
    return "no"


def looks_like_pdf_url(url: str) -> bool:
    lowered = str(url or "").lower().split("?", 1)[0]
    return lowered.endswith(".pdf") or "/pdf" in lowered or "download" in lowered


def infer_format(url: str, *, fallback: str = "") -> str:
    lowered = str(url or "").lower()
    if not url:
        return fallback or "unknown"
    if "grobid-xml" in lowered or lowered.endswith(".tei") or lowered.endswith(".tei.xml"):
        return "tei_xml"
    if lowered.endswith(".xml") or ".xml?" in lowered:
        return "jats_xml" if "pmc" in lowered or "jats" in lowered else "xml"
    if "pmc/articles" in lowered and "report=xml" in lowered:
        return "jats_xml"
    if "arxiv.org/pdf" in lowered:
        return "pdf"
    if looks_like_pdf_url(url):
        return "pdf"
    if lowered.startswith("https://doi.org/"):
        return "publisher_html"
    return fallback or "publisher_html"


def route_for_candidate(url: str, *, is_oa: str, source: str, fmt: str) -> str:
    source_l = source.casefold()
    url_l = str(url or "").casefold()
    if "openalex_content" in source_l:
        return "oa_direct"
    if fmt in {"jats_xml", "tei_xml"} or "pmc" in source_l:
        return "pmc_xml"
    if "arxiv" in source_l or "arxiv.org" in url_l:
        return "arxiv"
    if "core" in source_l:
        return "core"
    if "unpaywall" in source_l:
        return "oa_direct"
    if is_oa == "yes" and fmt in {"pdf", "publisher_html", "html", "xml", "tei_xml", "jats_xml"}:
        return "oa_direct"
    if url_l.startswith("https://doi.org/") or "doi.org/" in url_l:
        return "institution_publisher_html"
    return "oa_direct" if is_oa == "yes" else "institution_publisher_html"


@dataclass
class FulltextCandidate:
    paper_id: str
    url: str
    format: str
    route: str
    source: str
    is_oa: str = "unknown"
    confidence: float = 0.5
    license: str = ""
    raw: dict[str, Any] | None = None


def candidate_rank(candidate: FulltextCandidate) -> tuple[int, float]:
    route_order = {
        "pmc_xml": 100,
        "arxiv": 94,
        "core": 90,
        "unpaywall": 88,
        "oa_direct": 84,
        "institution_publisher_html": 70,
        "manual_needed": 10,
    }
    format_order = {
        "jats_xml": 40,
        "publisher_html": 34,
        "html": 30,
        "pdf": 26,
        "unknown": 0,
    }
    oa_bonus = 10 if candidate.is_oa == "yes" else 0
    return (route_order.get(candidate.route, 0) + format_order.get(candidate.format, 0) + oa_bonus, candidate.confidence)


class MetadataIndex:
    def __init__(self, db_path: str | Path = DEFAULT_LIBRARY_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_metadata (
                paper_id TEXT PRIMARY KEY,
                title TEXT,
                authors_json TEXT,
                year INTEGER,
                venue TEXT,
                doi TEXT,
                abstract TEXT,
                is_oa TEXT DEFAULT 'unknown',
                best_fulltext_url TEXT,
                best_fulltext_format TEXT,
                best_fulltext_route TEXT,
                sources_json TEXT,
                citation_count INTEGER,
                openalex_id TEXT,
                semantic_scholar_id TEXT,
                metadata_completeness REAL,
                missing_fields_json TEXT,
                source_paper_id TEXT,
                llm_pseudo_abstract TEXT,
                llm_pseudo_abstract_source_json TEXT,
                llm_pseudo_abstract_model TEXT,
                llm_pseudo_abstract_updated_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paper_metadata_doi ON paper_metadata(doi);
            CREATE INDEX IF NOT EXISTS idx_paper_metadata_is_oa ON paper_metadata(is_oa);
            CREATE INDEX IF NOT EXISTS idx_paper_metadata_route ON paper_metadata(best_fulltext_route);

            CREATE TABLE IF NOT EXISTS paper_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT,
                title TEXT,
                doi TEXT,
                abstract_present INTEGER,
                is_oa TEXT DEFAULT 'unknown',
                url TEXT,
                format TEXT,
                raw_json TEXT,
                updated_at TEXT,
                UNIQUE(paper_id, source, source_id)
            );

            CREATE INDEX IF NOT EXISTS idx_paper_sources_paper ON paper_sources(paper_id);
            CREATE INDEX IF NOT EXISTS idx_paper_sources_source ON paper_sources(source);

            CREATE TABLE IF NOT EXISTS fulltext_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT NOT NULL,
                url TEXT NOT NULL,
                format TEXT,
                route TEXT,
                source TEXT,
                is_oa TEXT DEFAULT 'unknown',
                confidence REAL DEFAULT 0.5,
                license TEXT,
                status TEXT DEFAULT 'candidate',
                raw_json TEXT,
                updated_at TEXT,
                UNIQUE(paper_id, url, format, source)
            );

            CREATE INDEX IF NOT EXISTS idx_fulltext_candidates_paper ON fulltext_candidates(paper_id);
            CREATE INDEX IF NOT EXISTS idx_fulltext_candidates_route ON fulltext_candidates(route);
            CREATE INDEX IF NOT EXISTS idx_fulltext_candidates_is_oa ON fulltext_candidates(is_oa);

            CREATE TABLE IF NOT EXISTS metadata_enrichment_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                backends_json TEXT,
                limit_count INTEGER,
                candidates_seen INTEGER,
                papers_updated INTEGER,
                api_calls INTEGER,
                errors INTEGER,
                report_json TEXT
            );

            DROP VIEW IF EXISTS paper_index_view;
            CREATE VIEW paper_index_view AS
            SELECT
                paper_id,
                title,
                authors_json AS authors,
                year,
                venue,
                doi,
                abstract,
                CASE
                    WHEN abstract IS NOT NULL AND length(trim(abstract))>0 THEN 'verified_metadata_abstract'
                    WHEN llm_pseudo_abstract IS NOT NULL AND length(trim(llm_pseudo_abstract))>0 THEN 'pseudo_summary_only_not_evidence'
                    ELSE 'missing'
                END AS abstract_status,
                llm_pseudo_abstract,
                llm_pseudo_abstract_source_json,
                llm_pseudo_abstract_model,
                is_oa,
                best_fulltext_url AS url,
                best_fulltext_format AS format,
                best_fulltext_route AS route,
                sources_json AS source,
                citation_count,
                metadata_completeness,
                missing_fields_json,
                updated_at
            FROM paper_metadata;
            """
        )
        self.ensure_optional_columns()
        self.recreate_paper_index_view()
        self.conn.commit()

    def ensure_optional_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(paper_metadata)").fetchall()}
        optional = {
            "llm_pseudo_abstract": "TEXT",
            "llm_pseudo_abstract_source_json": "TEXT",
            "llm_pseudo_abstract_model": "TEXT",
            "llm_pseudo_abstract_updated_at": "TEXT",
        }
        for name, column_type in optional.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE paper_metadata ADD COLUMN {name} {column_type}")

    def recreate_paper_index_view(self) -> None:
        self.conn.executescript(
            """
            DROP VIEW IF EXISTS paper_index_view;
            CREATE VIEW paper_index_view AS
            SELECT
                paper_id,
                title,
                authors_json AS authors,
                year,
                venue,
                doi,
                abstract,
                CASE
                    WHEN abstract IS NOT NULL AND length(trim(abstract))>0 THEN 'verified_metadata_abstract'
                    WHEN llm_pseudo_abstract IS NOT NULL AND length(trim(llm_pseudo_abstract))>0 THEN 'pseudo_summary_only_not_evidence'
                    ELSE 'missing'
                END AS abstract_status,
                llm_pseudo_abstract,
                llm_pseudo_abstract_source_json,
                llm_pseudo_abstract_model,
                is_oa,
                best_fulltext_url AS url,
                best_fulltext_format AS format,
                best_fulltext_route AS route,
                sources_json AS source,
                citation_count,
                metadata_completeness,
                missing_fields_json,
                updated_at
            FROM paper_metadata;
            """
        )

    def migrate_from_legacy(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM abstract_papers").fetchall()
        migrated = 0
        for row in rows:
            self.upsert_from_legacy_row(row)
            migrated += 1
        self.conn.commit()
        return {"legacy_abstract_papers": len(rows), "migrated_or_refreshed": migrated}

    def upsert_from_legacy_row(self, row: sqlite3.Row) -> None:
        paper_id = str(row["paper_id"] or "")
        if not paper_id:
            return
        authors = json_loads(row["authors_json"], [])
        sources = clean_list(json_loads(row["source_apis_json"], []), limit=50)
        raw = json_loads(row["raw_json"], {})
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        status = oa_status(row["open_access"])
        if status == "unknown":
            status = oa_status(raw_meta.get("is_oa"))
        candidates = self.legacy_candidates(paper_id, row, status, raw)
        best = max(candidates, key=candidate_rank) if candidates else None
        data = {
            "paper_id": paper_id,
            "title": row["title"] or "",
            "authors_json": json.dumps(authors, ensure_ascii=False),
            "year": row["year"],
            "venue": row["venue"] or "",
            "doi": normalize_doi(row["doi"]),
            "abstract": row["abstract"] or "",
            "is_oa": status,
            "best_fulltext_url": best.url if best else "",
            "best_fulltext_format": best.format if best else "",
            "best_fulltext_route": best.route if best else "manual_needed",
            "sources_json": json.dumps(sources, ensure_ascii=False),
            "citation_count": row["citation_count"],
            "openalex_id": row["openalex_id"] or "",
            "semantic_scholar_id": row["semantic_scholar_id"] or "",
            "source_paper_id": paper_id,
        }
        data["metadata_completeness"], missing = self.completeness(data)
        data["missing_fields_json"] = json.dumps(missing, ensure_ascii=False)
        data["created_at"] = row["created_at"] or utc_now()
        data["updated_at"] = utc_now()
        self.upsert_paper_metadata(data)
        for source in sources or ["unknown"]:
            self.upsert_paper_source(
                paper_id=paper_id,
                source=source,
                source_id=str(raw.get("source_id") or row["openalex_id"] or row["semantic_scholar_id"] or row["doi"] or paper_id),
                title=data["title"],
                doi=data["doi"],
                abstract_present=bool(data["abstract"]),
                is_oa=status,
                url=row["landing_page_url"] or row["pdf_url"] or "",
                fmt=infer_format(row["pdf_url"] or row["landing_page_url"] or ""),
                raw=raw,
            )
        for candidate in candidates:
            self.upsert_fulltext_candidate(candidate)

    def legacy_candidates(self, paper_id: str, row: sqlite3.Row, status: str, raw: dict[str, Any]) -> list[FulltextCandidate]:
        candidates: list[FulltextCandidate] = []

        def add(url: str, source: str, *, fmt: str | None = None, is_oa: str | None = None, confidence: float = 0.5, license_: str = "", raw_item: dict[str, Any] | None = None) -> None:
            url = str(url or "").strip()
            if not url or not re.match(r"^https?://", url, re.I):
                return
            c_oa = oa_status(is_oa if is_oa is not None else status)
            c_fmt = fmt or infer_format(url)
            candidates.append(
                FulltextCandidate(
                    paper_id=paper_id,
                    url=url,
                    format=c_fmt,
                    route=route_for_candidate(url, is_oa=c_oa, source=source, fmt=c_fmt),
                    source=source,
                    is_oa=c_oa,
                    confidence=confidence,
                    license=license_,
                    raw=raw_item or {},
                )
            )

        pdf_url = str(row["pdf_url"] or "")
        landing = str(row["landing_page_url"] or "")
        doi = normalize_doi(row["doi"])
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        if pdf_url:
            add(pdf_url, "legacy_pdf_url", fmt="pdf", confidence=0.72)
        best_oa = raw_meta.get("best_oa_location") if isinstance(raw_meta.get("best_oa_location"), dict) else {}
        if best_oa:
            add(best_oa.get("url_for_pdf") or best_oa.get("pdf_url") or "", "openalex_best_oa", fmt="pdf", is_oa="yes", confidence=0.9, license_=str(best_oa.get("license") or ""), raw_item=best_oa)
            add(best_oa.get("url") or "", "openalex_best_oa", is_oa="yes", confidence=0.82, license_=str(best_oa.get("license") or ""), raw_item=best_oa)
        open_pdf = raw_meta.get("open_access_pdf") if isinstance(raw_meta.get("open_access_pdf"), dict) else {}
        if open_pdf:
            add(open_pdf.get("url") or open_pdf.get("url_for_pdf") or "", "semantic_scholar_open_access_pdf", fmt="pdf", is_oa="yes", confidence=0.82, raw_item=open_pdf)
        oa_locations = raw_meta.get("oa_locations") if isinstance(raw_meta.get("oa_locations"), list) else []
        for loc in oa_locations[:8]:
            if not isinstance(loc, dict):
                continue
            add(loc.get("url_for_pdf") or loc.get("pdf_url") or "", "openalex_oa_location", fmt="pdf", is_oa="yes", confidence=0.8, license_=str(loc.get("license") or ""), raw_item=loc)
            add(loc.get("url") or "", "openalex_oa_location", is_oa="yes", confidence=0.72, license_=str(loc.get("license") or ""), raw_item=loc)
        arxiv_id = str(raw.get("arxiv_id") or "").strip()
        if arxiv_id:
            add(f"https://arxiv.org/pdf/{arxiv_id}.pdf", "arxiv", fmt="pdf", is_oa="yes", confidence=0.95, raw_item={"arxiv_id": arxiv_id})
        if landing:
            add(landing, "legacy_landing", confidence=0.45)
        if doi:
            add(f"https://doi.org/{doi}", "doi", fmt="publisher_html", is_oa=status, confidence=0.5)
        return self.deduplicate_candidates(candidates)

    @staticmethod
    def deduplicate_candidates(candidates: list[FulltextCandidate]) -> list[FulltextCandidate]:
        best_by_key: dict[tuple[str, str, str], FulltextCandidate] = {}
        for candidate in candidates:
            key = (candidate.url, candidate.format, candidate.source)
            old = best_by_key.get(key)
            if old is None or candidate_rank(candidate) > candidate_rank(old):
                best_by_key[key] = candidate
        return list(best_by_key.values())

    def upsert_paper_metadata(self, data: dict[str, Any]) -> None:
        columns = [
            "paper_id", "title", "authors_json", "year", "venue", "doi", "abstract", "is_oa",
            "best_fulltext_url", "best_fulltext_format", "best_fulltext_route", "sources_json",
            "citation_count", "openalex_id", "semantic_scholar_id", "metadata_completeness",
            "missing_fields_json", "source_paper_id", "created_at", "updated_at",
        ]
        placeholders = ",".join("?" for _ in columns)
        update = ",".join(f"{col}=excluded.{col}" for col in columns if col != "paper_id")
        self.conn.execute(
            f"INSERT INTO paper_metadata ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(paper_id) DO UPDATE SET {update}",
            [data.get(col) for col in columns],
        )

    def upsert_paper_source(self, *, paper_id: str, source: str, source_id: str, title: str, doi: str, abstract_present: bool, is_oa: str, url: str, fmt: str, raw: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_sources
                (paper_id, source, source_id, title, doi, abstract_present, is_oa, url, format, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, source, source_id) DO UPDATE SET
                title=excluded.title,
                doi=excluded.doi,
                abstract_present=excluded.abstract_present,
                is_oa=excluded.is_oa,
                url=excluded.url,
                format=excluded.format,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                paper_id,
                source or "unknown",
                source_id or "",
                title or "",
                doi or "",
                1 if abstract_present else 0,
                oa_status(is_oa),
                url or "",
                fmt or infer_format(url),
                json.dumps(raw or {}, ensure_ascii=False, default=str),
                utc_now(),
            ),
        )

    def upsert_fulltext_candidate(self, candidate: FulltextCandidate) -> None:
        self.conn.execute(
            """
            INSERT INTO fulltext_candidates
                (paper_id, url, format, route, source, is_oa, confidence, license, raw_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, url, format, source) DO UPDATE SET
                route=excluded.route,
                is_oa=excluded.is_oa,
                confidence=max(fulltext_candidates.confidence, excluded.confidence),
                license=COALESCE(NULLIF(excluded.license,''), fulltext_candidates.license),
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                candidate.paper_id,
                candidate.url,
                candidate.format,
                candidate.route,
                candidate.source,
                oa_status(candidate.is_oa),
                float(candidate.confidence),
                candidate.license or "",
                json.dumps(candidate.raw or {}, ensure_ascii=False, default=str),
                utc_now(),
            ),
        )

    @staticmethod
    def completeness(data: dict[str, Any]) -> tuple[float, list[str]]:
        required = ["paper_id", "title", "authors_json", "year", "venue", "doi", "abstract", "is_oa", "best_fulltext_url", "sources_json"]
        missing: list[str] = []
        score = 0
        for field in required:
            value = data.get(field)
            if field == "is_oa":
                present = oa_status(value) != "unknown"
            elif field == "authors_json":
                present = bool(json_loads(value, []))
            elif field == "sources_json":
                present = bool(json_loads(value, []))
            else:
                present = value is not None and str(value).strip() != ""
            if present:
                score += 1
            else:
                missing.append(field)
        return round(score / len(required), 3), missing

    def stats(self) -> dict[str, Any]:
        def one(query: str, params: tuple[Any, ...] = ()) -> Any:
            row = self.conn.execute(query, params).fetchone()
            return row[0] if row else None

        def rows(query: str) -> list[dict[str, Any]]:
            return [dict(r) for r in self.conn.execute(query).fetchall()]

        return {
            "legacy_abstract_papers": one("SELECT count(*) FROM abstract_papers"),
            "paper_metadata": one("SELECT count(*) FROM paper_metadata"),
            "paper_sources": one("SELECT count(*) FROM paper_sources"),
            "fulltext_candidates": one("SELECT count(*) FROM fulltext_candidates"),
            "is_oa": rows("SELECT is_oa, count(*) AS n FROM paper_metadata GROUP BY is_oa ORDER BY n DESC"),
            "has_abstract": rows("SELECT CASE WHEN abstract IS NOT NULL AND length(trim(abstract))>0 THEN 1 ELSE 0 END AS has_abstract, count(*) AS n FROM paper_metadata GROUP BY has_abstract"),
            "has_doi": rows("SELECT CASE WHEN doi IS NOT NULL AND doi!='' THEN 1 ELSE 0 END AS has_doi, count(*) AS n FROM paper_metadata GROUP BY has_doi"),
            "has_best_url": rows("SELECT CASE WHEN best_fulltext_url IS NOT NULL AND best_fulltext_url!='' THEN 1 ELSE 0 END AS has_best_url, count(*) AS n FROM paper_metadata GROUP BY has_best_url"),
            "route": rows("SELECT best_fulltext_route AS route, count(*) AS n FROM paper_metadata GROUP BY best_fulltext_route ORDER BY n DESC"),
            "format": rows("SELECT best_fulltext_format AS format, count(*) AS n FROM paper_metadata GROUP BY best_fulltext_format ORDER BY n DESC"),
            "source": rows("SELECT source, count(*) AS n FROM paper_sources GROUP BY source ORDER BY n DESC LIMIT 30"),
            "candidate_route": rows("SELECT route, count(*) AS n FROM fulltext_candidates GROUP BY route ORDER BY n DESC"),
            "candidate_format": rows("SELECT format, count(*) AS n FROM fulltext_candidates GROUP BY format ORDER BY n DESC"),
            "missing_fields": self.missing_field_counts(),
        }

    def missing_field_counts(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in self.conn.execute("SELECT missing_fields_json FROM paper_metadata").fetchall():
            for field in json_loads(row["missing_fields_json"], []):
                counts[str(field)] = counts.get(str(field), 0) + 1
        return [{"field": key, "n": value} for key, value in sorted(counts.items(), key=lambda item: item[1], reverse=True)]

    def sync_legacy_abstract_papers(self) -> dict[str, int]:
        """Backfill legacy abstract_papers from normalized metadata safely.

        Several current LRB paths still read ``abstract_papers`` directly. This
        only fills missing/unknown legacy values and never downgrades an
        existing longer abstract.
        """
        if not self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='abstract_papers'").fetchone():
            return {"rows_seen": 0, "abstracts_filled": 0, "oa_filled": 0, "pdf_urls_filled": 0, "landing_urls_filled": 0}
        rows = self.conn.execute(
            """
            SELECT
                a.paper_id,
                a.abstract AS legacy_abstract,
                a.open_access AS legacy_open_access,
                a.pdf_url AS legacy_pdf_url,
                a.landing_page_url AS legacy_landing_page_url,
                m.abstract AS metadata_abstract,
                m.is_oa,
                m.best_fulltext_url,
                m.best_fulltext_format
            FROM abstract_papers a
            JOIN paper_metadata m ON m.source_paper_id=a.paper_id OR m.paper_id=a.paper_id
            """
        ).fetchall()
        counts = {"rows_seen": len(rows), "abstracts_filled": 0, "oa_filled": 0, "oa_upgraded_to_yes": 0, "pdf_urls_filled": 0, "landing_urls_filled": 0}
        for row in rows:
            updates: dict[str, Any] = {}
            legacy_abs = normalize_space(row["legacy_abstract"])
            meta_abs = normalize_space(row["metadata_abstract"])
            if meta_abs and len(meta_abs) > max(30, len(legacy_abs)):
                updates["abstract"] = meta_abs
                counts["abstracts_filled"] += 1
            if row["legacy_open_access"] is None and row["is_oa"] in {"yes", "no"}:
                updates["open_access"] = 1 if row["is_oa"] == "yes" else 0
                counts["oa_filled"] += 1
            elif row["legacy_open_access"] == 0 and row["is_oa"] == "yes":
                updates["open_access"] = 1
                counts["oa_upgraded_to_yes"] += 1
            best_url = normalize_space(row["best_fulltext_url"])
            if best_url and row["best_fulltext_format"] == "pdf" and not normalize_space(row["legacy_pdf_url"]):
                updates["pdf_url"] = best_url
                counts["pdf_urls_filled"] += 1
            if best_url and not normalize_space(row["legacy_landing_page_url"]):
                updates["landing_page_url"] = best_url
                counts["landing_urls_filled"] += 1
            if updates:
                updates["updated_at"] = utc_now()
                assignments = ", ".join(f"{key}=?" for key in updates)
                self.conn.execute(
                    f"UPDATE abstract_papers SET {assignments} WHERE paper_id=?",
                    [*updates.values(), row["paper_id"]],
                )
        self.conn.commit()
        return counts

    def enrichment_targets(self, *, limit: int, only_missing: bool = True, source_filter: str = "") -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if only_missing:
            clauses.append(
                """
                (
                    is_oa='unknown'
                    OR abstract IS NULL OR abstract=''
                    OR doi IS NULL OR doi=''
                    OR venue IS NULL OR venue=''
                    OR year IS NULL
                    OR authors_json IS NULL OR authors_json='' OR authors_json='[]'
                    OR best_fulltext_url IS NULL OR best_fulltext_url=''
                )
                """
            )
        clauses.append("(doi IS NOT NULL AND doi!='' OR title IS NOT NULL AND title!='')")
        if source_filter:
            clauses.append("lower(sources_json) LIKE ?")
            params.append(f"%{source_filter.casefold()}%")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        query = f"""
            SELECT * FROM paper_metadata
            {where}
            ORDER BY
                CASE WHEN doi IS NOT NULL AND doi!='' THEN 0 ELSE 1 END,
                metadata_completeness ASC,
                year DESC
            LIMIT ?
        """
        params.append(int(limit))
        return self.conn.execute(query, params).fetchall()

    def enrich(self, *, backends: list[str], limit: int = 50, only_missing: bool = True, sleep_seconds: float = 0.2, source_filter: str = "") -> dict[str, Any]:
        ensure_api_env()
        targets = self.enrichment_targets(limit=limit, only_missing=only_missing, source_filter=source_filter)
        started = utc_now()
        api_calls = 0
        errors = 0
        updated = 0
        for row in targets:
            paper_changed = False
            for backend in backends:
                try:
                    if backend == "openalex":
                        data = self.lookup_openalex(row)
                    elif backend == "unpaywall":
                        data = self.lookup_unpaywall(row)
                    elif backend == "core":
                        data = self.lookup_core(row)
                    elif backend == "crossref":
                        data = self.lookup_crossref(row)
                    elif backend in {"semantic_scholar", "s2"}:
                        data = self.lookup_semantic_scholar(row)
                    else:
                        continue
                    api_calls += 1
                    if data:
                        paper_changed = self.apply_enrichment(row["paper_id"], backend, data) or paper_changed
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                except Exception:
                    errors += 1
            if paper_changed:
                updated += 1
        self.refresh_best_candidates()
        finished = utc_now()
        report = {
            "started_at": started,
            "finished_at": finished,
            "backends": backends,
            "limit": limit,
            "source_filter": source_filter,
            "candidates_seen": len(targets),
            "papers_updated": updated,
            "api_calls": api_calls,
            "errors": errors,
            "stats": self.stats(),
        }
        self.conn.execute(
            """
            INSERT INTO metadata_enrichment_runs
                (started_at, finished_at, backends_json, limit_count, candidates_seen, papers_updated, api_calls, errors, report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (started, finished, json.dumps(backends), limit, len(targets), updated, api_calls, errors, json.dumps(report, ensure_ascii=False, default=str)),
        )
        self.conn.commit()
        return report

    def lookup_openalex(self, row: sqlite3.Row) -> dict[str, Any] | None:
        doi = normalize_doi(row["doi"])
        email = os.environ.get("OPENALEX_EMAIL") or os.environ.get("CONTACT_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
        if not doi:
            title = normalize_space(row["title"])
            if not title:
                return None
            url = f"https://api.openalex.org/works?search={urllib.parse.quote(title)}&per-page=5"
            if email:
                url += "&mailto=" + urllib.parse.quote(email)
            req = urllib.request.Request(url, headers={"User-Agent": "OptoMind/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                results = payload.get("results") if isinstance(payload, dict) else []
                return self.select_best_title_match(title, results, min_score=0.72)
            except Exception:
                return None
        url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='')}"
        if email:
            url += "?mailto=" + urllib.parse.quote(email)
        req = urllib.request.Request(url, headers={"User-Agent": "OptoMind/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None

    def lookup_unpaywall(self, row: sqlite3.Row) -> dict[str, Any] | None:
        doi = normalize_doi(row["doi"])
        if not doi:
            return None
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend

        return UnpaywallBackend().lookup(doi)

    def lookup_core(self, row: sqlite3.Row) -> dict[str, Any] | None:
        from tools.academic_backends.core_backend import CoreBackend

        backend = CoreBackend()
        if not backend.enabled:
            return None
        query = normalize_doi(row["doi"]) or normalize_space(row["title"])
        if not query:
            return None
        results = backend.search(query, max_results=3)
        if not results:
            return None
        doi = normalize_doi(row["doi"])
        if doi:
            for item in results:
                if normalize_doi(item.get("doi")) == doi:
                    return item
        return results[0]

    def lookup_crossref(self, row: sqlite3.Row) -> dict[str, Any] | None:
        doi = normalize_doi(row["doi"])
        if not doi:
            return None
        from tools.academic_backends.crossref_backend import CrossrefBackend

        return CrossrefBackend(rate_limit=0.25).verify_doi(doi)

    def lookup_semantic_scholar(self, row: sqlite3.Row) -> dict[str, Any] | None:
        doi = normalize_doi(row["doi"])
        fields = "paperId,title,abstract,year,authors,url,venue,journal,externalIds,citationCount,publicationTypes,openAccessPdf,isOpenAccess"
        if doi:
            url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(doi, safe='')}?fields={urllib.parse.quote(fields)}"
        else:
            title = normalize_space(row["title"])
            if not title:
                return None
            url = (
                "https://api.semanticscholar.org/graph/v1/paper/search"
                f"?query={urllib.parse.quote(title)}&limit=5&fields={urllib.parse.quote(fields)}"
            )
        headers = {"User-Agent": "OptoMind/1.0"}
        key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if key:
            headers["x-api-key"] = key
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if doi:
                if payload and (payload.get("abstract") or payload.get("title")):
                    return payload
                title = normalize_space(row["title"])
                return self.lookup_semantic_scholar_by_title(title, fields, headers)
            return self.select_best_title_match(row["title"], payload.get("data", []), min_score=0.72)
        except Exception:
            if doi:
                try:
                    title = normalize_space(row["title"])
                    return self.lookup_semantic_scholar_by_title(title, fields, headers)
                except Exception:
                    pass
            return None

    @staticmethod
    def select_best_title_match(title: str, results: Any, *, min_score: float) -> dict[str, Any] | None:
        if not isinstance(results, list):
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for item in results:
            if not isinstance(item, dict):
                continue
            score = title_match_score(title, item.get("title"))
            if best is None or score > best[0]:
                best = (score, item)
        if best and best[0] >= min_score:
            return best[1]
        return None

    @staticmethod
    def lookup_semantic_scholar_by_title(title: str, fields: str, headers: dict[str, str]) -> dict[str, Any] | None:
        title = normalize_space(title)
        if not title:
            return None
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={urllib.parse.quote(title)}&limit=5&fields={urllib.parse.quote(fields)}"
        )
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return MetadataIndex.select_best_title_match(title, payload.get("data", []), min_score=0.72)

    def apply_enrichment(self, paper_id: str, backend: str, data: dict[str, Any]) -> bool:
        row = self.conn.execute("SELECT * FROM paper_metadata WHERE paper_id=?", (paper_id,)).fetchone()
        if not row:
            return False
        title = row["title"] or normalize_space(data.get("title"))
        abstract = row["abstract"] or self.extract_abstract(backend, data)
        doi = normalize_doi(row["doi"] or data.get("doi") or (data.get("externalIds") or {}).get("DOI"))
        authors_json = row["authors_json"] if json_loads(row["authors_json"], []) else self.extract_authors_json(backend, data)
        year = row["year"] or self.extract_year(backend, data)
        venue = row["venue"] or self.extract_venue(backend, data)
        citation_count = row["citation_count"]
        new_citations = self.extract_citation_count(backend, data)
        if citation_count is None:
            citation_count = new_citations
        elif new_citations is not None:
            citation_count = max(int(citation_count or 0), int(new_citations))
        openalex_id = row["openalex_id"] or self.extract_openalex_id(backend, data)
        semantic_scholar_id = row["semantic_scholar_id"] or self.extract_semantic_scholar_id(backend, data)
        current_oa = oa_status(row["is_oa"])
        source_oa, candidates = self.enrichment_candidates(paper_id, backend, data)
        new_oa = merge_oa_status(current_oa, source_oa)
        sources = clean_list(json_loads(row["sources_json"], []) + [backend], limit=50)
        self.upsert_paper_source(
            paper_id=paper_id,
            source=backend,
            source_id=str(data.get("id") or data.get("source_id") or data.get("paperId") or doi or paper_id),
            title=title,
            doi=doi,
            abstract_present=bool(abstract),
            is_oa=source_oa,
            url=self.extract_primary_url(backend, data),
            fmt=infer_format(self.extract_primary_url(backend, data)),
            raw=data,
        )
        for candidate in candidates:
            self.upsert_fulltext_candidate(candidate)
        temp = dict(row)
        temp.update(
            {
                "title": title,
                "authors_json": authors_json,
                "year": year,
                "venue": venue,
                "abstract": abstract,
                "doi": doi,
                "is_oa": new_oa,
                "sources_json": json.dumps(sources, ensure_ascii=False),
                "citation_count": citation_count,
                "openalex_id": openalex_id,
                "semantic_scholar_id": semantic_scholar_id,
            }
        )
        temp["metadata_completeness"], missing = self.completeness(temp)
        temp["missing_fields_json"] = json.dumps(missing, ensure_ascii=False)
        temp["updated_at"] = utc_now()
        self.conn.execute(
            """
            UPDATE paper_metadata
            SET title=?, authors_json=?, year=?, venue=?, abstract=?, doi=?, is_oa=?,
                sources_json=?, citation_count=?, openalex_id=?, semantic_scholar_id=?,
                metadata_completeness=?, missing_fields_json=?, updated_at=?
            WHERE paper_id=?
            """,
            (
                temp["title"],
                temp["authors_json"],
                temp["year"],
                temp["venue"],
                temp["abstract"],
                temp["doi"],
                temp["is_oa"],
                temp["sources_json"],
                temp["citation_count"],
                temp["openalex_id"],
                temp["semantic_scholar_id"],
                temp["metadata_completeness"],
                temp["missing_fields_json"],
                temp["updated_at"],
                paper_id,
            ),
        )
        self.conn.commit()
        return True

    @staticmethod
    def extract_authors_json(backend: str, data: dict[str, Any]) -> str:
        names: list[str] = []
        if backend == "openalex":
            for item in data.get("authorships") or []:
                if isinstance(item, dict):
                    author = item.get("author") if isinstance(item.get("author"), dict) else {}
                    names.append(author.get("display_name") or item.get("raw_author_name") or "")
        elif backend in {"semantic_scholar", "s2"}:
            for item in data.get("authors") or []:
                if isinstance(item, dict):
                    names.append(item.get("name") or "")
        elif backend == "core":
            raw = data.get("authors")
            if isinstance(raw, list):
                names.extend(str(x) for x in raw)
            else:
                names.extend(re.split(r";|,", str(raw or "")))
        elif backend == "unpaywall":
            for item in data.get("z_authors") or []:
                if isinstance(item, dict):
                    full = normalize_space(" ".join(str(item.get(k) or "") for k in ("given", "family")))
                    names.append(full or item.get("name") or "")
        return json.dumps(clean_list(names, limit=50), ensure_ascii=False)

    @staticmethod
    def extract_year(backend: str, data: dict[str, Any]) -> int | None:
        value = data.get("publication_year") or data.get("year") or data.get("published_year")
        if not value:
            date_text = str(data.get("publication_date") or data.get("published_date") or data.get("published") or "")
            m = re.search(r"(19|20)\d{2}", date_text)
            value = m.group(0) if m else None
        try:
            return int(value) if value else None
        except Exception:
            return None

    @staticmethod
    def extract_venue(backend: str, data: dict[str, Any]) -> str:
        if backend == "openalex":
            primary = data.get("primary_location") if isinstance(data.get("primary_location"), dict) else {}
            source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
            host = data.get("host_venue") if isinstance(data.get("host_venue"), dict) else {}
            return normalize_space(source.get("display_name") or host.get("display_name"))
        if backend in {"semantic_scholar", "s2"}:
            journal = data.get("journal") if isinstance(data.get("journal"), dict) else {}
            return normalize_space(data.get("venue") or journal.get("name"))
        if backend == "core":
            return normalize_space(data.get("publisher") or data.get("journal") or data.get("venue"))
        return normalize_space(data.get("journal_name") or data.get("publisher") or data.get("venue"))

    @staticmethod
    def extract_citation_count(backend: str, data: dict[str, Any]) -> int | None:
        value = data.get("cited_by_count") if backend == "openalex" else data.get("citationCount") or data.get("citation_count")
        try:
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def extract_openalex_id(backend: str, data: dict[str, Any]) -> str:
        if backend == "openalex":
            return str(data.get("id") or "")
        ids = data.get("externalIds") if isinstance(data.get("externalIds"), dict) else {}
        return str(ids.get("OpenAlex") or "")

    @staticmethod
    def extract_semantic_scholar_id(backend: str, data: dict[str, Any]) -> str:
        if backend in {"semantic_scholar", "s2"}:
            return str(data.get("paperId") or "")
        ids = data.get("externalIds") if isinstance(data.get("externalIds"), dict) else {}
        return str(ids.get("CorpusId") or ids.get("SemanticScholar") or "")

    @staticmethod
    def extract_abstract(backend: str, data: dict[str, Any]) -> str:
        if backend == "openalex":
            inv = data.get("abstract_inverted_index")
            if isinstance(inv, dict):
                words: list[tuple[int, str]] = []
                for word, positions in inv.items():
                    for pos in positions or []:
                        try:
                            words.append((int(pos), str(word)))
                        except Exception:
                            pass
                return normalize_space(" ".join(word for _pos, word in sorted(words)))
        if backend == "core":
            return normalize_space(data.get("abstract_or_snippet") or data.get("abstract"))
        if backend == "crossref":
            raw = normalize_space(data.get("abstract_or_snippet") or data.get("abstract"))
            raw = re.sub(r"</?jats:[^>]+>", " ", raw, flags=re.I)
            raw = re.sub(r"<[^>]+>", " ", raw)
            return normalize_space(raw)
        if backend in {"semantic_scholar", "s2"}:
            return normalize_space(data.get("abstract"))
        return normalize_space(data.get("abstract") or data.get("abstract_or_snippet"))

    @staticmethod
    def extract_primary_url(backend: str, data: dict[str, Any]) -> str:
        if backend == "openalex":
            primary = data.get("primary_location") or {}
            best = data.get("best_oa_location") or {}
            return str(best.get("url_for_pdf") or best.get("pdf_url") or best.get("url") or primary.get("landing_page_url") or data.get("id") or "")
        if backend == "unpaywall":
            return str(data.get("best_oa_url") or "")
        if backend == "core":
            return str(data.get("pdf_url") or data.get("source_url") or data.get("url_or_doi") or "")
        if backend == "crossref":
            return str(data.get("pdf_url") or data.get("source_url") or data.get("url_or_doi") or "")
        if backend in {"semantic_scholar", "s2"}:
            oa_pdf = data.get("openAccessPdf") if isinstance(data.get("openAccessPdf"), dict) else {}
            return str(oa_pdf.get("url") or data.get("url") or "")
        return str(data.get("url") or data.get("source_url") or data.get("url_or_doi") or "")

    def enrichment_candidates(self, paper_id: str, backend: str, data: dict[str, Any]) -> tuple[str, list[FulltextCandidate]]:
        candidates: list[FulltextCandidate] = []

        def add(url: str, source: str, *, fmt: str | None = None, is_oa: str = "unknown", confidence: float = 0.7, license_: str = "", raw_item: dict[str, Any] | None = None) -> None:
            url = str(url or "").strip()
            if not url:
                return
            c_fmt = fmt or infer_format(url)
            candidates.append(
                FulltextCandidate(
                    paper_id=paper_id,
                    url=url,
                    format=c_fmt,
                    route=route_for_candidate(url, is_oa=oa_status(is_oa), source=source, fmt=c_fmt),
                    source=source,
                    is_oa=oa_status(is_oa),
                    confidence=confidence,
                    license=license_,
                    raw=raw_item or {},
                )
            )

        status = "unknown"
        if backend == "openalex":
            open_access = data.get("open_access") if isinstance(data.get("open_access"), dict) else {}
            status = oa_status(open_access.get("is_oa"))
            content_urls = data.get("content_urls") if isinstance(data.get("content_urls"), dict) else {}
            if content_urls:
                add(content_urls.get("grobid_xml") or "", "openalex_content", fmt="tei_xml", is_oa="yes", confidence=0.96, raw_item={"content_url_type": "grobid_xml"})
                add(content_urls.get("pdf") or "", "openalex_content", fmt="pdf", is_oa="yes", confidence=0.95, raw_item={"content_url_type": "pdf"})
            best = data.get("best_oa_location") if isinstance(data.get("best_oa_location"), dict) else {}
            if best:
                add(best.get("url_for_pdf") or best.get("pdf_url") or "", "openalex_best_oa", fmt="pdf", is_oa="yes", confidence=0.92, license_=str(best.get("license") or ""), raw_item=best)
                add(best.get("url") or "", "openalex_best_oa", is_oa="yes", confidence=0.84, license_=str(best.get("license") or ""), raw_item=best)
            for loc in (data.get("locations") or data.get("oa_locations") or [])[:8]:
                if not isinstance(loc, dict):
                    continue
                add(loc.get("pdf_url") or loc.get("url_for_pdf") or "", "openalex_location", fmt="pdf", is_oa=status, confidence=0.78, raw_item=loc)
                add(loc.get("landing_page_url") or loc.get("url") or "", "openalex_location", is_oa=status, confidence=0.7, raw_item=loc)
        elif backend == "unpaywall":
            status = oa_status(data.get("is_oa"))
            add(data.get("best_oa_url") or "", "unpaywall", is_oa=status, confidence=0.9, license_=str(data.get("best_oa_license") or ""), raw_item=data)
            for loc in data.get("oa_locations", []) or []:
                if not isinstance(loc, dict):
                    continue
                add(loc.get("url_for_pdf") or "", "unpaywall", fmt="pdf", is_oa="yes", confidence=0.85, license_=str(loc.get("license") or ""), raw_item=loc)
                add(loc.get("url") or "", "unpaywall", is_oa="yes", confidence=0.78, license_=str(loc.get("license") or ""), raw_item=loc)
        elif backend == "core":
            status = "yes"
            add(data.get("pdf_url") or data.get("source_url") or data.get("url_or_doi") or "", "core", is_oa="yes", confidence=0.82, raw_item=data)
        elif backend == "crossref":
            status = "unknown"
            add(data.get("pdf_url") or data.get("source_url") or data.get("url_or_doi") or "", "crossref", is_oa="unknown", confidence=0.48, raw_item=data)
        elif backend in {"semantic_scholar", "s2"}:
            status = oa_status(data.get("isOpenAccess"))
            oa_pdf = data.get("openAccessPdf") if isinstance(data.get("openAccessPdf"), dict) else {}
            if oa_pdf:
                status = "yes"
                add(oa_pdf.get("url") or "", "semantic_scholar_open_access_pdf", fmt="pdf", is_oa="yes", confidence=0.82, raw_item=oa_pdf)
        return status, self.deduplicate_candidates(candidates)

    def refresh_best_candidates(self) -> None:
        rows = self.conn.execute("SELECT * FROM paper_metadata").fetchall()
        for row in rows:
            candidates: list[FulltextCandidate] = []
            for c in self.conn.execute("SELECT * FROM fulltext_candidates WHERE paper_id=?", (row["paper_id"],)).fetchall():
                fmt = c["format"] or infer_format(c["url"])
                route = route_for_candidate(c["url"], is_oa=c["is_oa"] or "unknown", source=c["source"] or "", fmt=fmt)
                if route != c["route"]:
                    self.conn.execute("UPDATE fulltext_candidates SET route=?, updated_at=? WHERE id=?", (route, utc_now(), c["id"]))
                candidates.append(
                    FulltextCandidate(
                        paper_id=row["paper_id"],
                        url=c["url"],
                        format=fmt,
                        route=route,
                        source=c["source"] or "",
                        is_oa=c["is_oa"] or "unknown",
                        confidence=float(c["confidence"] or 0.5),
                        license=c["license"] or "",
                    )
                )
            best = max(candidates, key=candidate_rank) if candidates else None
            temp = dict(row)
            if best:
                temp.update({"best_fulltext_url": best.url, "best_fulltext_format": best.format, "best_fulltext_route": best.route})
            temp["metadata_completeness"], missing = self.completeness(temp)
            temp["missing_fields_json"] = json.dumps(missing, ensure_ascii=False)
            self.conn.execute(
                """
                UPDATE paper_metadata
                SET best_fulltext_url=?, best_fulltext_format=?, best_fulltext_route=?,
                    metadata_completeness=?, missing_fields_json=?, updated_at=?
                WHERE paper_id=?
                """,
                (
                    temp.get("best_fulltext_url") or "",
                    temp.get("best_fulltext_format") or "",
                    temp.get("best_fulltext_route") or "manual_needed",
                    temp["metadata_completeness"],
                    temp["missing_fields_json"],
                    utc_now(),
                    row["paper_id"],
                ),
            )
        self.conn.commit()


def ensure_api_env() -> None:
    def set_from_files(env_name: str, paths: list[Path]) -> None:
        if os.environ.get(env_name):
            return
        for path in paths:
            if not path.exists():
                continue
            try:
                value = path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if value:
                os.environ[env_name] = value
                return

    set_from_files("CORE_API_KEY", [DEFAULT_CORE_API_FILE, LEGACY_CORE_API_FILE])
    set_from_files("UNPAYWALL_EMAIL", [DEFAULT_UNPAYWALL_EMAIL_FILE, LEGACY_UNPAYWALL_EMAIL_FILE])
    set_from_files("SEMANTIC_SCHOLAR_API_KEY", [DEFAULT_SEMANTIC_SCHOLAR_API_FILE, LEGACY_SEMANTIC_SCHOLAR_API_FILE])
    set_from_files("OPENALEX_API_KEY", [DEFAULT_OPENALEX_CONTENT_KEYS_FILE, LEGACY_OPENALEX_CONTENT_KEYS_FILE])
