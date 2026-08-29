"""Small per-section overlays for a shared migrated knowledge base.

An overlay records section membership and conservative permission/scope
overrides.  It never contains a second copy of SQLite text.  The shared KB is
the only material store; overlays are lightweight routing metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .artifact_store import atomic_write_json


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def build_section_asset_overlay(
    *,
    section_id: str,
    sources: Iterable[dict[str, Any]],
    shared_kb_paths: Iterable[Path | str],
    output_path: Path,
) -> dict[str, Any]:
    """Write one section manifest without copying a database."""

    rows = [item for item in sources if isinstance(item, dict) and str(item.get("paper_id") or "").strip()]
    paper_ids = _unique(item.get("paper_id") for item in rows)
    chunk_ids = _unique(
        chunk_id
        for item in rows
        for chunk_id in item.get("canonical_chunk_ids") or []
    )
    paper_overrides: dict[str, dict[str, Any]] = {}
    chunk_overrides: dict[str, dict[str, Any]] = {}
    for item in rows:
        paper_id = str(item["paper_id"])
        paper_overrides[paper_id] = {
            "scope_fit": item.get("scope_fit", "unreviewed"),
            "use_permission": item.get("use_permission", "discovery_only"),
            "literature_role": item.get("literature_role", ""),
            "discovery_route": item.get("discovery_route", "legacy_unresolved"),
            "materialization_route": item.get("materialization_route", "not_materialized"),
        }
        for raw_chunk_id in item.get("canonical_chunk_ids") or []:
            chunk_id = str(raw_chunk_id).strip()
            if chunk_id:
                chunk_overrides[chunk_id] = {
                    "paper_id": paper_id,
                    "scope_fit": item.get("scope_fit", "unreviewed"),
                    "use_permission": item.get("use_permission", "discovery_only"),
                    "literature_role": item.get("literature_role", ""),
                }
    payload = {
        "schema_version": "research_harness.section_asset_overlay.r3_3.v1",
        "section_id": str(section_id),
        "shared_kb_paths": [str(Path(item)) for item in shared_kb_paths],
        "paper_ids": paper_ids,
        "chunk_ids": chunk_ids,
        "section_chunk_reference_count": len(chunk_ids),
        "paper_overrides": paper_overrides,
        "chunk_overrides": chunk_overrides,
        "database_copy_count": 0,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, payload)
    return payload


def read_section_asset_overlay(path: Path | None) -> dict[str, Any]:
    if path is None or not Path(path).exists():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
