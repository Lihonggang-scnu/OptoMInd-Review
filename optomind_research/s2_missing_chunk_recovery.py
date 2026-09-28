"""Recover missing S2 chunk IDs from persistent cache or the S2 snippet API.

This module never fabricates text.  A missing chunk is either recovered from a
cached/API response with the same deterministic ID or marked ``unavailable``
and excluded from writing material.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from optomind_research.s2_cache import S2PersistentCache
from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway
from optomind_research.s2_text_chunk_retriever import (
    S2TextChunkRetriever,
    _snippet_chunk_id,
)


@dataclass(slots=True)
class MissingChunkRecoveryResult:
    requested_ids: list[str] = field(default_factory=list)
    recovered_ids: list[str] = field(default_factory=list)
    unavailable_ids: list[str] = field(default_factory=list)
    source_by_id: dict[str, str] = field(default_factory=dict)
    api_calls: int = 0
    cache_hits: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    recovered_chunks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_chunk_id(chunk_id: str) -> tuple[str, int | None, int | None]:
    parts = str(chunk_id or "").split(":")
    if len(parts) < 5 or parts[0] != "s2chunk":
        return "", None, None
    try:
        start = int(parts[2])
    except (TypeError, ValueError):
        start = None
    try:
        end = int(parts[3])
    except (TypeError, ValueError):
        end = None
    return parts[1], start, end


def _cached_snippet_items(
    cache: S2PersistentCache,
    requested_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Read cached snippet responses without issuing a network request."""

    found: dict[str, dict[str, Any]] = {}
    try:
        with cache._connect() as conn:  # shared cache is an intentional API
            rows = conn.execute(
                "SELECT response_json FROM s2_cache "
                "WHERE endpoint LIKE '%/snippet/search%' AND expires_at>?",
                (__import__("time").time(),),
            ).fetchall()
    except Exception:
        return found
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
        except Exception:
            continue
        for item in (payload.get("data") or []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            paper = item.get("paper") or {}
            offset = snippet.get("snippetOffset") or {}
            chunk_id = _snippet_chunk_id(
                corpus_id=paper.get("corpusId"),
                start=offset.get("start"),
                end=offset.get("end"),
                text=str(snippet.get("text") or ""),
            )
            if chunk_id in requested_ids:
                found[chunk_id] = item
    return found


def recover_missing_chunks(
    chunk_ids: Iterable[str],
    *,
    cache: S2PersistentCache | None = None,
    gateway: S2IntelligenceGateway | None = None,
    query: str = "",
    paper_ids: Iterable[str] = (),
    min_chars: int = 100,
    max_api_calls: int = 1,
) -> MissingChunkRecoveryResult:
    """Recover exact IDs, preferring cache and failing closed."""

    requested = list(dict.fromkeys(str(item).strip() for item in chunk_ids if str(item).strip()))
    result = MissingChunkRecoveryResult(requested_ids=requested)
    if not requested:
        return result
    cache = cache or S2PersistentCache()
    cached = _cached_snippet_items(cache, set(requested))
    for chunk_id, item in cached.items():
        result.recovered_ids.append(chunk_id)
        result.source_by_id[chunk_id] = "persistent_cache"
        result.cache_hits += 1
        result.recovered_chunks.append(item)

    remaining = [item for item in requested if item not in cached]
    if remaining and gateway is not None and query and max_api_calls > 0:
        result.api_calls += 1
        try:
            retriever = S2TextChunkRetriever(gateway, min_chars=min_chars)
            retrieved = retriever.retrieve(
                [query],
                paper_ids=list(paper_ids) or None,
                limit_per_query=50,
            )
            by_id = {chunk.chunk_id: chunk for chunk in retrieved.accepted_chunks}
            for chunk_id in list(remaining):
                chunk = by_id.get(chunk_id)
                if chunk is not None:
                    result.recovered_ids.append(chunk_id)
                    result.source_by_id[chunk_id] = "s2_api"
                    result.recovered_chunks.append(chunk.to_dict())
        except Exception as exc:
            result.errors.append({"stage": "s2_api", "error": str(exc)[:300]})
    result.recovered_ids = list(dict.fromkeys(result.recovered_ids))
    result.unavailable_ids = [
        item for item in requested if item not in set(result.recovered_ids)
    ]
    return result


def write_recovered_chunks_jsonl(
    chunks: Iterable[dict[str, Any]],
    path: str | Path,
) -> Path:
    """Persist recovered chunks as an auditable handoff, preserving raw text."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            if isinstance(chunk, dict):
                handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return output

