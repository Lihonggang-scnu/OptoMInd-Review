"""SQLite persistence layer — search cache, paper store, session history.

Tables:
  papers       — cached paper/source records with full text
  searches     — query history with stats
  fulltext     — URL → markdown cache (mirrored from search_engine module)
  sessions     — research session metadata

Usage:
  from optomind_research.cache import ResearchCache
  cache = ResearchCache()
  cache.save_search(query, sources, backends, stats)
  hits = cache.lookup_papers(query)
"""

from __future__ import annotations

import hashlib, json, re, sqlite3, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = PROJECT_ROOT / "data"
DB_PATH = DB_DIR / "optomind_cache.db"
DB_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    source_id TEXT PRIMARY KEY,
    title TEXT,
    authors TEXT,
    year INTEGER,
    doi TEXT,
    url TEXT,
    abstract TEXT,
    fulltext TEXT,
    backend TEXT,
    curation_score REAL,
    evidence_type TEXT,
    data JSON,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    query TEXT NOT NULL,
    backends TEXT,
    num_results INTEGER,
    curated_count INTEGER,
    degraded_backends TEXT,
    stats_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS search_cache (
    cache_key TEXT PRIMARY KEY,
    normalized_query TEXT NOT NULL,
    query TEXT NOT NULL,
    backend TEXT NOT NULL,
    max_results INTEGER,
    results_json TEXT NOT NULL,
    result_count INTEGER,
    hit_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fulltext (
    url TEXT PRIMARY KEY,
    markdown TEXT,
    char_count INTEGER,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    question TEXT,
    answer_preview TEXT,
    num_sources INTEGER,
    num_verified INTEGER,
    model_tier TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS research_runs (
    session_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT,
    report_path TEXT,
    artifact_dir TEXT,
    num_sources INTEGER,
    num_chunks INTEGER,
    num_evidence INTEGER,
    num_covered_claims INTEGER,
    audit_json TEXT,
    capabilities_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_backend ON papers(backend);
CREATE INDEX IF NOT EXISTS idx_searches_query ON searches(query);
CREATE INDEX IF NOT EXISTS idx_searches_session ON searches(session_id);
CREATE INDEX IF NOT EXISTS idx_searches_created ON searches(created_at);
CREATE INDEX IF NOT EXISTS idx_search_cache_backend ON search_cache(backend);
CREATE INDEX IF NOT EXISTS idx_search_cache_updated ON search_cache(updated_at);
CREATE INDEX IF NOT EXISTS idx_fulltext_fetched ON fulltext(fetched_at);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_research_runs_created ON research_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_research_runs_question ON research_runs(question);
"""


QUERY_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "that", "this", "these",
    "those", "using", "based", "review", "study", "studies", "paper", "papers",
    "research", "application", "applications", "method", "methods", "limitations",
    "perspective", "representative", "seminal", "landmark", "highly", "cited",
}


def normalize_query_text(query: str) -> str:
    """Normalize a query for persistent cache matching."""

    text = str(query or "").casefold()
    text = text.replace("radiative cooling", "radiative-cooling")
    text = text.replace("daytime radiative-cooling", "daytime-radiative-cooling")
    text = text.replace("optical thin film", "optical-film")
    text = text.replace("optical coating", "optical-film")
    tokens = re.findall(r"[a-z0-9][a-z0-9\-]{1,}|[\u4e00-\u9fff]{2,}", text)
    cleaned = []
    for token in tokens:
        token = token.strip("-_")
        if not token or token in QUERY_STOPWORDS:
            continue
        cleaned.append(token)
    return " ".join(sorted(dict.fromkeys(cleaned)))


def _query_tokens(normalized_query: str) -> set[str]:
    return {token for token in str(normalized_query or "").split() if token}


class ResearchCache:
    """SQLite-backed cache for research agent."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        """Close the SQLite connection, mainly useful for tests and short-lived tools."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            conn.executescript(SCHEMA)
            conn.commit()

    # ═══════════ Papers ═══════════

    def save_papers(self, sources: List[Dict]) -> int:
        """Insert or replace paper records from search results."""
        conn = self._get_conn()
        count = 0
        for s in sources:
            sid = s.get("source_id") or s.get("title", "")[:80]
            if not sid:
                continue
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO papers
                       (source_id, title, authors, year, doi, url, abstract, fulltext, backend, curation_score, data)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sid,
                        s.get("title", ""),
                        json.dumps(s.get("authors", []), ensure_ascii=False),
                        s.get("year"),
                        s.get("doi", ""),
                        s.get("url_or_doi", "") or s.get("source_url", ""),
                        s.get("abstract_or_snippet", ""),
                        s.get("_fulltext", ""),
                        s.get("backend", ""),
                        s.get("curation_score", 0),
                        json.dumps(s, ensure_ascii=False, default=str),
                    ),
                )
                count += 1
            except Exception:
                pass
        conn.commit()
        return count

    def lookup_papers(self, query: str, limit: int = 20) -> List[Dict]:
        """Search cached papers by title or abstract LIKE query."""
        conn = self._get_conn()
        like = f"%{query}%"
        rows = conn.execute(
            """SELECT * FROM papers
               WHERE title LIKE ? OR abstract LIKE ?
               ORDER BY curation_score DESC, year DESC
               LIMIT ?""",
            (like, like, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            if d.get("data"):
                try:
                    data = json.loads(d.pop("data"))
                    data.update({k: v for k, v in d.items() if v and k != "data"})
                    results.append(data)
                except json.JSONDecodeError:
                    results.append(d)
        return results

    def get_paper(self, source_id: str) -> Optional[Dict]:
        row = self._get_conn().execute("SELECT * FROM papers WHERE source_id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def get_paper_by_doi(self, doi: str) -> Optional[Dict]:
        row = self._get_conn().execute("SELECT * FROM papers WHERE doi=?", (doi,)).fetchone()
        return dict(row) if row else None

    # ═══════════ Searches ═══════════

    def save_search(
        self,
        query: str,
        sources: List[Dict],
        backends: List[str],
        stats: Optional[Dict] = None,
        session_id: Optional[str] = None,
    ) -> int:
        conn = self._get_conn()
        sid = session_id or uuid.uuid4().hex[:8]
        cursor = conn.execute(
            """INSERT INTO searches (session_id, query, backends, num_results, curated_count, stats_json)
               VALUES (?,?,?,?,?,?)""",
            (
                sid,
                query,
                json.dumps(backends),
                len(sources),
                sum(1 for s in sources if s.get("curation_score", 0) >= 6),
                json.dumps(stats or {}, default=str),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    # ═══════════ Persistent search-result cache ═══════════

    @staticmethod
    def search_cache_key(query: str, backend: str) -> tuple[str, str]:
        normalized = normalize_query_text(query)
        digest = hashlib.sha256(f"{backend}|{normalized}".encode("utf-8")).hexdigest()
        return digest, normalized

    def save_search_results(
        self,
        query: str,
        backend: str,
        results: List[Dict],
        max_results: int = 5,
    ) -> bool:
        """Persist raw backend search results for future exact/similar reuse."""

        if results is None:
            return False
        key, normalized = self.search_cache_key(query, backend)
        try:
            payload = json.dumps(results, ensure_ascii=False, default=str)
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    """INSERT INTO search_cache
                       (cache_key, normalized_query, query, backend, max_results,
                        results_json, result_count, updated_at)
                       VALUES (?,?,?,?,?,?,?,datetime('now'))
                       ON CONFLICT(cache_key) DO UPDATE SET
                         query=excluded.query,
                         max_results=excluded.max_results,
                         results_json=excluded.results_json,
                         result_count=excluded.result_count,
                         updated_at=datetime('now')""",
                    (
                        key,
                        normalized,
                        query,
                        backend,
                        int(max_results),
                        payload,
                        len(results),
                    ),
                )
                conn.commit()
            return True
        except Exception:
            return False

    def get_cached_search_results(
        self,
        query: str,
        backend: str,
        max_results: int = 5,
        min_similarity: float = 0.58,
        scan_limit: int = 500,
    ) -> tuple[List[Dict], dict[str, Any]]:
        """Return cached results for an exact or similar query.

        Similarity is token-overlap based and intentionally forgiving because
        planner-generated scholarly queries often differ only by section words.
        """

        key, normalized = self.search_cache_key(query, backend)
        query_terms = _query_tokens(normalized)
        with self._lock:
            conn = self._get_conn()
            exact = conn.execute(
                "SELECT * FROM search_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if exact:
                conn.execute(
                    "UPDATE search_cache SET hit_count=hit_count+1, updated_at=datetime('now') WHERE cache_key=?",
                    (key,),
                )
                conn.commit()
                return self._decode_search_cache_row(exact, max_results, 1.0, "exact")

            rows = conn.execute(
                """SELECT * FROM search_cache
                   WHERE backend=?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (backend, int(scan_limit)),
            ).fetchall()

        best_row = None
        best_score = 0.0
        if query_terms:
            for row in rows:
                cached_terms = _query_tokens(row["normalized_query"])
                if not cached_terms:
                    continue
                intersection = len(query_terms & cached_terms)
                containment = intersection / max(1, min(len(query_terms), len(cached_terms)))
                jaccard = intersection / max(1, len(query_terms | cached_terms))
                score = 0.72 * containment + 0.28 * jaccard
                if score > best_score:
                    best_score = score
                    best_row = row

        if best_row is not None and best_score >= float(min_similarity):
            with self._lock:
                self._get_conn().execute(
                    "UPDATE search_cache SET hit_count=hit_count+1, updated_at=datetime('now') WHERE cache_key=?",
                    (best_row["cache_key"],),
                )
                self._get_conn().commit()
            return self._decode_search_cache_row(best_row, max_results, best_score, "similar")

        return [], {
            "cache_hit": False,
            "match_type": "miss",
            "similarity": round(best_score, 4),
            "normalized_query": normalized,
        }

    @staticmethod
    def _decode_search_cache_row(
        row: sqlite3.Row,
        max_results: int,
        similarity: float,
        match_type: str,
    ) -> tuple[List[Dict], dict[str, Any]]:
        try:
            results = json.loads(row["results_json"] or "[]")
        except json.JSONDecodeError:
            results = []
        if not isinstance(results, list):
            results = []
        decoded: List[Dict] = []
        for item in results[: max(1, int(max_results))]:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            meta = dict(copied.get("metadata") or {})
            meta.update({
                "search_cache_hit": True,
                "search_cache_match_type": match_type,
                "search_cache_similarity": round(float(similarity), 4),
                "search_cache_original_query": row["query"],
            })
            copied["metadata"] = meta
            decoded.append(copied)
        return decoded, {
            "cache_hit": True,
            "match_type": match_type,
            "similarity": round(float(similarity), 4),
            "cached_query": row["query"],
            "cached_count": row["result_count"],
        }

    def recent_searches(self, limit: int = 10) -> List[Dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM searches ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def search_history(self, query_hint: str = "", limit: int = 20) -> List[Dict]:
        if query_hint:
            rows = self._get_conn().execute(
                "SELECT * FROM searches WHERE query LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query_hint}%", limit),
            ).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM searches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════ Fulltext ═══════════

    def save_fulltext(self, url: str, markdown: str) -> bool:
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute(
                    "INSERT OR REPLACE INTO fulltext (url, markdown, char_count) VALUES (?,?,?)",
                    (url, markdown, len(markdown)),
                )
                conn.commit()
            return True
        except Exception:
            return False

    def get_fulltext(self, url: str) -> Optional[str]:
        with self._lock:
            row = self._get_conn().execute("SELECT markdown FROM fulltext WHERE url=?", (url,)).fetchone()
        return row["markdown"] if row else None

    def fulltext_stats(self) -> Dict:
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM fulltext").fetchone()[0]
        size = conn.execute("SELECT COALESCE(SUM(char_count),0) FROM fulltext").fetchone()[0]
        return {"cached_urls": count, "total_chars": size}

    # ═══════════ Sessions ═══════════

    def save_session(self, result: Dict[str, Any]) -> str:
        conn = self._get_conn()
        sid = result.get("session_id", uuid.uuid4().hex[:8])
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, question, answer_preview, num_sources, num_verified, model_tier)
               VALUES (?,?,?,?,?,?)""",
            (
                sid,
                result.get("question", ""),
                result.get("answer", "")[:200],
                len(result.get("sources", [])),
                result.get("stats", {}).get("citations_verified", 0),
                "qwen3.6-plus",
            ),
        )
        conn.commit()
        return sid

    def recent_sessions(self, limit: int = 10) -> List[Dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> Optional[Dict]:
        row = self._get_conn().execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def save_run(self, dossier: Any) -> str:
        """Persist a deep-research run without storing the full report in SQLite."""
        data = dossier.model_dump() if hasattr(dossier, "model_dump") else dict(dossier)
        ledger = data.get("claim_ledger", [])
        with self._lock:
            self._get_conn().execute(
                """INSERT OR REPLACE INTO research_runs
                   (session_id, question, status, report_path, artifact_dir, num_sources,
                    num_chunks, num_evidence, num_covered_claims, audit_json, capabilities_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.get("session_id", ""),
                    data.get("question", ""),
                    data.get("status", ""),
                    data.get("report_path", ""),
                    data.get("artifact_dir", ""),
                    len(data.get("sources", [])),
                    len(data.get("chunks", [])),
                    len(data.get("evidence", [])),
                    sum(item.get("status") == "covered" for item in ledger),
                    json.dumps(data.get("audit", {}), ensure_ascii=False),
                    json.dumps(data.get("capabilities", {}), ensure_ascii=False),
                ),
            )
            self._get_conn().commit()
        return str(data.get("session_id", ""))

    def research_history(self, query_hint: str = "", limit: int = 20) -> List[Dict]:
        self.index_artifact_history()
        if query_hint:
            rows = self._get_conn().execute(
                "SELECT * FROM research_runs WHERE question LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query_hint}%", limit),
            ).fetchall()
        else:
            rows = self._get_conn().execute(
                "SELECT * FROM research_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def index_artifact_history(self) -> int:
        """Recover run history from report manifests when a previous DB write failed."""
        output_root = PROJECT_ROOT / "outputs" / "research_sessions"
        if not output_root.exists():
            return 0
        indexed = 0
        for manifest_path in output_root.glob("*/report_manifest.json"):
            try:
                item = json.loads(manifest_path.read_text(encoding="utf-8"))
                rounds = item.get("rounds", [])
                last_round = rounds[-1] if rounds else {}
                session_id = str(item.get("session_id") or manifest_path.parent.name.split("-", 1)[0])
                with self._lock:
                    self._get_conn().execute(
                        """INSERT OR IGNORE INTO research_runs
                           (session_id, question, status, report_path, artifact_dir, num_sources,
                            num_chunks, num_evidence, num_covered_claims, audit_json, capabilities_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            session_id,
                            item.get("question", ""),
                            item.get("status", ""),
                            item.get("report_path", str(manifest_path.parent / "research_report.md")),
                            str(manifest_path.parent),
                            int(last_round.get("sources", 0)),
                            int(last_round.get("chunks", 0)),
                            int(last_round.get("evidence", 0)),
                            int(last_round.get("covered_claims", 0)),
                            json.dumps(item.get("audit", {}), ensure_ascii=False),
                            json.dumps(item.get("capabilities", {}), ensure_ascii=False),
                        ),
                    )
                    self._get_conn().commit()
                indexed += 1
            except Exception:
                continue
        return indexed

    # ═══════════ Maintenance ═══════════

    def stats(self) -> Dict:
        conn = self._get_conn()
        return {
            "total_papers": conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
            "total_searches": conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0],
            "total_search_cache": conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0],
            "search_cache_hits": conn.execute("SELECT COALESCE(SUM(hit_count),0) FROM search_cache").fetchone()[0],
            "total_fulltext": conn.execute("SELECT COUNT(*) FROM fulltext").fetchone()[0],
            "total_sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "total_research_runs": conn.execute("SELECT COUNT(*) FROM research_runs").fetchone()[0],
            "db_size_mb": round(self.db_path.stat().st_size / (1024 * 1024), 2) if self.db_path.exists() else 0,
        }

    def vacuum(self):
        self._get_conn().execute("VACUUM")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# Singleton
_cache: Optional[ResearchCache] = None


def get_cache() -> ResearchCache:
    global _cache
    if _cache is None:
        _cache = ResearchCache()
    return _cache
