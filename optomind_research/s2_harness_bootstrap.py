"""Prepare a topic-specific, policy-driven S2 overlay for the Review Harness."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from optomind_research.runtime.s2_policy_runtime import (
    S2PolicyError,
    load_s2_policy,
)
from optomind_research.runtime.topic_scoped_kb_stage import (
    MANIFEST_SCHEMA_VERSION,
    SCOPE_DECISION_RULE_VERSION,
    TopicScopedKBError,
    TopicScopedKBStage,
    _canonical_sha256,
    _manifest_hash_is_valid,
    _reuse_contract,
    _reuse_contract_is_valid,
    _sha256_file,
    build_s2_query_telemetry,
    derive_topic_scope_contract,
)
from optomind_research.s2_local_coverage import assess_local_coverage
from optomind_research.s2_discovery import (
    DiscoveryPortfolio,
    S2DiscoveryPortfolioBuilder,
    ScholarFacetRequest,
)


BOOTSTRAP_SCHEMA_VERSION = "review_harness.s2_bootstrap.v3"
BOOTSTRAP_REUSE_CONTRACT_SCHEMA_VERSION = (
    "optomind.s2_bootstrap_reuse_contract.v2"
)
MATERIAL_FLOW_LEDGER_SCHEMA_VERSION = "optomind.s2_material_flow.v1"
from optomind_research.s2_fulltext_acquisition import (
    S2FulltextAcquirer,
    decide_fulltext_escalation,
    resolve_oa_worker_count,
)
from optomind_research.s2_literature_graph import LiteratureGraph, S2LiteratureGraphBuilder
from optomind_research.s2_text_chunk_retriever import (
    S2TextChunkRetriever,
    TextChunkRetrievalResult,
    materialize_abstract_claim,
    merge_text_chunk_results,
)


def _read_query_plan(path: Path) -> tuple[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("query plan root must be an object")
    contract = derive_topic_scope_contract(raw)
    if not contract.valid:
        raise ValueError(
            "query plan cannot form a topic scope contract: "
            + ", ".join(contract.validation_errors)
        )
    return contract.canonical_question[:1200], list(contract.keywords)


def _sealed_report(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed.pop("report_sha256", None)
    sealed["report_sha256"] = _canonical_sha256(sealed)
    return sealed


def _report_hash_is_valid(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    body = dict(payload)
    stored = str(body.pop("report_sha256", ""))
    return bool(stored) and stored == _canonical_sha256(body)


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    sealed = _sealed_report(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return sealed


def _failed_report(
    *,
    report_path: Path,
    runtime_kb: Path,
    base_kb: Path,
    started: float,
    error_code: str,
    error: Exception | str,
    policy_path: Path | None,
) -> dict[str, Any]:
    report = {
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "status": "failed",
        "error_code": error_code,
        "error": str(error),
        "runtime_kb_sqlite": str(runtime_kb),
        "source_base_kb_sqlite": str(base_kb),
        "policy_path": str(policy_path) if policy_path else "",
        "accepted_s2_body_chunks": 0,
        "graph_summary": {},
        "external_query_runs": [],
        "s2_query_telemetry": build_s2_query_telemetry(),
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "reused": False,
    }
    return _write_json(report_path, report)


def _bootstrap_reuse_contract(
    *,
    query_plan: dict[str, Any],
    base_kb_sqlite: Path,
    policy: Any,
    scope_contract: Any,
    effective_results_limit: int,
    effective_snippet_limit: int,
) -> dict[str, Any]:
    stage_contract = _reuse_contract(
        query_plan=query_plan,
        base_kb_sqlite=base_kb_sqlite,
        policy=policy,
        scope_contract=scope_contract,
        papers=(),
        chunks=(),
        graph=None,
        query_telemetry={},
        extra_manifest={},
    )
    stage_components = dict(stage_contract["components"])
    components = {
        key: stage_components[key]
        for key in (
            "query_plan_semantic_sha256",
            "source_base_kb_sha256",
            "effective_policy_sha256",
            "scope_contract_sha256",
        )
    }
    components.update(
        {
            "effective_results_limit": int(effective_results_limit),
            "effective_snippet_limit": int(effective_snippet_limit),
        }
    )
    contract = {
        "schema_version": BOOTSTRAP_REUSE_CONTRACT_SCHEMA_VERSION,
        "scope_decision_rule_version": SCOPE_DECISION_RULE_VERSION,
        "components": components,
    }
    contract["request_fingerprint_sha256"] = _canonical_sha256(contract)
    return contract


def _bootstrap_reuse_contract_is_valid(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != BOOTSTRAP_REUSE_CONTRACT_SCHEMA_VERSION:
        return False
    if value.get("scope_decision_rule_version") != SCOPE_DECISION_RULE_VERSION:
        return False
    body = dict(value)
    stored = str(body.pop("request_fingerprint_sha256", ""))
    return bool(stored) and stored == _canonical_sha256(body)


def _reuse_rejection_report(
    *,
    runtime_kb: Path,
    base_kb: Path,
    policy_path: Path | None,
    started: float,
    reason: str,
) -> dict[str, Any]:
    """Return a failure without altering the occupied immutable stage."""

    return {
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "status": "failed",
        "error_code": "s2_bootstrap_reuse_rejected",
        "error": reason,
        "runtime_kb_sqlite": str(runtime_kb),
        "source_base_kb_sqlite": str(base_kb),
        "policy_path": str(policy_path) if policy_path else "",
        "accepted_s2_body_chunks": 0,
        "graph_summary": {},
        "external_query_runs": [],
        "s2_query_telemetry": build_s2_query_telemetry(),
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "reused": False,
        "reuse_rejected": True,
        "existing_artifacts_preserved": True,
    }


# ---------------------------------------------------------------------------
# Repair 1: isolated rebuild path for stale-but-valid (contract-mismatch) dirs
# ---------------------------------------------------------------------------

_STALE_ARTIFACTS_DIRNAME = "_stale_bootstrap"


def _relocate_stale_artifacts(
    work_dir: Path,
    immutable_paths: "tuple[Path, ...]",
    started: float,
) -> Path:
    """Move stale bootstrap artifacts to a timestamped archive subdirectory.

    The archive dir name encodes the start timestamp so concurrent calls in
    the same second cannot collide.  Files that cannot be atomically renamed
    (cross-device) are copied then removed.  Missing paths are skipped.

    Returns the archive directory path for provenance tracking.
    """
    ts = f"{int(started * 1000)}"
    stale_dir = work_dir / f"{_STALE_ARTIFACTS_DIRNAME}_{ts}"
    stale_dir.mkdir(parents=True, exist_ok=True)
    for path in immutable_paths:
        if path.is_file():
            dest = stale_dir / path.name
            try:
                path.rename(dest)
            except OSError:
                shutil.copy2(str(path), str(dest))
                try:
                    path.unlink()
                except OSError:
                    pass
    return stale_dir


def _isolated_rebuild_report(
    *,
    stale_artifact_dir: Path,
    runtime_kb: Path,
    base_kb: Path,
    policy_path: "Path | None",
    started: float,
    reason: str,
) -> dict[str, Any]:
    """Report that stale artifacts were preserved and a fresh rebuild can proceed.

    Stale artifacts have been relocated to *stale_artifact_dir*.  The caller
    may now re-call :func:`build_s2_bootstrap_report` with the same *work_dir*
    (which is now empty) to produce a fresh compatible report.

    This status is returned for **contract-mismatch** cases only (query plan,
    policy, or schema version changed).  Integrity failures (hash corruption,
    unsafe run status) still return :func:`_reuse_rejection_report` with
    ``status="failed"`` so the caller cannot silently consume a corrupt KB.
    """
    return {
        "schema_version": BOOTSTRAP_SCHEMA_VERSION,
        "status": "isolated_rebuild_available",
        "error_code": "s2_bootstrap_reuse_contract_mismatch",
        "error": reason,
        "stale_artifact_dir": str(stale_artifact_dir),
        "runtime_kb_sqlite": str(runtime_kb),
        "source_base_kb_sqlite": str(base_kb),
        "policy_path": str(policy_path) if policy_path else "",
        "accepted_s2_body_chunks": 0,
        "graph_summary": {},
        "external_query_runs": [],
        "s2_query_telemetry": build_s2_query_telemetry(),
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "reused": False,
        "reuse_rejected": False,
        "existing_artifacts_preserved": True,
        "isolated_rebuild_available": True,
    }


def _empty_portfolio() -> DiscoveryPortfolio:
    return DiscoveryPortfolio(
        candidates=[],
        query_runs=[],
        pool_counts={},
        rejected_count=0,
    )


def _empty_chunks() -> TextChunkRetrievalResult:
    return TextChunkRetrievalResult(
        accepted_chunks=[],
        rejected_items=[],
        query_runs=[],
        paper_ids=[],
    )


def _material_inventory(
    kb_sqlite: Path,
    paper_ids: list[str],
) -> dict[str, dict[str, list[str]]]:
    """Return per-paper material classes from the canonical chunk table."""

    inventory = {
        paper_id: {
            "s2_body_chunk_ids": [],
            "oa_fulltext_chunk_ids": [],
            "abstract_claim_chunk_ids": [],
        }
        for paper_id in paper_ids
        if paper_id
    }
    if not inventory or not kb_sqlite.is_file():
        return inventory
    conn = sqlite3.connect(str(kb_sqlite))
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
        }
        if not {"paper_id", "chunk_id"} <= columns:
            return inventory
        select_columns = [
            column
            for column in (
                "paper_id",
                "chunk_id",
                "content_depth",
                "source_kind",
                "materialization_route",
                "route_provenance_json",
                "raw_json",
            )
            if column in columns
        ]
        placeholders = ",".join("?" for _ in inventory)
        rows = conn.execute(
            f"SELECT {','.join(select_columns)} FROM text_chunks "
            f"WHERE paper_id IN ({placeholders})",
            tuple(inventory),
        ).fetchall()
        for row in rows:
            mapping = dict(row)
            paper_id = str(mapping.get("paper_id") or "")
            chunk_id = str(mapping.get("chunk_id") or "")
            if not chunk_id or paper_id not in inventory:
                continue
            route: dict[str, Any] = {}
            for key in ("route_provenance_json", "raw_json"):
                try:
                    parsed = json.loads(mapping.get(key) or "{}")
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    route.update(parsed.get("route_provenance") or {})
                    route.update(parsed)
            depth = str(
                mapping.get("content_depth")
                or route.get("content_depth")
                or ""
            ).strip().casefold()
            source_kind = str(mapping.get("source_kind") or "").casefold()
            materialization = str(
                mapping.get("materialization_route")
                or route.get("materialization_route")
                or ""
            ).casefold()
            if depth == "abstract_claim" or "abstract_claim" in materialization:
                bucket = "abstract_claim_chunk_ids"
            elif (
                depth == "structured_snippet"
                or source_kind == "s2_body_snippet"
                or "s2_structured_body" in materialization
            ):
                bucket = "s2_body_chunk_ids"
            elif depth in {"fulltext", "partial_fulltext"}:
                bucket = "oa_fulltext_chunk_ids"
            else:
                continue
            inventory[paper_id][bucket].append(chunk_id)
        for classes in inventory.values():
            for key, values in classes.items():
                classes[key] = list(dict.fromkeys(values))
        return inventory
    finally:
        conn.close()


def _has_primary_material(classes: dict[str, list[str]]) -> bool:
    return bool(
        classes.get("s2_body_chunk_ids")
        or classes.get("oa_fulltext_chunk_ids")
    )


def _record_value(record: Any, key: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _overlay_paper_snapshots(
    kb_sqlite: Path,
    paper_ids: list[str],
) -> list[dict[str, Any]]:
    """Return lightweight paper records for local overlay rows."""

    ids = [paper_id for paper_id in paper_ids if paper_id]
    if not ids or not kb_sqlite.is_file():
        return []
    conn = sqlite3.connect(f"file:{kb_sqlite.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(papers)").fetchall()
        }
        select_columns = [
            column
            for column in ("paper_id", "title", "doi", "raw_json")
            if column in columns
        ]
        if "paper_id" not in select_columns:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT {','.join(select_columns)} FROM papers "
            f"WHERE paper_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            raw: dict[str, Any] = {}
            try:
                parsed = json.loads(row["raw_json"] or "{}")
                if isinstance(parsed, dict):
                    raw = parsed
            except (TypeError, json.JSONDecodeError):
                pass
            snapshots.append(
                {
                    "paper_id": str(row["paper_id"] or ""),
                    "title": str(row["title"] or ""),
                    "doi": str(row["doi"] or ""),
                    "abstract": str(
                        raw.get("abstract")
                        or raw.get("tldr")
                        or raw.get("search_text")
                        or ""
                    ),
                }
            )
        return snapshots
    finally:
        conn.close()


def _overlay_chunk_ids(
    kb_sqlite: Path,
    paper_ids: list[str],
) -> list[str]:
    """Return selected chunk ids from the run-local overlay."""

    ids = [paper_id for paper_id in paper_ids if paper_id]
    if not ids or not kb_sqlite.is_file():
        return []
    conn = sqlite3.connect(f"file:{kb_sqlite.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            "SELECT chunk_id FROM text_chunks "
            f"WHERE paper_id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        return sorted(
            str(row["chunk_id"] or "")
            for row in rows
            if str(row["chunk_id"] or "")
        )
    finally:
        conn.close()


def _local_coverage_skipped_events(
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """Record, in telemetry, the external routes avoided by a local hit."""

    if str(coverage.get("decision") or "") != "sufficient":
        return []
    routes = (
        ("discovery_search", "broad discovery"),
        ("snippet_search", "broad snippet search"),
        ("precise_followup", "precise follow-up"),
        ("batch_enrichment", "batch enrichment"),
        ("graph_expansion", "graph expansion"),
        ("oa_fulltext", "OA acquisition"),
    )
    return [
        {
            "query_category": category,
            "query": "",
            "channel": "",
            "endpoint": "",
            "status_category": "skipped",
            "status_code": 0,
            "result_count": 0,
            "cache_hit": True,
            "wait_seconds": 0.0,
            "ok": True,
            "reason": f"local_coverage_sufficient:{label}",
        }
        for category, label in routes
    ]


def _build_material_flow_ledger(
    *,
    papers: list[Any],
    inventory: dict[str, dict[str, list[str]]],
    precise_runs: list[dict[str, Any]],
    fulltext_report: dict[str, Any],
    abstract_outcomes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    precise_ids = {
        str(run.get("target_paper_id") or "")
        for run in precise_runs
        if str(run.get("query_category") or "") == "precise_missing_paper"
    }
    oa_outcomes = {
        str(row.get("paper_id") or ""): dict(row)
        for row in ((fulltext_report.get("stats") or {}).get("paper_outcomes") or [])
        if isinstance(row, dict) and str(row.get("paper_id") or "")
    }
    oa_skipped = {
        str(row.get("paper_id") or ""): str(row.get("reason") or "")
        for row in (fulltext_report.get("skipped") or [])
        if isinstance(row, dict) and str(row.get("paper_id") or "")
    }
    rows: list[dict[str, Any]] = []
    counts = {
        "s2_body": 0,
        "oa_fulltext": 0,
        "abstract_claim": 0,
        "discovery_only": 0,
        "admitted": 0,
    }
    for paper in papers:
        paper_id = str(_record_value(paper, "paper_id", "") or "")
        classes = inventory.get(paper_id) or {
            "s2_body_chunk_ids": [],
            "oa_fulltext_chunk_ids": [],
            "abstract_claim_chunk_ids": [],
        }
        if classes["s2_body_chunk_ids"]:
            status = "s2_body"
            oa_status = "skipped_s2_material_sufficient"
            abstract_status = "skipped_s2_material_sufficient"
        elif classes["oa_fulltext_chunk_ids"]:
            status = "oa_fulltext"
            oa_status = str(oa_outcomes.get(paper_id, {}).get("status") or "oa_fulltext_success")
            abstract_status = "skipped_oa_material_sufficient"
        elif classes["abstract_claim_chunk_ids"]:
            status = "abstract_claim"
            oa_status = str(
                oa_outcomes.get(paper_id, {}).get("status")
                or oa_skipped.get(paper_id)
                or "oa_unavailable"
            )
            abstract_status = str(
                abstract_outcomes.get(paper_id, {}).get("status") or "materialized"
            )
        else:
            status = "discovery_only"
            oa_status = str(
                oa_outcomes.get(paper_id, {}).get("status")
                or oa_skipped.get(paper_id)
                or "oa_unavailable"
            )
            abstract_status = str(
                abstract_outcomes.get(paper_id, {}).get("status")
                or "abstract_missing"
            )
        admitted = status != "discovery_only"
        counts[status] += 1
        counts["admitted"] += int(admitted)
        rows.append(
            {
                "paper_id": paper_id,
                "title": str(_record_value(paper, "title", "") or ""),
                "doi": str(_record_value(paper, "doi", "") or ""),
                "has_abstract": bool(
                    str(_record_value(paper, "abstract", "") or "").strip()
                ),
                "s2_precise_lookup_attempted": paper_id in precise_ids,
                "s2_body_chunk_ids": list(classes["s2_body_chunk_ids"]),
                "oa_status": oa_status,
                "oa_fulltext_chunk_ids": list(classes["oa_fulltext_chunk_ids"]),
                "abstract_status": abstract_status,
                "abstract_claim_chunk_ids": list(classes["abstract_claim_chunk_ids"]),
                "material_status": status,
                "admitted_to_downstream": admitted,
                "admission_rule": "any_one_material_class_nonempty",
            }
        )
    return {
        "schema_version": MATERIAL_FLOW_LEDGER_SCHEMA_VERSION,
        "fallback_order": [
            "s2_structured_body_snippet",
            "public_oa_fulltext",
            "verified_abstract_claim",
        ],
        "admission_rule": "s2_body OR oa_fulltext OR abstract_claim",
        "summary": {
            "paper_count": len(rows),
            "admitted_paper_count": counts["admitted"],
            "s2_body_paper_count": counts["s2_body"],
            "oa_fulltext_paper_count": counts["oa_fulltext"],
            "abstract_claim_paper_count": counts["abstract_claim"],
            "discovery_only_paper_count": counts["discovery_only"],
        },
        "papers": rows,
    }


def _identity_alias(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text.startswith("https://doi.org/"):
        text = text[len("https://doi.org/"):]
    if text.startswith("doi:"):
        text = text[4:]
    for prefix in ("s2:", "s2paper:", "semantic_scholar:", "semantic-scholar:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return re.sub(r"\s+", " ", text)


def _paper_identity_aliases(paper: Any) -> set[str]:
    aliases: set[str] = set()
    for value in (
        getattr(paper, "paper_id", ""),
        getattr(paper, "doi", ""),
        getattr(paper, "title", ""),
    ):
        alias = _identity_alias(value)
        if alias:
            aliases.add(alias)
    corpus_id = getattr(paper, "corpus_id", None)
    if corpus_id not in (None, ""):
        aliases.update({
            _identity_alias(corpus_id),
            _identity_alias(f"CorpusId:{corpus_id}"),
        })
    external_ids = getattr(paper, "external_ids", {}) or {}
    if isinstance(external_ids, dict):
        for value in external_ids.values():
            alias = _identity_alias(value)
            if alias:
                aliases.add(alias)
    return aliases


def _chunk_identity_aliases(chunk: Any) -> set[str]:
    aliases: set[str] = set()
    for value in (
        getattr(chunk, "paper_id", ""),
        getattr(chunk, "doi", ""),
        getattr(chunk, "title", ""),
        getattr(chunk, "corpus_id", None),
    ):
        alias = _identity_alias(value)
        if alias:
            aliases.add(alias)
    if getattr(chunk, "corpus_id", None) not in (None, ""):
        aliases.add(_identity_alias(f"CorpusId:{chunk.corpus_id}"))
    raw = getattr(chunk, "raw_metadata", {}) or {}
    item = raw.get("s2_item") if isinstance(raw, dict) else {}
    parent = item.get("paper") if isinstance(item, dict) else {}
    if isinstance(parent, dict):
        for key in ("paperId", "corpusId", "title"):
            alias = _identity_alias(parent.get(key))
            if alias:
                aliases.add(alias)
        external_ids = parent.get("externalIds") or {}
        if isinstance(external_ids, dict):
            for value in external_ids.values():
                alias = _identity_alias(value)
                if alias:
                    aliases.add(alias)
    return aliases


def _retain_validated_snippet_parents(
    *,
    discovery_papers: list[Any],
    resolved_papers: list[Any],
    chunks: list[Any],
    stage: TopicScopedKBStage,
    maximum_papers: int,
) -> tuple[list[Any], dict[str, Any]]:
    """Keep validated snippet parents inside the deterministic paper cap.

    Discovery order is retained as a tie-breaker, but a paper referenced by an
    accepted structured snippet is ranked first.  Identity matching is exact
    over S2 ID/corpus ID/DOI/title aliases; ambiguous aliases remain
    unresolved and are still rejected by the staging gate.
    """

    cap = max(0, int(maximum_papers))
    merged: dict[str, Any] = {}
    source_rank: dict[str, tuple[int, int]] = {}
    for index, paper in enumerate([*discovery_papers, *resolved_papers]):
        paper_id = str(getattr(paper, "paper_id", "") or "").strip()
        related_chunks = [
            chunk
            for chunk in chunks
            if _paper_identity_aliases(paper) & _chunk_identity_aliases(chunk)
        ]
        if not paper_id or not stage.accepts_s2_paper(
            paper,
            related_chunks=related_chunks,
        ):
            continue
        if paper_id not in merged:
            merged[paper_id] = paper
            source_rank[paper_id] = (
                0 if index < len(discovery_papers) else 1,
                index,
            )

    alias_to_ids: dict[str, set[str]] = {}
    for paper_id, paper in merged.items():
        for alias in _paper_identity_aliases(paper):
            alias_to_ids.setdefault(alias, set()).add(paper_id)
    required_ids: set[str] = set()
    ambiguous_chunks = 0
    for chunk in chunks:
        matches = {
            paper_id
            for alias in _chunk_identity_aliases(chunk)
            for paper_id in alias_to_ids.get(alias, set())
        }
        if len(matches) == 1:
            required_ids.update(matches)
        elif len(matches) > 1:
            ambiguous_chunks += 1

    def rank(item: tuple[str, Any]) -> tuple[Any, ...]:
        paper_id, paper = item
        required_rank = 0 if paper_id in required_ids else 1
        source, order = source_rank.get(paper_id, (2, 0))
        return (
            required_rank,
            source,
            -int(getattr(paper, "citation_count", 0) or 0),
            -int(getattr(paper, "year", 0) or 0),
            str(getattr(paper, "title", "") or "").casefold(),
            order,
            paper_id,
        )

    ordered = sorted(merged.items(), key=rank)
    selected = [paper for _paper_id, paper in ordered[:cap]]
    selected_ids = {str(getattr(paper, "paper_id", "") or "") for paper in selected}
    return selected, {
        "cap": cap,
        "discovery_count": len(discovery_papers),
        "resolved_count": len(resolved_papers),
        "validated_parent_ids": sorted(required_ids),
        "retained_parent_ids": sorted(required_ids & selected_ids),
        "replaced_discovery_count": max(
            0,
            len(set(str(getattr(paper, "paper_id", "") or "") for paper in discovery_papers))
            - len(selected_ids & set(str(getattr(paper, "paper_id", "") or "") for paper in discovery_papers)),
        ),
        "ambiguous_chunk_parent_count": ambiguous_chunks,
    }


def _build_policy_graph(
    *,
    seeds: list[Any],
    topic_queries: list[str],
    policy: Any,
    relation_controls: Mapping[str, Any] | None = None,
) -> LiteratureGraph:
    """Expand graph traffic to the configured depth with bounded frontiers.

    ``relation_controls`` is used only for supplementary plans: reference,
    citation/cited-by, recommendation, and multi-seed switches independently
    zero their limits.  Ordinary first-round calls pass ``None`` and keep the
    historical policy limits and seed behavior.
    """

    controls = dict(relation_controls or {})
    references_enabled = bool(controls.get("references", True))
    citations_enabled = bool(controls.get("citations", True))
    recommendations_enabled = bool(controls.get("recommendations", True))
    multi_seed_enabled = bool(controls.get("multi_seed", True))
    reference_limit = (
        policy.graph_reference_limit_per_seed
        if references_enabled
        else 0
    )
    citation_limit = (
        policy.graph_citation_limit_per_seed
        if citations_enabled
        else 0
    )
    recommendation_limit = (
        policy.graph_recommendation_limit
        if (
            recommendations_enabled
            and policy.feature_enabled("use_recommendations", default=True)
        )
        else 0
    )
    seed_cap = (
        policy.graph_seed_count if multi_seed_enabled else 1
    )

    builder = S2LiteratureGraphBuilder()
    merged = LiteratureGraph()
    frontier = list(seeds[: max(1, int(seed_cap))])
    seen_ids = {paper.paper_id for paper in frontier if paper.paper_id}
    for depth_index in range(max(0, int(policy.graph_depth))):
        if not frontier:
            break
        level = builder.expand_from_seeds(
            frontier,
            topic_queries=topic_queries,
            reference_limit_per_seed=reference_limit,
            citation_limit_per_seed=citation_limit,
            recommendation_limit=recommendation_limit,
        )
        next_frontier: list[Any] = []
        for paper_id, paper in level.nodes.items():
            if paper_id not in seen_ids:
                next_frontier.append(paper)
            merged.add_node(
                paper,
                {
                    **dict(level.node_annotations.get(paper_id) or {}),
                    "graph_depth_observed": depth_index + 1,
                },
            )
        for edge in level.edges:
            merged.add_edge(edge)
        merged.excluded_candidates.extend(level.excluded_candidates)
        merged.query_runs.extend(level.query_runs)
        seen_ids.update(level.nodes)
        next_frontier.sort(key=lambda paper: paper.paper_id)
        frontier = next_frontier[: max(1, int(seed_cap))]
    return merged


class _SupplementaryRouteUsage:
    """Truthful per-route usage recorder for supplementary tasks.

    Route caps are independent: a cap on one route never consumes or shrinks
    another route.  ``extra_request_cap`` is validated at the contract level
    as an emergency hard ceiling/audit field and is never used here to shrink
    a normal route.
    """

    def __init__(self) -> None:
        self.routes: dict[str, dict[str, Any]] = {}

    def record(
        self,
        route: str,
        *,
        configured_cap: int,
        eligible: int,
        attempted: int,
        outcomes: list[Any] | None = None,
    ) -> None:
        self.routes[route] = {
            "configured_cap": max(0, int(configured_cap)),
            "eligible": max(0, int(eligible)),
            "attempted": max(0, int(attempted)),
            "outcomes": list(outcomes or []),
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self.routes)


def _supplementary_execution_policy(
    marker: Any,
    *,
    discovery_direct_only: bool,
    fallback_extra_request_cap: int,
    fallback_graph_seed_cap: int,
) -> dict[str, Any] | None:
    """Resolve effective supplementary execution controls from a plan marker.

    Ordinary first-round plans return ``None`` and keep historical behavior.
    Generated-only supplementary markers drive the independent switches;
    legacy markers without the new keys default to the approved conservative
    values (role expansion off, exact-paper follow-up on, OA fallback on,
    graph per the legacy ``allow_graph_expansion`` flag, and a conservative
    extra-request cap derived from the configured policy budgets).
    """

    if not discovery_direct_only or not isinstance(marker, dict):
        return None
    expansion_policy = marker.get("expansion_policy")
    expansion_policy = expansion_policy if isinstance(expansion_policy, dict) else {}

    def _switch(key: str, default: bool) -> bool:
        if key in marker:
            return bool(marker.get(key))
        return bool(expansion_policy.get(key, default))

    individual_graph_keys = (
        "allow_reference_expansion",
        "allow_citation_expansion",
        "allow_recommendation_expansion",
        "allow_multi_seed_graph",
    )
    has_individual_graph = any(
        key in marker or key in expansion_policy
        for key in individual_graph_keys
    )
    if not has_individual_graph:
        legacy_graph = bool(
            marker.get(
                "allow_graph_expansion",
                expansion_policy.get("allow_graph_expansion", False),
            )
        )
        allow_reference_expansion = legacy_graph
        allow_citation_expansion = legacy_graph
        allow_recommendation_expansion = legacy_graph
        allow_multi_seed_graph = legacy_graph
    else:
        allow_reference_expansion = _switch(
            "allow_reference_expansion", False
        )
        allow_citation_expansion = _switch(
            "allow_citation_expansion", False
        )
        allow_recommendation_expansion = _switch(
            "allow_recommendation_expansion", False
        )
        allow_multi_seed_graph = _switch("allow_multi_seed_graph", False)
    graph_modes: list[str] = []
    if allow_reference_expansion:
        graph_modes.append("references")
    if allow_citation_expansion:
        graph_modes.extend(("citations", "cited_by"))
    if allow_recommendation_expansion:
        graph_modes.append("recommendations")
    if allow_multi_seed_graph:
        graph_modes.append("multi_seed")
    raw_extra_cap = marker.get("extra_request_cap")
    if raw_extra_cap is None:
        raw_extra_cap = expansion_policy.get("extra_request_cap")
    try:
        extra_request_cap = (
            int(raw_extra_cap)
            if raw_extra_cap is not None
            else max(1, int(fallback_extra_request_cap))
        )
    except (TypeError, ValueError):
        extra_request_cap = max(1, int(fallback_extra_request_cap))
    return {
        "active": True,
        "s2_snippet_results_per_query_cap": int(
            marker.get(
                "s2_snippet_results_per_query_cap",
                expansion_policy.get("s2_snippet_results_per_query_cap"),
            )
            if marker.get(
                "s2_snippet_results_per_query_cap",
                expansion_policy.get("s2_snippet_results_per_query_cap"),
            )
            is not None
            else 5
        ),
        "s2_precise_paper_cap": int(
            marker.get(
                "s2_precise_paper_cap",
                expansion_policy.get("s2_precise_paper_cap"),
            )
            if marker.get(
                "s2_precise_paper_cap",
                expansion_policy.get("s2_precise_paper_cap"),
            )
            is not None
            else 2
        ),
        "batch_enrichment_paper_cap": int(
            marker.get(
                "batch_enrichment_paper_cap",
                expansion_policy.get("batch_enrichment_paper_cap"),
            )
            if marker.get(
                "batch_enrichment_paper_cap",
                expansion_policy.get("batch_enrichment_paper_cap"),
            )
            is not None
            else 0
        ),
        "oa_fulltext_paper_cap": int(
            marker.get(
                "oa_fulltext_paper_cap",
                expansion_policy.get("oa_fulltext_paper_cap"),
            )
            if marker.get(
                "oa_fulltext_paper_cap",
                expansion_policy.get("oa_fulltext_paper_cap"),
            )
            is not None
            else 6
        ),
        "abstract_claim_paper_cap": int(
            marker.get(
                "abstract_claim_paper_cap",
                expansion_policy.get("abstract_claim_paper_cap"),
            )
            if marker.get(
                "abstract_claim_paper_cap",
                expansion_policy.get("abstract_claim_paper_cap"),
            )
            is not None
            else 8
        ),
        "graph_seed_cap": int(
            marker.get(
                "graph_seed_cap",
                expansion_policy.get("graph_seed_cap"),
            )
            if marker.get(
                "graph_seed_cap",
                expansion_policy.get("graph_seed_cap"),
            )
            is not None
            else max(1, int(fallback_graph_seed_cap))
        ),
        "allow_role_expansion": _switch("allow_role_expansion", False),
        "allow_exact_paper_followup": _switch(
            "allow_exact_paper_followup", True
        ),
        "allow_batch_enrichment": _switch(
            "allow_batch_enrichment", False
        ),
        "allow_oa_fulltext_fallback": _switch(
            "allow_oa_fulltext_fallback", True
        ),
        "allow_reference_expansion": allow_reference_expansion,
        "allow_citation_expansion": allow_citation_expansion,
        "allow_recommendation_expansion": allow_recommendation_expansion,
        "allow_multi_seed_graph": allow_multi_seed_graph,
        "allow_visual_processing": _switch(
            "allow_visual_processing", False
        ),
        "allow_graph_expansion": bool(
            allow_reference_expansion
            or allow_citation_expansion
            or allow_recommendation_expansion
        ),
        "graph_modes": sorted(set(graph_modes)),
        "result_cap": marker.get(
            "result_cap", expansion_policy.get("result_cap")
        ),
        "extra_request_cap": max(0, int(extra_request_cap)),
    }


def prepare_s2_harness_kb(
    *,
    query_plan_path: Path,
    base_kb_sqlite: Path,
    work_dir: Path,
    results_limit: int | None = None,
    snippet_limit: int | None = None,
    policy_path: Path | None = None,
    visual_fulltext_processing: bool = False,
    oa_fulltext_paper_cap: int = 0,
    semantic_relevance: Any | None = None,
) -> dict[str, Any]:
    """Create/reuse a policy-driven, topic-scoped KB enriched with S2 assets.

    ``results_limit`` and ``snippet_limit`` remain compatibility overrides for
    callers that need a bounded test run.  In normal operation both are
    ``None`` and the validated ``config/s2_policy.yaml`` controls the limits.
    """

    started = time.monotonic()
    work_dir.mkdir(parents=True, exist_ok=True)
    report_path = work_dir / "S2_BOOTSTRAP_REPORT.json"
    runtime_kb = work_dir / "review_knowledge_base.s2.sqlite"
    manifest_path = work_dir / "KB_MANIFEST.json"
    graph_path = work_dir / "S2_LITERATURE_GRAPH.json"
    telemetry_path = work_dir / "S2_QUERY_TELEMETRY.json"
    material_flow_path = work_dir / "S2_MATERIAL_FLOW_LEDGER.json"
    immutable_paths = (
        report_path,
        runtime_kb,
        manifest_path,
        graph_path,
        telemetry_path,
        material_flow_path,
    )
    occupied_before_call = any(path.exists() for path in immutable_paths)

    def _norm_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    def _compute_supplementary_semantic_scores(
        *,
        candidates: list[Any],
        background_cue: str,
        precise_queries: list[str],
        engine: Any,
    ) -> dict[str, dict[str, Any]]:
        """One batched semantic pass over generated-only candidates.

        Compares each candidate's title + abstract/TLDR with the broad
        background and every precise generated query.  Embedding failures are
        audited and fall back to the lexical usefulness score.
        """

        papers: list[Any] = []
        seen_paper_ids: set[str] = set()
        for candidate in candidates:
            paper = candidate.paper
            if not paper.paper_id or paper.paper_id in seen_paper_ids:
                continue
            seen_paper_ids.add(paper.paper_id)
            papers.append(paper)
        paper_texts = [
            " ".join(
                part
                for part in (
                    str(paper.title or ""),
                    str(paper.abstract or ""),
                    str(paper.tldr or ""),
                )
                if str(part).strip()
            )
            for paper in papers
        ]
        references = (
            [background_cue] + list(precise_queries)
            if background_cue
            else list(precise_queries)
        )
        mode = "semantic"
        fallback_error_code = ""
        try:
            engine.embed_texts([*paper_texts, *references])
        except Exception as exc:
            mode = "lexical_fallback"
            fallback_error_code = (
                f"{type(exc).__name__}:{exc}"[:160]
            )
        scores: dict[str, dict[str, Any]] = {}
        for paper, text in zip(papers, paper_texts):
            if mode != "semantic":
                scores[paper.paper_id] = {
                    "mode": mode,
                    "fallback_error_code": fallback_error_code,
                }
                continue
            background_similarity = (
                engine.cosine(text, background_cue)
                if background_cue
                else 0.0
            )
            precise_similarities = {
                str(query): engine.cosine(text, str(query))
                for query in precise_queries
            }
            max_precise_similarity = (
                max(precise_similarities.values())
                if precise_similarities
                else 0.0
            )
            matched_query = (
                max(
                    precise_similarities,
                    key=precise_similarities.get,
                )
                if precise_similarities
                else ""
            )
            scores[paper.paper_id] = {
                "mode": "semantic",
                "background_similarity": round(
                    float(background_similarity), 6
                ),
                "max_precise_similarity": round(
                    float(max_precise_similarity), 6
                ),
                "matched_query": str(matched_query),
                "fallback_error_code": "",
            }
        return scores

    try:
        policy = load_s2_policy(policy_path)
        query_plan = json.loads(query_plan_path.read_text(encoding="utf-8"))
        if not isinstance(query_plan, dict):
            raise ValueError("query plan root must be an object")
        scope_contract = derive_topic_scope_contract(query_plan)
        if not scope_contract.valid:
            raise ValueError(
                "query plan cannot form a topic scope contract: "
                + ", ".join(scope_contract.validation_errors)
            )
        # Generic generated-only marker: supplementary gap tasks execute only
        # their explicit discovery queries; the original question remains the
        # identity and exclusion guard but is not expanded into review or
        # foundation query variants.
        discovery_direct_only = scope_contract.discovery_mode == "generated_only"
        supplementary_marker = query_plan.get("supplementary_retrieval")
        if not isinstance(supplementary_marker, dict):
            supplementary_marker = (
                query_plan.get("output")
                if isinstance(query_plan.get("output"), dict)
                else {}
            ).get("supplementary_retrieval")
        allow_graph_expansion = bool(
            supplementary_marker.get("allow_graph_expansion")
            if isinstance(supplementary_marker, dict)
            else False
        )
        supplementary_policy = _supplementary_execution_policy(
            supplementary_marker,
            discovery_direct_only=discovery_direct_only,
            fallback_extra_request_cap=(
                max(1, int(policy.max_precise_snippet_papers))
                + max(1, int(policy.graph_seed_count))
                + max(1, int(policy.maximum_oa_downloads))
            ),
            fallback_graph_seed_cap=max(1, int(policy.graph_seed_count)),
        )
        supplementary_route_usage = (
            _SupplementaryRouteUsage()
            if supplementary_policy is not None
            else None
        )
        # Role expansion in S2DiscoveryPortfolioBuilder is suppressed whenever
        # direct_only=True.  Supplementary plans may therefore relax the
        # generated-only direct flag when the task policy explicitly allows
        # role expansion; the generated gap queries remain the only base search
        # terms and role expansion only appends configured role suffixes.
        # Ordinary first-round plans keep their historical direct_only value.
        effective_discovery_direct_only = discovery_direct_only
        if (
            supplementary_policy is not None
            and supplementary_policy["allow_role_expansion"]
        ):
            effective_discovery_direct_only = False
        graph_modes = (
            supplementary_policy["graph_modes"]
            if supplementary_policy is not None
            else []
        )
        # Generated-only tasks are bounded: references/citations/
        # recommendations/multi-seed graph expansion is suppressed at the
        # controller boundary unless the plan explicitly opts in.  Ordinary
        # plans keep the historical graph behavior (allowed=True).
        graph_expansion_allowed = (
            not discovery_direct_only
            or (
                bool(supplementary_policy["allow_graph_expansion"])
                if supplementary_policy is not None
                else allow_graph_expansion
            )
        )
        effective_results_limit = (
            max(1, int(results_limit))
            if results_limit is not None
            else (
                max(1, int(supplementary_policy["result_cap"]))
                if supplementary_policy is not None
                else policy.results_per_query
            )
        )
        effective_snippet_limit = (
            (
                max(0, int(snippet_limit))
                if supplementary_policy is not None
                else max(1, int(snippet_limit))
            )
            if snippet_limit is not None
            else (
                max(
                    0,
                    int(
                        supplementary_policy[
                            "s2_snippet_results_per_query_cap"
                        ]
                    ),
                )
                if supplementary_policy is not None
                else policy.snippet_results_per_query
            )
        )
        expected_reuse_contract = _bootstrap_reuse_contract(
            query_plan=query_plan,
            base_kb_sqlite=base_kb_sqlite,
            policy=policy,
            scope_contract=scope_contract,
            effective_results_limit=effective_results_limit,
            effective_snippet_limit=effective_snippet_limit,
        )
        query = scope_contract.canonical_question[:1200]
        search_queries = scope_contract.search_queries(
            max_items=policy.max_search_queries
        )

        if occupied_before_call:
            missing = [path.name for path in immutable_paths if not path.is_file()]
            if missing:
                raise TopicScopedKBError(
                    "occupied S2 bootstrap work_dir is incomplete: "
                    + ", ".join(missing)
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if report.get("schema_version") != BOOTSTRAP_SCHEMA_VERSION:
                stale_dir = _relocate_stale_artifacts(work_dir, immutable_paths, started)
                return _isolated_rebuild_report(
                    stale_artifact_dir=stale_dir, runtime_kb=runtime_kb,
                    base_kb=base_kb_sqlite, policy_path=policy_path,
                    started=started,
                    reason="existing S2 bootstrap report schema is stale",
                )
            if not _report_hash_is_valid(report):
                raise TopicScopedKBError(
                    "existing S2 bootstrap report failed its integrity hash"
                )
            stored_bootstrap_contract = report.get("reuse_contract")
            if not _bootstrap_reuse_contract_is_valid(stored_bootstrap_contract):
                raise TopicScopedKBError(
                    "existing S2 bootstrap report lacks a valid reuse contract"
                )
            if _canonical_sha256(stored_bootstrap_contract) != _canonical_sha256(
                expected_reuse_contract
            ):
                stale_dir = _relocate_stale_artifacts(work_dir, immutable_paths, started)
                return _isolated_rebuild_report(
                    stale_artifact_dir=stale_dir, runtime_kb=runtime_kb,
                    base_kb=base_kb_sqlite, policy_path=policy_path,
                    started=started,
                    reason="existing S2 bootstrap report does not match current inputs",
                )
            if str(report.get("status") or "") not in {
                "completed",
                "partial",
                "needs_more_literature",
            }:
                raise TopicScopedKBError(
                    "existing S2 bootstrap report has an unsafe status"
                )
            if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                raise TopicScopedKBError("existing KB_MANIFEST schema is stale")
            if not _manifest_hash_is_valid(manifest):
                raise TopicScopedKBError(
                    "existing KB_MANIFEST failed its integrity hash"
                )
            manifest_contract = manifest.get("reuse_contract")
            if not _reuse_contract_is_valid(manifest_contract):
                stale_dir = _relocate_stale_artifacts(work_dir, immutable_paths, started)
                return _isolated_rebuild_report(
                    stale_artifact_dir=stale_dir, runtime_kb=runtime_kb,
                    base_kb=base_kb_sqlite, policy_path=policy_path,
                    started=started,
                    reason="existing KB_MANIFEST lacks a valid reuse contract",
                )
            manifest_components = dict(manifest_contract.get("components") or {})
            expected_components = dict(expected_reuse_contract["components"])
            for key in (
                "query_plan_semantic_sha256",
                "source_base_kb_sha256",
                "effective_policy_sha256",
                "scope_contract_sha256",
            ):
                if manifest_components.get(key) != expected_components.get(key):
                    stale_dir = _relocate_stale_artifacts(
                        work_dir, immutable_paths, started
                    )
                    return _isolated_rebuild_report(
                        stale_artifact_dir=stale_dir, runtime_kb=runtime_kb,
                        base_kb=base_kb_sqlite, policy_path=policy_path,
                        started=started,
                        reason=f"existing KB_MANIFEST static input mismatch: {key}",
                    )
            if str(report.get("kb_manifest_sha256") or "") != str(
                manifest.get("manifest_sha256") or ""
            ):
                raise TopicScopedKBError(
                    "S2 bootstrap report is not bound to the current manifest"
                )
            runtime_sha256 = _sha256_file(runtime_kb)
            if runtime_sha256 not in {
                str(report.get("runtime_kb_sha256") or ""),
            } or runtime_sha256 != str(manifest.get("runtime_kb_sha256") or ""):
                raise TopicScopedKBError(
                    "S2 bootstrap runtime overlay failed its integrity hash"
                )
            telemetry_sha256 = _sha256_file(telemetry_path)
            if telemetry_sha256 != str(report.get("telemetry_sha256") or ""):
                raise TopicScopedKBError(
                    "S2 bootstrap telemetry failed its report binding"
                )
            if telemetry_sha256 != str(manifest.get("telemetry_sha256") or ""):
                raise TopicScopedKBError(
                    "S2 bootstrap telemetry failed its manifest binding"
                )
            if _sha256_file(graph_path) != str(report.get("graph_sha256") or ""):
                raise TopicScopedKBError(
                    "S2 literature graph failed its integrity hash"
                )
            material_flow_sha256 = _sha256_file(material_flow_path)
            if material_flow_sha256 != str(
                report.get("material_flow_ledger_sha256") or ""
            ):
                raise TopicScopedKBError(
                    "S2 material flow ledger failed its integrity hash"
                )
            resolved = dict(report)
            resolved.update(
                {
                    "runtime_kb_sqlite": str(runtime_kb),
                    "source_base_kb_sqlite": str(base_kb_sqlite),
                    "kb_manifest_path": str(manifest_path),
                    "graph_path": str(graph_path),
                    "material_flow_ledger_path": str(material_flow_path),
                    "reused": True,
                    "persisted_report_sha256": report.get("report_sha256", ""),
                }
            )
            return resolved

        coverage_requested_roles = list(policy.requested_roles)
        if (
            supplementary_policy is not None
            and not supplementary_policy["allow_role_expansion"]
        ):
            coverage_requested_roles = []
        local_coverage = assess_local_coverage(
            base_kb_sqlite=base_kb_sqlite,
            policy=policy,
            scope_contract=scope_contract,
            search_queries=search_queries,
            requested_roles=coverage_requested_roles,
        )
        local_coverage_decision = str(
            local_coverage.get("decision") or "insufficient"
        )
        local_only = local_coverage_decision == "sufficient"
        partial_local = local_coverage_decision == "partial"
        stage = TopicScopedKBStage(
            query_plan_path=query_plan_path,
            base_kb_sqlite=base_kb_sqlite,
            work_dir=work_dir,
            policy=policy,
            scope_contract=scope_contract,
        )
        stage.create_overlay()

        roles = list(policy.requested_roles)
        if (
            supplementary_policy is not None
            and not supplementary_policy["allow_role_expansion"]
        ):
            roles = []

        network_queries = list(search_queries)
        network_roles = list(roles)
        if local_only:
            network_queries = []
            network_roles = []
        elif partial_local:
            missing_queries = [
                str(query)
                for query in (local_coverage.get("missing_queries") or [])
                if str(query)
            ]
            missing_roles = [
                str(role)
                for role in (
                    (local_coverage.get("role_coverage") or {}).get(
                        "missing_roles"
                    )
                    or []
                )
                if str(role)
            ]
            role_metadata_available = bool(
                (local_coverage.get("role_coverage") or {}).get(
                    "metadata_available"
                )
            )
            network_roles = (
                list(missing_roles)
                if (
                    role_metadata_available
                    and missing_roles
                    and roles
                )
                else list(roles)
            )
            network_queries = list(missing_queries)
            for lens in (local_coverage.get("missing_lenses") or []):
                lens_text = str(lens).strip()
                if lens_text and lens_text not in network_queries:
                    network_queries.append(lens_text)
            if not network_queries and missing_roles and roles:
                network_queries = [query]

        portfolio = _empty_portfolio()
        if (
            policy.enabled
            and network_queries
            and policy.feature_enabled("use_relevance_search", default=True)
        ):
            discovery = S2DiscoveryPortfolioBuilder()
            portfolio = discovery.discover(
                [
                    ScholarFacetRequest(
                        facet_id="review_harness_bootstrap",
                        queries=network_queries,
                        requested_roles=network_roles,
                        max_results_per_query=effective_results_limit,
                        direct_only=effective_discovery_direct_only,
                    )
                ]
            )

        # Retain the ranker's complete ordered result up to the policy budget;
        # no positional first-N fallback can reintroduce a different topic.
        retained = [
            candidate
            for candidate in portfolio.candidates
            if candidate.decision != "reject"
        ]
        semantic_scores: dict[str, dict[str, Any]] = {}
        if (
            supplementary_policy is not None
            and discovery_direct_only
            and semantic_relevance is not None
        ):
            semantic_scores = _compute_supplementary_semantic_scores(
                candidates=retained,
                background_cue=scope_contract.search_background_cue,
                precise_queries=list(search_queries),
                engine=semantic_relevance,
            )
            stage.register_semantic_scores(semantic_scores)
        papers = []
        seen_papers: set[str] = set()
        if supplementary_policy is not None and discovery_direct_only:
            # Supplementary generated_only: rank accepted candidates by
            # usefulness before any precise/batch/graph/OA/abstract cap runs,
            # stable on the original discovery order for equal scores.
            evaluated: list[tuple[dict[str, Any], int, Any]] = []
            for original_index, candidate in enumerate(retained):
                paper = candidate.paper
                if not paper.paper_id or paper.paper_id in seen_papers:
                    continue
                decision = stage.evaluate_s2_paper(paper)
                if decision.get("accepted"):
                    evaluated.append((decision, original_index, paper))
            evaluated.sort(
                key=lambda item: (
                    -float(item[0].get("usefulness_score") or 0.0),
                    item[1],
                )
            )
            for decision, _original_index, paper in evaluated:
                if paper.paper_id in seen_papers:
                    continue
                seen_papers.add(paper.paper_id)
                papers.append(paper)
                if len(papers) >= policy.maximum_accepted_papers:
                    break
        else:
            retained = [
                candidate
                for candidate in retained
                if stage.accepts_s2_paper(candidate.paper)
            ]
            for candidate in retained:
                paper = candidate.paper
                if not paper.paper_id or paper.paper_id in seen_papers:
                    continue
                seen_papers.add(paper.paper_id)
                papers.append(paper)
                if len(papers) >= policy.maximum_accepted_papers:
                    break

        # Supplementary-only per-route caps: snippet results, precise papers,
        # batch papers, graph seeds, OA papers, and abstract papers each have
        # independent configured caps and never consume one another.
        # ``extra_request_cap`` is an audit/emergency ceiling only and never
        # shrinks a route.  Ordinary first-round plans never enter this block.
        graph_relation_controls: dict[str, bool] | None = None
        graph_relation_enabled = True
        graph_seed_cap = int(
            supplementary_policy["graph_seed_cap"]
            if supplementary_policy is not None
            else policy.graph_seed_count
        )
        if supplementary_policy is not None:
            graph_relation_controls = {
                "references": bool(
                    supplementary_policy["allow_reference_expansion"]
                ),
                "citations": bool(
                    supplementary_policy["allow_citation_expansion"]
                ),
                "recommendations": bool(
                    supplementary_policy["allow_recommendation_expansion"]
                ),
                "multi_seed": bool(
                    supplementary_policy["allow_multi_seed_graph"]
                ),
            }
            graph_relation_enabled = any(
                graph_relation_controls.get(key, False)
                for key in ("references", "citations", "recommendations")
            )
            graph_seed_cap = (
                (
                    max(1, int(supplementary_policy["graph_seed_cap"]))
                    if int(supplementary_policy["graph_seed_cap"]) > 0
                    else 0
                )
                if graph_relation_controls.get("multi_seed", True)
                else (
                    min(1, int(supplementary_policy["graph_seed_cap"]))
                    if int(supplementary_policy["graph_seed_cap"]) > 0
                    else 0
                )
            )

        chunks = _empty_chunks()
        retriever: S2TextChunkRetriever | None = None
        snippet_feature_gate = bool(
            policy.enabled
            and policy.feature_enabled("use_snippet_search", default=True)
            and search_queries
        )
        snippet_search_active = bool(
            snippet_feature_gate
            and effective_snippet_limit > 0
            and not local_only
            and (not partial_local or bool(network_queries))
        )
        if (
            snippet_search_active
        ):
            retriever = S2TextChunkRetriever(
                min_chars=policy.s2_body_snippet_min_chars
            )
            snippet_queries = (
                network_queries[: policy.max_snippet_queries]
                if partial_local
                else search_queries[: policy.max_snippet_queries]
            )
            chunks = retriever.retrieve(
                snippet_queries,
                paper_ids=[paper.paper_id for paper in papers if paper.paper_id] or None,
                limit_per_query=effective_snippet_limit,
                requested_roles=network_roles,
                scope_context={
                    "section_context": " ".join(scope_contract.lenses),
                },
            )
            for run in chunks.query_runs:
                run.setdefault("query_category", "broad_topic_snippet_search")
            if supplementary_policy is not None:
                supplementary_route_usage.record(
                    "snippet",
                    configured_cap=supplementary_policy[
                        "s2_snippet_results_per_query_cap"
                    ],
                    eligible=len(snippet_queries),
                    attempted=1,
                    outcomes=[{
                        "accepted_chunk_count": len(chunks.accepted_chunks),
                    }],
                )
        elif (
            supplementary_policy is not None
            and snippet_feature_gate
        ):
            snippet_queries = search_queries[: policy.max_snippet_queries]
            snippet_policy_reason = (
                "s2_snippet_results_per_query_cap=0"
                if int(
                    supplementary_policy["s2_snippet_results_per_query_cap"]
                )
                <= 0
                else "snippet_limit=0"
            )
            supplementary_route_usage.record(
                "snippet",
                configured_cap=supplementary_policy[
                    "s2_snippet_results_per_query_cap"
                ],
                eligible=len(snippet_queries),
                attempted=0,
                outcomes=[{"reason": snippet_policy_reason}],
            )
            chunks.query_runs.append(
                {
                    "query_category": (
                        "snippet_search_disabled_supplementary_policy"
                    ),
                    "reason": snippet_policy_reason,
                }
            )

        # Precise per-paper lookup is independent of broad snippet search:
        # a zero snippet cap must not disable an enabled precise cap.  For
        # ordinary first-round plans, precise lookup keeps its historical
        # behavior and only runs when the broad snippet stage is active.
        precise = _empty_chunks()
        precise_policy_reason = ""
        planned_precise = 0
        precise_route_active = bool(
            snippet_feature_gate
            and (snippet_search_active or supplementary_policy is not None)
            and not local_only
        )
        if precise_route_active:
            planned_precise = min(
                len(papers),
                int(
                    supplementary_policy["s2_precise_paper_cap"]
                    if supplementary_policy is not None
                    else policy.max_precise_snippet_papers
                ),
            )
            if (
                supplementary_policy is not None
                and not supplementary_policy["allow_exact_paper_followup"]
            ):
                precise_policy_reason = (
                    "allow_exact_paper_followup=false"
                )
            elif planned_precise <= 0:
                precise_policy_reason = "s2_precise_paper_cap=0"
            else:
                if retriever is None:
                    retriever = S2TextChunkRetriever(
                        min_chars=policy.s2_body_snippet_min_chars
                    )
                precise = retriever.retrieve_precise_missing_papers(
                    papers,
                    existing_chunks=chunks.accepted_chunks,
                    limit_per_paper=policy.precise_snippet_results_per_paper,
                    max_papers=planned_precise,
                    requested_roles=network_roles,
                    scope_context={
                        "section_context": " ".join(scope_contract.lenses),
                    },
                )
        chunks = merge_text_chunk_results(chunks, precise)
        if supplementary_policy is not None and snippet_feature_gate:
            supplementary_route_usage.record(
                "precise",
                configured_cap=supplementary_policy[
                    "s2_precise_paper_cap"
                ],
                eligible=len(papers),
                attempted=(
                    planned_precise
                    if not precise_policy_reason
                    else 0
                ),
                outcomes=[{
                    "accepted_chunk_count": len(precise.accepted_chunks),
                    "reason": precise_policy_reason or "allowed",
                }],
            )
        if precise_policy_reason:
            chunks.query_runs.append(
                {
                    "query_category": (
                        "precise_lookup_disabled_supplementary_policy"
                    ),
                    "reason": precise_policy_reason,
                    "requested_paper_count": planned_precise,
                }
            )

        enrichment_runs: list[dict[str, Any]] = []
        if (
            chunks.paper_ids
            and policy.enabled
            and policy.feature_enabled("use_batch_enrichment", default=True)
        ):
            batch_policy_reason = ""
            batch_cap = (
                int(supplementary_policy["batch_enrichment_paper_cap"])
                if supplementary_policy is not None
                else int(policy.max_batch_papers)
            )
            batch_ids = list(dict.fromkeys(chunks.paper_ids))[:batch_cap]
            if (
                supplementary_policy is not None
                and not supplementary_policy["allow_batch_enrichment"]
            ):
                batch_policy_reason = "allow_batch_enrichment=false"
            elif supplementary_policy is not None and batch_cap <= 0:
                batch_policy_reason = "batch_enrichment_paper_cap=0"
            if batch_policy_reason:
                enrichment_runs.append(
                    {
                        "query_category": (
                            "batch_enrichment_disabled_supplementary_policy"
                        ),
                        "reason": batch_policy_reason,
                        "requested_paper_count": len(batch_ids),
                    }
                )
                if supplementary_policy is not None:
                    supplementary_route_usage.record(
                        "batch_enrichment",
                        configured_cap=batch_cap,
                        eligible=len(chunks.paper_ids),
                        attempted=0,
                        outcomes=[{"reason": batch_policy_reason}],
                    )
            else:
                if retriever is None:
                    retriever = S2TextChunkRetriever(
                        min_chars=policy.s2_body_snippet_min_chars
                    )
                resolved, response = retriever.gateway.batch_papers(batch_ids)
                enrichment_runs.append(
                    {
                        "query_category": "batch_enrichment",
                        "endpoint": response.endpoint,
                        "status_code": response.status_code,
                        "status_category": response.status_category,
                        "cache_hit": response.cache_hit,
                        "wait_seconds": response.wait_seconds,
                        "result_count": len(resolved),
                        "paper_ids": batch_ids,
                    }
                )
                papers, retention = _retain_validated_snippet_parents(
                    discovery_papers=papers,
                    resolved_papers=resolved,
                    chunks=chunks.accepted_chunks,
                    stage=stage,
                    maximum_papers=policy.maximum_accepted_papers,
                )
                enrichment_runs[-1].update({
                    "parent_retention": retention,
                })
                if supplementary_policy is not None:
                    supplementary_route_usage.record(
                        "batch_enrichment",
                        configured_cap=batch_cap,
                        eligible=len(chunks.paper_ids),
                        attempted=len(batch_ids),
                        outcomes=[{
                            "result_count": len(resolved),
                            "reason": "allowed",
                        }],
                    )

        graph = None
        graph_runs: list[dict[str, Any]] = []
        if (
            policy.enabled
            and policy.graph_depth > 0
            and papers
            and policy.feature_enabled("build_literature_graph", default=True)
            and graph_expansion_allowed
            and graph_relation_enabled
            and not local_only
        ):
            graph_seeds = papers[: max(0, int(graph_seed_cap))]
            graph_route_reason = (
                "graph_seed_cap=0"
                if int(graph_seed_cap) <= 0
                else "allowed"
            )
            graph = None
            if graph_seeds:
                graph = _build_policy_graph(
                    seeds=graph_seeds,
                    topic_queries=(
                        network_queries
                        if partial_local and network_queries
                        else search_queries
                    ),
                    policy=policy,
                    relation_controls=graph_relation_controls,
                )
            graph_runs = list(graph.query_runs) if graph is not None else []
            if supplementary_policy is not None:
                graph_runs.append(
                    {
                        "query_category": "graph_expansion_policy",
                        "effective_graph_modes": list(graph_modes),
                        "relation_controls": dict(graph_relation_controls or {}),
                        "seed_count": len(graph_seeds),
                        "route_reason": graph_route_reason,
                    }
                )
                supplementary_route_usage.record(
                    "graph",
                    configured_cap=int(graph_seed_cap),
                    eligible=len(papers),
                    attempted=len(graph_seeds),
                    outcomes=[{"reason": graph_route_reason}],
                )
            if (
                graph is not None
                and policy.feature_enabled("use_ref_mentions", default=True)
            ):
                S2LiteratureGraphBuilder.add_snippet_reference_mentions(
                    graph, chunks.accepted_chunks
                )

        chunks_for_ingest = (
            chunks.accepted_chunks
            if policy.feature_enabled(
                "register_s2_body_snippets_as_text_chunks", default=True
            )
            else []
        )
        ingest = stage.ingest_s2(
            papers=papers,
            chunks=chunks_for_ingest,
            graph=graph,
        )
        graph_path.write_text(
            json.dumps(
                stage.filtered_graph.to_dict() if stage.filtered_graph else {},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        network_paper_ids = [
            paper.paper_id for paper in papers if paper.paper_id
        ]
        selected_local_paper_ids = list(
            stage.selection_report.get("selected_paper_ids") or []
        )
        if local_only or partial_local:
            all_paper_ids = list(
                dict.fromkeys(
                    [*selected_local_paper_ids, *network_paper_ids]
                )
            )
            ledger_papers = [
                *_overlay_paper_snapshots(
                    runtime_kb, selected_local_paper_ids
                ),
                *papers,
            ]
        else:
            all_paper_ids = list(network_paper_ids)
            ledger_papers = list(papers)
        pre_oa_inventory = _material_inventory(runtime_kb, all_paper_ids)
        missing_primary_papers = [
            paper
            for paper in papers
            if not _has_primary_material(
                pre_oa_inventory.get(paper.paper_id, {})
            )
        ]
        sufficient_before_oa = [
            paper
            for paper in papers
            if _has_primary_material(
                pre_oa_inventory.get(paper.paper_id, {})
            )
        ]
        # Visual supplementary tasks need OA/fulltext acquisition for figure
        # extraction even when S2 primary text already exists.  Only then are
        # S2-sufficient papers included in the fulltext selection (still
        # bounded by the visual oa_fulltext_paper_cap and public-route limits).
        supplementary_visual_processing = bool(
            supplementary_policy is not None
            and supplementary_policy.get("allow_visual_processing")
        )
        visual_fulltext_eligible = bool(
            visual_fulltext_processing or supplementary_visual_processing
        )
        oa_selection_papers = (
            papers if visual_fulltext_eligible else missing_primary_papers
        )
        visual_intent: dict[str, Any] = (
            {
                "enabled": True,
                "reason": (
                    "allow_visual_processing=true; figure extraction "
                    "requires OA/fulltext acquisition even when S2 "
                    "primary text exists"
                ),
                "s2_sufficient_papers_selected_for_fulltext": [
                    paper.paper_id for paper in sufficient_before_oa
                ],
            }
            if visual_fulltext_eligible
            else {"enabled": False}
        )
        fulltext_report: dict[str, Any] = {
            "selected_paper_ids": [],
            "skipped": [
                {
                    "paper_id": paper.paper_id,
                    "reason": "s2_material_sufficient",
                }
                for paper in sufficient_before_oa
                if not visual_fulltext_eligible
            ],
            "visual_intent": visual_intent,
            "new_chunk_ids": [],
            "reused_chunk_ids": [],
            "new_paper_ids": [],
            "stats": {
                "attempted": 0,
                "downloaded": 0,
                "parse_failed": 0,
                "paper_outcomes": [],
            },
        }
        if local_only or partial_local:
            local_skip_reasons = []
            for paper_id in selected_local_paper_ids:
                if _has_primary_material(
                    pre_oa_inventory.get(paper_id, {})
                ):
                    local_skip_reasons.append(
                        {
                            "paper_id": paper_id,
                            "reason": "local_cache_material_sufficient",
                        }
                    )
                else:
                    local_skip_reasons.append(
                        {
                            "paper_id": paper_id,
                            "reason": (
                                "local_cache_paper_not_selected_for_oa"
                            ),
                        }
                    )
            fulltext_report["skipped"] = [
                *local_skip_reasons,
                *fulltext_report["skipped"],
            ]
        if (
            policy.enabled
            and policy.feature_enabled(
                "download_high_value_oa_without_llm_gate", default=True
            )
            and oa_selection_papers
        ):
            selections = [
                (
                    paper,
                    decide_fulltext_escalation(
                        paper,
                        role_labels=network_roles,
                        need_complete_context=True,
                        need_visual_assets=True,
                    ),
                )
                for paper in oa_selection_papers
            ]
            oa_disabled_reason = ""
            if (
                supplementary_policy is not None
                and not supplementary_policy["allow_oa_fulltext_fallback"]
            ):
                oa_disabled_reason = "allow_oa_fulltext_fallback=false"
            elif supplementary_policy is not None or visual_fulltext_processing:
                oa_cap = (
                    min(10, max(0, int(oa_fulltext_paper_cap)))
                    if visual_fulltext_processing
                    else int(supplementary_policy["oa_fulltext_paper_cap"])
                )
                if oa_cap <= 0:
                    oa_disabled_reason = "oa_fulltext_paper_cap=0"
                else:
                    selections = selections[:oa_cap]
                    if (
                        visual_fulltext_eligible
                        and len(oa_selection_papers) > oa_cap
                    ):
                        fulltext_report["skipped"].extend(
                            {
                                "paper_id": paper.paper_id,
                                "reason": (
                                    "visual_oa_fulltext_cap_reached"
                                ),
                            }
                            for paper in oa_selection_papers[oa_cap:]
                        )
                    fulltext = S2FulltextAcquirer(
                        kb_sqlite=runtime_kb,
                        download_dir=work_dir / "downloads",
                    ).acquire(
                        selections,
                        max_successes=min(
                            int(policy.maximum_oa_downloads), len(selections)
                        ),
                        source_task_id="review_harness_bootstrap",
                        max_workers=resolve_oa_worker_count(),
                    )
                    acquired = fulltext.to_dict()
                    fulltext_report = {
                        **acquired,
                        "skipped": [
                            *fulltext_report["skipped"],
                            *list(acquired.get("skipped") or []),
                        ],
                        "visual_intent": visual_intent,
                    }
            else:
                fulltext = S2FulltextAcquirer(
                    kb_sqlite=runtime_kb,
                    download_dir=work_dir / "downloads",
                ).acquire(
                    selections,
                    max_successes=policy.maximum_oa_downloads,
                    source_task_id="review_harness_bootstrap",
                    max_workers=resolve_oa_worker_count(),
                )
                acquired = fulltext.to_dict()
                fulltext_report = {
                    **acquired,
                    "skipped": [
                        *fulltext_report["skipped"],
                        *list(acquired.get("skipped") or []),
                    ],
                    "visual_intent": visual_intent,
                }
            if oa_disabled_reason:
                fulltext_report["skipped"].extend(
                    {
                        "paper_id": paper.paper_id,
                        "reason": (
                            "supplementary_policy_disabled:"
                            + oa_disabled_reason
                        ),
                    }
                    for paper in oa_selection_papers
                )
                fulltext_report["supplementary_policy_disabled"] = (
                    oa_disabled_reason
                )
            if supplementary_policy is not None:
                supplementary_route_usage.record(
                    "oa_fulltext",
                    configured_cap=int(
                        supplementary_policy["oa_fulltext_paper_cap"]
                    ),
                    eligible=len(oa_selection_papers),
                    attempted=(
                        len(selections)
                        if not oa_disabled_reason
                        else 0
                    ),
                    outcomes=[{
                        "downloaded": int(
                            (fulltext_report.get("stats") or {}).get(
                                "downloaded", 0
                            )
                        ),
                        "reason": oa_disabled_reason or "allowed",
                    }],
                )

        post_oa_inventory = _material_inventory(runtime_kb, all_paper_ids)
        abstract_outcomes: dict[str, dict[str, Any]] = {}
        abstract_chunks = []
        abstract_papers = []
        abstract_candidates = [
            paper
            for paper in papers
            if not _has_primary_material(
                post_oa_inventory.get(paper.paper_id, {})
            )
            and not (
                post_oa_inventory.get(paper.paper_id, {}).get(
                    "abstract_claim_chunk_ids"
                )
            )
        ]
        abstract_cap = (
            int(supplementary_policy["abstract_claim_paper_cap"])
            if supplementary_policy is not None
            else int(policy.max_abstract_claim_papers)
        )
        for index, paper in enumerate(abstract_candidates):
            if index >= abstract_cap:
                abstract_outcomes[paper.paper_id] = {
                    "status": "abstract_claim_budget_reached"
                }
                continue
            chunk, reason = materialize_abstract_claim(paper)
            if chunk is None:
                abstract_outcomes[paper.paper_id] = {"status": reason}
                continue
            abstract_chunks.append(chunk)
            abstract_papers.append(
                replace(
                    paper,
                    materialization_route=str(
                        chunk.route_provenance.get("materialization_route")
                        or "verified_abstract_claim_after_body_and_oa_miss"
                    ),
                    content_depth="abstract_claim",
                    use_permission="contextual_or_qualified_support",
                    route_events=[
                        *list(paper.route_events),
                        {
                            "event": "abstract_claim_materialized",
                            "after": [
                                "s2_structured_body_snippet_missing",
                                "public_oa_fulltext_missing",
                            ],
                        },
                    ],
                )
            )
            abstract_outcomes[paper.paper_id] = {
                "status": "materialized",
                "chunk_id": chunk.chunk_id,
            }
        if supplementary_policy is not None:
            supplementary_route_usage.record(
                "abstract_claim",
                configured_cap=abstract_cap,
                eligible=len(abstract_candidates),
                attempted=len(abstract_chunks),
                outcomes=[dict(abstract_outcomes)],
            )

        abstract_claim_ingest: dict[str, Any] = {}
        if abstract_chunks:
            abstract_claim_ingest = stage.ingest_s2_supplement(
                papers=abstract_papers,
                chunks=abstract_chunks,
                label="abstract_claim_after_s2_and_oa_miss",
            )

        final_inventory = _material_inventory(runtime_kb, all_paper_ids)
        material_flow = _build_material_flow_ledger(
            papers=ledger_papers,
            inventory=final_inventory,
            precise_runs=chunks.query_runs,
            fulltext_report=fulltext_report,
            abstract_outcomes=abstract_outcomes,
        )
        material_flow_path.write_text(
            json.dumps(material_flow, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        material_flow_sha256 = _sha256_file(material_flow_path)

        telemetry = build_s2_query_telemetry(
            discovery_runs=portfolio.query_runs,
            snippet_runs=chunks.query_runs,
            graph_runs=graph_runs,
            enrichment_runs=enrichment_runs,
            extra_events=_local_coverage_skipped_events(local_coverage),
        )
        if supplementary_policy is not None:
            supplementary_policy["route_usage"] = (
                supplementary_route_usage.to_dict()
                if supplementary_route_usage is not None
                else {}
            )
        selected_local_chunk_ids = _overlay_chunk_ids(
            runtime_kb, selected_local_paper_ids
        )
        stage_result = stage.finalize(
            query_telemetry=telemetry,
            extra_manifest={
                "graph_path": str(graph_path),
                "graph_summary": ingest.get("graph_summary") or {},
                "fulltext_fallback": fulltext_report,
                "abstract_claim_ingest": abstract_claim_ingest,
                "material_flow_ledger_path": str(material_flow_path),
                "material_flow_ledger_sha256": material_flow_sha256,
                "material_flow_summary": material_flow["summary"],
                "graph_depth": policy.graph_depth,
                "local_coverage_decision": local_coverage_decision,
                "local_coverage": local_coverage,
                "local_cache_reuse": bool(local_only or partial_local),
                "reused_local_paper_count": len(selected_local_paper_ids),
                "reused_local_chunk_count": len(selected_local_chunk_ids),
            },
        )
        if local_only:
            network_requests_avoided = [
                "broad_discovery",
                "broad_snippet_search",
                "precise_followup",
                "batch_enrichment",
                "graph_expansion",
                "oa_acquisition",
            ]
        elif partial_local:
            network_requests_avoided = [
                "broad_discovery_covered_queries",
                "broad_snippet_covered_queries",
            ]
        else:
            network_requests_avoided = []
        report = {
            "schema_version": BOOTSTRAP_SCHEMA_VERSION,
            "status": stage_result["status"],
            "query": query,
            "search_queries": search_queries,
            "executed_search_queries": list(network_queries),
            "local_coverage_decision": local_coverage_decision,
            "local_coverage": local_coverage,
            "local_cache_reuse": bool(local_only or partial_local),
            "local_first": bool(local_only),
            "network_requests_avoided": network_requests_avoided,
            "reused_local_paper_count": len(selected_local_paper_ids),
            "reused_local_chunk_count": len(selected_local_chunk_ids),
            "reused_local_paper_ids": sorted(selected_local_paper_ids),
            "reused_local_chunk_ids": selected_local_chunk_ids,
            "discovery_direct_only": bool(effective_discovery_direct_only),
            "discovery_generated_only": bool(discovery_direct_only),
            "graph_expansion_suppressed": bool(
                discovery_direct_only
                and not (
                    bool(supplementary_policy["allow_graph_expansion"])
                    if supplementary_policy is not None
                    else allow_graph_expansion
                )
            ),
            "graph_expansion_allowed": bool(graph_expansion_allowed),
            "supplementary_policy": supplementary_policy,
            "supplementary_graph_modes": list(graph_modes),
            "keyword_count": len(scope_contract.keywords),
            "scope_contract": scope_contract.to_dict(),
            "policy_path": policy.config_path,
            "policy_sha256": policy.config_sha256,
            "scope_decision_rule_version": SCOPE_DECISION_RULE_VERSION,
            "reuse_contract": expected_reuse_contract,
            "runtime_kb_sqlite": str(runtime_kb),
            "runtime_kb_sha256": _sha256_file(runtime_kb),
            "source_base_kb_sqlite": str(base_kb_sqlite),
            "source_base_kb_sha256": _sha256_file(base_kb_sqlite),
            "portfolio_candidates": len(portfolio.candidates),
            "retained_candidates": len(retained),
            "semantic_candidate_score_count": len(semantic_scores),
            "semantic_relevance_usage": (
                semantic_relevance.usage.to_dict()
                if semantic_relevance is not None
                and hasattr(semantic_relevance, "usage")
                else None
            ),
            "accepted_s2_body_chunks": int(ingest.get("chunks_accepted") or 0),
            "accepted_abstract_claim_chunks": int(
                abstract_claim_ingest.get("chunks_accepted") or 0
            ),
            "kb_ingest": ingest,
            "graph_summary": ingest.get("graph_summary") or {},
            "graph_path": str(graph_path),
            "graph_sha256": _sha256_file(graph_path),
            "fulltext_fallback": fulltext_report,
            "abstract_claim_ingest": abstract_claim_ingest,
            "material_flow_summary": material_flow["summary"],
            "material_flow_ledger_path": str(material_flow_path),
            "material_flow_ledger_sha256": material_flow_sha256,
            "external_query_runs": (
                list(portfolio.query_runs)
                + list(chunks.query_runs)
                + list(enrichment_runs)
                + list(graph_runs)
            ),
            # Ticket 1.4: external_query_runs duplicates the raw rows that
            # s2_query_telemetry aggregates.  Consumers must not SUM across
            # both; treat s2_query_telemetry as the authoritative aggregate
            # and deduplicate raw rows on
            # (wave_id, request_index, query, facet_id) when needed.
            "external_query_runs_note": (
                "raw-row archive only; s2_query_telemetry is the "
                "authoritative aggregate -- summing both double-counts"
            ),
            "s2_query_telemetry": telemetry,
            "telemetry_sha256": _sha256_file(telemetry_path),
            "provenance_counts": stage_result["provenance_counts"],
            "evidence": stage_result["evidence"],
            "table_counts": stage_result["table_counts"],
            "kb_manifest_path": stage_result["manifest_path"],
            "kb_manifest_sha256": stage_result["manifest_sha256"],
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_cny": 0.0,
            "wall_time_seconds": round(time.monotonic() - started, 3),
            "reused": False,
        }
        return _write_json(report_path, report)
    except (S2PolicyError, TopicScopedKBError, ValueError, OSError, json.JSONDecodeError) as exc:
        if occupied_before_call:
            return _reuse_rejection_report(
                runtime_kb=runtime_kb,
                base_kb=base_kb_sqlite,
                policy_path=policy_path,
                started=started,
                reason=str(exc),
            )
        return _failed_report(
            report_path=report_path,
            runtime_kb=runtime_kb,
            base_kb=base_kb_sqlite,
            started=started,
            error_code="s2_bootstrap_failed",
            error=exc,
            policy_path=policy_path,
        )
    except Exception as exc:  # the report must remain truthful for unexpected runtime failures
        if occupied_before_call:
            return _reuse_rejection_report(
                runtime_kb=runtime_kb,
                base_kb=base_kb_sqlite,
                policy_path=policy_path,
                started=started,
                reason=str(exc),
            )
        return _failed_report(
            report_path=report_path,
            runtime_kb=runtime_kb,
            base_kb=base_kb_sqlite,
            started=started,
            error_code="s2_bootstrap_unexpected_failure",
            error=exc,
            policy_path=policy_path,
        )
