"""Read-only, query-plan-driven local coverage assessment for S2 bootstrap.

This module is used exclusively by :mod:`optomind_research.s2_harness_bootstrap`
to decide whether a fresh harness run can reuse an already topic-scoped
persistent knowledge base before issuing any external S2 traffic.  It reuses
the same deterministic scope gate as the overlay stage, but never writes to
the base KB or the run-local overlay.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from optomind_research.runtime.topic_scoped_kb_stage import (
    TopicScopedKBStage,
    _meaningful_tokens,
    _permission_fields,
    _search_text_for_row,
    _table_names,
)


LOCAL_COVERAGE_SCHEMA_VERSION = "optomind.s2_local_coverage.v1"

_PRIMARY_DEPTHS = frozenset(
    {"fulltext", "partial_fulltext", "structured_snippet"}
)
_EVIDENCE_PERMISSIONS = frozenset(
    {"factual_support", "contextual_or_qualified_support"}
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _extract_roles(row: Mapping[str, Any]) -> list[str]:
    """Collect explicit role labels from structured columns and raw JSON."""

    roles: list[str] = []
    raw = _json_object(row.get("raw_json"))
    for key in ("literature_roles_json", "relation_roles_json"):
        value = row.get(key) or raw.get(key)
        if isinstance(value, str):
            parsed = _json_object({"value": value}).get("value")
            try:
                value = json.loads(str(parsed))
            except (TypeError, json.JSONDecodeError):
                value = []
        if isinstance(value, (list, tuple)):
            roles.extend(value)
    for key in (
        "literature_roles",
        "relation_roles",
        "roles",
        "role_labels",
        "requested_roles",
    ):
        value = raw.get(key) or row.get(key)
        if isinstance(value, (list, tuple)):
            roles.extend(value)
        elif isinstance(value, str) and value.strip():
            roles.append(value)
    route = _json_object(
        raw.get("route_provenance")
        or raw.get("route_provenance_json")
        or row.get("route_provenance_json")
    )
    for key in ("roles", "role_labels", "requested_roles"):
        value = route.get(key)
        if isinstance(value, (list, tuple)):
            roles.extend(value)
        elif isinstance(value, str) and value.strip():
            roles.append(value)
    return sorted(
        dict.fromkeys(
            str(role).strip().casefold()
            for role in roles
            if str(role).strip()
        )
    )


def _source_kind(row: Mapping[str, Any]) -> str:
    raw = _json_object(row.get("raw_json"))
    route = _json_object(
        raw.get("route_provenance")
        or raw.get("route_provenance_json")
        or row.get("route_provenance_json")
    )
    for key in ("source_kind", "text_provenance", "materialization_route"):
        value = row.get(key) or raw.get(key) or route.get(key)
        if value:
            return str(value).strip().casefold()
    return "unknown"


def _is_primary_usable(fields: Mapping[str, Any]) -> bool:
    return (
        str(fields.get("content_depth") or "") in _PRIMARY_DEPTHS
        and str(fields.get("use_permission") or "") in _EVIDENCE_PERMISSIONS
        and str(fields.get("scope_fit") or "") == "direct"
    )


def _matches(text: str, query_tokens: set[str]) -> bool:
    if not query_tokens:
        return True
    text_tokens = _meaningful_tokens(text)
    if not text_tokens:
        return False
    hits = len(query_tokens & text_tokens)
    if len(query_tokens) <= 2:
        return hits == len(query_tokens)
    return hits >= max(2, math.ceil(len(query_tokens) * 0.6))


def _coverage_entry(
    label: str,
    *,
    tokens: set[str],
    papers: Iterable[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    threshold_papers: int,
    threshold_chunks: int,
) -> dict[str, Any]:
    matching_papers = {
        str(paper.get("paper_id") or "")
        for paper in papers
        if _matches(str(paper.get("search_text") or ""), tokens)
    }
    matching_chunks = [
        chunk
        for chunk in chunks
        if _matches(str(chunk.get("search_text") or ""), tokens)
    ]
    matching_usable_papers = {
        str(chunk.get("paper_id") or "")
        for chunk in matching_chunks
        if str(chunk.get("paper_id") or "")
    }
    covered = (
        len(matching_usable_papers) >= max(1, int(threshold_papers))
        and len(matching_chunks) >= max(1, int(threshold_chunks))
    )
    return {
        "label": label,
        "tokens": sorted(tokens),
        "matching_paper_count": len(matching_papers),
        "matching_usable_paper_count": len(matching_usable_papers),
        "matching_usable_chunk_count": len(matching_chunks),
        "threshold_papers": max(1, int(threshold_papers)),
        "threshold_chunks": max(1, int(threshold_chunks)),
        "covered": covered,
    }


def assess_local_coverage(
    *,
    base_kb_sqlite: Path,
    policy: Any,
    scope_contract: Any,
    search_queries: list[str],
    requested_roles: Iterable[str],
) -> dict[str, Any]:
    """Assess how much of the current query plan the base KB already covers.

    The scan is read-only and uses the same identity/scope gate that the
    overlay stage applies, so a ``sufficient`` decision means the fresh run
    can seal normal artifacts without any external S2 traffic.
    """

    base = Path(base_kb_sqlite)
    roles = sorted(
        dict.fromkeys(str(role).strip().casefold() for role in requested_roles)
    )
    queries = [
        str(query).strip()
        for query in search_queries
        if str(query).strip()
    ]
    hard_papers = max(1, int(policy.minimum_factual_papers))
    hard_chunks = max(1, int(policy.minimum_factual_chunks))
    target_papers = max(hard_papers, int(policy.minimum_target_papers))
    target_chunks = max(hard_chunks, int(policy.minimum_factual_chunks))
    per_query_papers = max(
        1, math.ceil(target_papers / max(1, len(queries)))
    )
    per_query_chunks = max(
        1, math.ceil(target_chunks / max(1, len(queries)))
    )
    per_role_papers = (
        max(1, math.ceil(target_papers / max(1, len(roles))))
        if roles
        else 0
    )
    thresholds = {
        "hard_minimum_papers": hard_papers,
        "hard_minimum_chunks": hard_chunks,
        "target_papers": target_papers,
        "target_usable_chunks": target_chunks,
        "per_query_papers": per_query_papers,
        "per_query_chunks": per_query_chunks,
        "per_role_papers": per_role_papers,
    }

    def insufficient(reason: str) -> dict[str, Any]:
        return {
            "schema_version": LOCAL_COVERAGE_SCHEMA_VERSION,
            "decision": "insufficient",
            "reason": reason,
            "scope_contract_sha256": scope_contract.contract_sha256,
            "thresholds": thresholds,
            "counts": {
                "relevant_unique_papers": 0,
                "papers_with_usable_chunks": 0,
                "total_usable_chunks": 0,
                "weak_chunk_count": 0,
                "papers_with_weak_chunks_only": 0,
            },
            "query_coverage": [],
            "lens_coverage": [],
            "role_coverage": {
                "metadata_available": False,
                "roles": [],
                "missing_roles": [],
            },
            "source_kind_diversity": {
                "distinct_source_kinds": [],
                "source_kind_counts": {},
            },
            "missing_queries": list(queries),
            "missing_lenses": list(scope_contract.lenses),
            "missing_roles": [],
            "covered_queries": [],
            "covered_lenses": [],
            "covered_roles": [],
            "reused_paper_ids": [],
            "reused_chunk_ids": [],
            "why": [reason],
        }

    if not base.is_file():
        return insufficient("base_kb_missing_or_unreadable")

    conn = sqlite3.connect(f"file:{base.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        names = _table_names(conn)
        stage = TopicScopedKBStage(
            query_plan_path=base.parent / "_s2_local_coverage_unused.json",
            base_kb_sqlite=base,
            work_dir=base.parent,
            policy=policy,
            scope_contract=scope_contract,
        )
        selected, selection_report = stage._select_base_rows(conn)
    finally:
        conn.close()

    paper_table = names.get("papers")
    chunk_table = names.get("text_chunks")
    paper_rows = selected.get(paper_table, []) if paper_table else []
    chunk_rows = selected.get(chunk_table, []) if chunk_table else []

    paper_by_id: dict[str, Mapping[str, Any]] = {}
    for row in paper_rows:
        paper_id = str(row.get("paper_id") or "").strip()
        if paper_id:
            paper_by_id[paper_id] = row

    paper_entries: dict[str, dict[str, Any]] = {}
    for paper_id, row in paper_by_id.items():
        decision = stage._paper_decisions.get(paper_id, {})
        paper_entries[paper_id] = {
            "paper_id": paper_id,
            "scope_fit": str(decision.get("scope_fit") or "direct"),
            "roles": _extract_roles(row),
            "search_text": _search_text_for_row(row, table="papers"),
        }

    usable_chunks: list[dict[str, Any]] = []
    weak_chunks: list[dict[str, Any]] = []
    chunk_roles_by_paper: dict[str, set[str]] = {}
    for chunk in chunk_rows:
        paper_id = str(chunk.get("paper_id") or "").strip()
        if paper_id not in paper_entries:
            continue
        fields = _permission_fields(
            chunk,
            table="text_chunks",
            scope_fit=str(
                paper_entries[paper_id].get("scope_fit") or "direct"
            ),
        )
        entry = {
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "paper_id": paper_id,
            "content_depth": str(fields.get("content_depth") or ""),
            "use_permission": str(fields.get("use_permission") or ""),
            "scope_fit": str(fields.get("scope_fit") or ""),
            "source_kind": _source_kind(chunk),
            "roles": _extract_roles(chunk),
            "search_text": _search_text_for_row(chunk, table="text_chunks"),
        }
        if _is_primary_usable(fields):
            usable_chunks.append(entry)
        else:
            weak_chunks.append(entry)
        chunk_roles_by_paper.setdefault(paper_id, set()).update(entry["roles"])

    usable_paper_ids = {
        str(chunk.get("paper_id") or "")
        for chunk in usable_chunks
        if str(chunk.get("paper_id") or "")
    }
    weak_paper_ids = {
        str(chunk.get("paper_id") or "")
        for chunk in weak_chunks
        if str(chunk.get("paper_id") or "")
    }
    papers_with_weak_chunks_only = len(weak_paper_ids - usable_paper_ids)

    source_kinds: Counter[str] = Counter(
        str(chunk.get("source_kind") or "unknown") for chunk in usable_chunks
    )

    query_coverage = [
        _coverage_entry(
            query,
            tokens=_meaningful_tokens(query),
            papers=paper_entries.values(),
            chunks=usable_chunks,
            threshold_papers=per_query_papers,
            threshold_chunks=per_query_chunks,
        )
        for query in queries
    ]
    lens_coverage = [
        _coverage_entry(
            lens,
            tokens=_meaningful_tokens(lens),
            papers=paper_entries.values(),
            chunks=usable_chunks,
            threshold_papers=per_query_papers,
            threshold_chunks=per_query_chunks,
        )
        for lens in scope_contract.lenses
    ]

    role_metadata_available = bool(
        any(entry["roles"] for entry in paper_entries.values())
        or any(entry["roles"] for entry in usable_chunks)
    )
    role_counts: Counter[str] = Counter()
    for paper_id in usable_paper_ids:
        combined = set(paper_entries.get(paper_id, {}).get("roles") or [])
        combined.update(chunk_roles_by_paper.get(paper_id, set()))
        for role in roles:
            if role in combined:
                role_counts[role] += 1
    role_entries = [
        {
            "role": role,
            "matching_usable_paper_count": role_counts.get(role, 0),
            "threshold_papers": max(1, int(per_role_papers)),
            "covered": bool(
                role_metadata_available
                and role_counts.get(role, 0) >= max(1, int(per_role_papers))
            ),
        }
        for role in roles
    ]
    missing_roles = (
        [entry["role"] for entry in role_entries if not entry["covered"]]
        if role_metadata_available
        else []
    )
    missing_queries = [
        entry["label"] for entry in query_coverage if not entry["covered"]
    ]
    missing_lenses = [
        entry["label"] for entry in lens_coverage if not entry["covered"]
    ]
    covered_queries = [
        entry["label"] for entry in query_coverage if entry["covered"]
    ]
    covered_lenses = [
        entry["label"] for entry in lens_coverage if entry["covered"]
    ]
    covered_roles = [
        entry["role"] for entry in role_entries if entry["covered"]
    ]

    relevant_unique_papers = len(paper_entries)
    papers_with_usable_chunks = len(usable_paper_ids)
    total_usable_chunks = len(usable_chunks)
    if (
        papers_with_usable_chunks < hard_papers
        or total_usable_chunks < hard_chunks
    ):
        decision = "insufficient"
        why = [
            (
                f"local evidence below hard minimums: "
                f"{papers_with_usable_chunks} usable-chunk papers < "
                f"{hard_papers}, {total_usable_chunks} usable chunks < "
                f"{hard_chunks}"
            )
        ]
    elif (
        papers_with_usable_chunks >= target_papers
        and total_usable_chunks >= target_chunks
        and not missing_queries
        and not missing_lenses
        and not missing_roles
    ):
        decision = "sufficient"
        why = [
            (
                f"local coverage meets contract-derived thresholds: "
                f"{papers_with_usable_chunks}/{target_papers} usable-chunk "
                f"papers, {total_usable_chunks}/{target_chunks} usable "
                f"chunks, {len(covered_queries)}/{len(queries)} queries, "
                f"{len(covered_lenses)}/{len(scope_contract.lenses)} lenses"
            )
        ]
    else:
        decision = "partial"
        why = [
            (
                f"local evidence exists but gaps remain: "
                f"{papers_with_usable_chunks}/{target_papers} usable-chunk "
                f"papers, {total_usable_chunks}/{target_chunks} usable "
                f"chunks, missing queries={missing_queries}, "
                f"missing lenses={missing_lenses}, "
                f"missing roles={missing_roles}"
            )
        ]

    return {
        "schema_version": LOCAL_COVERAGE_SCHEMA_VERSION,
        "decision": decision,
        "base_kb_sqlite": str(base),
        "scope_contract_sha256": scope_contract.contract_sha256,
        "assessment_method": "topic_scoped_structured_scan",
        "fts_tables_available": {
            "paper_fts": "paper_fts" in names,
            "text_chunk_fts": "text_chunk_fts" in names,
        },
        "thresholds": thresholds,
        "counts": {
            "relevant_unique_papers": relevant_unique_papers,
            "papers_with_usable_chunks": papers_with_usable_chunks,
            "total_usable_chunks": total_usable_chunks,
            "weak_chunk_count": len(weak_chunks),
            "papers_with_weak_chunks_only": papers_with_weak_chunks_only,
        },
        "query_coverage": query_coverage,
        "lens_coverage": lens_coverage,
        "role_coverage": {
            "metadata_available": role_metadata_available,
            "roles": role_entries,
            "missing_roles": missing_roles,
        },
        "source_kind_diversity": {
            "distinct_source_kinds": sorted(source_kinds),
            "source_kind_counts": dict(sorted(source_kinds.items())),
        },
        "missing_queries": missing_queries,
        "missing_lenses": missing_lenses,
        "missing_roles": missing_roles,
        "covered_queries": covered_queries,
        "covered_lenses": covered_lenses,
        "covered_roles": covered_roles,
        "reused_paper_ids": sorted(paper_entries),
        "reused_chunk_ids": sorted(
            str(chunk.get("chunk_id") or "")
            for chunk in [*usable_chunks, *weak_chunks]
            if str(chunk.get("chunk_id") or "")
        ),
        "selection_report": {
            "source_row_counts": selection_report.get("source_row_counts") or {},
            "selected_row_counts": selection_report.get("selected_row_counts") or {},
            "rejected_row_counts": selection_report.get("rejected_row_counts") or {},
        },
        "why": why,
    }


__all__ = ["LOCAL_COVERAGE_SCHEMA_VERSION", "assess_local_coverage"]
