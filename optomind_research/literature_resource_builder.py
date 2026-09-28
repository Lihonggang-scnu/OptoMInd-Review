"""Literature Resource Builder: Query Plan -> abstract/fulltext resource bundle.

This module implements the second stage described by the user:

    confirmed Query Planner JSON
        -> Structured Literature Vault
        -> Atomic Relevance Plan
        -> feature-level paper scoring
        -> fulltext upgrade
        -> manual download list
        -> resource bundle for Evidence Extraction Agent

The implementation is intentionally conservative about access: it only uses
public metadata, legal OA URLs, local files, Jina/Firecrawl public page scraping,
and user-provided local uploads. It does not bypass paywalls.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from llm.qwen_chat_client import call_qwen_chat

from .config import PROJECT_ROOT, ResearchSettings, configure_secret_environment
from .search_engine import SearchEngine

try:
    from json_repair import repair_json
except Exception:  # pragma: no cover - optional dependency guard
    def repair_json(text: str) -> str:
        return text


DESKTOP = Path.home() / "Desktop"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
API_KEYS_DIR = PROJECT_ROOT / "api_keys"
USER_FULLTEXTS_DIR = PROJECT_ROOT / "user_fulltexts"
DEFAULT_ATOMIC_PLANNER_PROMPT = PROMPTS_DIR / "Atomic Relevance Planner.txt"
DEFAULT_FEATURE_SCORER_PROMPT = PROMPTS_DIR / "Feature-level Paper Scorer.txt"
DEFAULT_CURRENT_TOPIC_GATE_PROMPT = PROMPTS_DIR / "Current Topic Paper Gate.txt"
DEFAULT_WEB_LENS_EXTRACTOR_PROMPT = PROMPTS_DIR / "Web Lens Context Extractor.txt"
DEFAULT_SUPPLEMENTAL_FACET_PROMPT = PROMPTS_DIR / "Supplemental Scholar Facet Synthesizer.txt"
DEFAULT_FULLTEXT_QUALITY_PROMPT = PROMPTS_DIR / "Fulltext Quality Gate.txt"
DEFAULT_SOURCE_CREDIBILITY_PROMPT = PROMPTS_DIR / "Source Credibility Auditor.txt"
DEFAULT_QUERY_EXPANSION_PROMPT = PROMPTS_DIR / "Query Expansion Agent.txt"
DEFAULT_SCRAPED_PAGE_AUDITOR_PROMPT = PROMPTS_DIR / "Scraped Page Cache Auditor.txt"
LEGACY_ATOMIC_PLANNER_PROMPT = DESKTOP / "Atomic Relevance Planner.txt"
LEGACY_FEATURE_SCORER_PROMPT = DESKTOP / "Feature-level Paper Scorer.txt"
LEGACY_WEB_LENS_EXTRACTOR_PROMPT = DESKTOP / "Web Lens Context Extractor.txt"
LEGACY_SUPPLEMENTAL_FACET_PROMPT = DESKTOP / "Supplemental Scholar Facet Synthesizer.txt"
LEGACY_FULLTEXT_QUALITY_PROMPT = DESKTOP / "Fulltext Quality Gate.txt"
DEFAULT_INSTITUTION_CREDENTIALS = API_KEYS_DIR / "scnu-lib.txt"
LEGACY_INSTITUTION_CREDENTIALS = DESKTOP / "scnu-lib.txt"
DEFAULT_LIBRARY_DB = PROJECT_ROOT / "database" / "literature_resource_builder" / "literature_resources.sqlite"
DEFAULT_FULLTEXT_ROOT = PROJECT_ROOT / "literature_workspace" / "fulltext_library"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "literature_resource_builder"
DEFAULT_INSTITUTION_PROFILE_DIR = PROJECT_ROOT / "literature_workspace" / "browser_profiles" / "scnu"
DEFAULT_OPENALEX_KEYS_FILE = API_KEYS_DIR / "openalex.txt"
LEGACY_OPENALEX_KEYS_FILE = DESKTOP / "openalex.txt"

DEFAULT_TARGET_JOURNALS = [
    "Nature",
    "Nature Photonics",
    "Nature Communications",
    "Science",
    "Science Advances",
    "Optica",
    "Light: Science & Applications",
    "ACS Photonics",
    "Advanced Materials",
    "Advanced Functional Materials",
    "Nano Letters",
    "Energy & Environmental Science",
    "Applied Physics Letters",
]

DEFAULT_BACKENDS = [
    "openalex",
    "crossref",
    "semantic_scholar_public",
    "core",
    "arxiv",
    "tavily",
    "serper",
    "brave",
]

ACADEMIC_BACKENDS = {"openalex", "crossref", "semantic_scholar_public", "semantic_scholar", "core", "arxiv"}
WEB_LENS_BACKENDS = {"tavily", "serper", "brave"}
WEB_ONLY_BACKENDS = WEB_LENS_BACKENDS | {"duckduckgo", "firecrawl"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str, length: int = 16) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:length]


def normalize_doi(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = text.replace("doi:", "").replace("DOI:", "").strip()
    return text.lower()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_text_file(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


def json_candidate(text: str) -> str:
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    first = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if first >= 0:
        text = text[first:]
    last_obj = max(text.rfind("}"), text.rfind("]"))
    if last_obj >= 0:
        text = text[: last_obj + 1]
    return text


def parse_json_like(text: str, fallback: Any = None) -> Any:
    try:
        return json.loads(repair_json(json_candidate(text)))
    except Exception:
        return fallback


def tokenize(text: str) -> list[str]:
    normalized = str(text or "").casefold()
    return [
        item
        for item in re.findall(r"[a-z0-9][a-z0-9_.+\-/]{1,}|[\u4e00-\u9fff]{2,}", normalized)
        if item.strip()
    ]


def normalize_title_identity(title: str) -> str:
    """Return a stable title identity across casing and punctuation variants."""
    return " ".join(tokenize(normalize_space(title)))


def clean_list(values: Any, limit: int = 50) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[，,、;\n]+", values)
    if not isinstance(values, list):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_space(str(value or "").strip(" \t\r\n-•;；,，、"))
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


def looks_like_pdf_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return lowered.endswith(".pdf") or "/pdf/" in lowered or "download" in lowered and "pdf" in lowered


def safe_filename(value: str, fallback: str = "paper") -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "-", str(value or "")).strip("-")
    return (text[:120] or fallback).strip(".")


@dataclass
class AbstractPaperRecord:
    paper_id: str
    title: str = ""
    doi: str = ""
    semantic_scholar_id: str = ""
    openalex_id: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    abstract: str = ""
    citation_count: int | None = None
    open_access: bool | None = None
    pdf_url: str = ""
    landing_page_url: str = ""
    source_apis: list[str] = field(default_factory=list)
    query_used: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    topic_tags: list[str] = field(default_factory=list)
    embedding_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_candidate(self) -> dict[str, Any]:
        source_audit = self.raw.get("source_audit") if isinstance(self.raw, dict) else None
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "abstract": self.abstract,
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "landing_page_url": self.landing_page_url,
            "pdf_url": self.pdf_url,
            "source_apis": self.source_apis,
            "source_audit": source_audit or {},
        }


@dataclass
class AtomicFeature:
    feature_id: str
    feature_name: str
    feature_type: str = "mechanism"
    description: str = ""
    positive_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    retrieval_terms: list[str] = field(default_factory=list)
    weight: float = 0.8
    recall_intent: str = ""
    facet_origin: str = "query_plan"
    counts_toward_max_features: bool = True


@dataclass
class FulltextRecord:
    paper_id: str
    doi: str = ""
    title: str = ""
    fulltext_status: str = "unavailable"
    fulltext_type: str = ""
    local_file_path: str = ""
    parsed_text_path: str = ""
    chunk_index_path: str = ""
    source_url: str = ""
    access_method: str = ""
    downloaded_at: str = ""
    used_for_queries: list[str] = field(default_factory=list)
    human_uploaded: bool = False
    error: str = ""


class AcademicFulltextResolver:
    """Quality-ordered academic fulltext resolver facade.

    The heavy implementation currently lives inside LiteratureResourceBuilder
    so this patch stays small. Keeping this facade gives the next refactor a
    stable extraction point.
    """

    def __init__(self, builder: Any) -> None:
        self.builder = builder

    def candidates(self, paper: AbstractPaperRecord) -> list[dict[str, Any]]:
        return self.builder.fulltext_candidate_urls(paper)


class LiteratureResourceLibrary:
    """SQLite store for abstract and fulltext resource libraries."""

    def __init__(self, db_path: str | Path = DEFAULT_LIBRARY_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS abstract_papers (
                paper_id TEXT PRIMARY KEY,
                doi TEXT,
                semantic_scholar_id TEXT,
                openalex_id TEXT,
                title TEXT,
                title_identity TEXT,
                authors_json TEXT,
                year INTEGER,
                venue TEXT,
                abstract TEXT,
                citation_count INTEGER,
                open_access INTEGER,
                pdf_url TEXT,
                landing_page_url TEXT,
                source_apis_json TEXT,
                query_used_json TEXT,
                matched_keywords_json TEXT,
                topic_tags_json TEXT,
                embedding_id TEXT,
                raw_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lrb_abstract_doi ON abstract_papers(doi);
            CREATE INDEX IF NOT EXISTS idx_lrb_abstract_s2 ON abstract_papers(semantic_scholar_id);
            CREATE INDEX IF NOT EXISTS idx_lrb_abstract_oa ON abstract_papers(openalex_id);
            CREATE INDEX IF NOT EXISTS idx_lrb_abstract_title ON abstract_papers(title);
            CREATE INDEX IF NOT EXISTS idx_lrb_abstract_year ON abstract_papers(year);

            CREATE TABLE IF NOT EXISTS abstract_searches (
                search_id TEXT PRIMARY KEY,
                query TEXT,
                backend TEXT,
                result_count INTEGER,
                cached INTEGER,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lrb_search_query ON abstract_searches(query);

            CREATE TABLE IF NOT EXISTS fulltext_records (
                paper_id TEXT PRIMARY KEY,
                doi TEXT,
                title TEXT,
                fulltext_status TEXT,
                fulltext_type TEXT,
                local_file_path TEXT,
                parsed_text_path TEXT,
                chunk_index_path TEXT,
                source_url TEXT,
                access_method TEXT,
                downloaded_at TEXT,
                used_for_queries_json TEXT,
                human_uploaded INTEGER,
                error TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_lrb_fulltext_status ON fulltext_records(fulltext_status);

            CREATE TABLE IF NOT EXISTS resource_sessions (
                session_id TEXT PRIMARY KEY,
                user_query TEXT,
                artifact_dir TEXT,
                stats_json TEXT,
                created_at TEXT
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self.conn.execute("PRAGMA table_info(abstract_papers)").fetchall()
        }
        if "title_identity" not in columns:
            self.conn.execute("ALTER TABLE abstract_papers ADD COLUMN title_identity TEXT")
        stale_titles = self.conn.execute(
            "SELECT paper_id, title FROM abstract_papers WHERE COALESCE(title_identity, '')=''"
        ).fetchall()
        if stale_titles:
            self.conn.executemany(
                "UPDATE abstract_papers SET title_identity=? WHERE paper_id=?",
                [
                    (normalize_title_identity(row["title"] or ""), row["paper_id"])
                    for row in stale_titles
                ],
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lrb_abstract_title_identity "
            "ON abstract_papers(title_identity)"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def paper_key(self, record: AbstractPaperRecord) -> str:
        doi = normalize_doi(record.doi)
        if doi:
            return f"doi:{doi}"
        if record.semantic_scholar_id:
            return f"s2:{record.semantic_scholar_id}"
        if record.openalex_id:
            return f"openalex:{record.openalex_id}"
        if record.title:
            return f"title:{stable_hash(normalize_title_identity(record.title), 20)}"
        return f"paper:{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _author_identity(author: str) -> str:
        return " ".join(sorted(set(tokenize(author))))

    @classmethod
    def _same_work_version(
        cls,
        existing: AbstractPaperRecord,
        incoming: AbstractPaperRecord,
    ) -> bool:
        title_identity = normalize_title_identity(incoming.title)
        if (
            not title_identity
            or title_identity != normalize_title_identity(existing.title)
            or len(title_identity) < 20
            or len(title_identity.split()) < 3
        ):
            return False
        existing_doi = normalize_doi(existing.doi)
        incoming_doi = normalize_doi(incoming.doi)
        if existing_doi and incoming_doi and existing_doi != incoming_doi:
            return False
        if existing.year and incoming.year and abs(int(existing.year) - int(incoming.year)) > 3:
            return False
        existing_authors = {
            identity
            for author in existing.authors
            if (identity := cls._author_identity(author))
        }
        incoming_authors = {
            identity
            for author in incoming.authors
            if (identity := cls._author_identity(author))
        }
        if not existing_authors or not incoming_authors:
            return False
        overlap = len(existing_authors & incoming_authors)
        required_overlap = 1 if min(len(existing_authors), len(incoming_authors)) <= 2 else 2
        return overlap >= required_overlap

    def find_existing_paper_id(self, record: AbstractPaperRecord) -> str:
        doi = normalize_doi(record.doi)
        checks = []
        if doi:
            checks.append(("doi", doi))
        if record.semantic_scholar_id:
            checks.append(("semantic_scholar_id", record.semantic_scholar_id))
        if record.openalex_id:
            checks.append(("openalex_id", record.openalex_id))
        for column, value in checks:
            row = self.conn.execute(
                f"SELECT paper_id FROM abstract_papers WHERE {column}=? LIMIT 1",
                (value,),
            ).fetchone()
            if row:
                return str(row["paper_id"])
        title_identity = normalize_title_identity(record.title)
        if title_identity:
            rows = self.conn.execute(
                "SELECT * FROM abstract_papers WHERE title_identity=? LIMIT 20",
                (title_identity,),
            ).fetchall()
            for row in rows:
                existing = self._row_to_abstract(row)
                if self._same_work_version(existing, record):
                    return existing.paper_id
        return ""

    def upsert_abstract(self, record: AbstractPaperRecord) -> tuple[str, bool]:
        existing_id = self.find_existing_paper_id(record)
        is_new = not existing_id
        if existing_id:
            record.paper_id = existing_id
            existing = self.get_abstract(existing_id)
            if existing:
                record = self._merge_record(existing, record)
        else:
            record.paper_id = self.paper_key(record)
        record.doi = normalize_doi(record.doi)
        record.updated_at = utc_now()
        self.conn.execute(
            """
            INSERT INTO abstract_papers (
                paper_id, doi, semantic_scholar_id, openalex_id, title, title_identity, authors_json,
                year, venue, abstract, citation_count, open_access, pdf_url,
                landing_page_url, source_apis_json, query_used_json,
                matched_keywords_json, topic_tags_json, embedding_id, raw_json,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paper_id) DO UPDATE SET
                doi=excluded.doi,
                semantic_scholar_id=excluded.semantic_scholar_id,
                openalex_id=excluded.openalex_id,
                title=excluded.title,
                title_identity=excluded.title_identity,
                authors_json=excluded.authors_json,
                year=excluded.year,
                venue=excluded.venue,
                abstract=excluded.abstract,
                citation_count=excluded.citation_count,
                open_access=excluded.open_access,
                pdf_url=excluded.pdf_url,
                landing_page_url=excluded.landing_page_url,
                source_apis_json=excluded.source_apis_json,
                query_used_json=excluded.query_used_json,
                matched_keywords_json=excluded.matched_keywords_json,
                topic_tags_json=excluded.topic_tags_json,
                embedding_id=excluded.embedding_id,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                record.paper_id,
                record.doi,
                record.semantic_scholar_id,
                record.openalex_id,
                record.title,
                normalize_title_identity(record.title),
                json.dumps(record.authors, ensure_ascii=False),
                record.year,
                record.venue,
                record.abstract,
                record.citation_count,
                None if record.open_access is None else int(bool(record.open_access)),
                record.pdf_url,
                record.landing_page_url,
                json.dumps(record.source_apis, ensure_ascii=False),
                json.dumps(record.query_used, ensure_ascii=False),
                json.dumps(record.matched_keywords, ensure_ascii=False),
                json.dumps(record.topic_tags, ensure_ascii=False),
                record.embedding_id,
                json.dumps(record.raw, ensure_ascii=False, default=str),
                record.created_at,
                record.updated_at,
            ),
        )
        self.conn.commit()
        return record.paper_id, is_new

    def _merge_record(self, old: AbstractPaperRecord, new: AbstractPaperRecord) -> AbstractPaperRecord:
        old_doi = normalize_doi(old.doi)
        new_doi = normalize_doi(new.doi)
        old_openalex_id = old.openalex_id
        new_openalex_id = new.openalex_id
        old_semantic_scholar_id = old.semantic_scholar_id
        new_semantic_scholar_id = new.semantic_scholar_id
        old_is_preprint = "arxiv" in old.venue.casefold() or str((old.raw.get("raw_metadata") or {}).get("type") or "").casefold() == "preprint"
        new_is_preprint = "arxiv" in new.venue.casefold() or str((new.raw.get("raw_metadata") or {}).get("type") or "").casefold() == "preprint"
        prefer_new_bibliography = bool(new_doi and not old_doi) or (old_is_preprint and not new_is_preprint)

        old.doi = new_doi if prefer_new_bibliography and new_doi else old_doi or new_doi
        old.semantic_scholar_id = (
            new_semantic_scholar_id
            if prefer_new_bibliography and new_semantic_scholar_id
            else old_semantic_scholar_id or new_semantic_scholar_id
        )
        old.openalex_id = (
            new_openalex_id
            if prefer_new_bibliography and new_openalex_id
            else old_openalex_id or new_openalex_id
        )
        old.title = old.title if len(old.title) >= len(new.title) else new.title
        merged_authors: list[str] = []
        seen_authors: set[str] = set()
        ordered_authors = (new.authors + old.authors) if prefer_new_bibliography else (old.authors + new.authors)
        for author in ordered_authors:
            identity = self._author_identity(author) or normalize_space(author).casefold()
            if not identity or identity in seen_authors:
                continue
            seen_authors.add(identity)
            merged_authors.append(normalize_space(author))
        old.authors = merged_authors[:50]
        if prefer_new_bibliography:
            old.year = new.year or old.year
            old.venue = new.venue or old.venue
        else:
            old.year = old.year or new.year
            old.venue = old.venue or new.venue
        old.abstract = old.abstract if len(old.abstract) >= len(new.abstract) else new.abstract
        old.citation_count = max([v for v in [old.citation_count, new.citation_count] if isinstance(v, int)] or [0]) or None
        if old.open_access is True or new.open_access is True:
            old.open_access = True
        elif old.open_access is False and new.open_access is False:
            old.open_access = False
        else:
            old.open_access = old.open_access if old.open_access is not None else new.open_access
        old.pdf_url = old.pdf_url or new.pdf_url
        old.landing_page_url = (
            new.landing_page_url
            if prefer_new_bibliography and new.landing_page_url
            else old.landing_page_url or new.landing_page_url
        )
        old.source_apis = clean_list(old.source_apis + new.source_apis, limit=20)
        old.query_used = clean_list(old.query_used + new.query_used, limit=80)
        old.matched_keywords = clean_list(old.matched_keywords + new.matched_keywords, limit=80)
        old.topic_tags = clean_list(old.topic_tags + new.topic_tags, limit=40)
        old.raw = {**old.raw, **{f"latest_{k}": v for k, v in new.raw.items() if k not in old.raw}}
        old.raw["identity_aliases"] = clean_list(
            [
                *(old.raw.get("identity_aliases") or []),
                *(f"doi:{value}" for value in [old_doi, new_doi] if value),
                *(f"openalex:{value}" for value in [old_openalex_id, new_openalex_id] if value),
                *(f"s2:{value}" for value in [old_semantic_scholar_id, new_semantic_scholar_id] if value),
                f"title:{normalize_title_identity(old.title)}",
            ],
            limit=50,
        )
        return old

    def get_abstract(self, paper_id: str) -> AbstractPaperRecord | None:
        row = self.conn.execute("SELECT * FROM abstract_papers WHERE paper_id=?", (paper_id,)).fetchone()
        return self._row_to_abstract(row) if row else None

    def update_abstract_raw(self, paper_id: str, raw: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE abstract_papers SET raw_json=?, updated_at=? WHERE paper_id=?",
            (json.dumps(raw or {}, ensure_ascii=False, default=str), utc_now(), paper_id),
        )
        self.conn.commit()

    def all_abstracts(self, limit: int = 5000) -> list[AbstractPaperRecord]:
        rows = self.conn.execute(
            "SELECT * FROM abstract_papers ORDER BY year DESC, updated_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._row_to_abstract(row) for row in rows]

    def search_abstracts(self, terms: Iterable[str], limit: int = 80) -> list[AbstractPaperRecord]:
        terms = [normalize_space(t) for t in terms if normalize_space(t)]
        if not terms:
            return self.all_abstracts(limit)
        rows_by_id: dict[str, sqlite3.Row] = {}
        for term in terms[:12]:
            like = f"%{term}%"
            rows = self.conn.execute(
                """
                SELECT * FROM abstract_papers
                WHERE title LIKE ? OR abstract LIKE ? OR venue LIKE ?
                ORDER BY year DESC, updated_at DESC
                LIMIT ?
                """,
                (like, like, like, max(10, int(limit))),
            ).fetchall()
            for row in rows:
                rows_by_id[row["paper_id"]] = row
        return [self._row_to_abstract(row) for row in list(rows_by_id.values())[:limit]]

    def _row_to_abstract(self, row: sqlite3.Row) -> AbstractPaperRecord:
        return AbstractPaperRecord(
            paper_id=row["paper_id"],
            doi=row["doi"] or "",
            semantic_scholar_id=row["semantic_scholar_id"] or "",
            openalex_id=row["openalex_id"] or "",
            title=row["title"] or "",
            authors=json.loads(row["authors_json"] or "[]"),
            year=row["year"],
            venue=row["venue"] or "",
            abstract=row["abstract"] or "",
            citation_count=row["citation_count"],
            open_access=None if row["open_access"] is None else bool(row["open_access"]),
            pdf_url=row["pdf_url"] or "",
            landing_page_url=row["landing_page_url"] or "",
            source_apis=json.loads(row["source_apis_json"] or "[]"),
            query_used=json.loads(row["query_used_json"] or "[]"),
            matched_keywords=json.loads(row["matched_keywords_json"] or "[]"),
            topic_tags=json.loads(row["topic_tags_json"] or "[]"),
            embedding_id=row["embedding_id"] or "",
            raw=json.loads(row["raw_json"] or "{}"),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def record_search(self, query: str, backend: str, result_count: int, cached: bool = False) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO abstract_searches
            (search_id, query, backend, result_count, cached, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (stable_hash(f"{backend}|{query}", 24), query, backend, int(result_count), int(cached), utc_now()),
        )
        self.conn.commit()

    def get_fulltext(self, paper_id: str) -> FulltextRecord | None:
        row = self.conn.execute("SELECT * FROM fulltext_records WHERE paper_id=?", (paper_id,)).fetchone()
        if not row:
            return None
        return FulltextRecord(
            paper_id=row["paper_id"],
            doi=row["doi"] or "",
            title=row["title"] or "",
            fulltext_status=row["fulltext_status"] or "unavailable",
            fulltext_type=row["fulltext_type"] or "",
            local_file_path=row["local_file_path"] or "",
            parsed_text_path=row["parsed_text_path"] or "",
            chunk_index_path=row["chunk_index_path"] or "",
            source_url=row["source_url"] or "",
            access_method=row["access_method"] or "",
            downloaded_at=row["downloaded_at"] or "",
            used_for_queries=json.loads(row["used_for_queries_json"] or "[]"),
            human_uploaded=bool(row["human_uploaded"]),
            error=row["error"] or "",
        )

    def all_available_fulltexts(self) -> list[FulltextRecord]:
        rows = self.conn.execute(
            "SELECT paper_id FROM fulltext_records WHERE fulltext_status='available' ORDER BY downloaded_at DESC"
        ).fetchall()
        return [ft for pid in rows if (ft := self.get_fulltext(pid["paper_id"]))]

    def save_fulltext_record(self, record: FulltextRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO fulltext_records (
                paper_id, doi, title, fulltext_status, fulltext_type, local_file_path,
                parsed_text_path, chunk_index_path, source_url, access_method,
                downloaded_at, used_for_queries_json, human_uploaded, error, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(paper_id) DO UPDATE SET
                doi=excluded.doi,
                title=excluded.title,
                fulltext_status=excluded.fulltext_status,
                fulltext_type=excluded.fulltext_type,
                local_file_path=excluded.local_file_path,
                parsed_text_path=excluded.parsed_text_path,
                chunk_index_path=excluded.chunk_index_path,
                source_url=excluded.source_url,
                access_method=excluded.access_method,
                downloaded_at=excluded.downloaded_at,
                used_for_queries_json=excluded.used_for_queries_json,
                human_uploaded=excluded.human_uploaded,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                record.paper_id,
                record.doi,
                record.title,
                record.fulltext_status,
                record.fulltext_type,
                record.local_file_path,
                record.parsed_text_path,
                record.chunk_index_path,
                record.source_url,
                record.access_method,
                record.downloaded_at,
                json.dumps(record.used_for_queries, ensure_ascii=False),
                int(record.human_uploaded),
                record.error,
                utc_now(),
            ),
        )
        self.conn.commit()

    def save_session(self, session_id: str, user_query: str, artifact_dir: str, stats: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO resource_sessions
            (session_id, user_query, artifact_dir, stats_json, created_at)
            VALUES (?,?,?,?,?)
            """,
            (session_id, user_query, artifact_dir, json.dumps(stats, ensure_ascii=False), utc_now()),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        return {
            "abstract_papers": self.conn.execute("SELECT COUNT(*) FROM abstract_papers").fetchone()[0],
            "fulltext_records": self.conn.execute("SELECT COUNT(*) FROM fulltext_records").fetchone()[0],
            "available_fulltexts": self.conn.execute(
                "SELECT COUNT(*) FROM fulltext_records WHERE fulltext_status='available'"
            ).fetchone()[0],
            "db_path": str(self.db_path),
        }


class LiteratureResourceBuilder:
    """End-to-end second-stage resource builder."""

    def __init__(
        self,
        *,
        db_path: str | Path = DEFAULT_LIBRARY_DB,
        output_root: str | Path = DEFAULT_OUTPUT_ROOT,
        fulltext_root: str | Path = DEFAULT_FULLTEXT_ROOT,
        atomic_prompt_path: str | Path = DEFAULT_ATOMIC_PLANNER_PROMPT,
        scorer_prompt_path: str | Path = DEFAULT_FEATURE_SCORER_PROMPT,
        current_topic_gate_prompt_path: str | Path = DEFAULT_CURRENT_TOPIC_GATE_PROMPT,
        web_lens_extractor_prompt_path: str | Path = DEFAULT_WEB_LENS_EXTRACTOR_PROMPT,
        supplemental_facet_prompt_path: str | Path = DEFAULT_SUPPLEMENTAL_FACET_PROMPT,
        fulltext_quality_prompt_path: str | Path = DEFAULT_FULLTEXT_QUALITY_PROMPT,
        source_credibility_prompt_path: str | Path = DEFAULT_SOURCE_CREDIBILITY_PROMPT,
        query_expansion_prompt_path: str | Path = DEFAULT_QUERY_EXPANSION_PROMPT,
        scraped_page_auditor_prompt_path: str | Path = DEFAULT_SCRAPED_PAGE_AUDITOR_PROMPT,
        institution_credentials_path: str | Path = DEFAULT_INSTITUTION_CREDENTIALS,
        institution_profile_dir: str | Path = DEFAULT_INSTITUTION_PROFILE_DIR,
        institution_browser_channel: str = "edge-cdp",
        institution_cdp_endpoint: str = "http://127.0.0.1:9222",
        enable_institutional_access: bool = False,
        enable_scansci_legal_backup: bool = True,
        scansci_timeout_seconds: int = 45,
        manual_fulltext_dir: str | Path = USER_FULLTEXTS_DIR,
        real_llm: bool = True,
        atomic_model_tier: str = "premium_model",
        scoring_model_tier: str = "standard_model",
        audit_model_tier: str = "cheap_model",
        web_lens_extractor_model_tier: str = "standard_model",
        supplemental_facet_model_tier: str = "advanced_model",
        backends: list[str] | None = None,
        target_journals: list[str] | None = None,
    ) -> None:
        configure_secret_environment()
        self.settings = ResearchSettings()
        self.library = LiteratureResourceLibrary(db_path)
        self.output_root = Path(output_root)
        self.fulltext_root = Path(fulltext_root)
        self.atomic_prompt_path = self.prefer_existing_path(atomic_prompt_path, LEGACY_ATOMIC_PLANNER_PROMPT)
        self.scorer_prompt_path = self.prefer_existing_path(scorer_prompt_path, LEGACY_FEATURE_SCORER_PROMPT)
        self.current_topic_gate_prompt_path = Path(current_topic_gate_prompt_path)
        self.web_lens_extractor_prompt_path = self.prefer_existing_path(web_lens_extractor_prompt_path, LEGACY_WEB_LENS_EXTRACTOR_PROMPT)
        self.supplemental_facet_prompt_path = self.prefer_existing_path(supplemental_facet_prompt_path, LEGACY_SUPPLEMENTAL_FACET_PROMPT)
        self.fulltext_quality_prompt_path = self.prefer_existing_path(fulltext_quality_prompt_path, LEGACY_FULLTEXT_QUALITY_PROMPT)
        self.source_credibility_prompt_path = Path(source_credibility_prompt_path)
        self.query_expansion_prompt_path = Path(query_expansion_prompt_path)
        self.scraped_page_auditor_prompt_path = Path(scraped_page_auditor_prompt_path)
        self.institution_credentials_path = self.prefer_existing_path(institution_credentials_path, LEGACY_INSTITUTION_CREDENTIALS)
        self.institution_profile_dir = Path(institution_profile_dir)
        self.institution_browser_channel = str(institution_browser_channel or "edge-cdp")
        self.institution_cdp_endpoint = str(institution_cdp_endpoint or "http://127.0.0.1:9222")
        self.enable_institutional_access = bool(enable_institutional_access)
        self.fulltext_access_policy = (
            "institution_opt_in" if self.enable_institutional_access else "oa_only"
        )
        self.enable_scansci_legal_backup = bool(enable_scansci_legal_backup)
        self.scansci_timeout_seconds = int(scansci_timeout_seconds or 45)
        self.manual_fulltext_dir = Path(manual_fulltext_dir)
        self.real_llm = real_llm
        self.atomic_model_tier = atomic_model_tier
        self.scoring_model_tier = scoring_model_tier
        self.audit_model_tier = audit_model_tier
        self.web_lens_extractor_model_tier = web_lens_extractor_model_tier
        self.supplemental_facet_model_tier = supplemental_facet_model_tier
        self.backends = backends or list(DEFAULT_BACKENDS)
        self.target_journals = target_journals or list(DEFAULT_TARGET_JOURNALS)
        self.engine = SearchEngine()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.fulltext_root.mkdir(parents=True, exist_ok=True)
        self.manual_fulltext_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics: list[str] = []
        self.progress_events: list[dict[str, Any]] = []
        self._progress_callback: Callable[[dict[str, Any]], None] | None = None
        self._emit_lock = threading.Lock()
        if self.enable_institutional_access:
            self.institution_access = self.load_institution_credentials(
                self.institution_credentials_path
            )
        else:
            # OA-only runs must not even inspect an institution credential
            # file.  This keeps the temporary access policy auditable and
            # prevents a configured SCNU file from becoming an implicit route.
            self.institution_access = {
                "configured": False,
                "username_present": False,
                "password_present": False,
                "portal_url_present": False,
                "mode": "disabled_by_oa_only_policy",
            }
        self.academic_fulltext_resolver = AcademicFulltextResolver(self)
        self.institution_backend = (
            self.create_institution_backend()
            if self.enable_institutional_access
            else None
        )

    @staticmethod
    def prefer_existing_path(primary: str | Path, legacy: str | Path) -> Path:
        primary_path = Path(primary)
        if primary_path.exists():
            return primary_path
        legacy_path = Path(legacy)
        return legacy_path if legacy_path.exists() else primary_path

    @staticmethod
    def load_institution_credentials(path: str | Path) -> dict[str, Any]:
        """Load only non-secret credential status for later browser/session use.

        The password is intentionally not returned, logged, or serialized. When
        institution access is needed, a browser/session resolver can re-read the
        file inside its private login routine.
        """
        path = Path(path)
        status: dict[str, Any] = {
            "configured": False,
            "path": str(path),
            "username_present": False,
            "password_present": False,
            "portal_url_present": False,
            "mode": "not_configured",
        }
        if not path.exists():
            return status
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            status["mode"] = "unreadable"
            return status
        if not raw:
            status["mode"] = "empty_file"
            return status

        username = ""
        password = ""
        portal_url = ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                username = str(parsed.get("username") or parsed.get("account") or parsed.get("user") or "")
                password = str(parsed.get("password") or parsed.get("pass") or parsed.get("pwd") or "")
                portal_url = str(parsed.get("portal_url") or parsed.get("url") or parsed.get("login_url") or "")
        except Exception:
            parsed = None
        if not username and not password:
            pairs: dict[str, str] = {}
            plain_lines: list[str] = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    pairs[key.strip().lower()] = value.strip()
                elif ":" in line and re.match(r"^[A-Za-z_ -]{3,32}\s*:", line):
                    key, value = line.split(":", 1)
                    pairs[key.strip().lower()] = value.strip()
                else:
                    plain_lines.append(line)
            username = pairs.get("username") or pairs.get("account") or pairs.get("user") or pairs.get("id") or ""
            password = pairs.get("password") or pairs.get("pass") or pairs.get("pwd") or ""
            portal_url = pairs.get("portal_url") or pairs.get("url") or pairs.get("login_url") or ""
            if pairs:
                ordered_pairs = list(pairs.items())
                for key, value in ordered_pairs:
                    key_l = key.casefold()
                    if not username and any(marker in key_l for marker in ["\u8d26\u53f7", "\u8d26\u6237", "\u7528\u6237\u540d", "\u5b66\u53f7", "user", "account", "login"]):
                        username = value
                    if not password and any(marker in key_l for marker in ["\u5bc6\u7801", "\u53e3\u4ee4", "password", "pass", "pwd"]):
                        password = value
                    if not portal_url and any(marker in key_l for marker in ["\u7f51\u5740", "\u94fe\u63a5", "\u5165\u53e3", "url", "portal"]):
                        portal_url = value
                values_only = [value for _key, value in ordered_pairs if value]
                if not username and values_only:
                    username = values_only[0]
                if not password and len(values_only) >= 2:
                    password = values_only[1]
                if not portal_url:
                    portal_url = next((value for value in values_only[2:] if value.lower().startswith(("http://", "https://"))), "")
            if not username and plain_lines:
                username = plain_lines[0]
            if not password and len(plain_lines) >= 2:
                password = plain_lines[1]
            if not portal_url:
                portal_url = next((line for line in plain_lines[2:] if line.lower().startswith(("http://", "https://"))), "")
        status.update(
            {
                "configured": bool(username and password),
                "username_present": bool(username),
                "password_present": bool(password),
                "portal_url_present": bool(portal_url),
                "mode": "credential_file_ready" if username and password else "credential_file_incomplete",
            }
        )
        return status

    def create_institution_backend(self) -> Any:
        try:
            from tools.academic_backends.institutional_access_backend import InstitutionalAccessBackend

            return InstitutionalAccessBackend(
                profile_dir=self.institution_profile_dir,
                enabled=self.enable_institutional_access,
                headless=True,
                browser_channel=self.institution_browser_channel,
                cdp_endpoint=self.institution_cdp_endpoint,
            )
        except Exception as exc:
            self.diagnostics.append(f"institution_backend_unavailable:{type(exc).__name__}")
            return None

    def _emit(self, phase: str, doing: str, result: str = "", reason: str = "", **payload: Any) -> None:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "phase": phase,
            "doing": doing,
            "result": result,
            "reason": reason,
            "payload": payload,
        }
        with self._emit_lock:
            self.progress_events.append(event)
            if self._progress_callback:
                try:
                    self._progress_callback(event)
                except Exception:
                    pass

    def run(
        self,
        query_plan: dict[str, Any],
        *,
        max_queries: int = 10,
        results_per_backend: int = 50,
        per_backend_results: dict[str, int] | None = None,
        from_year: int | None = 2014,
        max_abstract_candidates: int = 500,
        feature_candidate_limit: int = 60,
        scoring_batch_size: int = 12,
        fulltext_top_n: int = 150,
        feature_top_k: int = 10,
        max_fulltext_downloads: int = 150,
        max_features: int = 10,
        enable_query_expansion: bool = True,
        enable_citation_expansion: bool = True,
        max_reference_dois: int = 3,
        source_audit_limit: int = 300,
        scoring_max_workers: int = 4,
        enable_web_lens_supplement: bool = True,
        web_lens_content_limit: int = 300,
        web_lens_extraction_batch_size: int = 8,
        unified_facet_library: bool = True,
        facet_papers_per_feature: int = 20,
        facet_recall_top_per_backend: int = 12,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        artifact_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        self.progress_events = []
        self._progress_callback = progress_callback
        user_query = self._user_query(query_plan)
        artifact_path = Path(artifact_dir) if artifact_dir else self.output_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{session_id}"
        artifact_path.mkdir(parents=True, exist_ok=True)

        self._emit("start", "Start Stage 2: Query Planner JSON -> literature resource library", user_query=user_query, artifact_dir=str(artifact_path))
        self._emit(
            "institution_access",
            "Check institution access configuration",
            result="enabled" if self.enable_institutional_access else "disabled",
            access_policy=self.fulltext_access_policy,
            reason=(
                "Institutional access is explicitly enabled; credentials are never logged."
                if self.enable_institutional_access
                else "OA-only policy: institutional credentials and browser routes are disabled."
            ),
            credential_status={k: v for k, v in self.institution_access.items() if k != "path"},
            backend_status=(
                self.institution_backend.check_status()
                if self.institution_backend
                else {"enabled": False, "mode": "oa_only"}
            ),
        )
        queries = self.build_search_queries(query_plan, max_queries=max_queries)
        academic_backends, web_backends = self.split_backends(self.backends)
        self._emit(
            "search_plan",
            "Generate retrieval queries",
            result=f"{len(queries)} queries",
            queries=queries,
            academic_backends=academic_backends,
            web_lens_backends=web_backends,
            retrieval_policy="Web backends are used only to supplement scholarly facets and are not inserted directly into the literature library.",
        )
        abstract_update: dict[str, Any] = {
            "mode": "unified_facet_library",
            "skipped_global_query_backend_matrix": bool(unified_facet_library),
            "queries_executed": [],
            "new_records_added": 0,
            "duplicate_records_skipped": 0,
        }
        if not unified_facet_library:
            abstract_update = self.update_abstract_library(
                queries,
                backends=academic_backends,
                results_per_backend=results_per_backend,
                from_year=from_year,
                per_backend_results=per_backend_results,
            )
            if enable_query_expansion and abstract_update["new_records_added"] < max(4, max_queries):
                expanded = self.expand_queries_low_cost(query_plan, queries, reason="too few retrieval results")
                extra_queries = [q for q in expanded if q not in queries][: max(0, max_queries - len(queries) + 3)]
                if extra_queries:
                    extra_update = self.update_abstract_library(
                        extra_queries,
                        backends=academic_backends[:5],
                        results_per_backend=max(1, results_per_backend),
                        from_year=from_year,
                        per_backend_results=per_backend_results,
                    )
                    abstract_update["queries_executed"].extend(extra_update["queries_executed"])
                    abstract_update["new_records_added"] += extra_update["new_records_added"]
                    abstract_update["duplicate_records_skipped"] += extra_update["duplicate_records_skipped"]
                    abstract_update["query_expansion_used"] = True
                    abstract_update["expanded_queries"] = extra_queries

        atomic_plan = self.create_atomic_relevance_plan(query_plan, max_features=max_features)
        atomic_plan = self.limit_atomic_features(atomic_plan, max_features=max_features)
        web_default_results = max(
            [(per_backend_results or {}).get(backend, 10) for backend in web_backends] or [10]
        )
        web_lens_context = self.collect_web_lens_context(
            query_plan,
            queries,
            backends=web_backends,
            results_per_backend=web_default_results,
            per_backend_results=per_backend_results,
            max_pages_for_content=web_lens_content_limit,
            extraction_batch_size=web_lens_extraction_batch_size,
            enabled=enable_web_lens_supplement,
        )
        supplemental_facet_plan = self.synthesize_supplemental_facets_from_web(
            query_plan,
            atomic_plan,
            web_lens_context,
            max_supplemental_facets=max(4, max_features),
            enabled=enable_web_lens_supplement,
        )
        atomic_plan = self.merge_supplemental_facets(atomic_plan, supplemental_facet_plan)
        research_facet_plan = self.research_facet_plan(atomic_plan)
        self._emit("facet_plan", "Completed scholarly facet planning", result=f"{len(self.atomic_features(atomic_plan))} Scholar Facets")
        if unified_facet_library:
            abstract_update = self.maintain_unified_facet_abstract_library(
                atomic_plan,
                query_plan=query_plan,
                seed_queries=queries,
                backends=academic_backends,
                papers_per_facet=facet_papers_per_feature,
                top_per_backend=facet_recall_top_per_backend,
                from_year=from_year,
            )
            facet_bibliometric_update = abstract_update.get("facet_bibliometric_recall", {})
        else:
            facet_bibliometric_update = self.update_facet_bibliometric_recall(
                atomic_plan,
                from_year=from_year,
            )
        papers = self.select_relevant_abstract_pool(query_plan, limit=max_abstract_candidates, atomic_plan=atomic_plan)
        self._emit("candidate_pool", "Selected candidate papers from the structured literature element library", result=f"{len(papers)} candidates", reason="Used for scholarly-facet recall and source credibility audit.")
        source_audit = self.audit_source_credibility(papers[: max(0, source_audit_limit)])
        ranking_tables = self.feature_wise_recall(atomic_plan, papers, per_feature=feature_candidate_limit)
        self._emit("feature_recall", "Completed candidate recall by scholarly facet", result=f"{len(ranking_tables)} facet recall tables")
        facet_literature_map = self.build_facet_literature_map(atomic_plan, ranking_tables)
        scoring_results = self.score_feature_candidates(
            atomic_plan,
            ranking_tables,
            batch_size=scoring_batch_size,
            max_workers=scoring_max_workers,
        )
        self._emit("feature_scoring", "Completed feature-level paper scoring", result=f"{len(scoring_results)} feature scoring results")
        selected = self.decide_fulltext_upgrade(
            papers,
            atomic_plan,
            scoring_results,
            query_plan=query_plan,
            facet_literature_map=facet_literature_map,
            overall_top_n=fulltext_top_n,
            feature_top_k=feature_top_k,
        )
        self._emit("fulltext_selection", "Selected candidates for automatic full-text acquisition", result=f"{len(selected['selected_for_fulltext_upgrade'])} papers", reason="Prioritize OA/PDF/arXiv/Unpaywall, then public landing pages.")

        fulltext_update = self.acquire_fulltexts(
            selected["selected_for_fulltext_upgrade"],
            max_downloads=max_fulltext_downloads,
        )
        self._emit(
            "fulltext_acquisition",
            "Completed automatic full-text acquisition",
            result=(
                f"available full texts {len(fulltext_update.get('new_fulltexts_downloaded', [])) + len(fulltext_update.get('fulltexts_reused_from_cache', []))}"
                f"; deferred downloads {len(fulltext_update.get('deferred_fulltext_acquisition', []))}"
                f"; real failures/manual downloads {len(fulltext_update.get('failed_fulltext_acquisition', []))}"
            ),
        )

        citation_expansion = {
            "enabled": bool(enable_citation_expansion),
            "seed_papers": [],
            "new_abstract_records_from_references": 0,
            "rerun_feature_scoring": False,
            "reference_dois": [],
        }
        if enable_citation_expansion:
            self._emit("citation_expansion", "Start reference DOI backtracking from acquired full texts", max_reference_dois=max_reference_dois)
            citation_expansion = self.expand_from_references(
                fulltext_update.get("new_fulltexts_downloaded", []) + fulltext_update.get("fulltexts_reused_from_cache", []),
                max_reference_dois=max_reference_dois,
            )
            self._emit(
                "citation_expansion",
                "Completed reference DOI backtracking",
                result=f"new abstract records {citation_expansion.get('new_abstract_records_from_references', 0)}; DOI count {len(citation_expansion.get('reference_dois', []))}",
            )
            if citation_expansion["new_abstract_records_from_references"] > 0:
                papers = self.select_relevant_abstract_pool(query_plan, limit=max_abstract_candidates, atomic_plan=atomic_plan)
                ranking_tables = self.feature_wise_recall(atomic_plan, papers, per_feature=feature_candidate_limit)
                facet_literature_map = self.build_facet_literature_map(atomic_plan, ranking_tables)
                scoring_results = self.score_feature_candidates(
                    atomic_plan,
                    ranking_tables,
                    batch_size=scoring_batch_size,
                    max_workers=scoring_max_workers,
                )
                selected = self.decide_fulltext_upgrade(
                    papers,
                    atomic_plan,
                    scoring_results,
                    query_plan=query_plan,
                    facet_literature_map=facet_literature_map,
                    overall_top_n=fulltext_top_n,
                    feature_top_k=feature_top_k,
                )
                citation_expansion["rerun_feature_scoring"] = True

        bundle_next = self.build_resource_bundle_for_next_agent(selected, fulltext_update, candidate_papers=papers)
        bundle_next["available_fulltexts"] = self.check_fulltext_quality(bundle_next["available_fulltexts"], user_query)
        bundle_next = self.apply_downstream_quality_gate(bundle_next)
        human_required = {
            "papers_requiring_manual_download": bundle_next["manual_download_list"],
            "papers_deferred_to_next_download_batch": bundle_next.get("deferred_download_list", []),
            "papers_recommended_for_confirmation": selected["selected_for_fulltext_upgrade"][: min(10, len(selected["selected_for_fulltext_upgrade"]))],
        }
        resource_bundle = {
            "agent_name": "Literature Resource Builder",
            "resource_update_session": {
                "session_id": session_id,
                "user_query": user_query,
                "abstract_library_update": abstract_update,
                "atomic_relevance_plan": atomic_plan,
                "web_lens_context": web_lens_context,
                "supplemental_facet_plan": supplemental_facet_plan,
                "research_facet_plan": research_facet_plan,
                "facet_bibliometric_recall": facet_bibliometric_update,
                "feature_ranking_tables": ranking_tables,
                "facet_literature_map": facet_literature_map,
                "feature_scoring_results": scoring_results,
                "screening_result": {
                    "candidate_papers_from_abstracts": [asdict(p) for p in papers[: max_abstract_candidates]],
                    "selected_for_fulltext_upgrade": selected["selected_for_fulltext_upgrade"],
                },
                "citation_expansion": citation_expansion,
                "source_credibility_audit": source_audit,
                "fulltext_library_update": fulltext_update,
                "fulltext_access_policy": self.fulltext_access_policy,
                "human_review_required": human_required,
                "diagnostics": self.diagnostics,
                "progress_events": self.progress_events,
                "library_stats": self.library.stats(),
            },
            "resource_bundle_for_next_agent": bundle_next,
            "artifact_dir": str(artifact_path),
            "resource_bundle_path": str(artifact_path / "resource_bundle.json"),
        }
        self.write_artifacts(
            artifact_path,
            query_plan=query_plan,
            resource_bundle=resource_bundle,
            atomic_plan=atomic_plan,
            web_lens_context=web_lens_context,
            supplemental_facet_plan=supplemental_facet_plan,
            research_facet_plan=research_facet_plan,
            facet_literature_map=facet_literature_map,
            scoring_results=scoring_results,
            ranking_tables=ranking_tables,
            selected=selected,
            papers=papers,
        )
        stats = {
            "abstract_records_considered": len(papers),
            "selected_for_fulltext": len(selected["selected_for_fulltext_upgrade"]),
            "available_fulltexts": len(bundle_next["available_fulltexts"]),
            "deferred_download": len(bundle_next.get("deferred_download_list", [])),
            "manual_download": len(bundle_next["manual_download_list"]),
        }
        self.library.save_session(session_id, user_query, str(artifact_path), stats)
        self._emit("done", "Stage 2 completed", result=str(artifact_path), reason="resource_bundle.json has been written and can be passed to the next evidence extraction stage.")
        resource_bundle["resource_update_session"]["progress_events"] = self.progress_events
        (artifact_path / "progress_events.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False, default=str) for event in self.progress_events) + "\n",
            encoding="utf-8",
        )
        (artifact_path / "resource_bundle.json").write_text(
            json.dumps(resource_bundle, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._progress_callback = None
        return resource_bundle

    @staticmethod
    def _user_query(query_plan: dict[str, Any]) -> str:
        return str(((query_plan.get("input") or {}).get("user_query")) or query_plan.get("question") or "").strip()

    @staticmethod
    def _planner_output(query_plan: dict[str, Any]) -> dict[str, Any]:
        return query_plan.get("output") if isinstance(query_plan.get("output"), dict) else query_plan

    def build_search_queries(self, query_plan: dict[str, Any], max_queries: int = 8) -> list[str]:
        output = self._planner_output(query_plan)
        keywords = clean_list((output.get("keyword_decomposition") or {}).get("keywords", []), limit=30)
        scope = output.get("scope_definition") or {}
        scope_items = clean_list(scope.get("scope_items", []), limit=12)
        understanding = normalize_space(output.get("problem_understanding", ""))
        user_query = self._user_query(query_plan)
        seeds = []
        seeds.extend(keywords[: max_queries])
        if understanding:
            seeds.append(understanding[:180])
        for item in scope_items[:3]:
            seeds.append(item)
        if user_query:
            seeds.append(user_query)
        queries: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            q = normalize_space(seed)
            if not q or q.casefold() in seen:
                continue
            seen.add(q.casefold())
            queries.append(q)
            if len(queries) >= max_queries:
                break
        if not any(re.search(r"[A-Za-z]", q) for q in queries):
            english_like = [
                item for item in keywords
                if re.search(r"[A-Za-z]", item)
            ]
            queries.extend(english_like[: max(0, max_queries - len(queries))])
        return queries[:max_queries] or [user_query]

    def update_abstract_library(
        self,
        queries: list[str],
        *,
        backends: list[str],
        results_per_backend: int,
        from_year: int | None,
        per_backend_results: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        new_records = 0
        duplicates = 0
        executed: list[dict[str, Any]] = []

        def _fetch(query: str, backend: str) -> tuple[str, str, list, float, str | None]:
            limit = (per_backend_results or {}).get(backend, results_per_backend)
            self._emit("search", "Searching academic backend", query=query, backend=backend, results_per_backend=limit)
            started = time.time()
            try:
                raw_results = self._search_backend(backend, query, limit, from_year=from_year)
                return query, backend, raw_results, time.time() - started, None
            except Exception as exc:
                return query, backend, [], time.time() - started, f"{backend} search failed: {type(exc).__name__}"

        tasks = [(q, b) for q in queries for b in backends]
        n_workers = min(len(tasks), 6)
        if n_workers <= 1:
            fetch_results = [_fetch(q, b) for q, b in tasks]
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                futures = [ex.submit(_fetch, q, b) for q, b in tasks]
                fetch_results = [f.result() for f in as_completed(futures)]

        for query, backend, raw_results, elapsed, error in fetch_results:
            if error:
                self.diagnostics.append(error)
            self.library.record_search(query, backend, len(raw_results), cached=any((r.get("metadata") or {}).get("search_cache_hit") for r in raw_results if isinstance(r, dict)))
            inserted_for_backend = 0
            duplicate_for_backend = 0
            for raw in raw_results:
                record = self.raw_to_abstract_record(raw, query=query, matched_keywords=[query])
                if not record.title and not record.doi:
                    continue
                _, is_new = self.library.upsert_abstract(record)
                if is_new:
                    new_records += 1
                    inserted_for_backend += 1
                else:
                    duplicates += 1
                    duplicate_for_backend += 1
            executed.append({
                "query": query,
                "backend": backend,
                "raw_results": len(raw_results),
                "new_records": inserted_for_backend,
                "duplicates": duplicate_for_backend,
                "elapsed_sec": round(elapsed, 2),
            })
            self._emit(
                "search",
                "Completed one backend search",
                result=f"{backend}: raw={len(raw_results)}, new={inserted_for_backend}, dup={duplicate_for_backend}",
                query=query,
                backend=backend,
                elapsed_sec=round(elapsed, 2),
                engine_stats=dict(getattr(self.engine, "stats", {}) or {}),
            )
        return {
            "queries_executed": executed,
            "new_records_added": new_records,
            "duplicate_records_skipped": duplicates,
            "total_records_after_update": self.library.stats()["abstract_papers"],
            "query_expansion_used": False,
            "expanded_queries": [],
        }

    def _search_backend(self, backend: str, query: str, n: int, *, from_year: int | None = None) -> list[dict[str, Any]]:
        backend = backend.strip()
        if backend == "openalex":
            from tools.academic_backends.openalex_backend import OpenAlexBackend
            return OpenAlexBackend().search(query, max_results=n, from_year=from_year)
        if backend == "crossref":
            from tools.academic_backends.crossref_backend import CrossrefBackend
            return CrossrefBackend().search(query, max_results=n, from_year=from_year)
        return self.engine.search(query, [backend], max_results=n)

    def raw_to_abstract_record(self, raw: dict[str, Any], *, query: str, matched_keywords: list[str]) -> AbstractPaperRecord:
        raw = dict(raw or {})
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
        doi = normalize_doi(raw.get("doi") or external.get("DOI") or "")
        source_id = str(raw.get("source_id") or "")
        s2_id = str(raw.get("semantic_scholar_paper_id") or raw.get("paperId") or "")
        if source_id.startswith("s2:") and not s2_id:
            s2_id = source_id.split(":", 1)[1]
        openalex_id = str(raw.get("openalex_id") or "")
        if source_id.startswith("openalex:") and not openalex_id:
            openalex_id = source_id.split(":", 1)[1]

        pdf_url = (
            str(raw.get("pdf_url") or "")
            or str((raw_meta.get("open_access_pdf") or {}).get("url") or "")
            or str((raw_meta.get("open_access_pdf") or {}).get("url_for_pdf") or "")
        )
        landing = str(raw.get("url_or_doi") or raw.get("source_url") or "")
        venue = str(raw.get("journal_or_venue") or raw.get("venue") or raw.get("journal") or "")
        citation_count = raw_meta.get("citation_count", raw_meta.get("cited_by_count", raw_meta.get("is_referenced_by_count")))
        try:
            citation_count = int(citation_count) if citation_count is not None else None
        except Exception:
            citation_count = None
        open_access = raw_meta.get("is_oa")
        if open_access is None:
            open_access = bool(pdf_url) if pdf_url else None
        authors = raw.get("authors") or []
        if isinstance(authors, str):
            authors = clean_list(authors.split(","), limit=50)
        elif isinstance(authors, list):
            authors = [str(a.get("name", "")) if isinstance(a, dict) else str(a) for a in authors]
        return AbstractPaperRecord(
            paper_id="",
            doi=doi,
            semantic_scholar_id=s2_id,
            openalex_id=openalex_id,
            title=normalize_space(str(raw.get("title") or "")),
            authors=clean_list(authors, limit=50),
            year=self._safe_year(raw.get("year")),
            venue=venue,
            abstract=normalize_space(str(raw.get("abstract_or_snippet") or raw.get("abstract") or "")),
            citation_count=citation_count,
            open_access=open_access,
            pdf_url=pdf_url,
            landing_page_url=landing,
            source_apis=clean_list([str(raw.get("backend") or "")], limit=10),
            query_used=clean_list([query], limit=20),
            matched_keywords=clean_list(matched_keywords, limit=40),
            topic_tags=[],
            raw=raw,
        )

    @staticmethod
    def _safe_year(value: Any) -> int | None:
        try:
            year = int(value)
            return year if 1500 <= year <= 2100 else None
        except Exception:
            return None

    @staticmethod
    def split_backends(backends: list[str]) -> tuple[list[str], list[str]]:
        academic: list[str] = []
        web: list[str] = []
        for item in backends or []:
            backend = str(item or "").strip()
            if not backend:
                continue
            if backend in WEB_ONLY_BACKENDS:
                if backend in WEB_LENS_BACKENDS and backend not in web:
                    web.append(backend)
                continue
            if backend not in academic:
                academic.append(backend)
        return academic, web

    @staticmethod
    def is_web_only_record(paper: AbstractPaperRecord) -> bool:
        sources = {str(x or "").strip() for x in paper.source_apis}
        if not sources:
            return False
        if sources.issubset(WEB_ONLY_BACKENDS) and not (paper.doi or paper.semantic_scholar_id or paper.openalex_id):
            return True
        return False

    def web_lens_query(self, query: str) -> str:
        current_year = datetime.now().year
        suffix = f"latest recent advances review perspective bottleneck {current_year} {current_year - 1}"
        return normalize_space(f"{query} {suffix}")[:360]

    def collect_web_lens_context(
        self,
        query_plan: dict[str, Any],
        queries: list[str],
        *,
        backends: list[str],
        results_per_backend: int = 10,
        per_backend_results: dict[str, int] | None = None,
        max_pages_for_content: int = 300,
        extraction_batch_size: int = 8,
        enabled: bool = True,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "enabled": bool(enabled),
            "policy": "Web results are used only to discover supplemental Scholar Facets; they are not inserted into the literature library and cannot be cited as paper evidence.",
            "backends": backends,
            "raw_web_results": [],
            "content_items": [],
            "web_context_summaries": [],
            "web_lens_observations": [],
            "extraction_batches": [],
            "diagnostics": [],
        }
        if not enabled or not backends:
            return context
        topic_anchors = self.query_topic_anchor_tokens(query_plan)
        domain_guard = self.query_domain_guard(query_plan)

        def _fetch_search(query: str, backend: str) -> tuple[str, str, list[dict[str, Any]], str | None]:
            limit = int((per_backend_results or {}).get(backend, results_per_backend) or results_per_backend)
            web_query = self.web_lens_query(query)
            self._emit("web_lens_search", "Searching web signals", query=web_query, backend=backend, results_per_backend=limit)
            try:
                return query, backend, self._search_backend(backend, web_query, limit, from_year=None), None
            except Exception as exc:
                return query, backend, [], f"{backend}: {type(exc).__name__}"

        tasks = [(q, b) for q in queries for b in backends]
        search_results: list[tuple[str, str, list[dict[str, Any]], str | None]] = []
        workers = min(6, max(1, len(tasks)))
        if workers <= 1:
            search_results = [_fetch_search(q, b) for q, b in tasks]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_fetch_search, q, b) for q, b in tasks]
                search_results = [f.result() for f in as_completed(futures)]

        seen_urls: set[str] = set()
        for query, backend, raw_results, error in search_results:
            if error:
                context["diagnostics"].append(error)
                self.diagnostics.append(f"web_lens_search_failed:{error}")
            for raw in raw_results:
                if not isinstance(raw, dict):
                    continue
                url = str(raw.get("source_url") or raw.get("url_or_doi") or "").strip()
                title = normalize_space(str(raw.get("title") or ""))
                snippet = normalize_space(str(raw.get("abstract_or_snippet") or raw.get("content") or ""))[:1200]
                if not url and not title:
                    continue
                if topic_anchors and not self.text_matches_topic_anchors(
                    f"{title} {snippet}", topic_anchors, strict=True
                ):
                    context["diagnostics"].append(
                        f"off_topic_web_result_rejected:{backend}:{title[:100]}"
                    )
                    continue
                if self.text_has_domain_conflict(title, snippet, domain_guard):
                    context["diagnostics"].append(
                        f"cross_domain_web_result_rejected:{backend}:{title[:100]}"
                    )
                    continue
                key = url or f"{backend}:{title.casefold()}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                context["raw_web_results"].append({
                    "query": query,
                    "backend": backend,
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "retrieval_method": raw.get("retrieval_method", "web_search_api"),
                    "latest_sort_requested": True,
                    "raw_score": raw.get("relevance_score"),
                })
        self._emit(
            "web_lens_search",
            "Completed web-signal retrieval",
            result=f"{len(context['raw_web_results'])} web signals",
            reason="These signals are used only to supplement scholarly facets and are not inserted into the literature library.",
        )

        content_targets = context["raw_web_results"][: max(0, int(max_pages_for_content or 0))]

        def _fetch_content(item: dict[str, Any]) -> dict[str, Any]:
            url = str(item.get("url") or "")
            markdown = ""
            error = ""
            if url:
                try:
                    markdown = self.engine.fetch_fulltext(url, method="jina") or ""
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:160]}"
            text = normalize_space(markdown) if markdown else ""
            excerpt = (text or str(item.get("snippet") or ""))[:3000]
            return {
                **item,
                "content_excerpt": excerpt,
                "content_chars": len(text),
                "content_fetch_error": error,
            }

        if content_targets:
            self._emit("web_lens_content", "Start reading web content excerpts", result=f"{len(content_targets)} pages", reason="Prefer Jina Reader/cache; fall back to search snippets when body text is unavailable.")
            workers = min(8, max(1, len(content_targets)))
            if workers <= 1:
                context["content_items"] = [_fetch_content(item) for item in content_targets]
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = [ex.submit(_fetch_content, item) for item in content_targets]
                    context["content_items"] = [f.result() for f in as_completed(futures)]
            self._emit("web_lens_content", "Completed web content reading", result=f"{len(context['content_items'])} content excerpts")

        prompt = read_text_file(self.web_lens_extractor_prompt_path)
        batches = [
            context["content_items"][i: i + max(1, extraction_batch_size)]
            for i in range(0, len(context["content_items"]), max(1, extraction_batch_size))
        ]

        def _extract_batch(index: int, batch: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
            if self.real_llm and prompt:
                extracted = self._extract_web_lens_batch_with_llm(prompt, query_plan, index, batch)
            else:
                extracted = self._extract_web_lens_batch_deterministic(index, batch)
            return index, extracted

        if batches:
            self._emit("web_lens_extract", "Start C-tier model extraction of web-based facet signals", result=f"{len(batches)} batches")
            ordered: list[Any] = [None] * len(batches)
            workers = min(6, max(1, len(batches)))
            if workers <= 1:
                for idx, batch in enumerate(batches):
                    ordered[idx] = _extract_batch(idx, batch)
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {ex.submit(_extract_batch, idx, batch): idx for idx, batch in enumerate(batches)}
                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            ordered[idx] = future.result()
                        except Exception as exc:
                            ordered[idx] = (idx, self._extract_web_lens_batch_deterministic(idx, batches[idx]))
                            self.diagnostics.append(f"web_lens_extract_failed:batch{idx}:{type(exc).__name__}")
            for idx, extracted in ordered:
                context["extraction_batches"].append(extracted)
                summary = extracted.get("web_context_summary") if isinstance(extracted, dict) else None
                if isinstance(summary, dict) and str(summary.get("summary_text") or "").strip():
                    context["web_context_summaries"].append(summary)
            self._emit("web_lens_extract", "Completed web information compression", result=f"{len(context['web_context_summaries'])} dense summaries")
        return context

    def _extract_web_lens_batch_with_llm(
        self,
        prompt: str,
        query_plan: dict[str, Any],
        batch_index: int,
        batch: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "batch_id": f"web_batch_{batch_index:03d}",
            "web_texts": batch,
            "instruction": "Summarize only the provided web texts into one dense English summary under 1000 English words. Do not add external facts.",
        }
        result = call_qwen_chat(
            "WebLensContextExtractorAgent",
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.web_lens_extractor_model_tier,
            temperature=0,
            max_tokens=5000,
            response_format={"type": "json_object"},
        )
        parsed = parse_json_like(str(result.get("content") or ""), fallback={})
        return self._normalize_web_lens_extraction(parsed, batch_index, batch)

    def _extract_web_lens_batch_deterministic(self, batch_index: int, batch: list[dict[str, Any]]) -> dict[str, Any]:
        snippets: list[str] = []
        keywords: list[str] = []
        urls: list[str] = []
        for item in batch:
            text = f"{item.get('title', '')} {item.get('snippet', '')} {item.get('content_excerpt', '')}"
            snippet = normalize_space(text)[:260]
            if snippet:
                snippets.append(snippet)
            keywords.extend([t for t in tokenize(text) if re.search(r"[A-Za-z]", t) and len(t) > 3][:8])
            if item.get("url"):
                urls.append(str(item.get("url")))
        summary = normalize_space("；".join(snippets))[:1000]
        return {
            "web_context_summary": {
                "batch_id": f"web_batch_{batch_index:03d}",
                "summary_text": summary,
                "keywords_en": clean_list(keywords, limit=30),
                "source_urls": clean_list(urls, limit=20),
            },
            "mode": "deterministic_fallback",
        }

    def _normalize_web_lens_extraction(self, parsed: Any, batch_index: int, batch: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            return self._extract_web_lens_batch_deterministic(batch_index, batch)
        root = parsed.get("web_context_summary") if isinstance(parsed.get("web_context_summary"), dict) else parsed
        summary_text = normalize_space(str(root.get("summary_text") or root.get("summary") or ""))
        if not summary_text:
            return self._extract_web_lens_batch_deterministic(batch_index, batch)
        return {
            "web_context_summary": {
                "batch_id": str(root.get("batch_id") or f"web_batch_{batch_index:03d}"),
                "summary_text": summary_text[:1000],
                "keywords_en": [kw for kw in clean_list(root.get("keywords_en", []), limit=30) if re.search(r"[A-Za-z]", kw)],
                "source_urls": clean_list(root.get("source_urls", []), limit=30),
            }
        }

    def synthesize_supplemental_facets_from_web(
        self,
        query_plan: dict[str, Any],
        atomic_plan: dict[str, Any],
        web_lens_context: dict[str, Any],
        *,
        max_supplemental_facets: int = 10,
        enabled: bool = True,
    ) -> dict[str, Any]:
        summaries = (web_lens_context or {}).get("web_context_summaries") or []
        result_empty = {
            "supplemental_facet_plan": {
                "enabled": bool(enabled),
                "policy": "No web supplemental facets were added.",
                "supplemental_features": [],
            }
        }
        if not enabled or not summaries:
            return result_empty
        prompt = read_text_file(self.supplemental_facet_prompt_path)
        if self.real_llm and prompt:
            payload = {
                "problem_context": self._planner_output(query_plan),
                "existing_dimensions": (self.research_facet_plan(atomic_plan).get("facets") or []),
                "web_context_summaries": summaries[:80],
                "max_supplemental_facets": max_supplemental_facets,
            }
            llm = call_qwen_chat(
                "SupplementalScholarFacetSynthesizerAgent",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                model_tier=self.supplemental_facet_model_tier,
                temperature=0.05,
                max_tokens=6000,
                response_format={"type": "json_object"},
            )
            parsed = parse_json_like(str(llm.get("content") or ""), fallback={})
            normalized = self.normalize_supplemental_facet_plan(parsed, summaries, max_supplemental_facets=max_supplemental_facets)
            normalized = self.filter_supplemental_facets_by_topic(normalized, query_plan)
            if normalized["supplemental_facet_plan"]["supplemental_features"]:
                return normalized
        fallback = self.fallback_supplemental_facet_plan(
            summaries, max_supplemental_facets=max_supplemental_facets
        )
        return self.filter_supplemental_facets_by_topic(fallback, query_plan)

    def filter_supplemental_facets_by_topic(
        self,
        supplemental_plan: dict[str, Any],
        query_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Reject web-derived facets that do not retain the current topic identity."""
        copied = json.loads(json.dumps(supplemental_plan, ensure_ascii=False))
        root = copied.setdefault("supplemental_facet_plan", {})
        features = root.get("supplemental_features") or []
        anchors = self.query_topic_anchor_tokens(query_plan)
        domain_guard = self.query_domain_guard(query_plan)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for feature in features if isinstance(features, list) else []:
            if not isinstance(feature, dict):
                continue
            text = " ".join([
                str(feature.get("feature_name") or ""),
                str(feature.get("description") or ""),
                " ".join(clean_list(feature.get("retrieval_terms"), limit=30)),
                " ".join(clean_list(feature.get("positive_keywords"), limit=30)),
            ])
            matched = sorted(set(tokenize(text)) & anchors)
            if anchors and not matched:
                rejected.append({
                    "feature_name": str(feature.get("feature_name") or ""),
                    "reason": "no_current_topic_anchor",
                })
                continue
            if self.text_has_domain_conflict(
                str(feature.get("feature_name") or ""), text, domain_guard
            ):
                rejected.append({
                    "feature_name": str(feature.get("feature_name") or ""),
                    "reason": "physical_domain_conflict",
                })
                continue
            feature["topic_anchor_matches"] = matched[:12]
            accepted.append(feature)
        root["supplemental_features"] = accepted
        root["topic_gate"] = {
            "topic_anchor_count": len(anchors),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "rejected_samples": rejected[:20],
        }
        return copied

    def normalize_supplemental_facet_plan(self, parsed: Any, summaries: list[dict[str, Any]], *, max_supplemental_facets: int) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            return {"supplemental_facet_plan": {"enabled": True, "policy": "invalid_llm_output", "supplemental_features": []}}
        root = parsed.get("supplemental_facet_plan") if isinstance(parsed.get("supplemental_facet_plan"), dict) else parsed
        raw_features = root.get("supplemental_features") or root.get("features") or []
        features = []
        allowed = {"application", "mechanism", "material", "structure", "fabrication", "evaluation", "challenge", "hidden_relevance"}
        for idx, item in enumerate(raw_features if isinstance(raw_features, list) else [], 1):
            if not isinstance(item, dict):
                continue
            ftype = str(item.get("feature_type") or "hidden_relevance")
            if ftype not in allowed:
                ftype = "hidden_relevance"
            name = normalize_space(str(item.get("feature_name") or item.get("name") or ""))
            terms = [kw for kw in clean_list(item.get("retrieval_terms", []) or item.get("positive_keywords", []), limit=24) if re.search(r"[A-Za-z]", kw)]
            if not name or not terms:
                continue
            try:
                weight = float(item.get("weight", 0.55))
            except Exception:
                weight = 0.55
            features.append({
                "feature_id": str(item.get("feature_id") or f"SF{idx:02d}"),
                "feature_name": name[:120],
                "feature_type": ftype,
                "description": normalize_space(str(item.get("description") or ""))[:800],
                "positive_keywords": [kw for kw in clean_list(item.get("positive_keywords", terms), limit=24) if re.search(r"[A-Za-z]", kw)],
                "negative_keywords": clean_list(item.get("negative_keywords", []), limit=16),
                "retrieval_terms": terms,
                "weight": max(0.2, min(0.8, weight)),
                "recall_intent": normalize_space(str(item.get("recall_intent") or "Use this web-discovered lens to retrieve academic papers only."))[:500],
                "facet_origin": "web_lens_supplement",
                "counts_toward_max_features": False,
                "derived_from_web_summary_ids": clean_list(item.get("derived_from_web_summary_ids", []) or item.get("derived_from_web_observation_ids", []), limit=20),
                "use_policy": "academic_retrieval_only",
            })
            if len(features) >= max_supplemental_facets:
                break
        return {
            "supplemental_facet_plan": {
                "enabled": True,
                "policy": str(root.get("policy") or "Append-only supplemental dimensions from web summaries."),
                "source_summary_count": len(summaries),
                "supplemental_features": features,
            }
        }

    def fallback_supplemental_facet_plan(self, summaries: list[dict[str, Any]], *, max_supplemental_facets: int) -> dict[str, Any]:
        features = []
        seen: set[str] = set()
        for idx, summary in enumerate(summaries, 1):
            terms = [kw for kw in clean_list(summary.get("keywords_en", []), limit=12) if re.search(r"[A-Za-z]", kw)]
            if len(terms) < 2:
                text_terms = [t for t in tokenize(summary.get("summary_text", "")) if re.search(r"[A-Za-z]", t)]
                terms = clean_list(text_terms, limit=12)
            if len(terms) < 2:
                continue
            name = normalize_space(" ".join(terms[:5]))[:120]
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            features.append({
                "feature_id": f"SF{len(features) + 1:02d}",
                "feature_name": name,
                "feature_type": "hidden_relevance",
                "description": normalize_space(str(summary.get("summary_text") or ""))[:500],
                "positive_keywords": terms,
                "negative_keywords": [],
                "retrieval_terms": terms,
                "weight": 0.45,
                "recall_intent": "Retrieve academic papers for this supplemental dimension.",
                "facet_origin": "web_lens_supplement",
                "counts_toward_max_features": False,
                "derived_from_web_summary_ids": clean_list([summary.get("batch_id", "")], limit=5),
                "use_policy": "academic_retrieval_only",
            })
            if len(features) >= max_supplemental_facets:
                break
        return {
            "supplemental_facet_plan": {
                "enabled": True,
                "policy": "deterministic_fallback_from_web_summaries",
                "source_summary_count": len(summaries),
                "supplemental_features": features,
            }
        }

    @staticmethod
    def merge_supplemental_facets(atomic_plan: dict[str, Any], supplemental_facet_plan: dict[str, Any]) -> dict[str, Any]:
        features = ((atomic_plan.get("atomic_relevance_plan") or {}).get("atomic_features") or [])
        supplemental = ((supplemental_facet_plan.get("supplemental_facet_plan") or {}).get("supplemental_features") or [])
        if not isinstance(features, list) or not isinstance(supplemental, list):
            return atomic_plan
        copied = json.loads(json.dumps(atomic_plan, ensure_ascii=False))
        target = copied.setdefault("atomic_relevance_plan", {}).setdefault("atomic_features", [])
        seen = {normalize_space(str(item.get("feature_name") or "")).casefold() for item in target if isinstance(item, dict)}
        for item in supplemental:
            if not isinstance(item, dict):
                continue
            key = normalize_space(str(item.get("feature_name") or "")).casefold()
            if not key or key in seen:
                continue
            target.append(item)
            seen.add(key)
        copied["atomic_relevance_plan"]["base_feature_count"] = len(features)
        copied["atomic_relevance_plan"]["supplemental_feature_count"] = len(target) - len(features)
        return copied

    def audit_source_credibility(self, papers: list[AbstractPaperRecord]) -> dict[str, Any]:
        """Use a cheap model to label source credibility; keep deterministic fallback."""
        self._emit("source_audit", "Start source credibility audit", result=f"{len(papers)} papers", reason="Separate papers, preprints, institutional pages, commentaries, and generic web pages.")
        batch_size = 20
        batches = [papers[i: i + batch_size] for i in range(0, len(papers), batch_size)]

        def _audit_one(idx: int, batch: list[AbstractPaperRecord]) -> tuple[int, dict[str, Any], list[AbstractPaperRecord]]:
            audited = self._audit_source_batch_with_llm(batch) if self.real_llm else []
            if not audited:
                audited = [self._audit_source_deterministic(p) for p in batch]
            by_id = {str(item.get("paper_id") or ""): item for item in audited if isinstance(item, dict)}
            return idx, by_id, batch

        n_workers = min(max(1, len(batches)), 6)
        ordered: list[Any] = [None] * len(batches)
        if n_workers <= 1:
            for i, b in enumerate(batches):
                r = _audit_one(i, b)
                ordered[r[0]] = r
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as ex:
                futures = {ex.submit(_audit_one, i, b): i for i, b in enumerate(batches)}
                for future in as_completed(futures):
                    r = future.result()
                    ordered[r[0]] = r

        # serial DB write after all parallel LLM calls complete
        results: list[dict[str, Any]] = []
        cumulative = 0
        for idx, by_id, batch in ordered:
            for paper in batch:
                audit = self._normalize_source_audit(by_id.get(paper.paper_id), paper)
                paper.raw = dict(paper.raw or {})
                paper.raw["source_audit"] = audit
                self.library.update_abstract_raw(paper.paper_id, paper.raw)
                results.append(audit)
            cumulative += len(batch)
            self._emit("source_audit", "Completed one source-audit batch", result=f"{cumulative}/{len(papers)}", batch_size=len(batch))
        summary: dict[str, int] = {}
        for item in results:
            key = str(item.get("use_policy") or "unknown")
            summary[key] = summary.get(key, 0) + 1
        self._emit("source_audit", "Source credibility audit completed", result=json.dumps(summary, ensure_ascii=False))
        return {"audited_count": len(results), "summary_by_use_policy": summary, "results": results}

    def _audit_source_batch_with_llm(self, papers: list[AbstractPaperRecord]) -> list[dict[str, Any]]:
        if not papers:
            return []
        system = read_text_file(self.source_credibility_prompt_path) or (
            "You are a low-cost source credibility auditor. "
            "Judge source type only; do not judge scientific truth. "
            "Return strict JSON. Do not invent DOI, venue, or facts."
        )
        payload = {
            "task": "Classify each candidate source for literature-resource building.",
            "policy": {
                "peer_reviewed_article": "credible for conclusions if metadata supports it",
                "review_article": "credible for background and synthesis",
                "preprint": "usable but mark uncertainty",
                "publisher_page_or_repository_record": "metadata/abstract OK; full conclusions require fulltext",
                "news_or_commentary": "background only; never use as core conclusion",
                "unknown": "manual review or background only",
            },
            "output_schema": {
                "source_audit_results": [
                    {
                        "paper_id": "string",
                        "source_type": "peer_reviewed_article|review_article|preprint|publisher_page|repository_record|news_or_commentary|unknown",
                        "credibility_score": "0-5",
                        "is_peer_reviewed_likely": "boolean",
                        "use_policy": "fulltext_priority|abstract_ok|background_only|manual_review|exclude",
                        "reason": "short reason",
                    }
                ]
            },
            "candidates": [
                {
                    "paper_id": p.paper_id,
                    "title": p.title,
                    "year": p.year,
                    "venue": p.venue,
                    "doi": p.doi,
                    "landing_page_url": p.landing_page_url,
                    "pdf_url": p.pdf_url,
                    "source_apis": p.source_apis,
                    "abstract_snippet": (p.abstract or "")[:900],
                }
                for p in papers
            ],
        }
        try:
            result = call_qwen_chat(
                "SourceCredibilityAuditorAgent",
                [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                model_tier=self.audit_model_tier,
                temperature=0,
                max_tokens=3200,
                response_format={"type": "json_object"},
            )
            if str(result.get("content") or "").startswith(("[fallback]", "[mock]")):
                return []
            parsed = parse_json_like(result.get("content", ""), fallback={})
            rows = parsed.get("source_audit_results", []) if isinstance(parsed, dict) else []
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            self.diagnostics.append(f"source_audit_llm_failed:{type(exc).__name__}")
            return []

    def _audit_source_deterministic(self, paper: AbstractPaperRecord) -> dict[str, Any]:
        text = f"{paper.title} {paper.venue} {' '.join(paper.source_apis)} {paper.landing_page_url}".casefold()
        has_doi = bool(paper.doi)
        if "arxiv" in text:
            return {"paper_id": paper.paper_id, "source_type": "preprint", "credibility_score": 3, "is_peer_reviewed_likely": False, "use_policy": "abstract_ok", "reason": "arXiv/preprint signal"}
        if re.search(r"\breview\b|perspective|progress|\u7efc\u8ff0|\u8ff0\u8bc4", text, re.I):
            score = 4 if has_doi else 2
            policy = "abstract_ok" if has_doi else "background_only"
            return {"paper_id": paper.paper_id, "source_type": "review_article" if has_doi else "news_or_commentary", "credibility_score": score, "is_peer_reviewed_likely": has_doi, "use_policy": policy, "reason": "review/commentary signal"}
        if has_doi or paper.openalex_id or paper.semantic_scholar_id:
            return {"paper_id": paper.paper_id, "source_type": "peer_reviewed_article", "credibility_score": 4, "is_peer_reviewed_likely": True, "use_policy": "fulltext_priority" if (paper.open_access or paper.pdf_url) else "abstract_ok", "reason": "DOI or scholarly index metadata"}
        if any(api in {"tavily", "serper", "brave", "duckduckgo", "firecrawl"} for api in paper.source_apis):
            return {"paper_id": paper.paper_id, "source_type": "news_or_commentary", "credibility_score": 2, "is_peer_reviewed_likely": False, "use_policy": "background_only", "reason": "web search result without DOI"}
        return {"paper_id": paper.paper_id, "source_type": "unknown", "credibility_score": 1, "is_peer_reviewed_likely": False, "use_policy": "manual_review", "reason": "insufficient metadata"}

    def _normalize_source_audit(self, item: dict[str, Any] | None, paper: AbstractPaperRecord) -> dict[str, Any]:
        base = self._audit_source_deterministic(paper)
        if not isinstance(item, dict):
            return base
        source_type = str(item.get("source_type") or base["source_type"])
        if source_type not in {"peer_reviewed_article", "review_article", "preprint", "publisher_page", "repository_record", "news_or_commentary", "unknown"}:
            source_type = base["source_type"]
        use_policy = str(item.get("use_policy") or base["use_policy"])
        if use_policy not in {"fulltext_priority", "abstract_ok", "background_only", "manual_review", "exclude"}:
            use_policy = base["use_policy"]
        try:
            score = int(item.get("credibility_score", base["credibility_score"]))
        except Exception:
            score = int(base["credibility_score"])
        return {
            "paper_id": paper.paper_id,
            "source_type": source_type,
            "credibility_score": max(0, min(5, score)),
            "is_peer_reviewed_likely": bool(item.get("is_peer_reviewed_likely", base["is_peer_reviewed_likely"])),
            "use_policy": use_policy,
            "reason": normalize_space(str(item.get("reason") or base.get("reason") or ""))[:500],
            "audited_at": utc_now(),
        }

    @staticmethod
    def source_audit_for_paper(paper: AbstractPaperRecord) -> dict[str, Any]:
        if isinstance(paper.raw, dict) and isinstance(paper.raw.get("source_audit"), dict):
            return paper.raw["source_audit"]
        return {}

    def expand_queries_low_cost(self, query_plan: dict[str, Any], existing_queries: list[str], reason: str) -> list[str]:
        if not self.real_llm:
            return []
        system = read_text_file(self.query_expansion_prompt_path) or (
            "You are a low-cost academic query expansion agent. Generate only short English search queries; do not answer the question. "
            "Given the user query plan, existing queries, and trigger reason, add 3 to 6 English scholarly retrieval queries. "
            "Queries should cover reviews, representative experiments, methods, materials, performance evaluation, or hidden adjacent directions. Return JSON only."
        )
        user = json.dumps(
            {
                "query_plan": query_plan,
                "existing_queries": existing_queries,
                "reason": reason,
                "output_schema": {"expanded_queries": ["short English scholarly query"]},
            },
            ensure_ascii=False,
        )
        result = call_qwen_chat(
            "LiteratureQueryExpansionAgent",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model_tier="standard_model",
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        payload = parse_json_like(result.get("content", ""), fallback={}) if not str(result.get("content", "")).startswith("[") else {}
        return clean_list(payload.get("expanded_queries", []), limit=8)

    def select_relevant_abstract_pool(
        self,
        query_plan: dict[str, Any],
        limit: int,
        *,
        atomic_plan: dict[str, Any] | None = None,
    ) -> list[AbstractPaperRecord]:
        output = self._planner_output(query_plan)
        query_terms = []
        query_terms.extend(clean_list((output.get("keyword_decomposition") or {}).get("keywords", []), limit=40))
        query_terms.extend(clean_list((output.get("scope_definition") or {}).get("scope_items", []), limit=20))
        if output.get("problem_understanding"):
            query_terms.append(str(output.get("problem_understanding")))

        feature_terms: list[str] = []
        feature_ids: set[str] = set()
        if atomic_plan:
            for feature in self.atomic_features(atomic_plan):
                feature_ids.add(feature.feature_id)
                feature_terms.extend([feature.feature_name, feature.description, *feature.retrieval_terms, *feature.positive_keywords])
        feature_terms = clean_list(feature_terms, limit=160)
        all_terms = clean_list(query_terms + feature_terms, limit=200)
        topic_anchors = self.query_topic_anchor_tokens(query_plan)
        domain_guard = self.query_domain_guard(query_plan)

        channel_by_id: dict[str, set[str]] = {}
        paper_by_id: dict[str, AbstractPaperRecord] = {}

        def add_channel(
            papers: Iterable[AbstractPaperRecord],
            channel: str,
            *,
            strict_topic_gate: bool = False,
        ) -> None:
            for paper in papers:
                if not paper or not paper.paper_id or self.is_web_only_record(paper):
                    continue
                if topic_anchors and not self.paper_matches_topic_anchors(
                    paper, topic_anchors, strict=strict_topic_gate
                ):
                    continue
                if self.text_has_domain_conflict(
                    paper.title, paper.abstract, domain_guard
                ):
                    continue
                paper_by_id[paper.paper_id] = paper
                channel_by_id.setdefault(paper.paper_id, set()).add(channel)

        # 1) direct semantic/lexical match from the confirmed question.
        add_channel(self.library.search_abstracts(query_terms, limit=max(limit * 3, 800)), "question_terms")

        # 2) each Scholar Facet contributes its own retrieval vocabulary.
        if feature_terms:
            add_channel(self.library.search_abstracts(feature_terms, limit=max(limit * 4, 1200)), "scholar_facet_terms")

        # 3) broad recent/update pool, then keep bibliometric, OA, review, and source-quality signals.
        broad = [p for p in self.library.all_abstracts(limit=max(limit * 12, 5000)) if not self.is_web_only_record(p)]
        tagged = []
        high_value = []
        recent_new = []
        for paper in broad:
            tags = [str(tag) for tag in paper.topic_tags]
            text = f"{paper.title} {paper.abstract} {paper.venue}".casefold()
            if any(tag.startswith("scholar_facet:") or tag.startswith("facet_role:") for tag in tags):
                tagged.append(paper)
            if (
                (paper.citation_count or 0) >= 80
                or paper.open_access
                or paper.pdf_url
                or paper.doi
                or re.search(r"\breview\b|\bperspective\b|\broadmap\b|\bprogress\b", text, re.I)
                or self.journal_priority_bonus(paper.venue) > 0
            ):
                high_value.append(paper)
            if paper.updated_at:
                recent_new.append(paper)
        add_channel(tagged, "facet_bibliometric_roles", strict_topic_gate=True)
        add_channel(high_value, "metadata_high_value", strict_topic_gate=True)
        add_channel(
            recent_new[: max(limit * 4, 1200)],
            "recent_library_updates",
            strict_topic_gate=True,
        )

        # 4) fallback: never allow a large recall run to collapse into a tiny candidate pool.
        if len(paper_by_id) < min(limit, 300):
            add_channel(
                broad[: max(limit * 2, 1000)],
                "broad_fallback",
                strict_topic_gate=True,
            )

        scored = []
        for pid, paper in paper_by_id.items():
            channels = channel_by_id.get(pid, set())
            score = self.local_relevance_score(paper, all_terms)
            score += self.metadata_channel_score(paper)
            score += min(0.24, 0.06 * len(channels))
            if "facet_bibliometric_roles" in channels:
                score += 0.16
            if "metadata_high_value" in channels:
                score += 0.10
            if "question_terms" in channels:
                score += 0.08
            if "scholar_facet_terms" in channels:
                score += 0.08
            scored.append((score, paper.year or 0, paper.citation_count or 0, pid, paper))
        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        selected = [paper for *_rest, paper in scored[:limit]]
        self._emit(
            "candidate_pool",
            "Completed multi-channel candidate pool",
            result=f"{len(selected)}/{len(paper_by_id)} papers selected for scoring",
            reason="Merged question relevance, facet relevance, bibliometric roles, high-value metadata, and recent library updates to avoid an overly narrow candidate pool after broad recall.",
            channel_counts={channel: sum(1 for s in channel_by_id.values() if channel in s) for channel in sorted({c for s in channel_by_id.values() for c in s})},
        )
        return selected

    def local_relevance_score(self, paper: AbstractPaperRecord, terms: list[str]) -> float:
        text = f"{paper.title} {paper.abstract} {paper.venue}".casefold()
        term_tokens = set(token for term in terms for token in tokenize(term))
        if not term_tokens:
            return 0.1
        overlap = sum(1 for token in term_tokens if token in text)
        base = overlap / max(1, len(term_tokens))
        if paper.doi:
            base += 0.05
        if paper.abstract:
            base += min(0.08, len(paper.abstract) / 4000)
        if paper.year and paper.year >= 2020:
            base += 0.05
        if paper.citation_count:
            base += min(0.10, math.log10(max(1, paper.citation_count)) / 35)
        if any(str(tag).startswith("scholar_facet:") for tag in paper.topic_tags):
            base += 0.04
        base += self.journal_priority_bonus(paper.venue)
        return min(1.0, base)

    def metadata_channel_score(self, paper: AbstractPaperRecord) -> float:
        score = 0.0
        text = f"{paper.title} {paper.abstract} {paper.venue}".casefold()
        if paper.doi:
            score += 0.04
        if paper.open_access or paper.pdf_url:
            score += 0.10
        if paper.citation_count:
            score += min(0.16, math.log10(max(1, paper.citation_count)) / 12)
        if paper.year and paper.year >= datetime.now().year - 5:
            score += 0.06
        if re.search(r"\breview\b|\bperspective\b|\broadmap\b|\bprogress\b", text, re.I):
            score += 0.08
        if any(str(tag).startswith("facet_role:") for tag in paper.topic_tags):
            score += 0.12
        score += self.journal_priority_bonus(paper.venue)
        return min(0.6, score)

    def create_atomic_relevance_plan(self, query_plan: dict[str, Any], *, max_features: int | None = None) -> dict[str, Any]:
        if not self.real_llm:
            return self.fallback_atomic_plan(query_plan)
        prompt = read_text_file(self.atomic_prompt_path)
        if not prompt:
            self.diagnostics.append("Scholar Facet Planner prompt missing; using deterministic fallback decomposition.")
            return self.fallback_atomic_plan(query_plan)
        result = call_qwen_chat(
            "AtomicRelevancePlannerAgent",
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "query_plan": query_plan,
                            "runtime_constraints": {
                                "max_atomic_features": max_features or 10,
                                "note": "Prioritize a robust run. Output the most important scholarly facets instead of maximizing count.",
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            model_tier=self.atomic_model_tier,
            temperature=0.1,
            max_tokens=4200,
            response_format={"type": "json_object"},
        )
        text = str(result.get("content") or "")
        payload = parse_json_like(text, fallback=None)
        normalized = self.normalize_atomic_plan(payload, query_plan)
        normalized = self.filter_atomic_plan_by_topic(normalized, query_plan)
        if len(normalized["atomic_relevance_plan"]["atomic_features"]) < 4:
            self.diagnostics.append(
                "Scholar Facet Planner retained fewer than four topic-consistent features; "
                "using deterministic query-plan facets."
            )
            fallback = self.fallback_atomic_plan(query_plan)
            fallback["atomic_relevance_plan"]["topic_gate"] = normalized[
                "atomic_relevance_plan"
            ].get("topic_gate", {})
            return fallback
        return normalized

    def filter_atomic_plan_by_topic(
        self,
        atomic_plan: dict[str, Any],
        query_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Fail closed when an LLM facet loses the current problem identity."""
        copied = json.loads(json.dumps(atomic_plan, ensure_ascii=False))
        root = copied.setdefault("atomic_relevance_plan", {})
        features = root.get("atomic_features") or []
        anchors = self.query_topic_anchor_tokens(query_plan)
        domain_guard = self.query_domain_guard(query_plan)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        for feature in features if isinstance(features, list) else []:
            if not isinstance(feature, dict):
                continue
            text = " ".join([
                str(feature.get("feature_name") or ""),
                str(feature.get("description") or ""),
                " ".join(clean_list(feature.get("positive_keywords"), limit=30)),
                " ".join(clean_list(feature.get("retrieval_terms"), limit=30)),
                str(feature.get("recall_intent") or ""),
            ])
            matches = sorted(set(tokenize(text)) & anchors)
            if anchors and not matches:
                rejected.append({
                    "feature_id": str(feature.get("feature_id") or ""),
                    "feature_name": str(feature.get("feature_name") or ""),
                    "reason": "no_current_topic_anchor",
                })
                continue
            if self.text_has_domain_conflict(
                str(feature.get("feature_name") or ""), text, domain_guard
            ):
                rejected.append({
                    "feature_id": str(feature.get("feature_id") or ""),
                    "feature_name": str(feature.get("feature_name") or ""),
                    "reason": "physical_domain_conflict",
                })
                continue
            feature["topic_anchor_matches"] = matches[:12]
            accepted.append(feature)
        root["atomic_features"] = accepted
        root["topic_gate"] = {
            "anchor_tokens": sorted(anchors),
            "domain_guard": domain_guard,
            "accepted": len(accepted),
            "rejected": len(rejected),
            "rejected_features": rejected,
        }
        return copied

    def normalize_atomic_plan(self, payload: Any, query_plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"atomic_relevance_plan": {"user_query": self._user_query(query_plan), "atomic_features": []}}
        plan = payload.get("atomic_relevance_plan") if isinstance(payload.get("atomic_relevance_plan"), dict) else payload
        features_raw = plan.get("atomic_features") or plan.get("features") or []
        features: list[dict[str, Any]] = []
        allowed = {
            "application", "mechanism", "material", "structure", "fabrication",
            "evaluation", "challenge", "hidden_relevance",
        }
        for idx, item in enumerate(features_raw if isinstance(features_raw, list) else [], 1):
            if not isinstance(item, dict):
                continue
            fid = str(item.get("feature_id") or f"F{idx:02d}")
            ftype = str(item.get("feature_type") or "mechanism")
            if ftype not in allowed:
                ftype = "hidden_relevance"
            weight = item.get("weight", 0.8)
            try:
                weight = max(0.1, min(1.5, float(weight)))
            except Exception:
                weight = 0.8
            features.append({
                "feature_id": fid,
                "feature_name": normalize_space(str(item.get("feature_name") or item.get("name") or fid)),
                "feature_type": ftype,
                "description": normalize_space(str(item.get("description") or "")),
                "positive_keywords": clean_list(item.get("positive_keywords", []), limit=20),
                "negative_keywords": clean_list(item.get("negative_keywords", []), limit=20),
                "retrieval_terms": clean_list(item.get("retrieval_terms", []), limit=20),
                "weight": weight,
                "recall_intent": normalize_space(str(item.get("recall_intent") or "")),
                "facet_origin": normalize_space(str(item.get("facet_origin") or item.get("origin") or "query_plan")),
                "counts_toward_max_features": bool(item.get("counts_toward_max_features", True)),
            })
        return {
            "atomic_relevance_plan": {
                "user_query": str(plan.get("user_query") or self._user_query(query_plan)),
                "atomic_features": features[:12],
            }
        }

    def fallback_atomic_plan(self, query_plan: dict[str, Any]) -> dict[str, Any]:
        output = self._planner_output(query_plan)
        keywords = clean_list((output.get("keyword_decomposition") or {}).get("keywords", []), limit=16)
        scope_items = clean_list((output.get("scope_definition") or {}).get("scope_items", []), limit=8)
        user_query = self._user_query(query_plan)
        seeds = scope_items or keywords or [user_query]
        types = ["application", "mechanism", "material", "structure", "evaluation", "challenge", "hidden_relevance"]
        features = []
        for idx, seed in enumerate(seeds[:8], 1):
            terms = clean_list([seed] + keywords[:8], limit=10)
            features.append({
                "feature_id": f"F{idx:02d}",
                "feature_name": seed[:80],
                "feature_type": types[(idx - 1) % len(types)],
                "description": f"Retrieve papers that may provide local evidence or background leads for: {seed}.",
                "positive_keywords": terms,
                "negative_keywords": [],
                "retrieval_terms": terms[:8],
                "weight": 0.8,
                "recall_intent": f"Retrieve abstracts directly or indirectly related to: {seed}.",
                "facet_origin": "query_plan_fallback",
                "counts_toward_max_features": True,
            })
        return {"atomic_relevance_plan": {"user_query": user_query, "atomic_features": features}}

    def atomic_features(self, atomic_plan: dict[str, Any]) -> list[AtomicFeature]:
        features = []
        for item in (atomic_plan.get("atomic_relevance_plan") or {}).get("atomic_features", []):
            if not isinstance(item, dict):
                continue
            features.append(AtomicFeature(
                feature_id=str(item.get("feature_id") or ""),
                feature_name=str(item.get("feature_name") or ""),
                feature_type=str(item.get("feature_type") or "mechanism"),
                description=str(item.get("description") or ""),
                positive_keywords=clean_list(item.get("positive_keywords", []), limit=30),
                negative_keywords=clean_list(item.get("negative_keywords", []), limit=30),
                retrieval_terms=clean_list(item.get("retrieval_terms", []), limit=30),
                weight=float(item.get("weight") or 0.8),
                recall_intent=str(item.get("recall_intent") or ""),
                facet_origin=str(item.get("facet_origin") or item.get("origin") or "query_plan"),
                counts_toward_max_features=bool(item.get("counts_toward_max_features", True)),
            ))
        return features

    @staticmethod
    def limit_atomic_features(atomic_plan: dict[str, Any], *, max_features: int = 10) -> dict[str, Any]:
        if max_features <= 0:
            return atomic_plan
        plan = atomic_plan.get("atomic_relevance_plan") or {}
        features = plan.get("atomic_features") or []
        if isinstance(features, list) and len(features) > max_features:
            copied = json.loads(json.dumps(atomic_plan, ensure_ascii=False))
            copied["atomic_relevance_plan"]["atomic_features"] = features[:max_features]
            return copied
        return atomic_plan

    def research_facet_plan(self, atomic_plan: dict[str, Any]) -> dict[str, Any]:
        facets = []
        for feature in self.atomic_features(atomic_plan):
            facets.append({
                "facet_id": feature.feature_id,
                "facet_name": feature.feature_name,
                "facet_type": feature.feature_type,
                "description": feature.description,
                "retrieval_terms": feature.retrieval_terms,
                "positive_keywords": feature.positive_keywords,
                "negative_keywords": feature.negative_keywords,
                "weight": feature.weight,
                "recall_intent": feature.recall_intent,
                "facet_origin": feature.facet_origin,
                "counts_toward_max_features": feature.counts_toward_max_features,
            })
        return {
            "concept_name": "Scholar Facet",
            "concept_name_en": "Scholar Facet",
            "design_intent": "Decompose a research problem into small interpretable scholarly attention dimensions. Each facet recalls directly relevant papers, citation-landmark papers, review or perspective papers, and recent frontier papers.",
            "facets": facets,
        }

    def facet_terms(self, feature: AtomicFeature, *, limit: int = 8) -> list[str]:
        terms = clean_list(
            [feature.feature_name, *feature.retrieval_terms, *feature.positive_keywords, feature.description],
            limit=40,
        )
        english = [term for term in terms if re.search(r"[A-Za-z]", term)]
        return (english or terms)[:limit]

    def facet_query_base(self, feature: AtomicFeature) -> str:
        terms = self.facet_terms(feature, limit=4)
        base = " ".join(terms)
        return normalize_space(base)[:220] or feature.feature_name

    def maintain_unified_facet_abstract_library(
        self,
        atomic_plan: dict[str, Any],
        *,
        query_plan: dict[str, Any],
        seed_queries: list[str],
        backends: list[str],
        papers_per_facet: int = 20,
        top_per_backend: int = 12,
        from_year: int | None = None,
    ) -> dict[str, Any]:
        """Maintain one merged abstract/metadata library by Scholar Facet.

        This replaces the old "query x backend" matrix for the main academic
        recall path.  Every backend contributes to the same local paper record
        protocol; the facet/role tags then tell downstream agents why a paper
        was recalled.
        """

        preferred_backends = ["openalex", "semantic_scholar_public", "crossref", "core", "arxiv"]
        topic_anchors = self.query_topic_anchor_tokens(query_plan)
        domain_guard = self.query_domain_guard(query_plan)
        active_backends: list[str] = []
        configured = {str(item).strip() for item in backends or [] if str(item).strip()}
        for backend in preferred_backends:
            if backend in configured and backend not in active_backends:
                active_backends.append(backend)
            if len(active_backends) >= 4:
                break
        if len(active_backends) < 4:
            for backend in configured:
                if backend not in WEB_ONLY_BACKENDS and backend not in active_backends:
                    active_backends.append(backend)
                if len(active_backends) >= 4:
                    break
        if not active_backends:
            active_backends = ["openalex", "semantic_scholar_public", "crossref", "arxiv"]

        current_year = datetime.now().year
        recent_from_year = max(int(from_year or 0), current_year - 5) if from_year else current_year - 5
        role_specs = [
            {
                "role": "direct_relevance",
                "query_suffix": "",
                "sort_mode": "relevance",
                "from_year": from_year,
                "target": max(8, int(papers_per_facet * 0.65)),
                "limit": max(10, int(top_per_backend)),
            },
            {
                "role": "citation_landmark",
                "query_suffix": "seminal landmark highly cited first demonstration",
                "sort_mode": "citation",
                "from_year": None,
                "target": 3,
                "limit": max(5, int(top_per_backend * 0.6)),
            },
            {
                "role": "review_perspective",
                "query_suffix": "review perspective progress roadmap",
                "sort_mode": "citation",
                "from_year": None,
                "target": 2,
                "limit": max(5, int(top_per_backend * 0.6)),
            },
            {
                "role": "recent_frontier",
                "query_suffix": "recent advances frontier emerging challenge",
                "sort_mode": "relevance",
                "from_year": recent_from_year,
                "target": max(4, int(papers_per_facet * 0.35)),
                "limit": max(8, int(top_per_backend)),
            },
        ]

        summary: dict[str, Any] = {
            "mode": "unified_facet_library",
            "concept_name": "Scholar Facet Library Maintainer",
            "concept_name_en": "Scholar Facet Library Maintainer",
            "policy": "Use each Scholar Facet as the minimum retrieval unit. OpenAlex, Semantic Scholar, Crossref, CORE, arXiv, and other academic backends collaboratively enrich the same paper records with abstracts, OA status, DOI, citation counts, and full-text entry points.",
            "active_academic_backends": active_backends,
            "seed_queries": seed_queries,
            "papers_per_facet_target": int(papers_per_facet),
            "top_per_backend": int(top_per_backend),
            "role_specs": role_specs,
            "queries_executed": [],
            "facets": [],
            "new_records_added": 0,
            "duplicate_records_skipped": 0,
            "raw_results": 0,
            "topic_gate": {
                "anchor_tokens": sorted(topic_anchors),
                "domain_guard": domain_guard,
                "off_topic_records_rejected": 0,
                "rejected_samples": [],
            },
            "facet_bibliometric_recall": {
                "concept": "unified_facet_library_role_recall",
                "local_first": False,
                "backends": active_backends,
                "recent_from_year": recent_from_year,
                "facets": [],
                "totals": {"external_calls": 0, "raw_results": 0, "new_records": 0, "duplicates": 0},
            },
        }

        def _role_fetch(backend: str, query: str, limit: int, sort_mode: str, role_from_year: int | None) -> list[dict[str, Any]]:
            if backend in {"openalex", "crossref", "semantic_scholar_public"} and sort_mode in {"citation", "relevance"}:
                return self.search_facet_bibliometric_backend(
                    backend,
                    query,
                    limit,
                    sort_mode=sort_mode,
                    from_year=role_from_year,
                )
            return self._search_backend(backend, query, limit, from_year=role_from_year)

        tasks: list[tuple[AtomicFeature, dict[str, Any], str, str]] = []
        for feature in self.atomic_features(atomic_plan):
            base = self.facet_query_base(feature)
            for spec in role_specs:
                query = normalize_space(f"{base} {spec['query_suffix']}")
                for backend in active_backends:
                    tasks.append((feature, spec, backend, query))

        self._emit(
            "unified_facet_library",
            "Start maintaining the structured literature element library by Scholar Facet",
            result=f"{len(tasks)} backend query tasks",
            backends=active_backends,
            papers_per_facet=papers_per_facet,
        )

        fetch_rows: list[tuple[AtomicFeature, dict[str, Any], str, str, list[dict[str, Any]], float, str | None]] = []

        def _fetch(task: tuple[AtomicFeature, dict[str, Any], str, str]) -> tuple[AtomicFeature, dict[str, Any], str, str, list[dict[str, Any]], float, str | None]:
            feature, spec, backend, query = task
            started = time.time()
            try:
                raw = _role_fetch(
                    backend,
                    query,
                    int(spec["limit"]),
                    str(spec["sort_mode"]),
                    spec.get("from_year"),
                )
                return feature, spec, backend, query, raw, time.time() - started, None
            except Exception as exc:
                return feature, spec, backend, query, [], time.time() - started, f"{backend}:{type(exc).__name__}"

        workers = min(8, max(1, len(tasks)))
        if workers <= 1:
            fetch_rows = [_fetch(task) for task in tasks]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_fetch, task) for task in tasks]
                fetch_rows = [future.result() for future in as_completed(futures)]

        facet_role_map: dict[str, dict[str, Any]] = {}
        for feature in self.atomic_features(atomic_plan):
            facet_role_map[feature.feature_id] = {
                "facet_id": feature.feature_id,
                "facet_name": feature.feature_name,
                "roles": {spec["role"]: {"external_queries": [], "raw_results": 0, "new_records": 0, "duplicates": 0} for spec in role_specs},
                "selected_local_focus_papers": [],
            }

        for feature, spec, backend, query, raw_results, elapsed, error in fetch_rows:
            role = str(spec["role"])
            role_summary = facet_role_map[feature.feature_id]["roles"][role]
            if error:
                self.diagnostics.append(f"unified_facet_library_search_failed:{feature.feature_id}:{role}:{error}")
            self.library.record_search(query, backend, len(raw_results), cached=any((r.get("metadata") or {}).get("search_cache_hit") for r in raw_results if isinstance(r, dict)))
            role_summary["external_queries"].append({
                "backend": backend,
                "query": query,
                "raw_results": len(raw_results),
                "sort_mode": spec.get("sort_mode"),
                "from_year": spec.get("from_year"),
                "elapsed_sec": round(elapsed, 2),
                "error": error or "",
            })
            role_summary["raw_results"] += len(raw_results)
            summary["raw_results"] += len(raw_results)
            summary["facet_bibliometric_recall"]["totals"]["external_calls"] += 1
            summary["facet_bibliometric_recall"]["totals"]["raw_results"] += len(raw_results)

            inserted_for_query = 0
            duplicate_for_query = 0
            for raw in raw_results:
                record = self.raw_to_abstract_record(
                    raw,
                    query=query,
                    matched_keywords=[feature.feature_name, role],
                )
                if not record.title and not record.doi:
                    continue
                if topic_anchors and not self.paper_matches_topic_anchors(
                    record, topic_anchors, strict=False
                ):
                    summary["topic_gate"]["off_topic_records_rejected"] += 1
                    if len(summary["topic_gate"]["rejected_samples"]) < 30:
                        summary["topic_gate"]["rejected_samples"].append({
                            "facet_id": feature.feature_id,
                            "role": role,
                            "backend": backend,
                            "title": record.title[:180],
                        })
                    continue
                if self.text_has_domain_conflict(
                    record.title, record.abstract, domain_guard
                ):
                    summary["topic_gate"]["off_topic_records_rejected"] += 1
                    if len(summary["topic_gate"]["rejected_samples"]) < 30:
                        summary["topic_gate"]["rejected_samples"].append({
                            "facet_id": feature.feature_id,
                            "role": role,
                            "backend": backend,
                            "title": record.title[:180],
                            "reason": "physical_domain_conflict",
                        })
                    continue
                record.topic_tags = clean_list(
                    record.topic_tags + [
                        "unified_facet_library",
                        f"scholar_facet:{feature.feature_id}",
                        f"facet_role:{role}",
                    ],
                    limit=40,
                )
                record.raw = {
                    **record.raw,
                    "unified_facet_library": {
                        "facet_id": feature.feature_id,
                        "facet_name": feature.feature_name,
                        "role": role,
                        "backend": backend,
                        "sort_mode": spec.get("sort_mode"),
                        "target_per_facet": papers_per_facet,
                    },
                }
                _, is_new = self.library.upsert_abstract(record)
                if is_new:
                    inserted_for_query += 1
                    role_summary["new_records"] += 1
                    summary["new_records_added"] += 1
                    summary["facet_bibliometric_recall"]["totals"]["new_records"] += 1
                else:
                    duplicate_for_query += 1
                    role_summary["duplicates"] += 1
                    summary["duplicate_records_skipped"] += 1
                    summary["facet_bibliometric_recall"]["totals"]["duplicates"] += 1
            summary["queries_executed"].append({
                "facet_id": feature.feature_id,
                "facet_name": feature.feature_name,
                "role": role,
                "backend": backend,
                "query": query,
                "raw_results": len(raw_results),
                "new_records": inserted_for_query,
                "duplicates": duplicate_for_query,
                "elapsed_sec": round(elapsed, 2),
            })

        for feature in self.atomic_features(atomic_plan):
            focus = self.rank_unified_facet_library(feature, limit=max(10, int(papers_per_facet)))
            facet_role_map[feature.feature_id]["selected_local_focus_papers"] = focus
            self._emit(
                "unified_facet_library",
                "Completed unified library maintenance for one Scholar Facet",
                result=f"{feature.feature_id}: focus={len(focus)}",
                facet_id=feature.feature_id,
                facet_name=feature.feature_name,
                role_counts={
                    role: {
                        "raw": data.get("raw_results", 0),
                        "new": data.get("new_records", 0),
                        "duplicates": data.get("duplicates", 0),
                    }
                    for role, data in facet_role_map[feature.feature_id]["roles"].items()
                },
            )

        facets = list(facet_role_map.values())
        summary["facets"] = facets
        summary["facet_bibliometric_recall"]["facets"] = facets
        return summary

    def rank_unified_facet_library(self, feature: AtomicFeature, *, limit: int = 20) -> list[dict[str, Any]]:
        terms = self.facet_terms(feature, limit=12)
        pool = self.library.search_abstracts(terms, limit=max(400, limit * 50))
        rows: list[dict[str, Any]] = []
        for paper in pool:
            if self.is_web_only_record(paper):
                continue
            tags = {str(tag) for tag in paper.topic_tags}
            relevance = self.facet_relevance_score(feature, paper)
            role_bonus = 0.0
            roles = []
            for tag in tags:
                if tag.startswith("facet_role:"):
                    role = tag.split(":", 1)[1]
                    roles.append(role)
                    role_bonus += {
                        "direct_relevance": 0.14,
                        "citation_landmark": 0.08,
                        "review_perspective": 0.08,
                        "recent_frontier": 0.10,
                    }.get(role, 0.04)
            score = (
                0.48 * relevance
                + 0.18 * self.metadata_channel_score(paper)
                + 0.14 * self.source_quality_score(paper)
                + 0.10 * self.freshness_score(paper)
                + min(0.18, role_bonus)
            )
            if not paper.abstract:
                score -= 0.05
            rows.append({
                "paper_id": paper.paper_id,
                "title": paper.title,
                "doi": paper.doi,
                "year": paper.year,
                "venue": paper.venue,
                "citation_count": paper.citation_count,
                "is_oa": paper.open_access,
                "has_abstract": bool(paper.abstract),
                "best_url": paper.pdf_url or paper.landing_page_url,
                "facet_roles": sorted(set(roles)),
                "score": round(max(0.0, min(1.0, score)), 4),
            })
        rows.sort(key=lambda item: (item["score"], item.get("year") or 0, item.get("citation_count") or 0), reverse=True)
        return rows[:limit]

    def update_facet_bibliometric_recall(
        self,
        atomic_plan: dict[str, Any],
        *,
        from_year: int | None = None,
        citation_target: int = 6,
        review_target: int = 4,
        recent_target: int = 8,
        top_per_backend: int = 20,
        skip_external_when_local_sufficient: bool = False,
    ) -> dict[str, Any]:
        """Local-first, API-backed bibliometric recall for each Scholar Facet."""
        current_year = datetime.now().year
        recent_from_year = max(int(from_year or 0), current_year - 5) if from_year else current_year - 5
        role_specs = {
            "citation_landmark": {
                "target": citation_target,
                "query_suffix": "seminal landmark highly cited first demonstration",
                "from_year": None,
                "sort_mode": "citation",
            },
            "review_perspective": {
                "target": review_target,
                "query_suffix": "review perspective progress roadmap",
                "from_year": None,
                "sort_mode": "citation",
            },
            "recent_frontier": {
                "target": recent_target,
                "query_suffix": "recent advances frontier",
                "from_year": recent_from_year,
                "sort_mode": "relevance",
            },
        }
        summary = {
            "concept": "Scholar Facet bibliometric recall",
            "local_first": True,
            "backends": ["openalex", "crossref", "semantic_scholar_public"],
            "top_per_backend": top_per_backend,
            "role_targets": {role: spec["target"] for role, spec in role_specs.items()},
            "recent_from_year": recent_from_year,
            "facets": [],
            "totals": {"external_calls": 0, "raw_results": 0, "new_records": 0, "duplicates": 0, "local_sufficient_roles": 0},
        }
        for feature in self.atomic_features(atomic_plan):
            facet_summary = {
                "facet_id": feature.feature_id,
                "facet_name": feature.feature_name,
                "roles": {},
            }
            base = self.facet_query_base(feature)
            for role, spec in role_specs.items():
                local = self.rank_facet_role_candidates(feature, role, limit=max(5, int(spec["target"]) * 3))
                role_summary = {
                    "local_candidates": len(local),
                    "external_queries": [],
                    "new_records": 0,
                    "duplicates": 0,
                    "raw_results": 0,
                    "skipped_external": False,
                }
                if skip_external_when_local_sufficient and len(local) >= int(spec["target"]):
                    role_summary["skipped_external"] = True
                    summary["totals"]["local_sufficient_roles"] += 1
                    facet_summary["roles"][role] = role_summary
                    continue
                query = normalize_space(f"{base} {spec['query_suffix']}")
                for backend in summary["backends"]:
                    raw_results = self.search_facet_bibliometric_backend(
                        backend,
                        query,
                        top_per_backend,
                        sort_mode=str(spec["sort_mode"]),
                        from_year=spec["from_year"],
                    )
                    role_summary["external_queries"].append({
                        "backend": backend,
                        "query": query,
                        "raw_results": len(raw_results),
                        "sort_mode": spec["sort_mode"],
                    })
                    role_summary["raw_results"] += len(raw_results)
                    summary["totals"]["external_calls"] += 1
                    summary["totals"]["raw_results"] += len(raw_results)
                    for raw in raw_results:
                        record = self.raw_to_abstract_record(
                            raw,
                            query=query,
                            matched_keywords=[feature.feature_name, role],
                        )
                        record.topic_tags = clean_list(
                            record.topic_tags + [
                                f"scholar_facet:{feature.feature_id}",
                                f"facet_role:{role}",
                            ],
                            limit=40,
                        )
                        record.raw = {
                            **record.raw,
                            "scholar_facet_recall": {
                                "facet_id": feature.feature_id,
                                "facet_name": feature.feature_name,
                                "role": role,
                                "backend": backend,
                                "sort_mode": spec["sort_mode"],
                            },
                        }
                        _, is_new = self.library.upsert_abstract(record)
                        if is_new:
                            role_summary["new_records"] += 1
                            summary["totals"]["new_records"] += 1
                        else:
                            role_summary["duplicates"] += 1
                            summary["totals"]["duplicates"] += 1
                facet_summary["roles"][role] = role_summary
                self._emit(
                    "facet_bibliometric_recall",
                    "Completed bibliometric supplemental recall for one Scholar Facet role",
                    result=f"{feature.feature_id}:{role} raw={role_summary['raw_results']} new={role_summary['new_records']} dup={role_summary['duplicates']}",
                    facet_id=feature.feature_id,
                    facet_name=feature.feature_name,
                    role=role,
                )
            summary["facets"].append(facet_summary)
        return summary

    def search_facet_bibliometric_backend(
        self,
        backend: str,
        query: str,
        max_results: int,
        *,
        sort_mode: str,
        from_year: int | None,
    ) -> list[dict[str, Any]]:
        try:
            if backend == "openalex":
                from tools.academic_backends.openalex_backend import OpenAlexBackend
                sort = "cited_by_count:desc" if sort_mode == "citation" else "relevance_score:desc"
                return OpenAlexBackend().search(query, max_results=max_results, from_year=from_year, sort=sort)
            if backend == "crossref":
                from tools.academic_backends.crossref_backend import CrossrefBackend
                sort = "is-referenced-by-count" if sort_mode == "citation" else "relevance"
                return CrossrefBackend().search(query, max_results=max_results, from_year=from_year, sort=sort)
            if backend == "semantic_scholar_public":
                raw = self.engine.search(query, ["semantic_scholar_public"], max_results=max_results * (3 if sort_mode == "citation" else 1))
                records = []
                for item in raw:
                    year = self._safe_year(item.get("year"))
                    if from_year and year and year < int(from_year):
                        continue
                    records.append(item)
                if sort_mode == "citation":
                    records.sort(key=lambda item: self.raw_citation_count(item), reverse=True)
                return records[:max_results]
        except Exception as exc:
            self.diagnostics.append(f"facet_bibliometric_backend_failed:{backend}:{type(exc).__name__}")
        return []

    @staticmethod
    def raw_citation_count(raw: dict[str, Any]) -> int:
        meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        value = meta.get("citation_count", meta.get("cited_by_count", meta.get("is_referenced_by_count", raw.get("citation_count", 0))))
        try:
            return int(value or 0)
        except Exception:
            return 0

    def rank_facet_role_candidates(self, feature: AtomicFeature, role: str, *, limit: int = 5) -> list[dict[str, Any]]:
        terms = self.facet_terms(feature, limit=10)
        pool = self.library.search_abstracts(terms, limit=max(120, limit * 40))
        rows = []
        for paper in pool:
            item = self.paper_facet_role_item(feature, paper, role)
            if not item:
                continue
            rows.append(item)
        rows.sort(key=lambda item: item.get("composite_score", 0.0), reverse=True)
        return rows[:limit]

    def paper_facet_role_item(self, feature: AtomicFeature, paper: AbstractPaperRecord, role: str) -> dict[str, Any] | None:
        relevance = self.facet_relevance_score(feature, paper)
        if relevance <= 0.015:
            return None
        anchor_hits = self.facet_topic_anchor_hits(feature, paper)
        if not self.facet_bibliometric_candidate_allowed(feature, paper, role, relevance=relevance, anchor_hits=anchor_hits):
            return None
        citation = max(0, int(paper.citation_count or 0))
        citation_influence = self.citation_influence_score(paper)
        text = f"{paper.title} {paper.abstract} {paper.venue}"
        is_review = bool(re.search(r"\breview\b|\bperspective\b|\bprogress\b|\broadmap\b|\u7efc\u8ff0|\u8fdb\u5c55", text, re.I))
        current_year = datetime.now().year
        recent_from_year = current_year - 5
        is_recent = bool(paper.year and int(paper.year) >= recent_from_year)
        source_quality = self.source_quality_score(paper)
        freshness = self.freshness_score(paper)
        review_value = 1.0 if is_review else 0.0

        if role == "citation_landmark":
            if citation <= 0:
                return None
            role_value = 0.85 if citation >= 100 else 0.65
            composite = 0.34 * relevance + 0.42 * citation_influence + 0.14 * source_quality + 0.10 * role_value
            evidence_role = "landmark_candidate"
        elif role == "review_perspective":
            if not is_review:
                return None
            role_value = 0.9
            composite = 0.38 * relevance + 0.22 * citation_influence + 0.16 * source_quality + 0.24 * role_value
            evidence_role = "review_or_perspective"
        elif role == "recent_frontier":
            if not is_recent:
                return None
            role_value = 0.75
            composite = 0.42 * relevance + 0.13 * citation_influence + 0.18 * source_quality + 0.22 * freshness + 0.05 * role_value
            evidence_role = "recent_frontier"
        else:
            return None

        score_vector = self.facet_score_vector(
            facet_relevance=relevance,
            citation_influence=citation_influence,
            source_quality=source_quality,
            recency=freshness,
            review_value=review_value,
            landmark_value=role_value if role == "citation_landmark" else 0.0,
            frontier_value=role_value if role == "recent_frontier" else 0.0,
            fulltext_availability=1.0 if (paper.open_access or paper.pdf_url) else 0.25,
        )
        return {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "doi": paper.doi,
            "year": paper.year,
            "venue": paper.venue,
            "citation_count": paper.citation_count,
            "citation_source": self.citation_source_label(paper),
            "retrieval_role": f"facet_{role}",
            "evidence_role": evidence_role,
            "facet_relevance_score": round(relevance, 4),
            "citation_influence": round(citation_influence, 4),
            "source_quality": round(source_quality, 4),
            "freshness_score": round(freshness, 4),
            "role_value": round(role_value, 4),
            "topic_anchor_hits": int(anchor_hits),
            "composite_score": round(max(0.0, min(1.0, composite)), 4),
            "score_vector": score_vector,
            "derived_scalar_score": round(max(0.0, min(1.0, composite)), 4),
        }

    @staticmethod
    def citation_source_label(paper: AbstractPaperRecord) -> str:
        sources = {str(x).lower() for x in paper.source_apis}
        if "openalex" in sources:
            return "openalex"
        if "semantic_scholar" in sources or "semantic_scholar_public" in sources:
            return "semantic_scholar"
        if "crossref" in sources:
            return "crossref"
        return "local_merged"

    @staticmethod
    def citation_influence_score(paper: AbstractPaperRecord) -> float:
        citation = max(0, int(paper.citation_count or 0))
        return min(1.0, math.log10(1 + citation) / 4.0) if citation else 0.0

    def source_quality_score(self, paper: AbstractPaperRecord) -> float:
        source_quality = 0.35
        if paper.doi:
            source_quality += 0.25
        if paper.venue:
            source_quality += 0.15
        if paper.open_access or paper.pdf_url:
            source_quality += 0.1
        source_quality += self.journal_priority_bonus(paper.venue)
        return max(0.0, min(1.0, source_quality))

    @staticmethod
    def freshness_score(paper: AbstractPaperRecord) -> float:
        current_year = datetime.now().year
        if paper.year and int(paper.year) >= current_year - 5:
            return 1.0
        if paper.year and int(paper.year) >= current_year - 10:
            return 0.4
        return 0.1

    @staticmethod
    def facet_score_vector(
        *,
        facet_relevance: float,
        citation_influence: float,
        source_quality: float,
        recency: float,
        review_value: float,
        landmark_value: float,
        frontier_value: float,
        fulltext_availability: float,
    ) -> dict[str, float]:
        return {
            "facet_relevance": round(max(0.0, min(1.0, facet_relevance)), 4),
            "citation_influence": round(max(0.0, min(1.0, citation_influence)), 4),
            "source_quality": round(max(0.0, min(1.0, source_quality)), 4),
            "recency": round(max(0.0, min(1.0, recency)), 4),
            "review_value": round(max(0.0, min(1.0, review_value)), 4),
            "landmark_value": round(max(0.0, min(1.0, landmark_value)), 4),
            "frontier_value": round(max(0.0, min(1.0, frontier_value)), 4),
            "fulltext_availability": round(max(0.0, min(1.0, fulltext_availability)), 4),
        }

    def facet_relevance_score(self, feature: AtomicFeature, paper: AbstractPaperRecord) -> float:
        terms = feature.retrieval_terms + feature.positive_keywords + [feature.feature_name, feature.description]
        tokens = set(token for term in terms for token in tokenize(term))
        if not tokens:
            return 0.05
        text = f"{paper.title} {paper.abstract} {paper.venue}".casefold()
        if not text.strip():
            return 0.0
        matched = sum(1 for token in tokens if token in text)
        title_text = paper.title.casefold()
        title_matched = sum(1 for token in tokens if token in title_text)
        return max(0.0, min(1.0, matched / max(1, len(tokens)) + min(0.18, title_matched * 0.025)))

    @staticmethod
    def generic_facet_tokens() -> set[str]:
        return {
            "trade-off", "trade", "off", "bottleneck", "challenge", "challenges",
            "performance", "power", "effect", "effects", "impact", "impacts",
            "review", "perspective", "progress", "roadmap", "recent", "frontier",
            "environmental", "stability", "degradation", "durable", "durability",
            "high", "low", "efficient", "efficiency", "system", "systems",
            "material", "materials", "coating", "coatings", "film", "films",
            "transparent", "transparency", "optical", "properties", "application",
            "applications", "method", "methods", "design", "fabrication",
            "development", "trajectory", "historical", "history", "milestone",
            "scientific", "scholarly", "survey", "literature", "current",
            "future", "opportunity", "opportunities", "representative",
            "major", "key", "fundamental", "practical", "real", "toward",
            "towards", "platform", "technology", "technologies", "device",
            "devices", "engineering", "evaluation", "quality", "scalable",
            "scalability", "broadband", "wideband", "experimental", "theoretical",
            "analysis", "approach", "approaches", "route", "routes",
            "lens", "lenses", "imaging", "photonics", "nanophotonic",
            "nanophotonics", "integrated", "integration",
        }

    def query_topic_anchor_tokens(self, query_plan: dict[str, Any]) -> set[str]:
        """Extract stable scientific identity tokens from a confirmed query plan.

        Anchors are intentionally stricter than ordinary retrieval keywords.
        They protect a persistent multi-topic library from reusing papers that
        are merely highly cited, recent, OA, or tagged by a previous run.
        """
        output = self._planner_output(query_plan)
        keywords = clean_list(
            (output.get("keyword_decomposition") or {}).get("keywords", []),
            limit=50,
        )
        generic = self.generic_facet_tokens() | {
            "including", "covering", "remaining", "transition", "actual",
            "system", "systems", "mechanism", "mechanisms", "structure",
            "structures", "application", "material", "materials",
        }
        phrase_counts: Counter[str] = Counter()
        for phrase in keywords:
            phrase_counts.update(set(tokenize(phrase)))
        anchors = {
            token for token, count in phrase_counts.items()
            if count >= 3 and len(token) >= 4 and token not in generic
        }
        # Preserve the confirmed topic noun phrase even when a rare synonym is
        # present only once in the keyword list.
        for phrase in keywords[:3]:
            anchors.update(
                token for token in tokenize(phrase)
                if len(token) >= 4 and token not in generic
            )
        if not anchors:
            anchors.update(
                token for token in tokenize(str(output.get("problem_understanding") or ""))
                if len(token) >= 5 and token not in generic
            )
        return anchors

    def query_domain_guard(self, query_plan: dict[str, Any]) -> dict[str, list[str]]:
        """Build a coarse physical-domain guard without hard-coding a topic."""
        output = self._planner_output(query_plan)
        context = json.dumps(output, ensure_ascii=False).casefold()
        optical_identity = {
            "optical", "optics", "photonic", "photonics", "photon", "light",
            "optoelectronic", "waveguide", "metalens", "metasurface",
        }
        if not (set(tokenize(context)) & optical_identity):
            return {"required_any": [], "blocked_title_terms": []}
        return {
            "required_any": sorted({
                "optical", "optics", "photonic", "photonics", "photon", "light",
                "optoelectronic", "electromagnetic",
            }),
            "blocked_title_terms": sorted({
                "acoustic", "acoustics", "ultrasonic", "ultrasound", "seismic",
                "radar", "elastic-wave", "elasticity",
            }),
        }

    @staticmethod
    def text_has_domain_conflict(
        title: str,
        body: str,
        guard: dict[str, list[str]],
    ) -> bool:
        blocked = set(guard.get("blocked_title_terms") or [])
        required = set(guard.get("required_any") or [])
        if not blocked or not required:
            return False
        title_tokens = set(tokenize(title))
        if not (title_tokens & blocked):
            return False
        # A title explicitly spanning both domains is retained as a possible
        # method-transfer source; a purely acoustic/radar title is not.
        return not bool(title_tokens & required)

    @staticmethod
    def text_matches_topic_anchors(
        text: str,
        anchors: set[str],
        *,
        strict: bool,
    ) -> bool:
        if not anchors:
            return True
        tokens = set(tokenize(text))
        hits = tokens & anchors
        return len(hits) >= (2 if strict and len(anchors) >= 4 else 1)

    def paper_matches_topic_anchors(
        self,
        paper: AbstractPaperRecord,
        anchors: set[str],
        *,
        strict: bool,
    ) -> bool:
        if not anchors:
            return True
        title_hits = set(tokenize(paper.title)) & anchors
        all_hits = set(tokenize(f"{paper.title} {paper.abstract}")) & anchors
        if strict:
            return len(all_hits) >= (2 if len(anchors) >= 4 else 1)
        return bool(all_hits)

    def facet_topic_anchor_hits(self, feature: AtomicFeature, paper: AbstractPaperRecord) -> int:
        """Count non-generic topic anchors shared by a facet and a paper.

        This guards the bibliometric side channel.  A paper can be highly cited
        and still irrelevant if it only matches broad words such as "trade-off".
        """

        terms = feature.retrieval_terms + feature.positive_keywords + [feature.feature_name, feature.description]
        tokens = {
            token
            for term in terms
            for token in tokenize(term)
            if len(token) >= 4 and token not in self.generic_facet_tokens()
        }
        if not tokens:
            return 0
        text = f"{paper.title} {paper.abstract} {paper.venue}".casefold()
        return sum(1 for token in tokens if token in text)

    def facet_bibliometric_candidate_allowed(
        self,
        feature: AtomicFeature,
        paper: AbstractPaperRecord,
        role: str,
        *,
        relevance: float | None = None,
        anchor_hits: int | None = None,
    ) -> bool:
        """Require topic consistency before citation/review/frontier bonuses.

        Citation count and review-like title words are useful for finding
        landmarks, but they must not override the user's scientific topic.
        """

        relevance = self.facet_relevance_score(feature, paper) if relevance is None else float(relevance)
        anchor_hits = self.facet_topic_anchor_hits(feature, paper) if anchor_hits is None else int(anchor_hits)
        role = str(role or "").replace("facet_", "")
        if role in {"citation_landmark", "review_perspective"}:
            return relevance >= 0.16 or (relevance >= 0.10 and anchor_hits >= 2)
        if role == "recent_frontier":
            return relevance >= 0.12 or (relevance >= 0.08 and anchor_hits >= 2)
        return True

    def build_facet_literature_map(self, atomic_plan: dict[str, Any], ranking_tables: list[dict[str, Any]]) -> dict[str, Any]:
        ranking_by_feature = {str(table.get("feature_id") or ""): table for table in ranking_tables}
        facets = []
        matrix_rows: list[dict[str, Any]] = []
        for feature in self.atomic_features(atomic_plan):
            relevance_top = []
            for row in (ranking_by_feature.get(feature.feature_id) or {}).get("top_candidates", [])[:15]:
                paper = self.library.get_abstract(row.get("paper_id", ""))
                if not paper:
                    continue
                relevance = float(row.get("program_score") or 0.0)
                text = f"{paper.title} {paper.abstract} {paper.venue}"
                is_review = bool(re.search(r"\breview\b|\bperspective\b|\bprogress\b|\broadmap\b|\u7efc\u8ff0|\u8fdb\u5c55", text, re.I))
                score_vector = self.facet_score_vector(
                    facet_relevance=relevance,
                    citation_influence=self.citation_influence_score(paper),
                    source_quality=self.source_quality_score(paper),
                    recency=self.freshness_score(paper),
                    review_value=1.0 if is_review else 0.0,
                    landmark_value=0.0,
                    frontier_value=0.4 if self.freshness_score(paper) >= 1.0 else 0.0,
                    fulltext_availability=1.0 if (paper.open_access or paper.pdf_url) else 0.25,
                )
                relevance_top.append({
                    "paper_id": paper.paper_id,
                    "title": paper.title,
                    "doi": paper.doi,
                    "year": paper.year,
                    "venue": paper.venue,
                    "citation_count": paper.citation_count,
                    "retrieval_role": "facet_relevance_top",
                    "evidence_role": "direct_relevance",
                    "program_score": row.get("program_score", 0),
                    "matched_terms": row.get("matched_terms", []),
                    "score_vector": score_vector,
                    "derived_scalar_score": round(relevance, 4),
                })
            citation_landmarks = self.rank_facet_role_candidates(feature, "citation_landmark", limit=6)
            review_perspectives = self.rank_facet_role_candidates(feature, "review_perspective", limit=4)
            recent_frontiers = self.rank_facet_role_candidates(feature, "recent_frontier", limit=8)
            merged: dict[str, dict[str, Any]] = {}
            for bucket in [relevance_top, citation_landmarks, review_perspectives, recent_frontiers]:
                for item in bucket:
                    pid = item.get("paper_id")
                    if not pid:
                        continue
                    existing = merged.setdefault(pid, {**item, "retrieval_roles": [], "evidence_roles": []})
                    role = item.get("retrieval_role")
                    evidence_role = item.get("evidence_role")
                    if role and role not in existing["retrieval_roles"]:
                        existing["retrieval_roles"].append(role)
                    if evidence_role and evidence_role not in existing["evidence_roles"]:
                        existing["evidence_roles"].append(evidence_role)
                    existing["composite_score"] = max(float(existing.get("composite_score") or 0), float(item.get("composite_score") or item.get("program_score") or 0))
                    if not existing.get("score_vector") and item.get("score_vector"):
                        existing["score_vector"] = item.get("score_vector")
                    existing["derived_scalar_score"] = max(
                        float(existing.get("derived_scalar_score") or 0),
                        float(item.get("derived_scalar_score") or item.get("composite_score") or item.get("program_score") or 0),
                    )
            merged_candidates = sorted(merged.values(), key=lambda item: item.get("composite_score", 0), reverse=True)
            for item in merged_candidates:
                matrix_rows.append({
                    "facet_id": feature.feature_id,
                    "facet_name": feature.feature_name,
                    "paper_id": item.get("paper_id"),
                    "title": item.get("title"),
                    "retrieval_roles": item.get("retrieval_roles", []),
                    "evidence_roles": item.get("evidence_roles", []),
                    "score_vector": item.get("score_vector", {}),
                    "derived_scalar_score": round(float(item.get("derived_scalar_score") or item.get("composite_score") or 0), 4),
                })
            facets.append({
                "facet_id": feature.feature_id,
                "facet_name": feature.feature_name,
                "facet_type": feature.feature_type,
                "role_budget": {
                    "citation_landmark": {"min": 1, "max": 4},
                    "review_perspective": {"min": 1, "max": 3},
                    "recent_frontier": {"min": 2, "max": 6},
                    "direct_relevance_should_dominate": True,
                },
                "relevance_top_papers": relevance_top,
                "citation_landmark_papers": citation_landmarks,
                "review_perspective_papers": review_perspectives,
                "recent_frontier_papers": recent_frontiers,
                "merged_candidates": merged_candidates,
            })
        return {
            "concept_name": "Scholar Facet Literature Map",
            "concept_name_en": "Scholar Facet Literature Map",
            "selection_policy": "For each facet, retain a controlled number of highly cited landmark papers, review or perspective papers, and recent frontier papers, while keeping directly relevant papers dominant.",
            "vector_schema": {
                "facet_relevance": "Direct relevance of the paper to the current Scholar Facet.",
                "citation_influence": "Normalized citation influence, mainly used to identify landmark or backbone papers.",
                "source_quality": "Metadata quality, including DOI, venue, OA status, and full-text entry points.",
                "recency": "Recent-frontier value, highest for papers from the last five years.",
                "review_value": "Value as a review, perspective, progress, or roadmap paper.",
                "landmark_value": "Value as a seminal, highly cited, or backbone paper.",
                "frontier_value": "Value as a recent frontier paper.",
                "fulltext_availability": "Availability of parseable full text.",
            },
            "paper_facet_matrix": {
                "rows": matrix_rows,
                "row_count": len(matrix_rows),
            },
            "facets": facets,
        }

    def feature_wise_recall(
        self,
        atomic_plan: dict[str, Any],
        papers: list[AbstractPaperRecord],
        *,
        per_feature: int = 24,
    ) -> list[dict[str, Any]]:
        tables = []
        for feature in self.atomic_features(atomic_plan):
            terms = feature.retrieval_terms + feature.positive_keywords + [feature.feature_name, feature.description]
            term_tokens = set(token for term in terms for token in tokenize(term))
            negative_tokens = set(token for term in feature.negative_keywords for token in tokenize(term))
            rows = []
            for paper in papers:
                text = f"{paper.title} {paper.abstract}".casefold()
                if not text.strip():
                    continue
                pos = sum(1 for token in term_tokens if token in text)
                neg = sum(1 for token in negative_tokens if token in text)
                lexical = pos / max(1, len(term_tokens))
                abstract_bonus = min(0.12, len(paper.abstract) / 5000)
                score = lexical + abstract_bonus + self.journal_priority_bonus(paper.venue) - 0.08 * neg
                if paper.year and paper.year >= 2020:
                    score += 0.04
                if paper.open_access or paper.pdf_url:
                    score += 0.04
                audit = self.source_audit_for_paper(paper)
                use_policy = str(audit.get("use_policy") or "")
                if use_policy == "fulltext_priority":
                    score += 0.05
                elif use_policy == "background_only":
                    score -= 0.03
                elif use_policy == "exclude":
                    score -= 0.3
                if score <= 0 and len(papers) > per_feature:
                    continue
                rows.append({
                    "paper_id": paper.paper_id,
                    "title": paper.title,
                    "year": paper.year,
                    "venue": paper.venue,
                    "doi": paper.doi,
                    "program_score": round(max(0.0, min(1.0, score)), 4),
                    "matched_terms": [token for token in sorted(term_tokens) if token in text][:12],
                    "source_audit": audit,
                })
            rows.sort(key=lambda r: r["program_score"], reverse=True)
            tables.append({
                "feature_id": feature.feature_id,
                "feature_name": feature.feature_name,
                "candidate_count": len(rows),
                "top_candidates": rows[:per_feature],
            })
        return tables

    def score_feature_candidates(
        self,
        atomic_plan: dict[str, Any],
        ranking_tables: list[dict[str, Any]],
        *,
        batch_size: int = 12,
        max_workers: int = 1,
    ) -> list[dict[str, Any]]:
        prompt = read_text_file(self.scorer_prompt_path)
        features_by_id = {f.feature_id: f for f in self.atomic_features(atomic_plan)}
        grouped: dict[str, dict[str, Any]] = {}
        tasks: list[tuple[AtomicFeature, int, list[AbstractPaperRecord]]] = []
        for table in ranking_tables:
            feature = features_by_id.get(str(table.get("feature_id")))
            if not feature:
                continue
            candidates = []
            for row in table.get("top_candidates", []):
                paper = self.library.get_abstract(row.get("paper_id", ""))
                if paper:
                    candidates.append(paper)
            for start in range(0, len(candidates), max(1, batch_size)):
                batch = candidates[start: start + max(1, batch_size)]
                tasks.append((feature, start // max(1, batch_size), batch))
            grouped[feature.feature_id] = {
                "feature_id": feature.feature_id,
                "feature_name": feature.feature_name,
                "batches": {},
            }

        def score_one(feature: AtomicFeature, batch_index: int, batch: list[AbstractPaperRecord]) -> tuple[str, int, list[dict[str, Any]]]:
            if self.real_llm and prompt:
                scored = self._score_batch_with_llm(prompt, feature, batch)
            else:
                scored = self._score_batch_deterministic(feature, batch)
            return feature.feature_id, batch_index, scored

        workers = max(1, min(int(max_workers or 1), max(1, len(tasks))))
        self._emit("feature_scoring", "Start feature-level scoring", result=f"{len(tasks)} batches", max_workers=workers, batch_size=batch_size)
        if workers == 1:
            for feature, batch_index, batch in tasks:
                fid, idx, scored = score_one(feature, batch_index, batch)
                grouped[fid]["batches"][idx] = scored
                self._emit("feature_scoring", "Completed one scoring batch", result=f"{fid}#{idx}", papers=len(batch))
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(score_one, feature, batch_index, batch): (feature, batch_index, batch)
                    for feature, batch_index, batch in tasks
                }
                for future in as_completed(future_map):
                    feature, batch_index, batch = future_map[future]
                    try:
                        fid, idx, scored = future.result()
                    except Exception as exc:
                        fid, idx = feature.feature_id, batch_index
                        scored = self._score_batch_deterministic(feature, batch)
                        self.diagnostics.append(f"feature_scoring_batch_failed:{fid}#{idx}:{type(exc).__name__}")
                    grouped[fid]["batches"][idx] = scored
                    self._emit("feature_scoring", "Completed one scoring batch", result=f"{fid}#{idx}", papers=len(batch))

        all_results = []
        for fid, item in grouped.items():
            scored_rows: list[dict[str, Any]] = []
            for idx in sorted(item["batches"]):
                scored_rows.extend(item["batches"][idx])
            all_results.append({
                "feature_scoring_result": {
                    "feature_id": item["feature_id"],
                    "feature_name": item["feature_name"],
                    "scored_papers": scored_rows,
                }
            })
        return all_results

    def _score_batch_with_llm(self, prompt: str, feature: AtomicFeature, papers: list[AbstractPaperRecord]) -> list[dict[str, Any]]:
        payload = {
            "atomic_feature": asdict(feature),
            "candidate_papers": [p.to_candidate() for p in papers],
        }
        result = call_qwen_chat(
            "FeatureLevelPaperScorerAgent",
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model_tier=self.scoring_model_tier,
            temperature=0,
            max_tokens=5000,
            response_format={"type": "json_object"},
        )
        text = str(result.get("content") or "")
        if text.startswith("[fallback]") or text.startswith("[mock]"):
            return self._score_batch_deterministic(feature, papers)
        parsed = parse_json_like(text, fallback={})
        rows = ((parsed.get("feature_scoring_result") or {}).get("scored_papers") if isinstance(parsed, dict) else None) or []
        normalized = self.normalize_scored_rows(rows, feature, papers)
        if not normalized:
            return self._score_batch_deterministic(feature, papers)
        return normalized

    def normalize_scored_rows(self, rows: Any, feature: AtomicFeature, papers: list[AbstractPaperRecord]) -> list[dict[str, Any]]:
        known = {paper.paper_id: paper for paper in papers}
        out = []
        if not isinstance(rows, list):
            return out
        for item in rows:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("paper_id") or "")
            if pid not in known:
                continue
            try:
                score = int(item.get("score", 0))
            except Exception:
                score = 0
            score = max(0, min(5, score))
            confidence = str(item.get("confidence") or "medium")
            if confidence not in {"low", "medium", "high"}:
                confidence = "medium"
            out.append({
                "paper_id": pid,
                "score": score,
                "confidence": confidence,
                "evidence_text": normalize_space(str(item.get("evidence_text") or ""))[:600],
                "reason": normalize_space(str(item.get("reason") or ""))[:600],
                "should_keep_for_feature": bool(item.get("should_keep_for_feature", score >= 3)),
            })
        return out

    def _score_batch_deterministic(self, feature: AtomicFeature, papers: list[AbstractPaperRecord]) -> list[dict[str, Any]]:
        rows = []
        terms = feature.positive_keywords + feature.retrieval_terms + [feature.feature_name]
        tokens = set(token for term in terms for token in tokenize(term))
        neg_tokens = set(token for term in feature.negative_keywords for token in tokenize(term))
        for paper in papers:
            text = f"{paper.title} {paper.abstract}".casefold()
            pos = sum(1 for token in tokens if token in text)
            neg = sum(1 for token in neg_tokens if token in text)
            ratio = pos / max(1, len(tokens))
            score = int(round(min(5, max(0, ratio * 7 - neg))))
            if score == 0 and ratio > 0:
                score = 1
            evidence = self._snippet_for_terms(paper.abstract or paper.title, tokens)
            rows.append({
                "paper_id": paper.paper_id,
                "score": score,
                "confidence": "low" if not paper.abstract else "medium",
                "evidence_text": evidence,
                "reason": "Deterministic fallback score based on overlap between title/abstract and scholarly-facet keywords.",
                "should_keep_for_feature": score >= 3 or (score == 2 and feature.feature_type == "hidden_relevance"),
            })
        return rows

    @staticmethod
    def _snippet_for_terms(text: str, tokens: set[str], length: int = 260) -> str:
        text = normalize_space(text)
        lowered = text.casefold()
        for token in tokens:
            idx = lowered.find(token)
            if idx >= 0:
                start = max(0, idx - length // 3)
                return text[start: start + length]
        return text[:length]

    def _deterministic_current_topic_fit(
        self,
        query_plan: dict[str, Any],
        paper: AbstractPaperRecord,
    ) -> dict[str, Any]:
        """Fail-closed topic-role fallback when the LLM gate is unavailable.

        This deliberately distinguishes strong phrase-level overlap from broad
        method vocabulary.  It is a safety net, not a replacement for the
        semantic gate used in real runs.
        """
        output = self._planner_output(query_plan)
        keywords = clean_list(
            (output.get("keyword_decomposition") or {}).get("keywords", []),
            limit=40,
        )
        generic = self.generic_facet_tokens() | {
            "design", "method", "methods", "using", "based", "study",
            "studies", "review", "recent", "advanced", "performance",
        }
        signatures: list[set[str]] = []
        for phrase in keywords:
            tokens = {
                token for token in tokenize(phrase)
                if len(token) >= 3 and token not in generic
            }
            if tokens:
                signatures.append(tokens)
        paper_tokens = set(tokenize(f"{paper.title} {paper.abstract} {paper.venue}"))
        title_tokens = set(tokenize(paper.title))
        overlaps = [paper_tokens & signature for signature in signatures]
        best = max(overlaps, key=len, default=set())
        global_identity = set().union(*signatures[:3]) if signatures else set()
        identity_hits = paper_tokens & global_identity
        title_identity_hits = title_tokens & global_identity

        if len(best) >= 3 and (len(title_identity_hits) >= 2 or len(identity_hits) >= 4):
            relevance_class = "direct_core"
            score = 4
        elif len(best) >= 2 and len(identity_hits) >= 2:
            relevance_class = "supporting_context"
            score = 3
        elif len(best) >= 2 or len(identity_hits) >= 1:
            relevance_class = "method_transfer"
            score = 2
        else:
            relevance_class = "off_topic"
            score = 0
        return {
            "paper_id": paper.paper_id,
            "relevance_class": relevance_class,
            "directness_score": score,
            "confidence": "low",
            "matched_topic_components": sorted(best)[:8],
            "reason": "Deterministic phrase-level fallback used because the semantic current-topic gate was unavailable.",
            "keep_for_fulltext": relevance_class in {"direct_core", "supporting_context"},
            "audit_mode": "deterministic_fallback",
        }

    def _normalize_topic_fit_rows(
        self,
        rows: Any,
        papers: list[AbstractPaperRecord],
        query_plan: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        known = {paper.paper_id: paper for paper in papers}
        normalized: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("paper_id") or "")
                if pid not in known:
                    continue
                relevance_class = str(row.get("relevance_class") or "").strip().lower()
                if relevance_class not in {
                    "direct_core", "supporting_context", "method_transfer", "off_topic",
                }:
                    continue
                try:
                    directness = max(0, min(5, int(row.get("directness_score", 0))))
                except Exception:
                    directness = 0
                confidence = str(row.get("confidence") or "low").strip().lower()
                if confidence not in {"low", "medium", "high"}:
                    confidence = "low"
                keep = bool(row.get("keep_for_fulltext"))
                if relevance_class in {"direct_core", "supporting_context"}:
                    keep = True
                elif relevance_class == "off_topic":
                    keep = False
                normalized[pid] = {
                    "paper_id": pid,
                    "relevance_class": relevance_class,
                    "directness_score": directness,
                    "confidence": confidence,
                    "matched_topic_components": clean_list(
                        row.get("matched_topic_components", []), limit=12
                    ),
                    "reason": normalize_space(str(row.get("reason") or ""))[:600],
                    "keep_for_fulltext": keep,
                    "audit_mode": "semantic_llm",
                }
        for pid, paper in known.items():
            if pid not in normalized:
                normalized[pid] = self._deterministic_current_topic_fit(query_plan, paper)
        return normalized

    def audit_current_topic_fit(
        self,
        query_plan: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        batch_size: int = 10,
        max_workers: int = 6,
    ) -> dict[str, dict[str, Any]]:
        """Classify candidate roles against the current question before download.

        Feature relevance answers whether a paper is useful for one local
        facet.  This gate answers the different, global question: whether that
        paper is core, supporting, transferable, or off-topic for the current
        user request.  Persistent-cache availability never enters the prompt.
        """
        papers = [item.get("paper") for item in items if item.get("paper")]
        if not papers:
            return {}
        prompt = read_text_file(self.current_topic_gate_prompt_path)
        output = self._planner_output(query_plan)
        topic_context = {
            "user_query": self._user_query(query_plan),
            "problem_understanding": output.get("problem_understanding", ""),
            "scope_definition": output.get("scope_definition", {}),
            "keyword_decomposition": output.get("keyword_decomposition", {}),
        }
        item_by_id = {item["paper"].paper_id: item for item in items if item.get("paper")}
        batches = [papers[i:i + max(1, batch_size)] for i in range(0, len(papers), max(1, batch_size))]

        def audit_batch(index: int, batch: list[AbstractPaperRecord]) -> tuple[int, dict[str, dict[str, Any]]]:
            if not self.real_llm or not prompt:
                return index, {
                    paper.paper_id: self._deterministic_current_topic_fit(query_plan, paper)
                    for paper in batch
                }
            candidates = []
            for paper in batch:
                item = item_by_id.get(paper.paper_id, {})
                candidates.append({
                    "paper_id": paper.paper_id,
                    "title": paper.title,
                    "abstract": paper.abstract,
                    "year": paper.year,
                    "venue": paper.venue,
                    "matched_scholarly_facets": [
                        {
                            "feature_name": match.get("feature_name", ""),
                            "score": match.get("score", 0),
                            "evidence_text": match.get("evidence_text", ""),
                        }
                        for match in (item.get("matched_features") or [])[:10]
                    ],
                })
            result = call_qwen_chat(
                "CurrentTopicPaperGateAgent",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps({
                        "confirmed_topic": topic_context,
                        "candidate_papers": candidates,
                    }, ensure_ascii=False)},
                ],
                model_tier=self.scoring_model_tier,
                temperature=0,
                max_tokens=5000,
                response_format={"type": "json_object"},
                max_retries=2,
            )
            text = str(result.get("content") or "")
            parsed = parse_json_like(text, fallback={})
            rows = parsed.get("topic_fit_results", []) if isinstance(parsed, dict) else []
            return index, self._normalize_topic_fit_rows(rows, batch, query_plan)

        workers = max(1, min(int(max_workers or 1), len(batches)))
        results: dict[str, dict[str, Any]] = {}
        self._emit(
            "current_topic_gate",
            "Audit candidate roles against the current research topic",
            result=f"{len(papers)} papers in {len(batches)} batches",
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(audit_batch, idx, batch): (idx, batch) for idx, batch in enumerate(batches)}
            for future in as_completed(futures):
                idx, batch = futures[future]
                try:
                    _, batch_rows = future.result()
                except Exception as exc:
                    self.diagnostics.append(
                        f"current_topic_gate_batch_failed:{idx}:{type(exc).__name__}"
                    )
                    batch_rows = {
                        paper.paper_id: self._deterministic_current_topic_fit(query_plan, paper)
                        for paper in batch
                    }
                results.update(batch_rows)
                self._emit(
                    "current_topic_gate",
                    "Completed one current-topic audit batch",
                    result=f"{idx + 1}/{len(batches)}",
                    papers=len(batch),
                )
        counts = Counter(row.get("relevance_class", "unknown") for row in results.values())
        self._emit(
            "current_topic_gate",
            "Completed current-topic eligibility audit",
            result=json.dumps(dict(counts), ensure_ascii=False),
            reason="Core relevance is decided before OA status or cache reuse can influence acquisition.",
        )
        return results

    def decide_fulltext_upgrade(
        self,
        papers: list[AbstractPaperRecord],
        atomic_plan: dict[str, Any],
        scoring_results: list[dict[str, Any]],
        *,
        query_plan: dict[str, Any],
        facet_literature_map: dict[str, Any] | None = None,
        overall_top_n: int = 150,
        feature_top_k: int = 10,
    ) -> dict[str, Any]:
        paper_map = {paper.paper_id: paper for paper in papers}
        topic_anchors = set(
            ((atomic_plan.get("atomic_relevance_plan") or {}).get("topic_gate") or {}).get(
                "anchor_tokens", []
            )
        )
        domain_guard = (
            ((atomic_plan.get("atomic_relevance_plan") or {}).get("topic_gate") or {}).get(
                "domain_guard", {}
            )
        )
        if facet_literature_map:
            for facet in facet_literature_map.get("facets", []) or []:
                for bucket_name in ("citation_landmark_papers", "review_perspective_papers", "recent_frontier_papers"):
                    for cand in facet.get(bucket_name, []) or []:
                        paper = self.library.get_abstract(str(cand.get("paper_id") or ""))
                        if paper and (
                            not topic_anchors
                            or self.paper_matches_topic_anchors(
                                paper, topic_anchors, strict=False
                            )
                        ) and not self.text_has_domain_conflict(
                            paper.title, paper.abstract, domain_guard
                        ):
                            paper_map.setdefault(paper.paper_id, paper)
        feature_map = {f.feature_id: f for f in self.atomic_features(atomic_plan)}
        per_paper: dict[str, dict[str, Any]] = {}
        per_feature_rows: dict[str, list[dict[str, Any]]] = {}
        for result in scoring_results:
            fsr = result.get("feature_scoring_result") or {}
            fid = str(fsr.get("feature_id") or "")
            feature = feature_map.get(fid)
            weight = feature.weight if feature else 0.8
            rows = []
            for row in fsr.get("scored_papers", []) or []:
                pid = row.get("paper_id")
                if pid not in paper_map:
                    continue
                score = int(row.get("score") or 0)
                rows.append(row)
                item = per_paper.setdefault(pid, {"paper": paper_map[pid], "matched_features": [], "feature_score_sum": 0.0})
                if score >= 2 or row.get("should_keep_for_feature"):
                    item["matched_features"].append({
                        "feature_id": fid,
                        "feature_name": fsr.get("feature_name", ""),
                        "score": score,
                        "evidence_text": row.get("evidence_text", ""),
                        "reason": row.get("reason", ""),
                    })
                    item["feature_score_sum"] += (score / 5.0) * weight
            rows.sort(key=lambda r: int(r.get("score") or 0), reverse=True)
            per_feature_rows[fid] = rows

        facet_supplement_order: list[str] = []
        role_score = {
            "facet_citation_landmark": 3,
            "facet_review_perspective": 3,
            "facet_recent_frontier": 3,
        }
        role_bonus = {
            "facet_citation_landmark": 0.22,
            "facet_review_perspective": 0.16,
            "facet_recent_frontier": 0.18,
        }
        role_buckets = [
            ("citation_landmark_papers", "facet_citation_landmark", 3),
            ("review_perspective_papers", "facet_review_perspective", 2),
            ("recent_frontier_papers", "facet_recent_frontier", 4),
        ]
        for facet in (facet_literature_map or {}).get("facets", []) or []:
            fid = str(facet.get("facet_id") or "")
            feature = feature_map.get(fid)
            feature_name = str(facet.get("facet_name") or (feature.feature_name if feature else ""))
            for bucket_name, retrieval_role, per_facet_budget in role_buckets:
                kept = 0
                for cand in facet.get(bucket_name, []) or []:
                    if kept >= per_facet_budget:
                        break
                    pid = str(cand.get("paper_id") or "")
                    paper = paper_map.get(pid) or self.library.get_abstract(pid)
                    if not paper:
                        continue
                    if topic_anchors and not self.paper_matches_topic_anchors(
                        paper, topic_anchors, strict=False
                    ):
                        self.diagnostics.append(
                            f"facet_global_topic_mismatch_filtered:{fid}:{pid}:{retrieval_role}"
                        )
                        continue
                    if self.text_has_domain_conflict(
                        paper.title, paper.abstract, domain_guard
                    ):
                        self.diagnostics.append(
                            f"facet_physical_domain_conflict_filtered:{fid}:{pid}:{retrieval_role}"
                        )
                        continue
                    if feature and not self.facet_bibliometric_candidate_allowed(feature, paper, retrieval_role):
                        self.diagnostics.append(
                            f"facet_bibliometric_topic_mismatch_filtered:{fid}:{pid}:{retrieval_role}"
                        )
                        continue
                    item = per_paper.setdefault(pid, {"paper": paper, "matched_features": [], "feature_score_sum": 0.0})
                    item.setdefault("facet_retrieval_roles", [])
                    item.setdefault("evidence_roles", [])
                    item.setdefault("facet_selected_by", [])
                    item.setdefault("facet_score_vectors", [])
                    if retrieval_role not in item["facet_retrieval_roles"]:
                        item["facet_retrieval_roles"].append(retrieval_role)
                    evidence_role = str(cand.get("evidence_role") or "")
                    if evidence_role and evidence_role not in item["evidence_roles"]:
                        item["evidence_roles"].append(evidence_role)
                    if cand.get("score_vector"):
                        item["facet_score_vectors"].append({
                            "feature_id": fid,
                            "feature_name": feature_name,
                            "retrieval_role": retrieval_role,
                            "score_vector": cand.get("score_vector"),
                            "derived_scalar_score": cand.get("derived_scalar_score") or cand.get("composite_score"),
                        })
                    if retrieval_role not in item["facet_selected_by"]:
                        item["facet_selected_by"].append(retrieval_role)
                    if not any(m.get("feature_id") == fid and m.get("retrieval_role") == retrieval_role for m in item["matched_features"]):
                        item["matched_features"].append({
                            "feature_id": fid,
                            "feature_name": feature_name,
                            "score": role_score.get(retrieval_role, 3),
                            "evidence_text": f"{cand.get('evidence_role', '')}; citation_count={cand.get('citation_count')}; composite={cand.get('composite_score')}",
                            "reason": "Scholar Facet bibliometric supplemental recall for building a history-mechanism-frontier-bottleneck review backbone.",
                            "retrieval_role": retrieval_role,
                        })
                        item["feature_score_sum"] += role_bonus.get(retrieval_role, 0.12)
                    if pid not in facet_supplement_order:
                        facet_supplement_order.append(pid)
                    kept += 1

        topic_fit_by_id = self.audit_current_topic_fit(
            query_plan, list(per_paper.values())
        )

        selected_ids: set[str] = set()
        scored_candidates = []
        for pid, item in per_paper.items():
            paper: AbstractPaperRecord = item["paper"]
            matched = item["matched_features"]
            if not matched:
                continue
            topic_fit = topic_fit_by_id.get(pid)
            relevance_class = str((topic_fit or {}).get("relevance_class") or "")
            if relevance_class == "off_topic":
                self.diagnostics.append(f"current_topic_off_topic_filtered:{pid}")
                continue
            overall = item["feature_score_sum"]
            if re.search(r"\breview\b|\u7efc\u8ff0|progress|perspective", f"{paper.title} {paper.abstract}", re.I):
                overall += 0.25
            if paper.citation_count:
                overall += min(0.28, math.log10(max(1, paper.citation_count)) / 10)
            if paper.year and paper.year >= 2020:
                overall += 0.12
            if paper.open_access or paper.pdf_url:
                overall += 0.34
            audit = self.source_audit_for_paper(paper)
            use_policy = str(audit.get("use_policy") or "")
            if use_policy == "fulltext_priority":
                overall += 0.18
            elif use_policy == "abstract_ok":
                overall += 0.05
            elif use_policy == "background_only":
                overall -= 0.18
            elif use_policy == "manual_review":
                overall -= 0.08
            elif use_policy == "exclude":
                overall -= 0.6
            overall += min(0.22, 0.06 * len({m["feature_id"] for m in matched}))
            overall += self.journal_priority_bonus(paper.venue)
            if relevance_class == "direct_core":
                overall += 0.55
            elif relevance_class == "supporting_context":
                overall += 0.18
            elif relevance_class == "method_transfer":
                overall -= 0.20
            item["topic_fit"] = topic_fit or {}
            scored_candidates.append((overall, pid, item))
        scored_candidates.sort(reverse=True, key=lambda row: row[0])
        for _, pid, _ in scored_candidates[:overall_top_n]:
            selected_ids.add(pid)
        for fid, rows in per_feature_rows.items():
            kept = 0
            for row in rows:
                if kept >= feature_top_k:
                    break
                if int(row.get("score") or 0) >= 3 or row.get("should_keep_for_feature"):
                    selected_ids.add(str(row.get("paper_id")))
                    kept += 1
        supplement_cap = max(12, int(overall_top_n) // 2)
        supplement_used = 0
        for pid in facet_supplement_order:
            if supplement_used >= supplement_cap:
                break
            if pid not in selected_ids:
                selected_ids.add(pid)
                supplement_used += 1

        selected = []
        for overall, pid, item in scored_candidates:
            if pid not in selected_ids:
                continue
            paper: AbstractPaperRecord = item["paper"]
            selected_by = []
            rank = [x[1] for x in scored_candidates].index(pid) + 1
            if rank <= overall_top_n:
                selected_by.append("overall_topN")
            if any(pid in [r.get("paper_id") for r in per_feature_rows.get(m["feature_id"], [])[:feature_top_k]] for m in item["matched_features"]):
                selected_by.append("feature_topK")
            selected_by.extend(item.get("facet_selected_by", []))
            selected.append({
                "paper_id": paper.paper_id,
                "title": paper.title,
                "doi": paper.doi,
                "year": paper.year,
                "venue": paper.venue,
                "overall_score": round(overall, 4),
                "selected_by": sorted(set(selected_by)),
                "retrieval_roles": sorted(set(item.get("facet_retrieval_roles", []))),
                "evidence_roles": sorted(set(item.get("evidence_roles", []))),
                "facet_score_vectors": item.get("facet_score_vectors", []),
                "matched_features": item["matched_features"],
                "upgrade_reason": self._upgrade_reason(paper, item["matched_features"]),
                "fulltext_acquisition_priority": "high" if overall >= 1.0 else "medium",
                "pdf_url": paper.pdf_url,
                "landing_page_url": paper.landing_page_url,
                "open_access": paper.open_access,
                "citation_count": paper.citation_count,
                "source_audit": self.source_audit_for_paper(paper),
                "current_topic_fit": item.get("topic_fit", {}),
            })
        method_transfer_cap = max(6, min(20, int(max(1, overall_top_n) * 0.15)))
        method_transfer_used = 0
        portfolio = []
        for item in selected:
            role = str((item.get("current_topic_fit") or {}).get("relevance_class") or "")
            if role == "method_transfer":
                if method_transfer_used >= method_transfer_cap:
                    continue
                if not (item.get("current_topic_fit") or {}).get("keep_for_fulltext"):
                    continue
                method_transfer_used += 1
            portfolio.append(item)
        topic_counts = Counter(
            str((item.get("current_topic_fit") or {}).get("relevance_class") or "legacy_unclassified")
            for item in portfolio
        )
        self.diagnostics.append(
            "current_topic_fulltext_portfolio:" + json.dumps(dict(topic_counts), ensure_ascii=False)
        )
        return {
            "selected_for_fulltext_upgrade": portfolio,
            "current_topic_fit_summary": dict(topic_counts),
            "method_transfer_cap": method_transfer_cap,
        }

    def _upgrade_reason(self, paper: AbstractPaperRecord, matched_features: list[dict[str, Any]]) -> str:
        reasons = []
        if paper.pdf_url or paper.open_access:
            reasons.append("OA or PDF signal exists")
        if paper.venue and self.journal_priority_bonus(paper.venue) > 0:
            reasons.append("high-priority venue or source")
        if paper.year and paper.year >= 2020:
            reasons.append("recent paper")
        if matched_features:
            reasons.append(f"covers {len({m['feature_id'] for m in matched_features})} scholarly facets")
        return "; ".join(reasons) or "selected for full-text upgrade by feature relevance score"

    def journal_priority_bonus(self, venue: str) -> float:
        v = str(venue or "").casefold()
        for journal in self.target_journals:
            if journal.casefold() in v:
                return 0.18
        return 0.0

    @staticmethod
    def paper_has_open_access_hint(paper: AbstractPaperRecord) -> bool:
        raw = paper.raw or {}
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        if paper.open_access is True:
            return True
        if paper.pdf_url:
            return True
        for value in (
            raw.get("is_oa"),
            raw.get("open_access"),
            raw_meta.get("is_oa"),
            raw_meta.get("open_access"),
        ):
            if value is True:
                return True
            if isinstance(value, dict) and value.get("is_oa") is True:
                return True
            if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "oa", "open"}:
                return True
        return False

    @staticmethod
    def paper_likely_needs_institution(paper: AbstractPaperRecord) -> bool:
        """Heuristic split for fulltext acquisition scheduling.

        OA/JATS/PDF/arXiv style records can be fetched in parallel through
        public routes.  Non-OA records without a direct PDF are scheduled
        serially when Edge-CDP institution access is enabled.
        """

        raw = paper.raw or {}
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}
        if LiteratureResourceBuilder.paper_has_open_access_hint(paper):
            return False
        if str(raw.get("arxiv_id") or "").strip():
            return False
        best_oa = raw_meta.get("best_oa_location") if isinstance(raw_meta.get("best_oa_location"), dict) else {}
        if best_oa.get("url_for_pdf") or best_oa.get("pdf_url") or best_oa.get("url"):
            return False
        open_pdf = raw_meta.get("open_access_pdf") if isinstance(raw_meta.get("open_access_pdf"), dict) else {}
        if open_pdf.get("url") or open_pdf.get("url_for_pdf"):
            return False
        oa_locations = raw_meta.get("oa_locations") if isinstance(raw_meta.get("oa_locations"), list) else []
        if any(isinstance(loc, dict) and (loc.get("url_for_pdf") or loc.get("pdf_url") or loc.get("url")) for loc in oa_locations[:8]):
            return False
        return True

    @staticmethod
    def _paper_bibliographic_priority(
        paper: AbstractPaperRecord,
    ) -> tuple[int, int, int, int, int]:
        raw_meta = (
            paper.raw.get("raw_metadata")
            if isinstance(paper.raw.get("raw_metadata"), dict)
            else {}
        )
        is_preprint = (
            "arxiv" in paper.venue.casefold()
            or str(raw_meta.get("type") or "").casefold() == "preprint"
        )
        return (
            int(bool(normalize_doi(paper.doi))),
            int(not is_preprint),
            int(bool(paper.openalex_id or paper.semantic_scholar_id)),
            int(bool(paper.pdf_url)),
            int(paper.year or 0),
        )

    def _same_fulltext_work(
        self,
        left: AbstractPaperRecord,
        right: AbstractPaperRecord,
    ) -> bool:
        left_doi = normalize_doi(left.doi)
        right_doi = normalize_doi(right.doi)
        if left_doi and right_doi and left_doi == right_doi:
            return True
        if (
            left.semantic_scholar_id
            and right.semantic_scholar_id
            and left.semantic_scholar_id == right.semantic_scholar_id
        ):
            return True
        if left.openalex_id and right.openalex_id and left.openalex_id == right.openalex_id:
            return True
        return self.library._same_work_version(left, right)

    @staticmethod
    def _merge_candidate_dict_rows(
        items: list[dict[str, Any]],
        field: str,
        identity_fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in items:
            for row in item.get(field) or []:
                if not isinstance(row, dict):
                    continue
                identity = "|".join(
                    str(row.get(key) or "") for key in identity_fields
                )
                if not identity.strip("|"):
                    identity = json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                if identity not in merged:
                    merged[identity] = dict(row)
                    order.append(identity)
                    continue
                current = merged[identity]
                try:
                    current_score = float(current.get("score") or 0)
                    incoming_score = float(row.get("score") or 0)
                except Exception:
                    current_score = incoming_score = 0.0
                if incoming_score > current_score:
                    merged[identity] = {**current, **row}
        return [merged[key] for key in order]

    def deduplicate_fulltext_candidates(
        self,
        selected: list[dict[str, Any]],
    ) -> tuple[list[tuple[dict[str, Any], AbstractPaperRecord]], int]:
        """Collapse legacy/version aliases before cache checks and downloads."""
        groups: list[dict[str, Any]] = []
        for raw_item in selected:
            item = dict(raw_item)
            paper = self.library.get_abstract(str(item.get("paper_id") or ""))
            if paper is None:
                continue
            target = next(
                (
                    group
                    for group in groups
                    if any(
                        self._same_fulltext_work(paper, member)
                        for member in group["papers"]
                    )
                ),
                None,
            )
            if target is None:
                groups.append({"items": [item], "papers": [paper]})
            else:
                target["items"].append(item)
                target["papers"].append(paper)

        resolved: list[tuple[dict[str, Any], AbstractPaperRecord]] = []
        collapsed = 0
        for group in groups:
            items = group["items"]
            papers = group["papers"]
            canonical = max(papers, key=self._paper_bibliographic_priority)
            merged_paper = canonical
            for alias in papers:
                if alias.paper_id != canonical.paper_id:
                    merged_paper = self.library._merge_record(merged_paper, alias)
            merged_paper.paper_id = canonical.paper_id

            merged_item = dict(
                max(items, key=lambda row: float(row.get("overall_score") or 0.0))
            )
            merged_item["paper_id"] = canonical.paper_id
            merged_item["identity_alias_paper_ids"] = clean_list(
                [paper.paper_id for paper in papers],
                limit=50,
            )
            merged_item["identity_aliases_collapsed"] = max(0, len(papers) - 1)
            for field in ("selected_by", "retrieval_roles", "evidence_roles"):
                merged_item[field] = clean_list(
                    [value for item in items for value in (item.get(field) or [])],
                    limit=50,
                )
            merged_item["matched_features"] = self._merge_candidate_dict_rows(
                items,
                "matched_features",
                ("feature_id",),
            )
            merged_item["facet_score_vectors"] = self._merge_candidate_dict_rows(
                items,
                "facet_score_vectors",
                ("feature_id", "facet_id"),
            )
            merged_item["overall_score"] = max(
                [float(item.get("overall_score") or 0.0) for item in items] or [0.0]
            )
            merged_item["fulltext_acquisition_priority"] = (
                "high"
                if any(
                    item.get("fulltext_acquisition_priority") == "high"
                    for item in items
                )
                else "medium"
            )
            merged_item.update({
                "title": merged_paper.title,
                "doi": merged_paper.doi,
                "year": merged_paper.year,
                "venue": merged_paper.venue,
                "pdf_url": merged_paper.pdf_url,
                "landing_page_url": merged_paper.landing_page_url,
                "open_access": merged_paper.open_access,
                "citation_count": merged_paper.citation_count,
            })
            collapsed += max(0, len(papers) - 1)
            resolved.append((merged_item, merged_paper))
        return resolved, collapsed

    def prioritize_fulltext_download_queue(
        self,
        candidates: list[tuple[dict[str, Any], AbstractPaperRecord]],
        *,
        max_downloads: int,
    ) -> tuple[list[tuple[dict[str, Any], AbstractPaperRecord]], list[tuple[dict[str, Any], AbstractPaperRecord]]]:
        """Order new fulltext attempts by facet coverage before scalar score.

        A literature review can collapse if the download cap is consumed by one
        dominant direction.  This scheduler first reserves coverage across
        Scholar Facets, then fills the rest by overall score and availability.
        """

        if max_downloads <= 0 or len(candidates) <= max_downloads:
            return candidates[:max_downloads], candidates[max_downloads:]

        def candidate_score(item: dict[str, Any], paper: AbstractPaperRecord) -> float:
            try:
                score = float(item.get("overall_score") or 0.0)
            except Exception:
                score = 0.0
            score += min(0.35, 0.05 * len(item.get("matched_features") or []))
            if paper.open_access or paper.pdf_url:
                score += 0.18
            if item.get("source_audit", {}).get("use_policy") == "fulltext_priority":
                score += 0.12
            return score

        remaining: dict[str, tuple[dict[str, Any], AbstractPaperRecord]] = {
            item.get("paper_id") or paper.paper_id: (item, paper)
            for item, paper in candidates
        }
        selected: list[tuple[dict[str, Any], AbstractPaperRecord]] = []
        feature_order: list[str] = []
        buckets: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for item, paper in candidates:
            pid = item.get("paper_id") or paper.paper_id
            for feature in item.get("matched_features") or []:
                fid = str(feature.get("feature_id") or "")
                if not fid:
                    continue
                if fid not in feature_order:
                    feature_order.append(fid)
                try:
                    llm_feature_score = float(feature.get("score") or 0)
                except Exception:
                    llm_feature_score = 0.0
                specificity = 1.0 / max(1, len(item.get("matched_features") or []))
                feature_score = 10.0 * llm_feature_score + 0.6 * specificity + 0.1 * candidate_score(item, paper)
                try:
                    feature_score += 0.02 * float(item.get("overall_score") or 0)
                except Exception:
                    pass
                buckets[fid].append((feature_score, pid))

        for fid in feature_order:
            if len(selected) >= max_downloads:
                break
            for _score, pid in sorted(buckets.get(fid, []), reverse=True):
                pair = remaining.pop(pid, None)
                if pair is None:
                    continue
                selected.append(pair)
                break

        rest = list(remaining.values())
        rest.sort(key=lambda pair: candidate_score(pair[0], pair[1]), reverse=True)
        slots = max(0, max_downloads - len(selected))
        selected.extend(rest[:slots])
        overflow = rest[slots:]
        return selected, overflow

    def acquire_fulltexts(self, selected: list[dict[str, Any]], *, max_downloads: int = 150) -> dict[str, Any]:
        reused: list[dict[str, Any]] = []
        downloaded: list[dict[str, Any]] = []
        background_cached: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []

        resolved_candidates, identity_aliases_collapsed = (
            self.deduplicate_fulltext_candidates(selected)
        )
        if identity_aliases_collapsed:
            self._emit(
                "fulltext_identity",
                "Collapsed legacy or publication/preprint aliases before full-text acquisition",
                result=f"{identity_aliases_collapsed} aliases collapsed",
            )

        # ── Phase 1: serial pre-classification (SQLite reads stay on main thread) ──
        download_candidates: list[tuple[dict[str, Any], AbstractPaperRecord]] = []
        for item, paper in resolved_candidates:
            alias_ids = clean_list(
                [paper.paper_id, *(item.get("identity_alias_paper_ids") or [])],
                limit=50,
            )
            cached_records = [
                cached_record
                for alias_id in alias_ids
                if (cached_record := self.library.get_fulltext(alias_id)) is not None
            ]
            cached = next(
                (
                    record
                    for record in cached_records
                    if record.fulltext_status == "available"
                    and record.parsed_text_path
                    and Path(record.parsed_text_path).exists()
                ),
                None,
            )
            if cached is not None:
                try:
                    cached_text = Path(cached.parsed_text_path).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    cached_text = ""
                if cached.fulltext_type == "pdf" or str(cached.fulltext_type).startswith("pdf_") or cached.fulltext_type in {"jats_xml", "publisher_html"} or self._looks_like_article_fulltext(cached_text):
                    cached_payload = {
                        **asdict(cached),
                        "matched_features": item.get("matched_features", []),
                        "retrieval_roles": item.get("retrieval_roles", []),
                        "evidence_roles": item.get("evidence_roles", []),
                        "identity_alias_paper_ids": alias_ids,
                    }
                    if cached.paper_id != paper.paper_id:
                        cached_payload["cache_source_paper_id"] = cached.paper_id
                        cached_payload["paper_id"] = paper.paper_id
                        cached_payload["doi"] = paper.doi or cached.doi
                        cached_payload["title"] = paper.title or cached.title
                    reused.append(cached_payload)
                    self._emit("fulltext_acquisition", "Reuse full-text cache", result=paper.title, paper_id=paper.paper_id, access_method=cached.access_method)
                    continue
            cached = next(
                (record for record in cached_records if record.paper_id == paper.paper_id),
                cached_records[0] if cached_records else None,
            )
            # TTL skip — bypass if institution access is now enabled but wasn't used before
            if cached and cached.fulltext_status in {"background_page", "unavailable"} and self.is_recent_fulltext_attempt(cached):
                institution_upgrade = (
                    self.institution_access_enabled()
                    and "institution" not in (cached.access_method or "").lower()
                )
                if not institution_upgrade:
                    cached_payload = {**asdict(cached), "matched_features": item.get("matched_features", []), "retrieval_roles": item.get("retrieval_roles", []), "evidence_roles": item.get("evidence_roles", [])}
                    reason = (
                        "Recent automatic acquisition only obtained a background page; skip repeated fetching and add to manual/follow-up list."
                        if cached.fulltext_status == "background_page"
                        else "Recent automatic acquisition failed; skip repeated fetching and add to manual/follow-up list."
                    )
                    if cached.fulltext_status == "background_page":
                        background_cached.append(cached_payload)
                    failed.append({**item, "reason": reason, "fulltext_record": asdict(cached)})
                    self._emit("fulltext_acquisition", "Skip recent failed/background cache to avoid repeated full-text fetching cost", result=paper.title, reason=reason, paper_id=paper.paper_id, access_method=cached.access_method)
                    continue
                self._emit("fulltext_acquisition", "Institution access is enabled; retry a recently failed paper", result=paper.title, paper_id=paper.paper_id)
            download_candidates.append((item, paper))

        to_download, overflow = self.prioritize_fulltext_download_queue(download_candidates, max_downloads=max_downloads)
        if overflow:
            self._emit(
                "fulltext_acquisition",
                "Reordered full-text download queue by Scholar Facet coverage",
                result=f"{len(to_download)} papers attempted in this run; {len(overflow)} over the cap moved to follow-up/manual list",
            )
        for item, _paper in overflow:
            deferred.append({**item, "reason": "Exceeded the automatic new full-text download cap for this run; moved to follow-up queue after Scholar Facet coverage reordering, not a real failure."})

        def _fetch_one(args: tuple[dict[str, Any], AbstractPaperRecord]) -> tuple[dict[str, Any], AbstractPaperRecord, FulltextRecord]:
            _item, _paper = args
            return _item, _paper, self.acquire_one_fulltext(_paper, _item)

        raw_results: list[tuple[dict[str, Any], AbstractPaperRecord, FulltextRecord]] = []
        if self.institution_access_enabled() and self.institution_browser_channel in {"edge-cdp", "cdp"}:
            public_pool = [(item, paper) for item, paper in to_download if not self.paper_likely_needs_institution(paper)]
            institution_pool = [(item, paper) for item, paper in to_download if self.paper_likely_needs_institution(paper)]
            if public_pool:
                n_workers = min(len(public_pool), 6)
                self._emit("fulltext_acquisition", "Acquire OA/public full text in parallel", result=f"{len(public_pool)} papers, {n_workers} workers")
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    futures = {ex.submit(_fetch_one, args): args for args in public_pool}
                    for future in as_completed(futures):
                        try:
                            raw_results.append(future.result())
                        except Exception as exc:
                            _item, _paper = futures[future]
                            failed.append({**_item, "reason": f"Public full-text parallel acquisition exception: {type(exc).__name__}"})
            for item, paper in institution_pool:
                self._emit("fulltext_acquisition", "Acquire institution/publisher full text serially", title=paper.title, doi=paper.doi, paper_id=paper.paper_id)
                raw_results.append(_fetch_one((item, paper)))
        else:
            n_workers = min(len(to_download), 6)
            if n_workers <= 1:
                for item, paper in to_download:
                    self._emit("fulltext_acquisition", "Start acquiring one full text", title=paper.title, doi=paper.doi, paper_id=paper.paper_id)
                    raw_results.append(_fetch_one((item, paper)))
            else:
                self._emit("fulltext_acquisition", f"Acquire full text in parallel ({n_workers} workers)", result=f"{len(to_download)} papers pending")
                with ThreadPoolExecutor(max_workers=n_workers) as ex:
                    futures = {ex.submit(_fetch_one, args): args for args in to_download}
                    for future in as_completed(futures):
                        try:
                            raw_results.append(future.result())
                        except Exception as exc:
                            _item, _paper = futures[future]
                            failed.append({**_item, "reason": f"Parallel full-text acquisition exception: {type(exc).__name__}"})

        # ── Phase 3: serial SQLite save + classify (main thread) ──
        for item, paper, record in raw_results:
            self.library.save_fulltext_record(record)
            if record.fulltext_status == "available":
                downloaded.append({
                    **asdict(record),
                    "matched_features": item.get("matched_features", []),
                    "retrieval_roles": item.get("retrieval_roles", []),
                    "evidence_roles": item.get("evidence_roles", []),
                })
                self._emit("fulltext_acquisition", "Acquired parseable full text", result=paper.title, paper_id=paper.paper_id, access_method=record.access_method)
            elif record.fulltext_status == "background_page":
                background_cached.append({
                    **asdict(record),
                    "matched_features": item.get("matched_features", []),
                    "retrieval_roles": item.get("retrieval_roles", []),
                    "evidence_roles": item.get("evidence_roles", []),
                })
                failed.append({**item, "reason": record.error or "Public page was cached as background material but does not look like article full text.", "fulltext_record": asdict(record)})
                self._emit("fulltext_acquisition", "Cached as background web page but not accepted as full text", result=paper.title, reason=record.error, paper_id=paper.paper_id, access_method=record.access_method)
            else:
                failed.append({**item, "reason": record.error or "Could not automatically acquire legal full text.", "fulltext_record": asdict(record)})
        return {
            "fulltexts_reused_from_cache": reused,
            "new_fulltexts_downloaded": downloaded,
            "background_pages_cached": background_cached,
            "deferred_fulltext_acquisition": deferred,
            "failed_fulltext_acquisition": failed,
            "identity_aliases_collapsed": identity_aliases_collapsed,
        }

    @staticmethod
    def is_recent_fulltext_attempt(record: FulltextRecord, *, ttl_days: int = 7) -> bool:
        raw = str(record.downloaded_at or "").strip()
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() < ttl_days * 86400
        except Exception:
            return False

    def acquire_one_fulltext(self, paper: AbstractPaperRecord, selected_item: dict[str, Any]) -> FulltextRecord:
        urls = self.fulltext_candidate_urls(paper)
        self._emit(
            "fulltext_candidates",
            "Generate academic fulltext candidates",
            result=f"{len(urls)} candidates",
            paper_id=paper.paper_id,
            urls=[{**item, "url": self.redact_sensitive_url(item.get("url", ""))} for item in urls[:8]],
        )
        last_error = ""
        best_background: FulltextRecord | None = None
        for url_item in urls:
            url = url_item["url"]
            method = url_item["method"]
            kind = url_item.get("kind", "")
            try:
                self._emit("fulltext_try", "Try fulltext candidate", paper_id=paper.paper_id, method=method, kind=kind, url=self.redact_sensitive_url(url))
                if kind == "pmc_idconv":
                    record = self._resolve_pmc_idconv_and_download(paper, url, method)
                elif kind == "jats_xml":
                    record = self._download_xml_and_parse(paper, url, method)
                elif kind == "tei_xml":
                    record = self._download_tei_xml_and_parse(paper, url, method)
                elif kind == "publisher_html":
                    record = self._download_publisher_html_and_parse(paper, url, method)
                elif kind == "pdf" or looks_like_pdf_url(url):
                    record = self._download_pdf_and_parse(paper, url, method)
                else:
                    record = self._fetch_html_markdown(paper, url, method)
                if record.fulltext_status == "available":
                    record.used_for_queries = paper.query_used
                    return record
                if record.fulltext_status == "background_page" and best_background is None:
                    best_background = record
                last_error = record.error
            except Exception as exc:
                last_error = f"{method}: {type(exc).__name__}"
        scansci_record = self.try_scansci_legal_backup(paper)
        if scansci_record is not None:
            if scansci_record.fulltext_status == "available":
                scansci_record.used_for_queries = paper.query_used
                return scansci_record
            if scansci_record.fulltext_status == "background_page" and best_background is None:
                best_background = scansci_record
            if scansci_record.error:
                last_error = (last_error + " | " if last_error else "") + scansci_record.error
        if best_background is not None:
            best_background.used_for_queries = paper.query_used
            return best_background
        if self.institution_access.get("configured") and (paper.doi or paper.landing_page_url):
            last_error = (last_error + " | " if last_error else "") + "institution_browser_login_required"
        return FulltextRecord(
            paper_id=paper.paper_id,
            doi=paper.doi,
            title=paper.title,
            fulltext_status="unavailable",
            source_url=self.redact_sensitive_url(urls[0]["url"]) if urls else paper.landing_page_url,
            access_method=urls[0]["method"] if urls else "no_url",
            error=last_error or "no academic XML/HTML/PDF fulltext candidate succeeded",
        )

    def try_scansci_legal_backup(self, paper: AbstractPaperRecord) -> FulltextRecord | None:
        if not self.enable_scansci_legal_backup:
            return None
        if not paper.doi or not self.paper_has_open_access_hint(paper):
            return None
        pdf_dir = self.fulltext_root / "pdfs"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        try:
            from tools.academic_backends.scansci_legal_backend import ScanSciLegalBackend

            self._emit(
                "fulltext_try",
                "Try ScanSci legal OA PDF backup",
                paper_id=paper.paper_id,
                doi=paper.doi,
                timeout_seconds=self.scansci_timeout_seconds,
            )
            result = ScanSciLegalBackend().download_pdf(
                paper.doi,
                pdf_dir,
                use_institution=False,
                timeout_seconds=self.scansci_timeout_seconds,
            )
            if not result.get("success") or not result.get("file"):
                return FulltextRecord(
                    paper_id=paper.paper_id,
                    doi=paper.doi,
                    title=paper.title,
                    fulltext_status="unavailable",
                    fulltext_type="pdf",
                    access_method="scansci_legal_backup",
                    error=f"scansci_legal_backup_failed:{result.get('error') or result.get('reason') or 'no_pdf'}",
                )
            source = str(result.get("source") or "ScanSciLegal")
            pdf_path = Path(result["file"])
            raw = pdf_path.read_bytes()
            text, parser_name, structured_path = self._parse_pdf_file_structured(pdf_path, raw)
            if len(text.strip()) < 500:
                return FulltextRecord(
                    paper_id=paper.paper_id,
                    doi=paper.doi,
                    title=paper.title,
                    fulltext_status="unavailable",
                    fulltext_type=f"pdf_{parser_name}",
                    local_file_path=str(pdf_path),
                    source_url=source,
                    access_method=f"scansci_legal_backup:{source}+{parser_name}",
                    error="scansci PDF parsed text too short",
                )
            name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
            parsed_path = parsed_dir / f"{name}.scansci_legal.txt"
            parsed_path.write_text(f"Parser: {parser_name}\nStructured source: {structured_path}\nScanSci legal source: {source}\n\n{text}", encoding="utf-8")
            chunk_path = chunks_dir / f"{name}.scansci_legal.jsonl"
            self.write_chunks(chunk_path, paper, text)
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            return FulltextRecord(
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                fulltext_status=status,
                fulltext_type=f"pdf_{parser_name}",
                local_file_path=str(pdf_path),
                parsed_text_path=str(parsed_path),
                chunk_index_path=str(chunk_path),
                source_url=source,
                access_method=f"scansci_legal_backup:{source}+{parser_name}",
                downloaded_at=utc_now(),
                error="" if status == "available" else "source_audit_background_only",
            )
        except Exception as exc:
            return FulltextRecord(
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                fulltext_status="unavailable",
                fulltext_type="pdf",
                access_method="scansci_legal_backup",
                error=f"scansci_legal_backup_error:{type(exc).__name__}",
            )

    def fulltext_candidate_urls(self, paper: AbstractPaperRecord) -> list[dict[str, Any]]:
        """Return academic fulltext candidates in quality order.

        JATS/PMC XML and publisher HTML are tried before PDFs; page-reader
        scraping through Jina/Firecrawl is kept as the final fallback only.
        """
        urls: list[dict[str, Any]] = []

        def add(url: str, method: str, *, kind: str, source: str = "", quality_hint: str = "") -> None:
            url = str(url or "").strip()
            if not url or not re.match(r"^https?://", url, re.I):
                return
            if not any(existing["url"] == url and existing["kind"] == kind for existing in urls):
                urls.append(
                    {
                        "url": url,
                        "method": method,
                        "kind": kind,
                        "source": source or method,
                        "quality_hint": quality_hint or kind,
                    }
                )

        def add_html(url: str, method: str, source: str) -> None:
            if url and not looks_like_pdf_url(str(url)):
                add(url, method, kind="publisher_html", source=source, quality_hint="publisher_or_repository_html")

        def add_pdf(url: str, method: str, source: str) -> None:
            if url:
                add(url, method, kind="pdf", source=source, quality_hint="oa_pdf_structured_parse")

        raw = paper.raw or {}
        raw_meta = raw.get("raw_metadata") if isinstance(raw.get("raw_metadata"), dict) else {}

        def first_openalex_key() -> str:
            for env_name in ("OPENALEX_API_KEY", "OPENALEX_API_KEYS"):
                value = os.environ.get(env_name, "")
                if value.strip():
                    return re.split(r"[\s,;]+", value.strip())[0]
            try:
                for key_file in (DEFAULT_OPENALEX_KEYS_FILE, LEGACY_OPENALEX_KEYS_FILE):
                    if key_file.exists():
                        for line in key_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                            value = line.strip()
                            if value:
                                return value
            except Exception:
                pass
            return ""

        def openalex_work_id() -> str:
            value = str(paper.openalex_id or raw.get("openalex_id") or raw_meta.get("openalex_id") or "").strip()
            if value.startswith("https://openalex.org/"):
                value = value.rsplit("/", 1)[-1]
            if re.match(r"^W\d+$", value, re.I):
                return value.upper()
            source_id = str(raw.get("source_id") or paper.paper_id or "")
            match = re.search(r"\bW\d+\b", source_id, flags=re.I)
            return match.group(0).upper() if match else ""

        def is_open_access_hint() -> bool:
            return self.paper_has_open_access_hint(paper)

        for pmcid in self.pmcid_candidates(paper):
            add(
                f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/?report=xml",
                "pmc_jats_xml",
                kind="jats_xml",
                source="pmc",
                quality_hint="highest_quality_structured_xml",
            )
        if paper.doi:
            add(
                f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={urllib.parse.quote(paper.doi)}&format=json",
                "pmc_idconv",
                kind="pmc_idconv",
                source="pmc",
                quality_hint="resolve_pmc_xml_if_available",
            )

        best_oa = raw_meta.get("best_oa_location") if isinstance(raw_meta.get("best_oa_location"), dict) else {}
        content_urls = raw.get("content_urls") if isinstance(raw.get("content_urls"), dict) else {}
        if not content_urls:
            content_urls = raw_meta.get("content_urls") if isinstance(raw_meta.get("content_urls"), dict) else {}
        # Prefer the representation that preserves document structure.  A TEI
        # document is substantially easier to normalize, chunk, and trace than
        # a PDF; PDF remains an important legal fallback, not the first choice.
        if content_urls.get("grobid_xml"):
            add(content_urls.get("grobid_xml"), "openalex_content_grobid_xml", kind="tei_xml", source="openalex_content", quality_hint="structured_tei_xml")
        add_pdf(content_urls.get("pdf"), "openalex_content_pdf", "openalex_content")
        has_content = raw.get("has_content") if isinstance(raw.get("has_content"), dict) else {}
        if not has_content:
            has_content = raw_meta.get("has_content") if isinstance(raw_meta.get("has_content"), dict) else {}
        work_id = openalex_work_id()
        openalex_key = first_openalex_key() if work_id and (has_content.get("pdf") or has_content.get("grobid_xml") or is_open_access_hint()) else ""
        if openalex_key and (has_content.get("pdf") or is_open_access_hint()):
            # Keep credentials out of candidate URLs, artifacts, and logs.
            # _download_pdf_and_parse recognises this host and delegates to the
            # key-rotating OpenAlex client at request time.
            add_pdf(
                f"https://content.openalex.org/works/{work_id}.pdf",
                "openalex_content_pdf",
                "openalex_content",
            )
        add_html(best_oa.get("url"), "openalex_best_oa_html", "openalex")
        for loc in (raw_meta.get("oa_locations") or [])[:4] if isinstance(raw_meta.get("oa_locations"), list) else []:
            if isinstance(loc, dict):
                add_html(loc.get("url"), "openalex_oa_location_html", "openalex")
        add_html(raw.get("source_url") if "download" not in str(raw.get("source_url", "")).lower() else "", "source_landing_html", "source_api")
        add_html(raw.get("url_or_doi") if not looks_like_pdf_url(str(raw.get("url_or_doi", ""))) else "", "url_or_doi_html", "source_api")
        add_html(paper.landing_page_url, "record_landing_html", "record")
        if paper.doi:
            add_html(f"https://doi.org/{paper.doi}", "doi_publisher_html", "doi")

        add_pdf(paper.pdf_url, "record_pdf_url", "record")
        add_pdf(best_oa.get("pdf_url") or best_oa.get("url_for_pdf"), "openalex_best_oa_pdf", "openalex")
        for loc in (raw_meta.get("oa_locations") or [])[:4] if isinstance(raw_meta.get("oa_locations"), list) else []:
            if isinstance(loc, dict):
                add_pdf(loc.get("pdf_url") or loc.get("url_for_pdf") or (loc.get("url") if looks_like_pdf_url(str(loc.get("url", ""))) else ""), "openalex_oa_location_pdf", "openalex")
        open_pdf = raw_meta.get("open_access_pdf") if isinstance(raw_meta.get("open_access_pdf"), dict) else {}
        add_pdf(open_pdf.get("url") or open_pdf.get("url_for_pdf"), "semantic_scholar_open_access_pdf", "semantic_scholar")
        add_pdf(raw.get("source_url") if "download" in str(raw.get("source_url", "")).lower() else "", "source_download_url", "source_api")
        add_pdf(raw.get("url_or_doi") if looks_like_pdf_url(str(raw.get("url_or_doi", ""))) else "", "url_or_doi_pdf", "source_api")
        if paper.doi:
            oa = self.lookup_unpaywall(paper.doi)
            add_html(oa.get("best_oa_url", ""), "unpaywall_best_oa_html", "unpaywall")
            for loc in oa.get("oa_locations", [])[:3]:
                add_pdf(loc.get("url_for_pdf") or (loc.get("url") if looks_like_pdf_url(str(loc.get("url", ""))) else ""), "unpaywall_oa_location_pdf", "unpaywall")
                add_html(loc.get("url"), "unpaywall_oa_location_html", "unpaywall")
        arxiv_id = str(raw.get("arxiv_id") or "").strip()
        if arxiv_id:
            add_pdf(f"https://arxiv.org/pdf/{arxiv_id}.pdf", "arxiv_pdf", "arxiv")

        add(paper.landing_page_url, "landing_page_jina_firecrawl", kind="jina_firecrawl_fallback", source="page_reader", quality_hint="last_resort")
        return urls

    @staticmethod
    def redact_sensitive_url(url: str) -> str:
        return re.sub(r"([?&](?:api_key|key|token|insttoken|access_token)=)[^&#]+", r"\1***", str(url or ""), flags=re.I)

    def lookup_unpaywall(self, doi: str) -> dict[str, Any]:
        try:
            from tools.academic_backends.unpaywall_backend import UnpaywallBackend
            return UnpaywallBackend().lookup(doi) or {}
        except Exception as exc:
            self.diagnostics.append(f"Unpaywall lookup failed: {type(exc).__name__}")
            return {}

    @staticmethod
    def _recursive_values_by_key(obj: Any, keys: set[str]) -> list[str]:
        values: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).casefold() in keys:
                    if isinstance(value, (str, int, float)):
                        values.append(str(value))
                    elif isinstance(value, list):
                        values.extend(str(item) for item in value if isinstance(item, (str, int, float)))
                values.extend(LiteratureResourceBuilder._recursive_values_by_key(value, keys))
        elif isinstance(obj, list):
            for item in obj:
                values.extend(LiteratureResourceBuilder._recursive_values_by_key(item, keys))
        return values

    def pmcid_candidates(self, paper: AbstractPaperRecord) -> list[str]:
        raw_blob = json.dumps(paper.raw or {}, ensure_ascii=False, default=str) + " " + str(paper.landing_page_url or "")
        candidates = self._recursive_values_by_key(paper.raw or {}, {"pmcid", "pmc", "pmc_id"})
        candidates.extend(re.findall(r"\bPMC\d+\b", raw_blob, flags=re.I))
        out: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            match = re.search(r"PMC\s*\d+", str(value), flags=re.I)
            if not match:
                continue
            pmcid = re.sub(r"\s+", "", match.group(0).upper())
            if pmcid not in seen:
                seen.add(pmcid)
                out.append(pmcid)
        return out[:3]

    def _resolve_pmc_idconv_and_download(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OptoMindLiteratureResourceBuilder/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            records = payload.get("records") if isinstance(payload, dict) else []
            for item in records or []:
                pmcid = str(item.get("pmcid") or "").strip()
                if re.match(r"^PMC\d+$", pmcid, re.I):
                    xml_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid.upper()}/?report=xml"
                    return self._download_xml_and_parse(paper, xml_url, f"{method}->{pmcid.upper()}")
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="pmc_idconv", source_url=url, access_method=method, error="PMC idconv returned no PMCID")
        except Exception as exc:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="pmc_idconv", source_url=url, access_method=method, error=f"PMC idconv failed: {type(exc).__name__}")

    def _download_xml_and_parse(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        xml_dir = self.fulltext_root / "xml"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        xml_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OptoMindLiteratureResourceBuilder/1.0", "Accept": "application/xml,text/xml,*/*"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read(15_000_000)
            xml_text = raw.decode("utf-8", errors="replace").strip()
            if "<" not in xml_text[:200] or len(xml_text) < 500:
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="jats_xml", source_url=url, access_method=method, error="XML endpoint did not return article XML")
            parsed = self.parse_jats_xml_to_markdown(xml_text, fallback_title=paper.title)
            if len(parsed.strip()) < 1200:
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="jats_xml", source_url=url, access_method=method, error="JATS XML parsed text too short")
            xml_path = xml_dir / f"{name}.jats.xml"
            xml_path.write_text(xml_text, encoding="utf-8")
            parsed_path = parsed_dir / f"{name}.jats.md"
            parsed_path.write_text(parsed, encoding="utf-8")
            chunk_path = chunks_dir / f"{name}.jats.jsonl"
            self.write_chunks(chunk_path, paper, parsed)
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status=status, fulltext_type="jats_xml", local_file_path=str(xml_path), parsed_text_path=str(parsed_path), chunk_index_path=str(chunk_path), source_url=url, access_method=method, downloaded_at=utc_now(), error="" if status == "available" else "source_audit_background_only")
        except Exception as exc:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="jats_xml", source_url=url, access_method=method, error=f"JATS XML download/parse failed: {type(exc).__name__}")

    def _download_tei_xml_and_parse(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        xml_dir = self.fulltext_root / "xml"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        xml_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
        try:
            headers = {"User-Agent": "OptoMindLiteratureResourceBuilder/1.0", "Accept": "application/xml,text/xml,*/*"}
            from tools.academic_backends.openalex_content import fetch_openalex_content, is_openalex_content_url

            if is_openalex_content_url(url):
                raw, fetch_error = fetch_openalex_content(url, timeout=60, headers=headers)
                if raw is None:
                    return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="tei_xml", source_url=self.redact_sensitive_url(url), access_method=method, error=fetch_error or "OpenAlex TEI content fetch failed")
            else:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read(15_000_000)
            xml_text = raw.decode("utf-8", errors="replace").strip()
            if "<" not in xml_text[:200] or len(xml_text) < 500:
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="tei_xml", source_url=self.redact_sensitive_url(url), access_method=method, error="TEI XML endpoint did not return article XML")
            parsed = self.parse_tei_xml_to_markdown(xml_text, fallback_title=paper.title)
            if len(parsed.strip()) < 1200:
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="tei_xml", source_url=self.redact_sensitive_url(url), access_method=method, error="TEI XML parsed text too short")
            xml_path = xml_dir / f"{name}.tei.xml"
            xml_path.write_text(xml_text, encoding="utf-8")
            parsed_path = parsed_dir / f"{name}.tei.md"
            parsed_path.write_text(parsed, encoding="utf-8")
            chunk_path = chunks_dir / f"{name}.tei.jsonl"
            self.write_chunks(chunk_path, paper, parsed)
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status=status, fulltext_type="tei_xml", local_file_path=str(xml_path), parsed_text_path=str(parsed_path), chunk_index_path=str(chunk_path), source_url=self.redact_sensitive_url(url), access_method=method, downloaded_at=utc_now(), error="" if status == "available" else "source_audit_background_only")
        except Exception as exc:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="tei_xml", source_url=self.redact_sensitive_url(url), access_method=method, error=f"TEI XML download/parse failed: {type(exc).__name__}")

    @staticmethod
    def _xml_local_name(tag: str) -> str:
        return str(tag or "").split("}", 1)[-1].casefold()

    @staticmethod
    def _element_text(element: Any) -> str:
        return normalize_space(" ".join(part.strip() for part in element.itertext() if str(part).strip()))

    def parse_jats_xml_to_markdown(self, xml_text: str, *, fallback_title: str = "") -> str:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text.encode("utf-8"))
        parts: list[str] = []
        article_titles = [self._element_text(el) for el in root.iter() if self._xml_local_name(el.tag) == "article-title"]
        title = next((t for t in article_titles if t), fallback_title)
        if title:
            parts.append(f"# {title}")
        for abstract in [el for el in root.iter() if self._xml_local_name(el.tag) == "abstract"][:2]:
            text = self._element_text(abstract)
            if text:
                parts.append("## Abstract\n\n" + text)
        body_nodes = [el for el in root.iter() if self._xml_local_name(el.tag) == "body"]
        body = body_nodes[0] if body_nodes else root
        for sec in [el for el in body.iter() if self._xml_local_name(el.tag) == "sec"]:
            heading = ""
            paras: list[str] = []
            for child in list(sec):
                local = self._xml_local_name(child.tag)
                if local == "title" and not heading:
                    heading = self._element_text(child)
                elif local in {"p", "list", "fig", "table-wrap"}:
                    text = self._element_text(child)
                    if text and len(text) > 20:
                        paras.append(text)
            if heading or paras:
                parts.append(f"## {heading or 'Section'}\n\n" + "\n\n".join(paras[:80]))
        refs = []
        for ref in [el for el in root.iter() if self._xml_local_name(el.tag) == "ref"][:120]:
            text = self._element_text(ref)
            if text:
                refs.append(text)
        if refs:
            parts.append("## References\n\n" + "\n".join(f"- {ref}" for ref in refs))
        return "\n\n".join(part for part in parts if part.strip())

    def _download_publisher_html_and_parse(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        html_dir = self.fulltext_root / "html"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        html_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; OptoMindLiteratureResourceBuilder/1.0)", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read(12_000_000)
                final_url = resp.geturl()
            if "pdf" in content_type.lower() or raw[:4] == b"%PDF":
                return self._download_pdf_and_parse(paper, final_url or url, f"{method}->redirected_pdf")
            html_text = raw.decode("utf-8", errors="replace")
            markdown = self.html_to_structured_markdown(html_text, source_url=final_url or url)
            audit = self.audit_scraped_page_value(paper, final_url or url, markdown)
            if not self._looks_like_article_fulltext(markdown):
                if self.institution_access_enabled() and self.paper_likely_needs_institution(paper):
                    inst_record = self._download_html_via_institution_session(paper, final_url or url, method, name)
                    if inst_record.fulltext_status == "available":
                        return inst_record
                status = "background_page" if audit.get("worth_caching") else "unavailable"
                error = f"publisher_html_not_fulltext: {audit.get('page_type')} | {audit.get('reason', '')}"
                if status == "unavailable" and self.institution_access.get("configured"):
                    error += " | institution_browser_login_required"
                if status == "unavailable":
                    return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="publisher_html", source_url=final_url or url, access_method=method, error=error)
                suffix = ".publisher.background.md"
                chunk_suffix = ".publisher.background.jsonl"
            else:
                status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
                suffix = ".publisher.md" if status == "available" else ".publisher.background.md"
                chunk_suffix = ".publisher.jsonl" if status == "available" else ".publisher.background.jsonl"
                error = "" if status == "available" else "source_audit_background_only"
            html_path = html_dir / f"{name}.html"
            html_path.write_text(html_text, encoding="utf-8")
            parsed_path = parsed_dir / f"{name}{suffix}"
            parsed_path.write_text(markdown, encoding="utf-8")
            chunk_path = chunks_dir / f"{name}{chunk_suffix}"
            self.write_chunks(chunk_path, paper, markdown)
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status=status, fulltext_type="publisher_html", local_file_path=str(html_path), parsed_text_path=str(parsed_path), chunk_index_path=str(chunk_path), source_url=final_url or url, access_method=method, downloaded_at=utc_now(), error=error)
        except Exception as exc:
            if self.institution_access_enabled() and self.paper_likely_needs_institution(paper):
                inst_record = self._download_html_via_institution_session(paper, url, method, name)
                if inst_record.fulltext_status != "unavailable":
                    return inst_record
            suffix = " | institution_browser_login_required" if self.institution_access.get("configured") else ""
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="publisher_html", source_url=url, access_method=method, error=f"publisher HTML download/parse failed: {type(exc).__name__}{suffix}")

    @staticmethod
    def html_to_structured_markdown(html_text: str, *, source_url: str = "") -> str:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html_text or "", "html.parser")
            for bad in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                bad.decompose()
            root = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
            parts: list[str] = []
            if source_url:
                parts.append(f"Source URL: {source_url}")
            for node in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "figcaption", "caption"], recursive=True):
                text = normalize_space(node.get_text(" ", strip=True))
                if len(text) < 20 and node.name not in {"h1", "h2", "h3", "h4"}:
                    continue
                if node.name in {"h1", "h2", "h3", "h4"}:
                    level = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}[node.name]
                    parts.append(f"{level} {text}")
                elif node.name == "li":
                    parts.append(f"- {text}")
                else:
                    parts.append(text)
            return "\n\n".join(parts)
        except Exception:
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_text or "")
            text = re.sub(r"(?i)</(h1|h2|h3|h4)>", "\n\n", text)
            text = re.sub(r"(?i)<(h1|h2|h3|h4)[^>]*>", "\n\n## ", text)
            text = re.sub(r"(?i)</p>", "\n\n", text)
            text = re.sub(r"<[^>]+>", " ", text)
            return normalize_space(text)

    def _parse_pdf_file_structured(self, pdf_path: Path, raw: bytes) -> tuple[str, str, str]:
        structured_dir = self.fulltext_root / "structured"
        structured_dir.mkdir(parents=True, exist_ok=True)
        try:
            from tools.academic_backends.grobid_backend import GrobidBackend

            result = GrobidBackend().parse_pdf(str(pdf_path))
            tei_xml = (result or {}).get("tei_xml") if isinstance(result, dict) else ""
            if tei_xml:
                tei_path = structured_dir / f"{pdf_path.stem}.tei.xml"
                tei_path.write_text(tei_xml, encoding="utf-8")
                text = self.parse_tei_xml_to_markdown(tei_xml, fallback_title=pdf_path.stem)
                if len(text.strip()) >= 1000:
                    return text, "grobid", str(tei_path)
        except Exception as exc:
            self.diagnostics.append(f"grobid_parse_unavailable:{type(exc).__name__}")
        # Docling/Marker are valuable for offline high-fidelity PDF conversion, but on
        # Windows they can load torch/RapidOCR native libraries. In full-scale
        # literature acquisition, one native crash must not kill the entire run.
        # Keep the online pipeline robust by default; enable heavy parsers only for
        # isolated/offline reprocessing when explicitly requested.
        if os.getenv("OPTOMIND_ENABLE_HEAVY_PDF_PARSERS", "").strip().lower() not in {"1", "true", "yes", "on"}:
            self.diagnostics.append("heavy_pdf_parsers_skipped:default_safe_mode")
            return self._parse_pdf_bytes(raw), "pymupdf", ""
        try:
            from tools.academic_backends.docling_backend import DoclingBackend

            result = DoclingBackend().parse(str(pdf_path))
            if result:
                text = str(result.get("markdown") or result.get("text") or "")
                if len(text.strip()) >= 1000:
                    out_path = structured_dir / f"{pdf_path.stem}.docling.md"
                    out_path.write_text(text, encoding="utf-8")
                    return text, "docling", str(out_path)
        except Exception as exc:
            self.diagnostics.append(f"docling_parse_unavailable:{type(exc).__name__}")
        try:
            from tools.academic_backends.marker_backend import MarkerBackend

            result = MarkerBackend().parse(str(pdf_path), output_dir=str(structured_dir))
            if result:
                text = str(result.get("text") or result.get("markdown") or "")
                if len(text.strip()) >= 1000:
                    out_path = structured_dir / f"{pdf_path.stem}.marker.md"
                    out_path.write_text(text, encoding="utf-8")
                    return text, "marker", str(out_path)
        except Exception as exc:
            self.diagnostics.append(f"marker_parse_unavailable:{type(exc).__name__}")
        return self._parse_pdf_bytes(raw), "pymupdf", ""

    def parse_tei_xml_to_markdown(self, xml_text: str, *, fallback_title: str = "") -> str:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text.encode("utf-8"))
        parts: list[str] = []
        titles = [self._element_text(el) for el in root.iter() if self._xml_local_name(el.tag) == "title"]
        title = next((t for t in titles if t), fallback_title)
        if title:
            parts.append(f"# {title}")
        for div in [el for el in root.iter() if self._xml_local_name(el.tag) == "div"]:
            heading = ""
            paras: list[str] = []
            for child in list(div):
                local = self._xml_local_name(child.tag)
                if local == "head" and not heading:
                    heading = self._element_text(child)
                elif local in {"p", "figure"}:
                    text = self._element_text(child)
                    if text and len(text) > 20:
                        paras.append(text)
            if heading or paras:
                parts.append(f"## {heading or 'Section'}\n\n" + "\n\n".join(paras[:80]))
        refs = []
        for bibl in [el for el in root.iter() if self._xml_local_name(el.tag) in {"biblstruct", "bibl"}][:120]:
            text = self._element_text(bibl)
            if text:
                refs.append(text)
        if refs:
            parts.append("## References\n\n" + "\n".join(f"- {ref}" for ref in refs))
        return "\n\n".join(part for part in parts if part.strip())

    def institution_access_enabled(self) -> bool:
        return bool(
            self.enable_institutional_access
            and self.institution_backend is not None
            and getattr(self.institution_backend, "enabled", False)
        )

    def _download_pdf_via_institution_session(self, paper: AbstractPaperRecord, url: str, method: str, name: str) -> FulltextRecord:
        if not self.institution_access_enabled():
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_pdf", source_url=url, access_method=f"{method}+institution_disabled", error="institutional access disabled")
        pdf_dir = self.fulltext_root / "pdfs"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        result = self.institution_backend.fetch_url(url, output_dir=pdf_dir, filename_stem=f"{name}.institution", expect="pdf")
        if not result.ok or not result.local_file_path:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_pdf", source_url=url, access_method=f"{method}+institution_playwright", error=result.error or "institution session did not return PDF")
        pdf_path = Path(result.local_file_path)
        try:
            raw = pdf_path.read_bytes()
            text, parser_name, structured_path = self._parse_pdf_file_structured(pdf_path, raw)
            if len(text.strip()) < 500:
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type=f"pdf_{parser_name}", local_file_path=str(pdf_path), source_url=result.final_url or url, access_method=f"{method}+institution_playwright+{parser_name}", error="institution PDF parsed text too short")
            parsed_path = parsed_dir / f"{name}.institution.txt"
            parsed_path.write_text(f"Parser: {parser_name}\nStructured source: {structured_path}\nInstitution source: {result.final_url or url}\n\n{text}", encoding="utf-8")
            chunk_path = chunks_dir / f"{name}.institution.jsonl"
            self.write_chunks(chunk_path, paper, text)
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status=status, fulltext_type=f"pdf_{parser_name}", local_file_path=str(pdf_path), parsed_text_path=str(parsed_path), chunk_index_path=str(chunk_path), source_url=result.final_url or url, access_method=f"{method}+institution_playwright+{parser_name}", downloaded_at=utc_now(), error="" if status == "available" else "source_audit_background_only")
        except Exception as exc:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_pdf", local_file_path=str(pdf_path), source_url=result.final_url or url, access_method=f"{method}+institution_playwright", error=f"institution PDF parse failed: {type(exc).__name__}")

    def _download_html_via_institution_session(self, paper: AbstractPaperRecord, url: str, method: str, name: str) -> FulltextRecord:
        if not self.institution_access_enabled():
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_html", source_url=url, access_method=f"{method}+institution_disabled", error="institutional access disabled")
        html_dir = self.fulltext_root / "html"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        html_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        result = self.institution_backend.fetch_url(url, output_dir=html_dir, filename_stem=f"{name}.institution", expect="auto")
        if not result.ok:
            return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_html", source_url=url, access_method=f"{method}+institution_playwright", error=result.error or "institution session fetch failed")
        if result.local_file_path and result.local_file_path.lower().endswith(".pdf"):
            return self._download_pdf_via_institution_session(paper, result.final_url or url, method, name)
        html_text = result.text
        if not html_text and result.local_file_path and Path(result.local_file_path).exists():
            html_text = Path(result.local_file_path).read_text(encoding="utf-8", errors="replace")
        markdown = self.html_to_structured_markdown(html_text, source_url=result.final_url or url)
        audit = self.audit_scraped_page_value(paper, result.final_url or url, markdown)
        if not self._looks_like_article_fulltext(markdown):
            status = "background_page" if audit.get("worth_caching") else "unavailable"
            if status == "unavailable":
                return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status="unavailable", fulltext_type="institution_html", source_url=result.final_url or url, access_method=f"{method}+institution_playwright", error=f"institution_html_not_fulltext: {audit.get('page_type')} | {audit.get('reason', '')}")
            suffix = ".institution.background.md"
            chunk_suffix = ".institution.background.jsonl"
            error = f"not_fulltext_cached_as_background: {audit.get('reason', '')}"
        else:
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            suffix = ".institution.md" if status == "available" else ".institution.background.md"
            chunk_suffix = ".institution.jsonl" if status == "available" else ".institution.background.jsonl"
            error = "" if status == "available" else "source_audit_background_only"
        parsed_path = parsed_dir / f"{name}{suffix}"
        parsed_path.write_text(markdown, encoding="utf-8")
        chunk_path = chunks_dir / f"{name}{chunk_suffix}"
        self.write_chunks(chunk_path, paper, markdown)
        return FulltextRecord(paper_id=paper.paper_id, doi=paper.doi, title=paper.title, fulltext_status=status, fulltext_type="publisher_html", local_file_path=result.local_file_path, parsed_text_path=str(parsed_path), chunk_index_path=str(chunk_path), source_url=result.final_url or url, access_method=f"{method}+institution_playwright", downloaded_at=utc_now(), error=error)

    def _download_pdf_and_parse(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        pdf_dir = self.fulltext_root / "pdfs"
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
        pdf_path = pdf_dir / f"{name}.pdf"
        req = urllib.request.Request(url, headers={"User-Agent": "OptoMindLiteratureResourceBuilder/1.0"})
        try:
            from tools.academic_backends.openalex_content import fetch_openalex_content, is_openalex_content_url

            if is_openalex_content_url(url):
                raw, fetch_error = fetch_openalex_content(
                    url,
                    timeout=45,
                    headers={"User-Agent": "OptoMindLiteratureResourceBuilder/1.0"},
                )
                if raw is None:
                    return FulltextRecord(
                        paper_id=paper.paper_id,
                        doi=paper.doi,
                        title=paper.title,
                        fulltext_status="unavailable",
                        fulltext_type="pdf",
                        source_url=self.redact_sensitive_url(url),
                        access_method=method,
                        error=fetch_error or "OpenAlex content fetch failed",
                    )
                content_type = "application/pdf" if raw[:4] == b"%PDF" else "application/octet-stream"
            else:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    raw = resp.read(25_000_000)
            if "pdf" not in content_type.lower() and not raw[:4] == b"%PDF":
                if self.institution_access_enabled() and self.paper_likely_needs_institution(paper):
                    inst_record = self._download_pdf_via_institution_session(paper, url, method, name)
                    if inst_record.fulltext_status != "unavailable":
                        return inst_record
                return FulltextRecord(
                    paper_id=paper.paper_id,
                    doi=paper.doi,
                    title=paper.title,
                    fulltext_status="unavailable",
                    source_url=self.redact_sensitive_url(url),
                    access_method=method,
                    error=f"URL did not return a PDF, content_type={content_type}",
                )
            pdf_path.write_bytes(raw)
            text, parser_name, structured_path = self._parse_pdf_file_structured(pdf_path, raw)
            if len(text.strip()) < 500:
                return FulltextRecord(
                    paper_id=paper.paper_id,
                    doi=paper.doi,
                    title=paper.title,
                    fulltext_status="unavailable",
                    fulltext_type="pdf",
                    local_file_path=str(pdf_path),
                    source_url=self.redact_sensitive_url(url),
                    access_method=method,
                    error="PDF downloaded successfully but parsed text is too short",
                )
            parsed_path = parsed_dir / f"{name}.txt"
            parsed_path.write_text(f"Parser: {parser_name}\nStructured source: {structured_path}\n\n{text}", encoding="utf-8")
            chunk_path = chunks_dir / f"{name}.jsonl"
            self.write_chunks(chunk_path, paper, text)
            status = "background_page" if self._should_cache_as_background_due_to_source(paper) else "available"
            return FulltextRecord(
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                fulltext_status=status,
                fulltext_type=f"pdf_{parser_name}",
                local_file_path=str(pdf_path),
                parsed_text_path=str(parsed_path),
                chunk_index_path=str(chunk_path),
                source_url=self.redact_sensitive_url(url),
                access_method=f"{method}+{parser_name}",
                downloaded_at=utc_now(),
                error="" if status == "available" else "source_audit_background_only: parsed PDF kept as background material, not scholarly fulltext",
            )
        except Exception as exc:
            if self.institution_access_enabled() and self.paper_likely_needs_institution(paper):
                inst_record = self._download_pdf_via_institution_session(paper, url, method, name)
                if inst_record.fulltext_status != "unavailable":
                    return inst_record
            return FulltextRecord(
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                fulltext_status="unavailable",
                fulltext_type="pdf",
                source_url=self.redact_sensitive_url(url),
                access_method=method,
                error=f"PDF download/parse failed: {type(exc).__name__}",
            )

    @staticmethod
    def _parse_pdf_bytes(raw: bytes) -> str:
        import fitz
        doc = fitz.open(stream=raw, filetype="pdf")
        return "\n\n".join(f"# Page {i + 1}\n\n{page.get_text('text')}" for i, page in enumerate(doc))

    def _fetch_html_markdown(self, paper: AbstractPaperRecord, url: str, method: str) -> FulltextRecord:
        parsed_dir = self.fulltext_root / "parsed_text"
        chunks_dir = self.fulltext_root / "chunks"
        parsed_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)

        markdown = self.engine.fetch_fulltext(url, method="jina")
        access_method = f"{method}+jina"
        if len(markdown.strip()) < 800 and self.engine.firecrawl_key:
            alt = self.engine.fetch_fulltext(url, method="firecrawl")
            if len(alt) > len(markdown):
                markdown = alt
                access_method = f"{method}+firecrawl"

        if not markdown.strip():
            return FulltextRecord(
                paper_id=paper.paper_id,
                doi=paper.doi,
                title=paper.title,
                fulltext_status="unavailable",
                fulltext_type="html",
                source_url=url,
                access_method=access_method,
                error=(getattr(self.engine, "last_fulltext_errors", {}) or {}).get(url, "Jina/Firecrawl returned empty content"),
            )

        for linked_pdf in self._extract_pdf_links(markdown, base_url=url)[:3]:
            if linked_pdf == url:
                continue
            pdf_record = self._download_pdf_and_parse(paper, linked_pdf, f"{access_method}->linked_pdf")
            if pdf_record.fulltext_status == "available":
                return pdf_record

        page_audit = {"page_type": "fulltext", "worth_caching": True, "cache_as": "fulltext", "reason": "deterministic fulltext check passed"}
        if not self._looks_like_article_fulltext(markdown):
            page_audit = self.audit_scraped_page_value(paper, url, markdown)
            if not page_audit.get("worth_caching"):
                return FulltextRecord(
                    paper_id=paper.paper_id,
                    doi=paper.doi,
                    title=paper.title,
                    fulltext_status="unavailable",
                    fulltext_type="html",
                    source_url=url,
                    access_method=access_method,
                    error=f"not_article_fulltext: {page_audit.get('page_type', 'unknown')} | {page_audit.get('reason', '')}",
                )
            status = "background_page" if page_audit.get("cache_as") != "fulltext" else "available"
            suffix = ".background.md" if status == "background_page" else ".md"
            chunk_suffix = ".background.jsonl" if status == "background_page" else ".jsonl"
        else:
            status = "available"
            suffix = ".md"
            chunk_suffix = ".jsonl"
        if status == "available" and self._should_cache_as_background_due_to_source(paper):
            status = "background_page"
            suffix = ".background.md"
            chunk_suffix = ".background.jsonl"
            page_audit = {
                **page_audit,
                "cache_as": "background",
                "reason": "source credibility audit says this page is background material rather than scholarly fulltext",
            }

        name = safe_filename(f"{paper.paper_id}-{paper.title[:60]}", paper.paper_id)
        parsed_path = parsed_dir / f"{name}{suffix}"
        parsed_path.write_text(markdown, encoding="utf-8")
        chunk_path = chunks_dir / f"{name}{chunk_suffix}"
        self.write_chunks(chunk_path, paper, markdown)
        return FulltextRecord(
            paper_id=paper.paper_id,
            doi=paper.doi,
            title=paper.title,
            fulltext_status=status,
            fulltext_type="html_markdown",
            parsed_text_path=str(parsed_path),
            chunk_index_path=str(chunk_path),
            source_url=url,
            access_method=access_method,
            downloaded_at=utc_now(),
            error="" if status == "available" else f"not_fulltext_cached_as_{page_audit.get('cache_as', 'background')}: {page_audit.get('reason', '')}",
        )

    def _should_cache_as_background_due_to_source(self, paper: AbstractPaperRecord) -> bool:
        audit = self.source_audit_for_paper(paper)
        use_policy = str(audit.get("use_policy") or "")
        source_type = str(audit.get("source_type") or "")
        if use_policy in {"background_only", "manual_review", "exclude"}:
            return True
        if source_type in {"news_or_commentary", "unknown"}:
            return True
        if not paper.doi and source_type in {"publisher_page", "repository_record"}:
            return True
        return False

    def audit_scraped_page_value(self, paper: AbstractPaperRecord, url: str, markdown: str) -> dict[str, Any]:
        fallback = self._audit_scraped_page_value_deterministic(paper, url, markdown)
        if not self.real_llm:
            return fallback
        system = read_text_file(self.scraped_page_auditor_prompt_path) or (
            "You are a cheap page-cache auditor. "
            "Only decide whether a scraped page should be cached for a literature resource library. "
            "Do not decide whether the scientific claims are true. Return strict JSON only."
        )
        payload = {
            "paper": {
                "paper_id": paper.paper_id,
                "title": paper.title,
                "doi": paper.doi,
                "venue": paper.venue,
                "landing_page_url": paper.landing_page_url,
            },
            "scraped_url": url,
            "markdown_chars": len(markdown or ""),
            "markdown_preview": (markdown or "")[:2500],
            "output_schema": {
                "page_type": "fulltext|abstract_page|landing_page|repository_record|background_page|blocked_or_login|irrelevant",
                "worth_caching": "boolean",
                "cache_as": "fulltext|background|metadata_only|none",
                "manual_download_recommended": "boolean",
                "reason": "short reason",
            },
        }
        try:
            result = call_qwen_chat(
                "ScrapedPageCacheAuditorAgent",
                [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                model_tier=self.audit_model_tier,
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
            if str(result.get("content") or "").startswith(("[fallback]", "[mock]")):
                return fallback
            parsed = parse_json_like(result.get("content", ""), fallback={})
            return self._normalize_page_audit(parsed, fallback)
        except Exception as exc:
            self.diagnostics.append(f"page_cache_audit_failed:{type(exc).__name__}")
            return fallback

    def _audit_scraped_page_value_deterministic(self, paper: AbstractPaperRecord, url: str, markdown: str) -> dict[str, Any]:
        text = str(markdown or "")
        lowered = text.lower()
        if not text.strip():
            return {"page_type": "irrelevant", "worth_caching": False, "cache_as": "none", "manual_download_recommended": True, "reason": "empty scrape"}
        if any(x in lowered for x in ["login", "sign in", "captcha", "access denied", "forbidden"]):
            return {"page_type": "blocked_or_login", "worth_caching": False, "cache_as": "none", "manual_download_recommended": True, "reason": "blocked/login/captcha signal"}
        if self._looks_like_article_fulltext(text):
            return {"page_type": "fulltext", "worth_caching": True, "cache_as": "fulltext", "manual_download_recommended": False, "reason": "article section and length signals"}
        title_hit = paper.title and normalize_space(paper.title).casefold()[:60] in lowered
        has_abstract = "abstract" in lowered or "doi" in lowered or paper.doi and paper.doi.lower() in lowered
        if title_hit or has_abstract:
            return {"page_type": "abstract_page", "worth_caching": True, "cache_as": "background", "manual_download_recommended": True, "reason": "metadata/abstract page related to target paper but not fulltext"}
        if len(text) >= 1500:
            return {"page_type": "background_page", "worth_caching": True, "cache_as": "background", "manual_download_recommended": False, "reason": "non-fulltext page has enough topical background text"}
        return {"page_type": "irrelevant", "worth_caching": False, "cache_as": "none", "manual_download_recommended": True, "reason": "too short and no paper metadata signal"}

    @staticmethod
    def _normalize_page_audit(parsed: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            return fallback
        page_type = str(parsed.get("page_type") or fallback["page_type"])
        if page_type not in {"fulltext", "abstract_page", "landing_page", "repository_record", "background_page", "blocked_or_login", "irrelevant"}:
            page_type = fallback["page_type"]
        cache_as = str(parsed.get("cache_as") or fallback["cache_as"])
        if cache_as not in {"fulltext", "background", "metadata_only", "none"}:
            cache_as = fallback["cache_as"]
        return {
            "page_type": page_type,
            "worth_caching": bool(parsed.get("worth_caching", fallback["worth_caching"])),
            "cache_as": cache_as,
            "manual_download_recommended": bool(parsed.get("manual_download_recommended", fallback["manual_download_recommended"])),
            "reason": normalize_space(str(parsed.get("reason") or fallback.get("reason") or ""))[:500],
        }

    @staticmethod
    def classify_fulltext_failure(reason: str) -> str:
        text = str(reason or "").casefold()
        if any(marker in text for marker in ["captcha", "login", "sign in", "blocked", "cloudflare", "access denied", "forbidden"]):
            return "institution_or_anti_bot_required"
        if "timeout" in text:
            return "timeout_or_network"
        if any(marker in text for marker in ["no pdf", "no academic", "no usable", "no pmcid", "no_url"]):
            return "no_legal_fulltext_url_found"
        if any(marker in text for marker in ["parsed text too short", "too short", "not_fulltext", "metadata", "abstract_page"]):
            return "not_parseable_fulltext"
        if "rate" in text or "429" in text:
            return "rate_limited"
        return "automatic_fulltext_failed"

    def suggested_manual_download_filename(self, paper_id: str, doi: str, title: str) -> str:
        stem = doi or paper_id or title or "manual-fulltext"
        if doi:
            stem = "DOI_" + doi.replace("/", "_").replace(":", "_")
        return safe_filename(stem, "manual-fulltext") + ".pdf"

    def recommend_manual_download_routes(self, paper: AbstractPaperRecord | None, source: dict[str, Any], reason: str) -> list[str]:
        routes: list[str] = []
        doi = (paper.doi if paper else "") or str(source.get("doi") or "")
        landing = (paper.landing_page_url if paper else "") or str(source.get("landing_page_url") or "")
        category = self.classify_fulltext_failure(reason)
        if doi:
            routes.append(f"Open DOI landing page: https://doi.org/{doi}")
        if landing:
            routes.append(f"Open recorded landing page: {landing}")
        if paper and paper.open_access:
            routes.append("Try legal OA route first: publisher OA PDF, repository PDF, Unpaywall/OpenAlex OA link.")
        if (
            self.fulltext_access_policy == "institution_opt_in"
            and (
                category == "institution_or_anti_bot_required"
                or (paper and self.paper_likely_needs_institution(paper))
            )
        ):
            routes.append("Use SCNU library/VPN authenticated browser, search the DOI or title, then save publisher HTML or PDF.")
        routes.append("After download, put the file into user_fulltexts/; keep DOI in the filename when possible.")
        return routes

    @staticmethod
    def _looks_like_article_fulltext(markdown: str) -> bool:
        """Three-tier heuristic to decide if scraped/parsed text is a real article fulltext.

        Tier 0 – fast reject: too short, or obvious blocked-content signals.
        Tier 1 – strict structural (existing): line-start section headers ≥2.
        Tier 2 – academic-signal supplement: any-position section names ≥3 + academic
                  signals ≥2 + length ≥5000. Catches publisher HTML where BS4 strips
                  heading tags and section titles end up as plain paragraphs.
        Tier 3 – length fallback: very long content with at least one anchor keyword.
        """
        text = str(markdown or "")
        lowered = text.lower()
        stripped_len = len(text.strip())

        # Tier 0 – fast reject
        if stripped_len < 3000:
            return False
        blocked_signals = [
            "captcha", "sign in to continue", "access denied",
            "subscribe to read", "institutional access required",
            "this article requires a subscription", "log in to access",
        ]
        if sum(1 for s in blocked_signals if s in lowered) >= 2:
            return False

        section_names = ["abstract", "introduction", "methods", "materials", "results",
                         "discussion", "conclusion", "references"]

        # Tier 1 – strict structural (line-start headers)
        strict_hits = sum(
            1 for marker in section_names
            if re.search(rf"(?:^|\n)\s*#*\s*{marker}\b", lowered)
        )
        if strict_hits >= 2:
            return True
        if stripped_len > 14000 and ("references" in lowered or "introduction" in lowered):
            return True

        # Tier 2 – academic-signal supplement
        if stripped_len >= 5000:
            loose_hits = sum(1 for marker in section_names if re.search(rf"\b{marker}\b", lowered))
            academic_signals = sum(1 for pattern in [
                r"\[[\d,\s\u2013\u2014-]+\]",   # [1], [1,2], [1–3]
                r"\bfig(?:ure)?\s*\.?\s*\d",     # Fig. 1, Figure 2
                r"\btable\s*\d",                 # Table 1
                r"\bdoi\s*:\s*10\.",             # DOI mentions
                r"\bet al\.",                    # et al.
                r"\bvol\.\s*\d",                 # Vol. n (journal volume)
            ] if re.search(pattern, lowered))
            if loose_hits >= 3 and academic_signals >= 2:
                return True

        # Tier 3 – length fallback (keep existing behaviour)
        landing_markers = ["skip to main content", "my account", "download", "previous", "next"]
        if stripped_len < 10000 and sum(m in lowered for m in landing_markers) >= 2:
            return False
        return stripped_len > 20000

    @staticmethod
    def _extract_pdf_links(markdown: str, *, base_url: str = "") -> list[str]:
        links: list[str] = []
        for match in re.finditer(r"https?://[^\s)\]\"']+", str(markdown or "")):
            url = match.group(0).rstrip(".,;")
            lowered = url.lower()
            if lowered.endswith(".pdf") or "viewcontent.cgi" in lowered or "download" in lowered and "pdf" in lowered:
                links.append(url)
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", str(markdown or "")):
            href = match.group(1).strip()
            if not href:
                continue
            lowered = href.lower()
            if lowered.endswith(".pdf") or "viewcontent.cgi" in lowered or "download" in lowered and "pdf" in lowered:
                links.append(urllib.parse.urljoin(base_url, href))
        out: list[str] = []
        seen: set[str] = set()
        for link in links:
            if link not in seen:
                seen.add(link)
                out.append(link)
        return out

    @staticmethod
    def write_chunks(path: Path, paper: AbstractPaperRecord, text: str, chunk_chars: int = 1800, overlap: int = 240) -> None:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks = []
        buf = ""
        ordinal = 0
        for para in paragraphs or [text]:
            candidate = f"{buf}\n\n{para}".strip() if buf else para
            if len(candidate) <= chunk_chars:
                buf = candidate
                continue
            if buf:
                chunks.append(buf)
                ordinal += 1
                buf = buf[-overlap:] + "\n\n" + para
            else:
                for start in range(0, len(para), max(1, chunk_chars - overlap)):
                    chunks.append(para[start: start + chunk_chars])
        if buf:
            chunks.append(buf)
        with path.open("w", encoding="utf-8") as handle:
            for i, chunk in enumerate(chunks):
                handle.write(json.dumps({
                    "chunk_id": f"{paper.paper_id}:c{i:04d}",
                    "paper_id": paper.paper_id,
                    "doi": paper.doi,
                    "title": paper.title,
                    "ordinal": i,
                    "text": chunk,
                }, ensure_ascii=False) + "\n")

    def expand_from_references(self, fulltext_items: list[dict[str, Any]], *, max_reference_dois: int = 5) -> dict[str, Any]:
        doi_re = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
        dois: list[str] = []
        seeds = []
        for item in fulltext_items:
            path = item.get("parsed_text_path")
            paper_id = item.get("paper_id")
            if not path or not Path(path).exists():
                continue
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            found = [normalize_doi(x) for x in doi_re.findall(text)]
            if found:
                seeds.append(paper_id)
            for doi in found:
                if doi and doi not in dois:
                    dois.append(doi)
                if len(dois) >= max_reference_dois:
                    break
            if len(dois) >= max_reference_dois:
                break
        added = 0
        for doi in dois:
            try:
                from tools.academic_backends.crossref_backend import CrossrefBackend
                raw = CrossrefBackend().verify_doi(doi)
                if raw:
                    record = self.raw_to_abstract_record(raw, query=f"reference DOI {doi}", matched_keywords=[doi])
                    _, is_new = self.library.upsert_abstract(record)
                    added += int(is_new)
            except Exception as exc:
                self.diagnostics.append(f"Reference DOI backtracking failed for {doi}: {type(exc).__name__}")
        return {
            "enabled": True,
            "seed_papers": seeds,
            "new_abstract_records_from_references": added,
            "rerun_feature_scoring": False,
            "reference_dois": dois,
        }

    def build_resource_bundle_for_next_agent(
        self,
        selected: dict[str, Any],
        fulltext_update: dict[str, Any],
        *,
        candidate_papers: list[AbstractPaperRecord] | None = None,
    ) -> dict[str, Any]:
        available: list[dict[str, Any]] = []
        local_cached: list[dict[str, Any]] = []
        selected_by_id = {item["paper_id"]: item for item in selected.get("selected_for_fulltext_upgrade", [])}
        # The persistent library may contain thousands of papers from earlier
        # topics.  Candidate recall is intentionally broad, but the downstream
        # resource packet must be fail-closed: only the final, current-topic
        # selection may enter it.  Non-selected cached full texts remain useful
        # in the long-lived library without leaking into this run.
        relevant_cached_ids = set(selected_by_id)

        def to_available_item(item: dict[str, Any], *, origin: str, reason: str) -> dict[str, Any]:
            paper = self.library.get_abstract(item.get("paper_id", ""))
            return {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", "") or (paper.title if paper else ""),
                "doi": item.get("doi", "") or (paper.doi if paper else ""),
                "local_file_path": item.get("local_file_path", ""),
                "parsed_text_path": item.get("parsed_text_path", ""),
                "chunk_index_path": item.get("chunk_index_path", ""),
                "fulltext_type": item.get("fulltext_type", ""),
                "source_url": item.get("source_url", ""),
                "matched_features": item.get("matched_features", []),
                "retrieval_roles": item.get("retrieval_roles", []),
                "evidence_roles": item.get("evidence_roles", []),
                "upgrade_reason": reason,
                "origin": origin,
                "access_method": item.get("access_method", ""),
                "source_audit": self.source_audit_for_paper(paper) if paper else {},
                "current_topic_fit": item.get("current_topic_fit", {}),
            }

        for item in fulltext_update.get("new_fulltexts_downloaded", []):
            if item.get("paper_id") not in relevant_cached_ids:
                continue
            selected_item = selected_by_id.get(item.get("paper_id", ""), {})
            available.append(to_available_item(
                {**item, "current_topic_fit": selected_item.get("current_topic_fit", {})},
                origin="new_download",
                reason="New parseable full text acquired in this run and ready for evidence extraction.",
            ))
        for item in fulltext_update.get("fulltexts_reused_from_cache", []):
            if item.get("paper_id") not in relevant_cached_ids:
                continue
            selected_item = selected_by_id.get(item.get("paper_id", ""), {})
            available.append(to_available_item(
                {**item, "current_topic_fit": selected_item.get("current_topic_fit", {})},
                origin="selected_cache_reuse",
                reason="Selected paper matched local full-text cache; parsed text is reused for downstream processing.",
            ))

        already_ids = {item["paper_id"] for item in available}
        for ft in self.library.all_available_fulltexts():
            if ft.paper_id in already_ids:
                continue
            if relevant_cached_ids and ft.paper_id not in relevant_cached_ids:
                continue
            if not ft.parsed_text_path or not Path(ft.parsed_text_path).exists():
                continue
            selected_item = selected_by_id.get(ft.paper_id, {})
            cached_item = to_available_item(
                {
                    **asdict(ft),
                    "matched_features": selected_item.get("matched_features", []),
                    "retrieval_roles": selected_item.get("retrieval_roles", []),
                    "evidence_roles": selected_item.get("evidence_roles", []),
                    "current_topic_fit": selected_item.get("current_topic_fit", {}),
                },
                origin="relevant_cache_hit",
                reason="Local cached full text is relevant to this run and is added for downstream processing.",
            )
            local_cached.append(cached_item)
            available.append(cached_item)
        abstract_only = []
        manual = []
        deferred_downloads = []
        for deferred in fulltext_update.get("deferred_fulltext_acquisition", []):
            pid = deferred.get("paper_id")
            if pid not in relevant_cached_ids:
                continue
            source = selected_by_id.get(pid, deferred)
            paper = self.library.get_abstract(pid) if pid else None
            if paper and paper.abstract:
                abstract_only.append({
                    "paper_id": pid,
                    "title": paper.title,
                    "doi": paper.doi,
                    "abstract": paper.abstract,
                    "reason_high_value": source.get("upgrade_reason", "High abstract-level relevance; full-text download cap was reached in this run and the paper is waiting for follow-up upgrade."),
                    "matched_features": source.get("matched_features", []),
                    "source_audit": self.source_audit_for_paper(paper),
                    "status": "deferred_fulltext_download",
                })
            deferred_downloads.append({
                "paper_id": pid,
                "title": source.get("title") or (paper.title if paper else ""),
                "doi": source.get("doi") or (paper.doi if paper else ""),
                "year": source.get("year") or (paper.year if paper else None),
                "venue": source.get("venue") or (paper.venue if paper else ""),
                "landing_page_url": source.get("landing_page_url") or (paper.landing_page_url if paper else ""),
                "reason_deferred": deferred.get("reason") or source.get("upgrade_reason", ""),
                "matched_features": source.get("matched_features", []),
                "source_audit": self.source_audit_for_paper(paper) if paper else source.get("source_audit", {}),
                "suggested_action": "Continue automatic full-text acquisition in a later batch; this is not a manual failure.",
            })
        for failed in fulltext_update.get("failed_fulltext_acquisition", []):
            pid = failed.get("paper_id")
            if pid not in relevant_cached_ids:
                continue
            source = selected_by_id.get(pid, failed)
            paper = self.library.get_abstract(pid) if pid else None
            reason_needed = failed.get("reason") or source.get("upgrade_reason", "")
            title = source.get("title") or (paper.title if paper else "")
            doi = source.get("doi") or (paper.doi if paper else "")
            recommended_filename = self.suggested_manual_download_filename(pid or "", doi or "", title or "")
            if paper and paper.abstract:
                abstract_only.append({
                    "paper_id": pid,
                    "title": paper.title,
                    "doi": paper.doi,
                    "abstract": paper.abstract,
                    "reason_high_value": source.get("upgrade_reason", "High abstract-level relevance but no full text is currently available."),
                    "matched_features": source.get("matched_features", []),
                    "source_audit": self.source_audit_for_paper(paper),
                })
            manual.append({
                "paper_id": pid,
                "title": title,
                "doi": doi,
                "year": source.get("year") or (paper.year if paper else None),
                "venue": source.get("venue") or (paper.venue if paper else ""),
                "landing_page_url": source.get("landing_page_url") or (paper.landing_page_url if paper else ""),
                "failure_category": self.classify_fulltext_failure(reason_needed),
                "reason_needed": reason_needed,
                "matched_features": source.get("matched_features", []),
                "source_audit": self.source_audit_for_paper(paper) if paper else source.get("source_audit", {}),
                "recommended_download_routes": self.recommend_manual_download_routes(paper, source, reason_needed),
                "recommended_download_folder": str(self.manual_fulltext_dir),
                "recommended_filename": recommended_filename,
                "recommended_local_path": str(self.manual_fulltext_dir / recommended_filename),
                "suggested_action": "Download the legal fulltext manually, then put the PDF/HTML/XML into user_fulltexts/ before stage 3 ingestion.",
            })
        return {
            "available_fulltexts": available,
            "local_cached_fulltexts": local_cached,
            "background_pages_cached": [
                item for item in fulltext_update.get("background_pages_cached", [])
                if item.get("paper_id") in relevant_cached_ids
            ],
            "abstract_only_high_value_papers": abstract_only,
            "deferred_download_list": deferred_downloads,
            "manual_download_folder": str(self.manual_fulltext_dir),
            "manual_download_list": manual,
        }

    def check_fulltext_quality_legacy_disabled(self, fulltexts: list[dict], user_query: str) -> list[dict]:
        system = (
            "You are a scholarly full-text quality assessor. Given a user research question and paper excerpt, judge content quality and relevance. "
            "Return JSON only: {\"grade\":\"high|medium|low|irrelevant\",\"reason\":\"one short English sentence\"}."
        )
        for ft in fulltexts:
            try:
                text = ""
                parsed = ft.get("parsed_text_path")
                if parsed and Path(parsed).exists():
                    text = Path(parsed).read_text(encoding="utf-8", errors="replace")[:2000]
                if not text:
                    ft["quality_check"] = {"grade": "no_text", "reason": "No readable full-text content."}
                    continue
                if not self.real_llm:
                    ft["quality_check"] = {"grade": "unchecked", "reason": "Quality check skipped in mock mode."}
                    continue
                result = call_qwen_chat(
                    "FulltextQualityChecker",
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(
                            {"query": user_query, "title": ft.get("title", ""), "excerpt": text},
                            ensure_ascii=False,
                        )},
                    ],
                    model_tier="cheap_model",
                    max_tokens=500,
                    response_format={"type": "json_object"},
                )
                payload = parse_json_like(result.get("content", ""), fallback={}) if isinstance(result, dict) else {}
                ft["quality_check"] = payload if isinstance(payload, dict) and payload else {"grade": "parse_error", "reason": str(result)[:200]}
            except Exception as exc:
                ft["quality_check"] = {"grade": "error", "reason": str(exc)[:200]}
        return fulltexts

    def deterministic_fulltext_quality(self, ft: dict[str, Any], text: str, user_query: str) -> dict[str, Any]:
        fulltext_type = str(ft.get("fulltext_type") or "").lower()
        access_method = str(ft.get("access_method") or "").lower()
        lowered = str(text or "").lower()
        section_hits = sum(1 for marker in ["abstract", "introduction", "methods", "results", "discussion", "conclusion", "references"] if marker in lowered)
        is_review = bool(re.search(r"\breview\b|\bperspective\b|\broadmap\b|\bprogress\b", f"{ft.get('title','')} {text[:2000]}", re.I))
        if any(x in lowered[:2500] for x in ["captcha", "access denied", "sign in", "login", "subscribe to", "institutional access"]):
            return {"quality_tier": "exclude_or_refetch", "is_fulltext": False, "structure_type": "blocked_or_unknown", "query_relevance": "unclear", "reason": "Blocked/login/navigation page signal.", "recommended_action": "refetch_or_manual_download"}
        if fulltext_type == "jats_xml" and len(text) >= 1200:
            return {"quality_tier": "review_core" if is_review else "core_fulltext", "is_fulltext": True, "structure_type": "jats_xml", "query_relevance": "medium", "reason": "Structured XML with article body sections.", "recommended_action": "use_as_review_background" if is_review else "use_as_core"}
        if fulltext_type == "publisher_html" and len(text) >= 6000 and section_hits >= 2:
            return {"quality_tier": "review_core" if is_review else "core_fulltext", "is_fulltext": True, "structure_type": "publisher_html", "query_relevance": "medium", "reason": "Publisher HTML with sufficient article-like section structure.", "recommended_action": "use_as_review_background" if is_review else "use_as_core"}
        if fulltext_type.startswith("pdf_") and len(text) >= 8000:
            structure = "pdf_structured" if any(parser in access_method for parser in ["grobid", "docling", "marker"]) else "pdf_text"
            return {"quality_tier": "review_core" if is_review else "core_fulltext", "is_fulltext": True, "structure_type": structure, "query_relevance": "medium", "reason": "PDF parsed into substantial text.", "recommended_action": "use_as_review_background" if is_review else "use_as_core"}
        if len(text) >= 3000 and section_hits >= 1:
            return {"quality_tier": "supporting_evidence", "is_fulltext": False, "structure_type": "html_markdown", "query_relevance": "unclear", "reason": "Partial scholarly-looking text, but not enough for core fulltext.", "recommended_action": "use_as_auxiliary"}
        if len(text) >= 500:
            return {"quality_tier": "metadata_or_abstract_only", "is_fulltext": False, "structure_type": "metadata_page", "query_relevance": "unclear", "reason": "Short or metadata-like page.", "recommended_action": "use_as_auxiliary"}
        return {"quality_tier": "exclude_or_refetch", "is_fulltext": False, "structure_type": "blocked_or_unknown", "query_relevance": "unclear", "reason": "Parsed text is too short or unusable.", "recommended_action": "refetch_or_manual_download"}

    def normalize_fulltext_quality_payload(self, payload: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return fallback
        tier = str(payload.get("quality_tier") or payload.get("grade") or fallback["quality_tier"]).lower()
        old_grade_map = {"high": "core_fulltext", "medium": "core_fulltext", "low": "supporting_evidence", "irrelevant": "exclude_or_refetch", "no_text": "exclude_or_refetch"}
        tier = old_grade_map.get(tier, tier)
        allowed_tiers = {"core_fulltext", "review_core", "supporting_evidence", "metadata_or_abstract_only", "exclude_or_refetch"}
        if tier not in allowed_tiers:
            tier = fallback["quality_tier"]
        structure = str(payload.get("structure_type") or fallback.get("structure_type") or "blocked_or_unknown")
        allowed_structures = {"jats_xml", "publisher_html", "pdf_structured", "pdf_text", "html_markdown", "metadata_page", "abstract_only", "blocked_or_unknown"}
        if structure not in allowed_structures:
            structure = fallback.get("structure_type", "blocked_or_unknown")
        action = str(payload.get("recommended_action") or fallback.get("recommended_action") or "use_as_auxiliary")
        allowed_actions = {"use_as_core", "use_as_review_background", "use_as_auxiliary", "refetch_or_manual_download", "exclude"}
        if action not in allowed_actions:
            action = fallback.get("recommended_action", "use_as_auxiliary")
        relevance = str(payload.get("query_relevance") or fallback.get("query_relevance") or "unclear")
        if relevance not in {"high", "medium", "low", "unclear"}:
            relevance = fallback.get("query_relevance", "unclear")
        reason = normalize_space(str(payload.get("reason") or fallback.get("reason") or ""))[:500]
        reason_l = reason.lower()
        unrelated_signal = any(
            marker in reason_l
            for marker in [
                "not related",
                "unrelated",
                "irrelevant",
                "no direct content",
                "not specific",
                "not relevant",
            ]
        )
        # A source can only be a downstream core source when it is both a real
        # scholarly full text and materially relevant to the user query. LLMs
        # sometimes return inconsistent pairs such as review_core + low
        # relevance; keep the stronger safety invariant here.
        if tier in {"core_fulltext", "review_core"} and relevance in {"low", "unclear"}:
            if relevance == "low" and unrelated_signal:
                tier = "exclude_or_refetch"
                action = "exclude"
            else:
                tier = "supporting_evidence"
                action = "use_as_auxiliary"
        if tier == "core_fulltext" and relevance != "high":
            tier = "supporting_evidence"
            action = "use_as_auxiliary"
        return {
            "quality_tier": tier,
            "is_fulltext": bool(payload.get("is_fulltext", fallback.get("is_fulltext", False))),
            "structure_type": structure,
            "query_relevance": relevance,
            "reason": reason,
            "recommended_action": action,
        }

    def check_fulltext_quality(self, fulltexts: list[dict], user_query: str) -> list[dict]:
        system = read_text_file(self.fulltext_quality_prompt_path) or read_text_file(DEFAULT_FULLTEXT_QUALITY_PROMPT)
        if not system:
            system = "Classify scholarly fulltext quality into core_fulltext, review_core, supporting_evidence, metadata_or_abstract_only, or exclude_or_refetch. Return strict JSON only."
        for ft in fulltexts:
            try:
                parsed = ft.get("parsed_text_path")
                text = Path(parsed).read_text(encoding="utf-8", errors="replace") if parsed and Path(parsed).exists() else ""
                fallback = self.deterministic_fulltext_quality(ft, text, user_query)
                if not text:
                    ft["quality_check"] = {**fallback, "quality_tier": "exclude_or_refetch", "reason": "No readable parsed text."}
                    continue
                if not self.real_llm:
                    ft["quality_check"] = fallback
                    continue
                payload = {
                    "user_query": user_query,
                    "paper": {
                        "paper_id": ft.get("paper_id", ""),
                        "title": ft.get("title", ""),
                        "doi": ft.get("doi", ""),
                        "fulltext_type": ft.get("fulltext_type", ""),
                        "access_method": ft.get("access_method", ""),
                        "source_url": ft.get("source_url", ""),
                        "text_length": len(text),
                    },
                    "text_excerpt": text[:4500],
                }
                result = call_qwen_chat(
                    "FulltextQualityChecker",
                    [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    model_tier=self.audit_model_tier,
                    temperature=0,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
                parsed_payload = parse_json_like(result.get("content", ""), fallback={}) if isinstance(result, dict) else {}
                ft["quality_check"] = self.normalize_fulltext_quality_payload(parsed_payload, fallback)
            except Exception as exc:
                ft["quality_check"] = {"quality_tier": "exclude_or_refetch", "is_fulltext": False, "structure_type": "blocked_or_unknown", "query_relevance": "unclear", "reason": f"quality check failed: {type(exc).__name__}", "recommended_action": "refetch_or_manual_download"}
        return fulltexts

    def apply_downstream_quality_gate(self, bundle_next: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "core_fulltext": 0,
            "review_core": 0,
            "supporting_evidence": 0,
            "metadata_or_abstract_only": 0,
            "exclude_or_refetch": 0,
        }
        core_fulltexts: list[dict[str, Any]] = []
        review_core_fulltexts: list[dict[str, Any]] = []
        supporting_fulltexts: list[dict[str, Any]] = []
        metadata_only: list[dict[str, Any]] = []
        excluded_fulltexts: list[dict[str, Any]] = []
        for ft in bundle_next.get("available_fulltexts", []):
            qc = ft.get("quality_check") or {}
            tier = str(qc.get("quality_tier") or "").lower()
            if tier not in summary:
                tier = self.normalize_fulltext_quality_payload(qc, self.deterministic_fulltext_quality(ft, "", "")).get("quality_tier", "exclude_or_refetch")
            ft["downstream_use_policy"] = tier
            ft["is_core_source"] = tier in {"core_fulltext", "review_core"}
            ft["usable_for_downstream"] = ft["is_core_source"]
            ft["auxiliary_for_downstream"] = tier in {"supporting_evidence", "metadata_or_abstract_only"}
            ft["core_acceptance_basis"] = (
                "complete_or_substantial_scholarly_fulltext"
                if ft["is_core_source"]
                else "not_core_fulltext"
            )
            summary[tier] = summary.get(tier, 0) + 1
            if tier in {"core_fulltext", "review_core"}:
                core_fulltexts.append(ft)
                if tier == "review_core":
                    review_core_fulltexts.append(ft)
            elif tier == "supporting_evidence":
                supporting_fulltexts.append(ft)
            elif tier == "metadata_or_abstract_only":
                metadata_only.append(ft)
            else:
                excluded_fulltexts.append(ft)
        bundle_next["quality_gate_summary"] = summary
        bundle_next["downstream_core_fulltexts"] = core_fulltexts
        bundle_next["downstream_core_fulltext_count_including_reviews"] = len(core_fulltexts)
        bundle_next["review_core_fulltexts"] = review_core_fulltexts
        bundle_next["supporting_fulltexts_for_review"] = supporting_fulltexts
        bundle_next["metadata_or_abstract_only"] = metadata_only
        bundle_next["excluded_fulltexts_from_core"] = excluded_fulltexts
        return bundle_next

    def write_artifacts(
        self,
        artifact_dir: Path,
        *,
        query_plan: dict[str, Any],
        resource_bundle: dict[str, Any],
        atomic_plan: dict[str, Any],
        web_lens_context: dict[str, Any] | None = None,
        supplemental_facet_plan: dict[str, Any] | None = None,
        scoring_results: list[dict[str, Any]],
        ranking_tables: list[dict[str, Any]],
        selected: dict[str, Any],
        papers: list[AbstractPaperRecord],
        research_facet_plan: dict[str, Any] | None = None,
        facet_literature_map: dict[str, Any] | None = None,
    ) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "query_plan.json").write_text(json.dumps(query_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "resource_bundle.json").write_text(json.dumps(resource_bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (artifact_dir / "atomic_relevance_plan.json").write_text(json.dumps(atomic_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if web_lens_context is not None:
            (artifact_dir / "web_lens_context.json").write_text(json.dumps(web_lens_context, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if supplemental_facet_plan is not None:
            (artifact_dir / "supplemental_facet_plan.json").write_text(json.dumps(supplemental_facet_plan, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if research_facet_plan is not None:
            (artifact_dir / "research_facet_plan.json").write_text(json.dumps(research_facet_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if facet_literature_map is not None:
            (artifact_dir / "facet_literature_map.json").write_text(json.dumps(facet_literature_map, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "feature_scoring_results.json").write_text(json.dumps(scoring_results, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "feature_ranking_tables.json").write_text(json.dumps(ranking_tables, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "selected_for_fulltext_upgrade.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
        (artifact_dir / "abstract_records_considered.json").write_text(
            json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        summary = self.make_summary_markdown(resource_bundle)
        (artifact_dir / "run_summary.md").write_text(summary, encoding="utf-8")

    @staticmethod
    def make_summary_markdown(bundle: dict[str, Any]) -> str:
        session = bundle.get("resource_update_session", {})
        next_bundle = bundle.get("resource_bundle_for_next_agent", {})
        facet_map = session.get("facet_literature_map") or {}
        web_lens_context = session.get("web_lens_context") or {}
        supplemental_facet_plan = session.get("supplemental_facet_plan") or {}
        supplemental_features = ((supplemental_facet_plan.get("supplemental_facet_plan") or {}).get("supplemental_features") or [])
        facet_count = len(facet_map.get("facets", []) or [])
        lines = [
            "# Literature Resource Builder Run Summary",
            "",
            f"- Original user query: {session.get('user_query', '')}",
            f"- Scholar Facets: {facet_count}",
            f"- Raw web signals: {len(web_lens_context.get('raw_web_results', []) or [])}",
            f"- Dense web summaries: {len(web_lens_context.get('web_context_summaries', []) or [])}",
            f"- Supplemental web-derived facets: {len(supplemental_features)}",
            f"- New structured literature records: {(session.get('abstract_library_update') or {}).get('new_records_added', 0)}",
            f"- New bibliometric supplemental recall records: {((session.get('facet_bibliometric_recall') or {}).get('totals') or {}).get('new_records', 0)}",
            f"- Duplicate structured literature records skipped: {(session.get('abstract_library_update') or {}).get('duplicate_records_skipped', 0)}",
            f"- Available full texts: {len(next_bundle.get('available_fulltexts', []))}",
            f"- Relevant local cache additions: {len(next_bundle.get('local_cached_fulltexts', []))}",
            f"- Core full-text quality gate: {next_bundle.get('quality_gate_summary', {})}",
            f"- High-value abstract-only papers: {len(next_bundle.get('abstract_only_high_value_papers', []))}",
            f"- Deferred automatic download list: {len(next_bundle.get('deferred_download_list', []))}",
            f"- Real failure/manual download list: {len(next_bundle.get('manual_download_list', []))}",
            "",
            "## Available Full Texts",
        ]
        for item in next_bundle.get("available_fulltexts", [])[:20]:
            lines.append(
                f"- [{item.get('origin', 'unknown')}] {item.get('title', '')} | "
                f"DOI: {item.get('doi', '')} | {item.get('access_method', '')}"
            )
        if facet_map.get("facets"):
            lines.extend(["", "## Scholar Facet Literature Map (Excerpt)"])
            for facet in facet_map.get("facets", [])[:8]:
                lines.append(
                    f"- {facet.get('facet_id')}: {facet.get('facet_name')} | "
                    f"relevance {len(facet.get('relevance_top_papers', []))} / "
                    f"landmark {len(facet.get('citation_landmark_papers', []))} / "
                    f"review-perspective {len(facet.get('review_perspective_papers', []))} / "
                    f"recent-frontier {len(facet.get('recent_frontier_papers', []))}"
                )
        lines.append("")
        lines.append("## Real Failures / Manual Download List")
        for item in next_bundle.get("manual_download_list", [])[:20]:
            lines.append(f"- {item.get('title', '')} | DOI: {item.get('doi', '')} | reason: {item.get('reason_needed', '')}")
        if session.get("diagnostics"):
            lines.extend(["", "## Diagnostics"])
            for item in session.get("diagnostics", [])[:30]:
                lines.append(f"- {item}")
        return "\n".join(lines) + "\n"


def load_query_plan(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "handoff" in data and isinstance(data["handoff"], dict):
        return data["handoff"].get("plan") or data["handoff"]
    if isinstance(data, dict) and "plan" in data and isinstance(data["plan"], dict) and data.get("stage") == "query_planner_confirmed":
        return data["plan"]
    return data


def run_api_smoke_tests(query: str = "daytime radiative cooling film", *, output_path: str | Path | None = None) -> dict[str, Any]:
    configure_secret_environment()
    engine = SearchEngine()
    backends = ["openalex", "crossref", "semantic_scholar_public", "core", "arxiv", "tavily", "serper", "brave", "duckduckgo", "firecrawl"]
    results = []
    for backend in backends:
        started = time.time()
        try:
            if backend == "openalex":
                from tools.academic_backends.openalex_backend import OpenAlexBackend
                rows = OpenAlexBackend().search(query, max_results=1, from_year=2020)
            elif backend == "crossref":
                from tools.academic_backends.crossref_backend import CrossrefBackend
                rows = CrossrefBackend().search(query, max_results=1, from_year=2020)
            else:
                rows = engine.search(query, [backend], max_results=1)
            ok = bool(rows)
            sample = rows[0] if rows else {}
            results.append({
                "backend": backend,
                "ok": ok,
                "count": len(rows),
                "sample_title": sample.get("title", ""),
                "sample_doi": sample.get("doi", ""),
                "elapsed_sec": round(time.time() - started, 2),
            })
        except Exception as exc:
            results.append({
                "backend": backend,
                "ok": False,
                "count": 0,
                "error": type(exc).__name__,
                "elapsed_sec": round(time.time() - started, 2),
            })
    # Fulltext utilities: keep the URLs tiny and public.
    for name, func in [
        ("jina_reader", lambda: engine.fetch_fulltext("https://example.com", method="jina")),
        ("firecrawl_scrape", lambda: engine.fetch_fulltext("https://example.com", method="firecrawl")),
    ]:
        started = time.time()
        try:
            text = func()
            results.append({"backend": name, "ok": bool(text), "chars": len(text or ""), "elapsed_sec": round(time.time() - started, 2)})
        except Exception as exc:
            results.append({"backend": name, "ok": False, "error": type(exc).__name__, "elapsed_sec": round(time.time() - started, 2)})
    started = time.time()
    try:
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend
        item = UnpaywallBackend().lookup("10.1038/nature13883")
        results.append({
            "backend": "unpaywall",
            "ok": bool(item),
            "is_oa": bool((item or {}).get("is_oa")),
            "has_best_oa_url": bool((item or {}).get("best_oa_url")),
            "elapsed_sec": round(time.time() - started, 2),
        })
    except Exception as exc:
        results.append({"backend": "unpaywall", "ok": False, "error": type(exc).__name__, "elapsed_sec": round(time.time() - started, 2)})
    payload = {
        "query": query,
        "created_at": utc_now(),
        "results": results,
    }
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


__all__ = [
    "AbstractPaperRecord",
    "AtomicFeature",
    "FulltextRecord",
    "LiteratureResourceBuilder",
    "LiteratureResourceLibrary",
    "load_query_plan",
    "run_api_smoke_tests",
]
