"""Run-level, provenance-aware coverage deduplication.

The section ledgers remain the authoritative local work products.  This small
ledger is only a run-level cache of deterministic discovery and audit facts;
it never turns metadata into evidence and never removes a candidate from a
later section merely because another section used it.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .artifact_store import atomic_write_json


def _read(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not Path(path).is_file():
        return {}
    try:
        import json

        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _key(*parts: Any) -> str:
    raw = "\x1f".join(" ".join(str(part or "").split()).casefold() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load(path: Optional[Path]) -> Dict[str, Any]:
    value = _read(path)
    value.setdefault("schema_version", "research_harness.coverage_ledger.v1")
    value.setdefault("queries", {})
    value.setdefault("audits", {})
    value.setdefault("materials", {})
    value.setdefault("stats", {})
    return value


def save(path: Optional[Path], ledger: Dict[str, Any]) -> None:
    if not path:
        return
    ledger["updated_at_epoch"] = round(time.time(), 3)
    atomic_write_json(Path(path), ledger)


def query_key(topic_fingerprint: str, role: str, query: str) -> str:
    return _key("query", topic_fingerprint, role, query)


def material_key(topic_fingerprint: str, role: str, identity: str) -> str:
    return _key("material", topic_fingerprint, role, identity)


def audit_key(topic_fingerprint: str, role: str, identity: str) -> str:
    return _key("audit", topic_fingerprint, role, identity)


def get_query(path: Optional[Path], topic_fingerprint: str, role: str, query: str) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    return load(path).get("queries", {}).get(query_key(topic_fingerprint, role, query))


def record_query(
    path: Optional[Path],
    *,
    topic_fingerprint: str,
    role: str,
    query: str,
    candidates: Iterable[Dict[str, Any]],
) -> None:
    if not path:
        return
    ledger = load(path)
    rows = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        # Keep the cache compact and portable.  The source route and abstract
        # are retained because they are needed to inspect a replayed candidate.
        rows.append({
            key: item.get(key)
            for key in (
                "title", "doi", "year", "venue", "abstract", "tldr",
                "is_oa", "pdf_url", "oa_url", "open_access_url", "html_url",
                "repository_url", "semantic_scholar_id", "semantic_scholar_paper_id",
                "corpus_id", "citation_count", "relevance_score", "backends",
                "raw_metadata", "material_identity",
            )
            if key in item
        })
    key = query_key(topic_fingerprint, role, query)
    previous = ledger["queries"].get(key, {})
    ledger["queries"][key] = {
        "topic_fingerprint": topic_fingerprint,
        "role": role,
        "query": query,
        "candidates": rows or list(previous.get("candidates") or []),
        "search_count": int(previous.get("search_count", 0) or 0) + 1,
        "last_status": "completed",
    }
    ledger["stats"]["query_cache_writes"] = int(ledger["stats"].get("query_cache_writes", 0) or 0) + 1
    save(path, ledger)


def get_audit(path: Optional[Path], topic_fingerprint: str, role: str, identity: str) -> Optional[Dict[str, Any]]:
    if not path or not identity:
        return None
    return load(path).get("audits", {}).get(audit_key(topic_fingerprint, role, identity))


def record_audit(
    path: Optional[Path],
    *,
    topic_fingerprint: str,
    role: str,
    identity: str,
    decision: str,
    scope_fit: str,
    role_fit: list[str],
    audit_reason: str,
    not_usable_for: list[str],
) -> None:
    if not path or not identity:
        return
    ledger = load(path)
    key = audit_key(topic_fingerprint, role, identity)
    ledger["audits"][key] = {
        "topic_fingerprint": topic_fingerprint,
        "role": role,
        "material_identity": identity,
        "decision": decision,
        "scope_fit": scope_fit,
        "role_fit": list(role_fit),
        "audit_reason": audit_reason,
        "not_usable_for": list(not_usable_for),
        "cacheable": True,
    }
    ledger["stats"]["audit_cache_writes"] = int(ledger["stats"].get("audit_cache_writes", 0) or 0) + 1
    save(path, ledger)


def record_material(path: Optional[Path], *, topic_fingerprint: str, role: str, identity: str, paper_id: str, chunk_ids: list[str]) -> None:
    if not path or not identity:
        return
    ledger = load(path)
    ledger["materials"][material_key(topic_fingerprint, role, identity)] = {
        "topic_fingerprint": topic_fingerprint,
        "role": role,
        "material_identity": identity,
        "paper_id": paper_id,
        "chunk_ids": list(dict.fromkeys(chunk_ids)),
    }
    save(path, ledger)


def increment_stat(path: Optional[Path], name: str, amount: int = 1) -> None:
    if not path:
        return
    ledger = load(path)
    ledger["stats"][name] = int(ledger["stats"].get(name, 0) or 0) + int(amount)
    save(path, ledger)

