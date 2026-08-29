"""Local semantic-vector cache for durable OptoMind material units."""

from __future__ import annotations

import math
import sqlite3
import struct
import json
import time
import heapq
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .material_unit_store import content_hash

SCHEMA_VERSION = "optomind.material_semantic_cache.v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_REPRESENTATION_VERSION = "material-unit-surrogate.v1"
Embedder = Callable[[list[str]], list[list[float]]]
DEFAULT_EMBEDDING_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pack(values: Iterable[float]) -> bytes:
    vals = [float(v) for v in values]
    return struct.pack("<" + "f" * len(vals), *vals)


def _unpack(blob: bytes) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack("<" + "f" * (len(blob) // 4), blob))


def _normalize(values: Iterable[float]) -> list[float]:
    vals = [float(v) for v in values]
    norm = math.sqrt(sum(v * v for v in vals))
    return vals if norm < 1e-12 else [v / norm for v in vals]


def _surrogate(unit: Mapping[str, Any]) -> str:
    durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), Mapping) else {}
    card = unit.get("durable_content_card") if isinstance(unit.get("durable_content_card"), Mapping) else {}
    identity = unit.get("identity") if isinstance(unit.get("identity"), Mapping) else {}
    parts = [
        str(identity.get("title") or ""),
        str(durable.get("normalized_text") or durable.get("caption") or ""),
        str(card.get("observable_content") or ""),
    ]
    return " ".join(part.strip() for part in parts if part.strip())


def dashscope_embedder(
    texts: list[str],
    *,
    model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    base_url: str = DEFAULT_EMBEDDING_URL,
    timeout_seconds: float = 120.0,
    max_retries: int = 1,
    batch_size: int = 10,
    usage_accumulator: dict[str, int] | None = None,
) -> list[list[float]]:
    """Embed text batches through DashScope's OpenAI-compatible endpoint."""
    if not texts:
        return []
    if api_key is None:
        from config.qwen_config import get_qwen_client_config
        cfg = get_qwen_client_config("cheap_model")
        candidates = cfg.get("api_key_candidates") or []
        api_key = str((candidates[0] if candidates else {}).get("api_key") or cfg.get("api_key") or "")
    if not api_key:
        raise RuntimeError("No DashScope embedding API key is configured")
    vectors: list[list[float]] = []
    size = max(1, int(batch_size))
    for start in range(0, len(texts), size):
        batch = [str(value or "") for value in texts[start : start + size]]
        request = urllib.request.Request(
            url=base_url.rstrip("/"),
            data=json.dumps({"model": model, "input": batch, "encoding_format": "float"}, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error = ""
        for attempt in range(max(0, int(max_retries)) + 1):
            try:
                with urllib.request.urlopen(request, timeout=max(5.0, float(timeout_seconds))) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                rows = payload.get("data") if isinstance(payload, Mapping) else None
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise ValueError("embedding response count does not match request batch")
                ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
                batch_vectors = [row.get("embedding") for row in ordered]
                if any(not isinstance(vector, list) or not vector for vector in batch_vectors):
                    raise ValueError("embedding response contains an empty vector")
                vectors.extend([[float(value) for value in vector] for vector in batch_vectors])
                if usage_accumulator is not None:
                    usage = payload.get("usage") if isinstance(payload, Mapping) else {}
                    usage = usage if isinstance(usage, Mapping) else {}
                    input_tokens = int(
                        usage.get("prompt_tokens")
                        or usage.get("input_tokens")
                        or usage.get("total_tokens")
                        or 0
                    )
                    usage_accumulator["input_tokens"] = int(
                        usage_accumulator.get("input_tokens", 0)
                    ) + input_tokens
                    usage_accumulator["request_count"] = int(
                        usage_accumulator.get("request_count", 0)
                    ) + 1
                break
            except Exception as exc:
                last_error = type(exc).__name__
                if attempt >= max(0, int(max_retries)):
                    raise RuntimeError(f"DashScope embedding request failed: {last_error}") from None
                time.sleep(0.5 * (attempt + 1))
    return vectors


class MaterialSemanticCache:
    """SQLite-backed, resumable vectors with local cosine retrieval."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = bool(readonly)
        if self.readonly:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        if not self.readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS semantic_vectors (
            unit_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            representation_version TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector BLOB NOT NULL,
            surrogate TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (unit_id, content_hash, embedding_model, representation_version)
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_vectors_hash
            ON semantic_vectors(content_hash, embedding_model, representation_version);
        """)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MaterialSemanticCache":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def has(self, unit_id: str, content_hash_value: str, *, embedding_model: str = DEFAULT_EMBEDDING_MODEL, representation_version: str = DEFAULT_REPRESENTATION_VERSION) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM semantic_vectors WHERE unit_id=? AND content_hash=? AND embedding_model=? AND representation_version=?",
            (unit_id, content_hash_value, embedding_model, representation_version),
        ).fetchone()
        return row is not None

    def put(self, unit: Mapping[str, Any], vector: Iterable[float], *, embedding_model: str = DEFAULT_EMBEDDING_MODEL, representation_version: str = DEFAULT_REPRESENTATION_VERSION, surrogate: str | None = None) -> None:
        durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), Mapping) else {}
        chash = str(durable.get("content_hash") or content_hash(_surrogate(unit)))
        values = _normalize(vector)
        if not values:
            raise ValueError("embedding vector cannot be empty")
        now = _now()
        self._conn.execute(
            """
            INSERT INTO semantic_vectors(unit_id, content_hash, embedding_model, representation_version, dimension, vector, surrogate, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id, content_hash, embedding_model, representation_version) DO UPDATE SET
              dimension=excluded.dimension, vector=excluded.vector, surrogate=excluded.surrogate, updated_at=excluded.updated_at
            """,
            (str(unit.get("unit_id") or ""), chash, embedding_model, representation_version, len(values), _pack(values), surrogate or _surrogate(unit), now, now),
        )
        self._conn.commit()

    def ensure_units(self, units: Iterable[Mapping[str, Any]], embedder: Embedder, *, embedding_model: str = DEFAULT_EMBEDDING_MODEL, representation_version: str = DEFAULT_REPRESENTATION_VERSION, batch_size: int = 32) -> dict[str, int]:
        pending = []
        reused = 0
        for unit in units:
            durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), Mapping) else {}
            chash = str(durable.get("content_hash") or content_hash(_surrogate(unit)))
            if self.has(str(unit.get("unit_id") or ""), chash, embedding_model=embedding_model, representation_version=representation_version):
                reused += 1
            else:
                pending.append((unit, _surrogate(unit)))
        embedded = 0
        for start in range(0, len(pending), max(1, int(batch_size))):
            batch = pending[start : start + max(1, int(batch_size))]
            vectors = embedder([text for _, text in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedder returned a different number of vectors")
            for (unit, surrogate), vector in zip(batch, vectors):
                self.put(unit, vector, embedding_model=embedding_model, representation_version=representation_version, surrogate=surrogate)
                embedded += 1
        return {"requested": reused + embedded, "reused": reused, "embedded": embedded}

    def ensure_units_parallel(
        self,
        units: Iterable[Mapping[str, Any]],
        embedder: Embedder,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        representation_version: str = DEFAULT_REPRESENTATION_VERSION,
        batch_size: int = 32,
        workers: int = 4,
    ) -> dict[str, int]:
        """Embed pending units with bounded network concurrency.

        The embedder runs in worker threads, while SQLite writes happen in the
        calling thread. Completed batches are committed immediately, so a
        later invocation resumes without re-requesting them.
        """
        pending: list[tuple[Mapping[str, Any], str]] = []
        reused = 0
        for unit in units:
            durable = unit.get("durable_content") if isinstance(unit.get("durable_content"), Mapping) else {}
            chash = str(durable.get("content_hash") or content_hash(_surrogate(unit)))
            if self.has(str(unit.get("unit_id") or ""), chash, embedding_model=embedding_model, representation_version=representation_version):
                reused += 1
            else:
                pending.append((unit, _surrogate(unit)))

        size = max(1, int(batch_size))
        batches = [pending[start : start + size] for start in range(0, len(pending), size)]

        def embed_batch(batch: list[tuple[Mapping[str, Any], str]]):
            vectors = embedder([text for _, text in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedder returned a different number of vectors")
            return batch, vectors

        embedded = 0
        if not batches:
            return {"requested": reused, "reused": reused, "embedded": 0}
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futures = [pool.submit(embed_batch, batch) for batch in batches]
            for future in as_completed(futures):
                batch, vectors = future.result()
                for (unit, surrogate), vector in zip(batch, vectors):
                    self.put(unit, vector, embedding_model=embedding_model, representation_version=representation_version, surrogate=surrogate)
                    embedded += 1
        return {"requested": reused + embedded, "reused": reused, "embedded": embedded}

    def search(self, query_vector: Iterable[float], *, top_k: int = 10, embedding_model: str = DEFAULT_EMBEDDING_MODEL, representation_version: str = DEFAULT_REPRESENTATION_VERSION) -> list[dict[str, Any]]:
        query = _normalize(query_vector)
        if not query:
            return []
        rows = self._conn.execute(
            "SELECT * FROM semantic_vectors WHERE embedding_model=? AND representation_version=?",
            (embedding_model, representation_version),
        ).fetchall()
        scored = []
        for row in rows:
            vector = _unpack(row["vector"])
            if len(vector) != len(query):
                continue
            scored.append((sum(a * b for a, b in zip(query, vector)), row))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1]["unit_id"])))
        return [
            {"unit_id": row["unit_id"], "content_hash": row["content_hash"], "score": float(score), "embedding_model": row["embedding_model"], "representation_version": row["representation_version"], "surrogate": row["surrogate"]}
            for score, row in scored[: max(0, int(top_k))]
        ]

    def search_many(
        self,
        query_vectors: Iterable[Iterable[float]],
        *,
        top_k: int = 10,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        representation_version: str = DEFAULT_REPRESENTATION_VERSION,
    ) -> list[list[dict[str, Any]]]:
        """Score many queries while reading and unpacking the corpus once."""

        queries = [_normalize(vector) for vector in query_vectors]
        if not queries:
            return []
        limit = max(0, int(top_k))
        if not limit:
            return [[] for _ in queries]
        heaps: list[list[tuple[float, str, str, sqlite3.Row]]] = [
            [] for _ in queries
        ]
        rows = self._conn.execute(
            "SELECT * FROM semantic_vectors WHERE embedding_model=? "
            "AND representation_version=?",
            (embedding_model, representation_version),
        ).fetchall()
        try:
            import numpy as np

            dimension = len(queries[0]) if queries and queries[0] else 0
            compatible = [
                row
                for row in rows
                if int(row["dimension"] or 0) == dimension
            ]
            if dimension and compatible and all(
                len(query) == dimension for query in queries
            ):
                matrix = np.vstack(
                    [
                        np.frombuffer(row["vector"], dtype="<f4")
                        for row in compatible
                    ]
                )
                query_matrix = np.asarray(queries, dtype=np.float32)
                scores = matrix @ query_matrix.T
                results: list[list[dict[str, Any]]] = []
                count = min(limit, len(compatible))
                for query_index in range(len(queries)):
                    column = scores[:, query_index]
                    indices = np.argpartition(-column, count - 1)[:count]
                    ordered = sorted(
                        (int(index) for index in indices),
                        key=lambda index: (
                            -float(column[index]),
                            str(compatible[index]["unit_id"]),
                        ),
                    )
                    results.append(
                        [
                            {
                                "unit_id": compatible[index]["unit_id"],
                                "content_hash": compatible[index][
                                    "content_hash"
                                ],
                                "score": float(column[index]),
                                "embedding_model": compatible[index][
                                    "embedding_model"
                                ],
                                "representation_version": compatible[index][
                                    "representation_version"
                                ],
                                "surrogate": compatible[index]["surrogate"],
                            }
                            for index in ordered
                        ]
                    )
                return results
        except (ImportError, ValueError, MemoryError):
            pass

        for row in rows:
            vector = _unpack(row["vector"])
            unit_id = str(row["unit_id"])
            for index, query in enumerate(queries):
                if not query or len(vector) != len(query):
                    continue
                score = float(sum(a * b for a, b in zip(query, vector)))
                candidate = (
                    score,
                    unit_id,
                    str(row["content_hash"]),
                    row,
                )
                heap = heaps[index]
                if len(heap) < limit:
                    heapq.heappush(heap, candidate)
                elif candidate[:3] > heap[0][:3]:
                    heapq.heapreplace(heap, candidate)
        results: list[list[dict[str, Any]]] = []
        for heap in heaps:
            ranked = sorted(heap, key=lambda item: (-item[0], item[1]))
            results.append(
                [
                    {
                        "unit_id": row["unit_id"],
                        "content_hash": row["content_hash"],
                        "score": score,
                        "embedding_model": row["embedding_model"],
                        "representation_version": row[
                            "representation_version"
                        ],
                        "surrogate": row["surrogate"],
                    }
                    for score, _unit_id, _content_hash, row in ranked
                ]
            )
        return results

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0])


__all__ = ["SCHEMA_VERSION", "DEFAULT_EMBEDDING_MODEL", "DEFAULT_REPRESENTATION_VERSION", "DEFAULT_EMBEDDING_URL", "dashscope_embedder", "MaterialSemanticCache"]
