"""Persistent, version-independent cache for online Semantic Scholar calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_S2_CACHE = PROJECT_ROOT / "database" / "s2_cache" / "s2_online_cache.sqlite"


def utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def canonical_request_key(
    method: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    *,
    schema_version: str = "v1",
) -> str:
    payload = {
        "method": method.upper(),
        "endpoint": endpoint,
        "params": params or {},
        "body": body or {},
        "schema_version": schema_version,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class CacheLookup:
    hit: bool
    payload: Any = None
    status_code: int = 0
    age_seconds: float = 0.0
    negative: bool = False


class S2PersistentCache:
    """SQLite cache that survives code upgrades and keeps raw response metadata."""

    def __init__(self, path: str | Path = DEFAULT_S2_CACHE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS s2_cache (
                    cache_key TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    body_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    negative INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    schema_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_s2_cache_endpoint
                    ON s2_cache(endpoint);
                CREATE INDEX IF NOT EXISTS idx_s2_cache_expires
                    ON s2_cache(expires_at);
                CREATE TABLE IF NOT EXISTS s2_request_metrics (
                    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT,
                    endpoint TEXT NOT NULL,
                    status_category TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    wait_seconds REAL NOT NULL,
                    retry_count INTEGER NOT NULL,
                    key_slot INTEGER,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS s2_rate_limit (
                    scope TEXT PRIMARY KEY,
                    next_allowed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS s2_precise_empty_index (
                    paper_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    first_confirmed_at REAL NOT NULL,
                    last_confirmed_at REAL NOT NULL,
                    confirm_count INTEGER NOT NULL DEFAULT 1
                );
                """
            )

    def reserve_request_slot(
        self,
        *,
        min_interval_seconds: float,
        scope: str = "semantic_scholar_all_endpoints",
        now: float | None = None,
    ) -> float:
        """Reserve one cross-process request slot and return its wait time.

        Semantic Scholar documents one introductory request rate across its
        endpoint families.  Storing the next slot in the shared cache database
        makes independently constructed transports and worker processes obey
        the same schedule instead of each believing it owns a separate quota.
        """

        interval = max(0.0, float(min_interval_seconds))
        if interval <= 0:
            return 0.0
        current = utc_timestamp() if now is None else float(now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT next_allowed_at FROM s2_rate_limit WHERE scope=?",
                (scope,),
            ).fetchone()
            reserved_at = max(
                current,
                float(row["next_allowed_at"]) if row is not None else current,
            )
            conn.execute(
                """
                INSERT INTO s2_rate_limit(scope,next_allowed_at) VALUES(?,?)
                ON CONFLICT(scope) DO UPDATE SET
                    next_allowed_at=excluded.next_allowed_at
                """,
                (scope, reserved_at + interval),
            )
        return max(0.0, reserved_at - current)

    def reserve_lane_slot(
        self,
        *,
        lane_id: str,
        min_interval_seconds: float,
        now: float | None = None,
    ) -> float:
        """Reserve one per-key cross-process pacing slot by non-secret lane id.

        The lane id is a stable SHA-256 prefix (``lane:<16 hex>``) and never
        contains key material.  This gives independently constructed
        transports in different processes the same per-key schedule while the
        public/no-key path keeps the single conservative global scope.
        """

        if not isinstance(lane_id, str) or not lane_id.startswith("lane:"):
            raise ValueError("lane_id must be a non-secret lane identifier")
        suffix = lane_id[len("lane:") :]
        if len(suffix) != 16 or any(
            char not in "0123456789abcdef" for char in suffix
        ):
            raise ValueError("lane_id must be a stable SHA-256 prefix")
        return self.reserve_request_slot(
            min_interval_seconds=min_interval_seconds,
            scope=lane_id,
            now=now,
        )

    def get(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        *,
        schema_version: str = "v1",
        now: float | None = None,
    ) -> CacheLookup:
        key = canonical_request_key(
            method, endpoint, params, body, schema_version=schema_version
        )
        current = utc_timestamp() if now is None else now
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM s2_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if row is None or float(row["expires_at"]) <= current:
            return CacheLookup(hit=False)
        try:
            payload = json.loads(str(row["response_json"]))
        except json.JSONDecodeError:
            return CacheLookup(hit=False)
        return CacheLookup(
            hit=True,
            payload=payload,
            status_code=int(row["status_code"]),
            age_seconds=max(0.0, current - float(row["created_at"])),
            negative=bool(row["negative"]),
        )

    def put(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        *,
        status_code: int,
        payload: Any,
        ttl_seconds: float,
        schema_version: str = "v1",
        negative: bool = False,
    ) -> str:
        key = canonical_request_key(
            method, endpoint, params, body, schema_version=schema_version
        )
        now = utc_timestamp()
        body_hash = hashlib.sha256(
            json.dumps(body or {}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        response_json = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO s2_cache(
                    cache_key,method,endpoint,params_json,body_hash,status_code,
                    response_json,negative,created_at,expires_at,schema_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status_code=excluded.status_code,
                    response_json=excluded.response_json,
                    negative=excluded.negative,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    schema_version=excluded.schema_version
                """,
                (
                    key,
                    method.upper(),
                    endpoint,
                    json.dumps(params or {}, sort_keys=True, default=str),
                    body_hash,
                    int(status_code),
                    response_json,
                    1 if negative else 0,
                    now,
                    now + max(1.0, float(ttl_seconds)),
                    schema_version,
                ),
            )
        return key


    def record_precise_empty(
        self,
        paper_key: str,
        *,
        title: str = "",
        now: float | None = None,
    ) -> None:
        """Persist that one paper has no S2 snippet-index body text.

        The precise/snippet lookup for such papers is a guaranteed zero
        result, so remembering it keeps later runs from paying the same
        request cost again (backend-fix ticket 1.1/1.2).
        """

        key = str(paper_key or "").strip()
        if not key:
            return
        current = utc_timestamp() if now is None else float(now)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO s2_precise_empty_index(
                    paper_key,title,first_confirmed_at,last_confirmed_at,confirm_count
                ) VALUES(?,?,?,?,1)
                ON CONFLICT(paper_key) DO UPDATE SET
                    last_confirmed_at=excluded.last_confirmed_at,
                    confirm_count=s2_precise_empty_index.confirm_count + 1
                """,
                (key, str(title or ""), current, current),
            )

    def precise_empty_confirmed_since(self, since_ts: float) -> set[str]:
        """Return paper keys confirmed empty at or after *since_ts*.

        Keys confirmed before the cutoff are deliberately omitted so a
        paper can be re-checked once S2 may have indexed new full text.
        """

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT paper_key FROM s2_precise_empty_index "
                "WHERE last_confirmed_at >= ?",
                (float(since_ts),),
            ).fetchall()
        return {str(row["paper_key"]) for row in rows}
    def record_metric(
        self,
        *,
        cache_key: str,
        endpoint: str,
        status_category: str,
        status_code: int,
        elapsed_seconds: float,
        wait_seconds: float,
        retry_count: int,
        key_slot: int | None,
        cache_hit: bool,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO s2_request_metrics(
                    cache_key,endpoint,status_category,status_code,elapsed_seconds,
                    wait_seconds,retry_count,key_slot,cache_hit,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cache_key,
                    endpoint,
                    status_category,
                    int(status_code),
                    float(elapsed_seconds),
                    float(wait_seconds),
                    int(retry_count),
                    key_slot,
                    1 if cache_hit else 0,
                    utc_timestamp(),
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(conn.execute("SELECT count(*) FROM s2_cache").fetchone()[0])
            metrics = int(
                conn.execute("SELECT count(*) FROM s2_request_metrics").fetchone()[0]
            )
            hits = int(
                conn.execute(
                    "SELECT count(*) FROM s2_request_metrics WHERE cache_hit=1"
                ).fetchone()[0]
            )
            waits = float(
                conn.execute(
                    "SELECT coalesce(sum(wait_seconds),0) FROM s2_request_metrics"
                ).fetchone()[0]
            )
        return {
            "cache_entries": total,
            "request_metrics": metrics,
            "cache_hits": hits,
            "wait_seconds": round(waits, 3),
            "path": str(self.path),
        }
