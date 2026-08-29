"""Section Literature Coverage tool registry — 11 deterministic FunctionTools.

All tools are pure-Python closures bound to a SectionCoverageContext.
No LLM calls happen inside any tool. The agent (AgentScope ReAct) handles
all planning, ranking judgements, and scope/role decisions.
"""

from __future__ import annotations

import json
import functools
import hashlib
import logging
import multiprocessing
import os
import queue
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agentscope.tool import FunctionTool

from .artifact_schemas import (
    AcquisitionStatus,
    CandidateAction,
    CandidateDecision,
    GapEntry,
    LocalCoverageAudit,
    LocalRoleAudit,
    MaterializationManifest,
    MaterializedPaper,
    OACandidate,
    OACandidateLedger,
    RolePlan,
    RolePriority,
    SectionContext,
    SectionCoveragePlan,
    SectionGapReport,
    SectionMaterialPackage,
    SectionSourceLedger,
    ScopeFit,
    SourceEntry,
)
from .artifact_store import atomic_write_json
from .tool_provider import SectionCoverageContext, ToolProvider
from .topic_identity import (
    anchor_retrieval_query,
    assess_topic_alignment,
    topic_tokens,
)
from .argument_quality_policy import evidence_ceiling
from .review_quality_contract import (
    build_adaptive_coverage_contract,
    evaluate_adaptive_coverage,
    normalize_scope_fit,
    source_route_record,
)
from .phase2_phase3_feedback import canonical_material_identity
from .coverage_ledger import (
    get_audit as get_global_audit,
    get_query as get_global_query,
    increment_stat as increment_global_stat,
    record_audit as record_global_audit,
    record_material as record_global_material,
    record_query as record_global_query,
)
from .coverage_decision_contract import (
    COVERAGE_ROLES as CONTRACT_COVERAGE_ROLES,
    build_uncovered_query_targets,
    candidate_query_affinity,
    candidate_has_legal_route,
    candidate_is_materializable,
    canonical_candidate_decision,
    closed_scientific_components,
    compact_text,
    decode_json_payload,
    derive_uncovered_roles,
    evaluate_candidate_topic_affinity,
    evaluate_coverage_readiness,
    admit_context_call,
    admit_batched_audit_call,
    assess_explicit_scope_boundary,
    assess_retrieved_paper_scope_boundary,
    build_compact_batched_audit_payload,
    estimate_json_tokens,
    stable_payload_fingerprint,
    structured_snippet_route_decision,
    normalize_scientific_query,
    normalize_scope_violation_records,
    scope_violation_outcome,
)

logger = logging.getLogger(__name__)

COVERAGE_ROLES = CONTRACT_COVERAGE_ROLES
MIN_VALID_PDF_BYTES = 5_000
LOCAL_CANDIDATE_LEDGER = "LOCAL_CANDIDATE_LEDGER.json"
SEARCH_BUDGET_LEDGER = "SEARCH_BUDGET_LEDGER.json"
MAX_INSPECTION_CANDIDATES = 6
LOCAL_AUDIT_MAX_INSPECTION_CANDIDATES = 40
MAX_ABSTRACT_PREVIEW_CHARS = 1200
MAX_ADJACENT_MATERIALIZED_PER_SECTION = 2
MAX_OA_ROUTES_PER_CANDIDATE = 3
MAX_MATERIALIZATION_SECONDS_PER_PAPER = 150
MAX_MATERIALIZATION_SECONDS_PER_CALL = 170
ARTICLE_EVIDENCE_PORTFOLIO = "ARTICLE_EVIDENCE_PORTFOLIO.json"
COVERAGE_WAVE_TELEMETRY = "COVERAGE_WAVE_TELEMETRY.json"
MAX_AUDIT_CANDIDATES_PER_WAVE = 6

_HIGH_STRENGTH_COVERAGE_ROLES = frozenset({
    "mechanism",
    "method",
    "controversy",
})
_ROLE_ALLOWED_CLAIM_KINDS = {
    "foundation": frozenset({
        "background", "paper_reported_claim", "trend", "author_synthesis",
    }),
    "mechanism": frozenset({
        "mechanism", "causality", "paper_reported_claim", "author_synthesis",
    }),
    "method": frozenset({
        "method", "measurement", "paper_reported_claim", "author_synthesis",
    }),
    "frontier": frozenset({
        "trend", "candidate_lead", "paper_reported_claim", "author_synthesis",
        "background",
    }),
    "controversy": frozenset({
        "controversy", "paper_reported_claim", "author_synthesis",
    }),
    "application": frozenset({
        "application", "paper_reported_claim", "author_synthesis", "background",
        "trend",
    }),
}


def _normalise_coverage_claim_kinds(value: Any) -> set[str]:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                value = json.loads(raw)
            except Exception:
                value = raw.split(",")
        else:
            value = raw.split(",")
    try:
        values = list(value or [])
    except TypeError:
        values = []
    return {
        str(item).strip().casefold().replace("-", "_")
        for item in values
        if str(item).strip()
    }


def _coverage_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _coverage_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coverage_depth(value: Any, *, default: str = "metadata") -> str:
    raw = str(value or default).strip().casefold().replace("-", "_")
    return {
        "full_text": "fulltext",
        "fulltext_with_visuals": "fulltext",
        "abstract_only": "abstract",
        "materialized_abstract_claim": "abstract_claim",
        "s2_body_snippet": "structured_snippet",
        "text_chunk": "structured_snippet",
    }.get(raw, raw)


def _coverage_canonical_chunk_ids(record: Mapping[str, Any]) -> List[str]:
    raw = record.get("canonical_chunk_ids")
    if isinstance(raw, str):
        raw = [raw]
    try:
        ids = [str(item).strip() for item in (raw or []) if str(item).strip()]
    except TypeError:
        ids = []
    if not ids and str(record.get("chunk_id") or "").strip():
        ids = [str(record.get("chunk_id")).strip()]
    return list(dict.fromkeys(ids))


def _coverage_contract_present(record: Mapping[str, Any]) -> bool:
    if "permission_contract_present" in record:
        return _coverage_bool(record.get("permission_contract_present"))
    for key in (
        "content_depth",
        "use_permission",
        "source_kind",
        "discovery_route",
        "materialization_route",
        "route_provenance",
        "context_complete",
        "allowed_claim_kinds",
    ):
        if key not in record:
            continue
        value = record.get(key)
        if key == "context_complete" or value not in (None, "", [], {}):
            return True
    return False


def _coverage_context_complete(record: Mapping[str, Any], depth: str) -> bool:
    if "context_complete" in record and record.get("context_complete") is not None:
        return _coverage_bool(record.get("context_complete"))
    for key in ("route_provenance", "provenance"):
        nested = _coverage_json_object(record.get(key))
        if "context_complete" in nested:
            return _coverage_bool(nested.get("context_complete"))
    for event in record.get("route_events") or []:
        if isinstance(event, Mapping) and "context_complete" in event:
            return _coverage_bool(event.get("context_complete"))
    return depth == "fulltext"


def _role_material_is_coverage_eligible(
    record: Mapping[str, Any],
    role: str,
) -> bool:
    """Return whether an adopted chunk can close the requested literature role."""

    if not isinstance(record, Mapping) or not _coverage_canonical_chunk_ids(record):
        return False
    role = str(role or "").strip().casefold()
    depth = _coverage_depth(record.get("content_depth"))
    allowed = _normalise_coverage_claim_kinds(record.get("allowed_claim_kinds"))
    contract_present = _coverage_contract_present(record)
    permission = str(record.get("use_permission") or "").strip().casefold()
    discovery = str(record.get("discovery_route") or "").strip().casefold()
    materialization = str(
        record.get("materialization_route") or ""
    ).strip().casefold()
    legacy_source_defaults = (
        depth == "metadata"
        and permission in {"", "discovery_only"}
        and not allowed
        and str(record.get("acquisition_status") or "").strip().casefold() == "fulltext"
        and discovery in {"", "unknown"}
        and materialization in {"", "unknown", "not_materialized"}
        and not record.get("route_events")
    )
    effective = dict(record)
    legacy_fulltext = (
        not contract_present and depth in {"fulltext", "structured_snippet"}
    ) or legacy_source_defaults
    if legacy_source_defaults:
        depth = "fulltext"
    effective["content_depth"] = depth
    effective["context_complete"] = _coverage_context_complete(record, depth)
    if legacy_fulltext:
        # Simplified legacy KB fixtures have real text chunks but no route
        # columns.  Their historical meaning is fulltext; only this exact
        # compatibility case receives the factual ceiling.
        effective.setdefault("scope_fit", "direct")
        if legacy_source_defaults:
            effective["scope_fit"] = "direct"
        effective["use_permission"] = "factual_support"
        effective["context_complete"] = True
    ceiling, _ = evidence_ceiling(effective)

    role_kinds = _ROLE_ALLOWED_CLAIM_KINDS.get(role, frozenset())
    if allowed and role_kinds and not (allowed & role_kinds):
        return False
    if role in _HIGH_STRENGTH_COVERAGE_ROLES:
        return (
            depth in {"fulltext", "structured_snippet"}
            and ceiling == "factual_support"
        )
    return ceiling in {
        "factual_support",
        "contextual_or_qualified_support",
        "background_and_candidate_only",
    }


def _role_coverage_sources(
    sources: Iterable[Mapping[str, Any]],
    role: str,
) -> List[Mapping[str, Any]]:
    return [
        source for source in sources
        if isinstance(source, Mapping)
        and str(source.get("literature_role") or "").strip().casefold() == str(role).strip().casefold()
        and _role_material_is_coverage_eligible(source, role)
    ]
ROLE_DEFINITIONS = {
    "foundation": (
        "establishes the historical or conceptual basis of this section; a "
        "generic review introduction is not automatically foundational"
    ),
    "mechanism": (
        "explains causal physics, governing equations, or an underlying process"
    ),
    "method": (
        "explains how the section-specific design, measurement, fabrication, "
        "characterization, or analysis is actually performed"
    ),
    "frontier": (
        "documents a recent advance that changes the current capability boundary"
    ),
    "controversy": (
        "documents conflicting evidence, a definitional or measurement dispute, "
        "or an unresolved scientific disagreement; a limitation alone is not a controversy"
    ),
    "application": (
        "documents deployment, system integration, use-case performance, or "
        "application-specific constraints"
    ),
}


def _article_portfolio_path(ctx: SectionCoverageContext) -> Optional[Path]:
    path = getattr(ctx, "article_evidence_portfolio_path", None)
    if path:
        return Path(path)
    return None


def _empty_article_portfolio(ctx: SectionCoverageContext) -> Dict[str, Any]:
    return {
        "schema_version": "phase2.article_evidence_portfolio.v1",
        "topic_fingerprint": str(
            (ctx.section_data.get("topic_identity") or {}).get("fingerprint") or ""
        ),
        "candidates": [],
        "audits": {},
        "materials": {},
        "section_links": {},
        "telemetry": {
            "candidate_upserts": 0,
            "candidate_reuse_hits": 0,
            "audit_reuse_hits": 0,
            "material_reuse_hits": 0,
            "duplicate_identities_collapsed": 0,
        },
    }


def _read_article_portfolio(ctx: SectionCoverageContext) -> Dict[str, Any]:
    path = _article_portfolio_path(ctx)
    value = {}
    if path and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = raw if isinstance(raw, dict) else {}
        except Exception:
            value = {}
    if not value:
        value = _empty_article_portfolio(ctx)
    base = _empty_article_portfolio(ctx)
    for key, default in base.items():
        if key not in value:
            value[key] = default
    if not isinstance(value.get("candidates"), list):
        value["candidates"] = []
    if not isinstance(value.get("audits"), dict):
        value["audits"] = {}
    if not isinstance(value.get("materials"), dict):
        value["materials"] = {}
    if not isinstance(value.get("section_links"), dict):
        value["section_links"] = {}
    if not isinstance(value.get("telemetry"), dict):
        value["telemetry"] = dict(base["telemetry"])
    for key, default in base["telemetry"].items():
        value["telemetry"].setdefault(key, default)
    return value


def _write_article_portfolio(ctx: SectionCoverageContext, value: Dict[str, Any]) -> None:
    path = _article_portfolio_path(ctx)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_artifact(path.parent, path.name, value)


def _article_candidate_id(identity: str) -> str:
    return "article_cand_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def _article_candidate_roles(candidate: Dict[str, Any]) -> List[str]:
    values = [candidate.get("role"), *(candidate.get("role_fit") or [])]
    return list(dict.fromkeys(
        str(value).strip().casefold()
        for value in values
        if str(value or "").strip()
    ))


def _role_union(*values: Any) -> List[str]:
    """Stable role union used at every candidate persistence boundary."""

    result: List[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            role = str(item or "").strip().casefold()
            if role in COVERAGE_ROLES and role not in result:
                result.append(role)
    return result


def _merge_role_provenance(*values: Any) -> Dict[str, List[str]]:
    """Merge role -> originating query records without overwriting."""

    merged: Dict[str, List[str]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for raw_role, raw_queries in value.items():
            role = str(raw_role or "").strip().casefold()
            if role not in COVERAGE_ROLES:
                continue
            queries = raw_queries if isinstance(raw_queries, list) else [raw_queries]
            bucket = merged.setdefault(role, [])
            for query in queries:
                text = str(query or "").strip()
                if text and text not in bucket:
                    bucket.append(text)
    return merged


def _article_audit_for_candidate(ctx: SectionCoverageContext, candidate: Dict[str, Any]) -> Dict[str, Any]:
    identity = _candidate_identity(candidate)
    if not identity:
        return {}
    portfolio = _read_article_portfolio(ctx)
    cached = portfolio.get("audits", {}).get(identity) or {}
    if not isinstance(cached, dict):
        return {}
    role = str(candidate.get("role") or "").casefold()
    role_fit = {str(item).casefold() for item in cached.get("role_fit") or []}
    if cached.get("decision") not in {"approved", "rejected"}:
        return {}
    if role and role_fit and role not in role_fit and cached.get("decision") == "approved":
        return {}
    portfolio["telemetry"]["audit_reuse_hits"] = int(
        portfolio["telemetry"].get("audit_reuse_hits", 0) or 0
    ) + 1
    _write_article_portfolio(ctx, portfolio)
    return dict(cached)


def _apply_article_audit(ctx: SectionCoverageContext, candidate: Dict[str, Any]) -> Dict[str, Any]:
    cached = _article_audit_for_candidate(ctx, candidate)
    if not cached:
        return candidate
    candidate = dict(candidate)
    candidate["scope_fit"] = cached.get("scope_fit", candidate.get("scope_fit", "unreviewed"))
    candidate["decision"] = cached.get("decision", candidate.get("decision", "deferred"))
    candidate["role_fit"] = _role_union(
        candidate.get("role"), candidate.get("role_fit"), cached.get("role_fit")
    )
    candidate["role_provenance"] = _merge_role_provenance(
        candidate.get("role_provenance"), cached.get("role_provenance")
    )
    candidate["scope_violations"] = normalize_scope_violation_records(
        [*(candidate.get("scope_violations") or []), *(cached.get("scope_violations") or [])]
    )
    candidate["boundary_violations"] = normalize_scope_violation_records(
        [*(candidate.get("boundary_violations") or []), *(cached.get("boundary_violations") or [])]
    )
    candidate["audit_reason"] = str(cached.get("audit_reason") or "article_portfolio_audit_reused")
    candidate["not_usable_for"] = list(cached.get("not_usable_for") or candidate.get("not_usable_for") or [])
    contract = canonical_candidate_decision(candidate)
    candidate["candidate_action"] = contract.action
    candidate["material_identity"] = _candidate_identity(candidate)
    candidate["candidate_action_provenance"] = _candidate_action_provenance(contract)
    return candidate


def _upsert_article_candidate(ctx: SectionCoverageContext, candidate: Dict[str, Any]) -> Dict[str, Any]:
    identity = _candidate_identity(candidate)
    if not identity or _article_portfolio_path(ctx) is None:
        return dict(candidate)
    portfolio = _read_article_portfolio(ctx)
    rows = portfolio["candidates"]
    existing = next((row for row in rows if str(row.get("material_identity") or "") == identity), None)
    compact = {
        key: candidate.get(key)
        for key in (
            "title", "doi", "year", "venue", "authors", "abstract", "is_oa",
            "oa_url", "pdf_url", "url_for_pdf", "best_oa_url", "open_access_url",
            "html_url", "repository_url", "content_urls", "alternate_urls",
            "semantic_scholar_id", "corpus_id", "openalex_id", "tldr",
            "text_availability", "citation_count", "backends", "query_texts",
            "relevance_score", "scope_fit", "role_fit", "decision", "candidate_action",
            "audit_reason", "not_usable_for", "topic_fingerprint",
            "explicit_topic_bridge", "role_provenance", "scope_violations",
            "boundary_violations",
        )
        if candidate.get(key) is not None
    }
    compact.update({
        "material_identity": identity,
        "candidate_id": _article_candidate_id(identity),
        "source_sections": list(dict.fromkeys([
            *(existing.get("source_sections") if existing else []),
            ctx.section_id,
        ])),
        "roles": list(dict.fromkeys([
            *(existing.get("roles") if existing else []),
            *_article_candidate_roles(candidate),
        ])),
    })
    compact["role_fit"] = _role_union(
        *(existing.get("role_fit") if existing else []),
        candidate.get("role"), candidate.get("role_fit"),
    )
    compact["role_provenance"] = _merge_role_provenance(
        existing.get("role_provenance") if existing else {},
        candidate.get("role_provenance") or {},
    )
    compact["scope_violations"] = normalize_scope_violation_records(
        [*(existing.get("scope_violations") if existing else []), *(candidate.get("scope_violations") or [])]
    )
    compact["boundary_violations"] = normalize_scope_violation_records(
        [*(existing.get("boundary_violations") if existing else []), *(candidate.get("boundary_violations") or [])]
    )
    if existing is None:
        rows.append(compact)
        portfolio["telemetry"]["candidate_upserts"] = int(
            portfolio["telemetry"].get("candidate_upserts", 0) or 0
        ) + 1
    else:
        old = dict(existing)
        old_abstract = str(old.get("abstract") or "")
        if len(str(compact.get("abstract") or "")) < len(old_abstract):
            compact["abstract"] = old_abstract
        old.update({key: value for key, value in compact.items() if value not in (None, "", [], {})})
        existing.clear()
        existing.update(old)
        portfolio["telemetry"]["duplicate_identities_collapsed"] = int(
            portfolio["telemetry"].get("duplicate_identities_collapsed", 0) or 0
        ) + 1
    _write_article_portfolio(ctx, portfolio)
    return _apply_article_audit(ctx, {
        **dict(candidate),
        **compact,
        "candidate_id": compact["candidate_id"],
        "role": str(candidate.get("role") or (compact.get("roles") or [""])[0]),
    })


def _article_candidates_for_role(
    ctx: SectionCoverageContext,
    role: str,
    queries: Iterable[str],
) -> List[Dict[str, Any]]:
    if _article_portfolio_path(ctx) is None:
        return []
    query_list = [str(query).strip() for query in queries if str(query).strip()]
    topic_identity = ctx.section_data.get("topic_identity") or {}
    topic_fp = str(topic_identity.get("fingerprint") or "") if isinstance(topic_identity, dict) else ""
    portfolio = _read_article_portfolio(ctx)
    result: List[Dict[str, Any]] = []
    for row in portfolio.get("candidates", []):
        if not isinstance(row, dict):
            continue
        roles = {str(item).casefold() for item in row.get("roles") or []}
        if role.casefold() not in roles:
            continue
        identity = str(row.get("material_identity") or "")
        cached_audit = portfolio.get("audits", {}).get(identity) or {}
        effective_decision = str(
            cached_audit.get("decision") or row.get("decision") or ""
        ).casefold()
        effective_scope = str(
            cached_audit.get("scope_fit") or row.get("scope_fit") or ""
        ).casefold()
        # Only an adjudicated approved article can satisfy a reusable role.
        # Rejected, deferred, out-of-scope, and unbridged adjacent rows must
        # leave the backend search available for a genuinely new portfolio.
        if effective_decision != "approved" or effective_scope in {
            "out_of_scope", "contextual", "unreviewed",
        }:
            continue
        if effective_scope == "adjacent" and not (
            row.get("explicit_topic_bridge")
            or cached_audit.get("explicit_topic_bridge")
        ):
            continue
        affinity = candidate_query_affinity(
            row,
            query_list,
            topic_fingerprint=str(row.get("topic_fingerprint") or ""),
            exact_topic_fingerprint=topic_fp,
        )
        if not affinity.get("accepted"):
            continue
        quality = evaluate_candidate_topic_affinity(
            row,
            ctx.section_data,
            queries=query_list,
            components=[
                item
                for target in _coverage_query_targets(ctx)
                if str(target.get("role") or "") == role
                for item in (target.get("components") or target.get("missing_components") or [])
            ],
        )
        if not quality.get("accepted"):
            continue
        reusable = dict(row)
        reusable["decision"] = "approved"
        reusable["scope_fit"] = effective_scope or quality.get("scope_fit", "direct")
        reusable["cache_reuse_affinity"] = affinity
        reusable["topic_quality"] = quality
        result.append(reusable)
    result.sort(key=lambda row: (
        -float(row.get("relevance_score") or 0.0),
        -int(row.get("citation_count") or 0),
        str(row.get("title") or "").casefold(),
        str(row.get("material_identity") or ""),
    ))
    if result:
        portfolio = _read_article_portfolio(ctx)
        portfolio["telemetry"]["candidate_reuse_hits"] = int(
            portfolio["telemetry"].get("candidate_reuse_hits", 0) or 0
        ) + len(result)
        _write_article_portfolio(ctx, portfolio)
    return result


def _reusable_cached_candidates(
    ctx: SectionCoverageContext,
    rows: Iterable[Mapping[str, Any]],
    *,
    role: str,
    queries: Iterable[str],
    topic_fingerprint: str,
) -> List[Dict[str, Any]]:
    """Filter global-cache rows so weak evidence cannot suppress retrieval."""

    query_list = [str(query).strip() for query in queries if str(query).strip()]
    targets = _coverage_query_targets(ctx)
    component_values = [
        item
        for target in targets
        if not target.get("role") or str(target.get("role")) == role
        for item in (target.get("components") or target.get("missing_components") or [])
    ]
    reusable: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        if str(row.get("role") or role).casefold() not in {"", role.casefold()}:
            continue
        decision = str(row.get("decision") or "").casefold()
        scope = str(row.get("scope_fit") or "").casefold()
        if decision == "rejected" or scope in {"out_of_scope", "contextual"}:
            continue
        if scope == "adjacent" and not row.get("explicit_topic_bridge"):
            continue
        affinity = candidate_query_affinity(
            row,
            query_list,
            topic_fingerprint=str(row.get("topic_fingerprint") or ""),
            exact_topic_fingerprint=topic_fingerprint,
        )
        if not affinity.get("accepted"):
            continue
        quality = evaluate_candidate_topic_affinity(
            row,
            ctx.section_data,
            queries=query_list,
            components=component_values,
        )
        if not quality.get("accepted"):
            continue
        row["cache_reuse_affinity"] = affinity
        row["topic_quality"] = quality
        reusable.append(row)
    return reusable


def _record_article_audit(ctx: SectionCoverageContext, candidate: Dict[str, Any]) -> None:
    if _article_portfolio_path(ctx) is None:
        return
    identity = _candidate_identity(candidate)
    if not identity:
        return
    portfolio = _read_article_portfolio(ctx)
    existing = portfolio["audits"].get(identity) or {}
    merged_roles = list(dict.fromkeys([
        *(existing.get("role_fit") or []),
        *(candidate.get("role_fit") or []),
    ]))
    # A rejected decision is never overwritten by a weaker/deferred record;
    # an approved record is retained only with its explicit route clamp.
    decision = str(candidate.get("decision") or existing.get("decision") or "deferred")
    if existing.get("decision") == "rejected" and decision == "deferred":
        decision = "rejected"
    portfolio["audits"][identity] = {
        "material_identity": identity,
        "decision": decision,
        "scope_fit": str(candidate.get("scope_fit") or existing.get("scope_fit") or "unreviewed"),
        "role_fit": merged_roles,
        "role_provenance": _merge_role_provenance(
            existing.get("role_provenance"), candidate.get("role_provenance")
        ),
        "scope_violations": normalize_scope_violation_records([
            *(existing.get("scope_violations") or []),
            *(candidate.get("scope_violations") or []),
        ]),
        "boundary_violations": normalize_scope_violation_records([
            *(existing.get("boundary_violations") or []),
            *(candidate.get("boundary_violations") or []),
        ]),
        "audit_reason": str(candidate.get("audit_reason") or existing.get("audit_reason") or ""),
        "explicit_topic_bridge": bool(
            candidate.get("explicit_topic_bridge")
            or existing.get("explicit_topic_bridge")
        ),
        "not_usable_for": list(dict.fromkeys([
            *(existing.get("not_usable_for") or []),
            *(candidate.get("not_usable_for") or []),
        ])),
        "source_sections": list(dict.fromkeys([
            *(existing.get("source_sections") or []),
            ctx.section_id,
        ])),
        "cacheable": decision in {"approved", "rejected"},
    }
    _write_article_portfolio(ctx, portfolio)


def _record_article_material(
    ctx: SectionCoverageContext,
    candidate: Dict[str, Any],
    *,
    paper_id: str,
    chunk_ids: Iterable[str],
) -> None:
    if _article_portfolio_path(ctx) is None:
        return
    identity = _candidate_identity(candidate)
    if not identity or not paper_id:
        return
    portfolio = _read_article_portfolio(ctx)
    existing = portfolio["materials"].get(identity) or {}
    previous_chunks = list(existing.get("chunk_ids") or [])
    portfolio["materials"][identity] = {
        "material_identity": identity,
        "paper_id": paper_id,
        "chunk_ids": list(dict.fromkeys([
            *previous_chunks,
            *(str(item) for item in chunk_ids if str(item)),
        ])),
        "source_sections": list(dict.fromkeys([
            *(existing.get("source_sections") or []), ctx.section_id,
        ])),
    }
    _write_article_portfolio(ctx, portfolio)


def _article_material_for_candidate(ctx: SectionCoverageContext, candidate: Dict[str, Any]) -> Dict[str, Any]:
    identity = _candidate_identity(candidate)
    if not identity or _article_portfolio_path(ctx) is None:
        return {}
    value = _read_article_portfolio(ctx).get("materials", {}).get(identity) or {}
    if value:
        portfolio = _read_article_portfolio(ctx)
        portfolio["telemetry"]["material_reuse_hits"] = int(
            portfolio["telemetry"].get("material_reuse_hits", 0) or 0
        ) + 1
        _write_article_portfolio(ctx, portfolio)
    return dict(value) if isinstance(value, dict) else {}


def _read_wave_telemetry(ctx: SectionCoverageContext) -> Dict[str, Any]:
    value = _read_artifact(ctx.work_dir, COVERAGE_WAVE_TELEMETRY) or {}
    value.setdefault("schema_version", "phase2.coverage_wave_telemetry.v1")
    value.setdefault("section_id", ctx.section_id)
    value.setdefault("waves", [])
    value.setdefault("total_audit_calls", 0)
    value.setdefault("total_model_calls", 0)
    value.setdefault("audit_payload_input_tokens", 0)
    value.setdefault("model_input_tokens", 0)
    value.setdefault("model_output_tokens", 0)
    value.setdefault("batched_llm_cost_cny", 0.0)
    value.setdefault("cost_basis", "unavailable")
    value.setdefault("cost_is_estimated", False)
    value.setdefault("budget_rejections", [])
    value.setdefault("stop_reasons", [])
    value.setdefault("max_waves", int(getattr(ctx, "max_coverage_waves", 0) or 0))
    value.setdefault("max_audit_calls", int(getattr(ctx, "max_audit_calls_per_section", 0) or 0))
    return value


def _write_wave_telemetry(ctx: SectionCoverageContext, value: Dict[str, Any]) -> None:
    _write_artifact(ctx.work_dir, COVERAGE_WAVE_TELEMETRY, value)


def _current_wave_index(ctx: SectionCoverageContext) -> int:
    request = ctx.phase3_coverage_request if isinstance(ctx.phase3_coverage_request, dict) else {}
    try:
        if request.get("wave_index") is not None:
            return max(0, int(request.get("wave_index") or 0))
    except (TypeError, ValueError):
        pass
    ledger = _read_search_budget_ledger(ctx)
    rounds = [item for item in ledger.get("rounds", []) if isinstance(item, dict)]
    return max([int(item.get("wave_index") or 0) for item in rounds] or [0])


def _mark_audit_wave(
    ctx: SectionCoverageContext,
    *,
    wave_index: int,
    candidate_ids: List[str],
    payload_tokens: int,
    output_tokens: int = 0,
) -> Dict[str, Any]:
    telemetry = _read_wave_telemetry(ctx)
    row = next((item for item in telemetry["waves"] if int(item.get("wave_index") or 0) == int(wave_index)), None)
    if row is None:
        row = {
            "wave_index": int(wave_index),
            "audit_calls": 0,
            "candidate_ids": [],
            "audit_payload_input_tokens": 0,
            "audit_output_tokens": 0,
        }
        telemetry["waves"].append(row)
    row["audit_calls"] = int(row.get("audit_calls", 0) or 0) + 1
    row["candidate_ids"] = list(dict.fromkeys([*(row.get("candidate_ids") or []), *candidate_ids]))
    row["audit_payload_input_tokens"] = int(row.get("audit_payload_input_tokens", 0) or 0) + int(payload_tokens or 0)
    row["audit_output_tokens"] = int(row.get("audit_output_tokens", 0) or 0) + int(output_tokens or 0)
    telemetry["total_audit_calls"] = int(telemetry.get("total_audit_calls", 0) or 0) + 1
    telemetry["audit_payload_input_tokens"] = int(telemetry.get("audit_payload_input_tokens", 0) or 0) + int(payload_tokens or 0)
    telemetry["total_model_calls"] = int(telemetry.get("total_model_calls", 0) or 0) + 1
    telemetry["model_input_tokens"] = int(telemetry.get("model_input_tokens", 0) or 0) + int(payload_tokens or 0)
    telemetry["model_output_tokens"] = int(telemetry.get("model_output_tokens", 0) or 0) + int(output_tokens or 0)
    telemetry["batched_llm_calls"] = int(telemetry.get("batched_llm_calls", 0) or 0) + 1
    telemetry["batched_llm_input_tokens"] = int(
        telemetry.get("batched_llm_input_tokens", 0) or 0
    ) + int(payload_tokens or 0)
    telemetry["batched_llm_output_tokens"] = int(
        telemetry.get("batched_llm_output_tokens", 0) or 0
    ) + int(output_tokens or 0)
    _write_wave_telemetry(ctx, telemetry)
    _bump_phase2_telemetry(
        ctx,
        batched_llm_calls=1,
        batched_llm_input_tokens=int(payload_tokens or 0),
        batched_llm_output_tokens=int(output_tokens or 0),
    )
    return telemetry


def _reconcile_batched_audit_usage(
    ctx: SectionCoverageContext,
    usage: Mapping[str, Any],
) -> None:
    """Replace one payload estimate with one normalized Qwen call receipt.

    The orchestrator may audit more than one wave.  Token counters are
    corrected by delta, while monetary cost is accumulated per call rather
    than overwritten by the latest call.  The final section receipt is added
    by the orchestrator after all waves finish.
    """

    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    call_cost_cny = round(float(usage.get("cost_cny") or 0.0), 6)
    usage_basis = str(usage.get("cost_basis") or "unavailable")
    usage_estimated = bool(usage.get("cost_is_estimated"))
    wave = _read_wave_telemetry(ctx)
    wave_index = _current_wave_index(ctx)
    row = next(
        (
            item for item in wave.get("waves", [])
            if int(item.get("wave_index") or 0) == wave_index
            and int(item.get("audit_calls") or 0) > 0
        ),
        None,
    )
    delta_input = 0
    delta_output = 0
    if row is not None:
        old_input = int(row.get("audit_payload_input_tokens") or 0)
        old_output = int(row.get("audit_output_tokens") or 0)
        delta_input = input_tokens - old_input
        delta_output = output_tokens - old_output
        row["audit_payload_input_tokens"] = input_tokens
        row["audit_output_tokens"] = output_tokens
        wave["audit_payload_input_tokens"] = max(
            0,
            int(wave.get("audit_payload_input_tokens") or 0)
            + delta_input,
        )
        wave["model_input_tokens"] = max(
            0,
            int(wave.get("model_input_tokens") or 0)
            + delta_input,
        )
        wave["model_output_tokens"] = max(
            0,
            int(wave.get("model_output_tokens") or 0)
            + delta_output,
        )
        wave["batched_llm_input_tokens"] = max(
            0,
            int(wave.get("batched_llm_input_tokens") or 0)
            + delta_input,
        )
        wave["batched_llm_output_tokens"] = max(
            0,
            int(wave.get("batched_llm_output_tokens") or 0)
            + delta_output,
        )
        previous_wave_cost = float(wave.get("batched_llm_cost_cny") or 0.0)
        wave["batched_llm_cost_cny"] = round(
            previous_wave_cost + call_cost_cny,
            6,
        )
        previous_basis = str(wave.get("cost_basis") or "unavailable")
        wave["cost_basis"] = (
            usage_basis
            if previous_basis in {"", "unavailable"}
            or previous_basis == usage_basis
            else "mixed"
        )
        wave["cost_is_estimated"] = bool(
            wave.get("cost_is_estimated") or usage_estimated
        )
        for key in ("model_tier", "model_name", "pricing_source"):
            if usage.get(key):
                wave[key] = usage[key]
        if usage.get("usage_receipt_id"):
            wave["last_usage_receipt_id"] = str(usage["usage_receipt_id"])
    _write_wave_telemetry(ctx, wave)

    phase2 = _phase2_telemetry(ctx)
    old_phase2_input = int(phase2.get("batched_llm_input_tokens") or 0)
    old_phase2_output = int(phase2.get("batched_llm_output_tokens") or 0)
    phase2["batched_llm_input_tokens"] = max(
        0, old_phase2_input + delta_input
    )
    phase2["batched_llm_output_tokens"] = max(
        0, old_phase2_output + delta_output
    )
    previous_phase2_cost = float(phase2.get("batched_llm_cost_cny") or 0.0)
    phase2["batched_llm_cost_cny"] = round(
        previous_phase2_cost + call_cost_cny,
        6,
    )
    previous_basis = str(phase2.get("cost_basis") or "unavailable")
    phase2["cost_basis"] = (
        usage_basis
        if previous_basis in {"", "unavailable"}
        or previous_basis == usage_basis
        else "mixed"
    )
    phase2["cost_is_estimated"] = bool(
        phase2.get("cost_is_estimated") or usage_estimated
    )
    for key in ("model_tier", "model_name", "pricing_source"):
        if usage.get(key):
            phase2[key] = usage[key]
    if usage.get("usage_receipt_id"):
        phase2["last_usage_receipt_id"] = str(usage["usage_receipt_id"])
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", phase2)


def _mark_search_wave(
    ctx: SectionCoverageContext,
    *,
    wave_index: int,
    backend_stats: Mapping[str, Any],
    candidate_count: int,
) -> None:
    telemetry = _read_wave_telemetry(ctx)
    row = next((item for item in telemetry["waves"] if int(item.get("wave_index") or 0) == int(wave_index)), None)
    if row is None:
        row = {"wave_index": int(wave_index), "audit_calls": 0, "candidate_ids": []}
        telemetry["waves"].append(row)
    row["search_calls"] = int(row.get("search_calls", 0) or 0) + 1
    row["candidate_count"] = int(row.get("candidate_count", 0) or 0) + int(candidate_count or 0)
    row["s2_calls"] = int(row.get("s2_calls", 0) or 0) + int(backend_stats.get("semantic_scholar_calls", 0) or 0)
    row["openalex_calls"] = int(row.get("openalex_calls", 0) or 0) + int(backend_stats.get("openalex_calls", 0) or 0)
    telemetry["total_search_calls"] = int(telemetry.get("total_search_calls", 0) or 0) + 1
    backend_receipts = list(telemetry.get("backend_calls") or [])
    for backend, call_key in (
        ("semantic_scholar", "semantic_scholar_calls"),
        ("openalex", "openalex_calls"),
    ):
        call_count = int(backend_stats.get(call_key, 0) or 0)
        if call_count:
            backend_receipts.append({
                "backend": backend,
                "calls": call_count,
                "wave_index": int(wave_index),
                "status": "failed" if backend_stats.get(f"{backend}_error") else "completed",
                "error": str(backend_stats.get(f"{backend}_error") or "")[:240],
            })
            telemetry["backend_call_count"] = int(
                telemetry.get("backend_call_count", 0) or 0
            ) + call_count
            if backend_stats.get(f"{backend}_error"):
                telemetry["backend_failure_count"] = int(
                    telemetry.get("backend_failure_count", 0) or 0
                ) + 1
    telemetry["backend_calls"] = backend_receipts[-50:]
    _write_wave_telemetry(ctx, telemetry)
    phase2 = _phase2_telemetry(ctx)
    phase2_receipts = list(phase2.get("backend_calls") or [])
    for backend, call_key in (
        ("semantic_scholar", "semantic_scholar_calls"),
        ("openalex", "openalex_calls"),
    ):
        call_count = int(backend_stats.get(call_key, 0) or 0)
        if not call_count:
            continue
        failure = str(
            backend_stats.get(f"{backend}_error")
            or backend_stats.get(f"{backend}_fallback_error")
            or ""
        )[:240]
        phase2_receipts.append({
            "backend": backend,
            "calls": call_count,
            "wave_index": int(wave_index),
            "status": "failed" if failure else "completed",
            "error": failure,
        })
        phase2["backend_call_count"] = int(
            phase2.get("backend_call_count", 0) or 0
        ) + call_count
        if failure:
            phase2["backend_failure_count"] = int(
                phase2.get("backend_failure_count", 0) or 0
            ) + 1
    phase2["backend_calls"] = phase2_receipts[-50:]
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", phase2)


def _audit_call_preflight(ctx: SectionCoverageContext, candidate_ids: List[str], payload_tokens: int) -> Any:
    telemetry = _read_wave_telemetry(ctx)
    wave = _current_wave_index(ctx)
    row = next((item for item in telemetry.get("waves", []) if int(item.get("wave_index") or 0) == wave), {})
    max_audits = int(getattr(ctx, "max_audit_calls_per_section", 2) or 2)
    cumulative_budget = int(getattr(ctx, "context_cumulative_budget_tokens", 0) or 0) or 120_000
    per_call_budget = int(getattr(ctx, "context_per_call_budget_tokens", 0) or 0) or 16_000
    reserve = int(getattr(ctx, "context_output_reserve_tokens", 0) or 0) or 1_000
    admission = admit_batched_audit_call(
        wave_index=wave,
        audit_calls_in_wave=int(row.get("audit_calls", 0) or 0),
        predicted_input_tokens=max(1, int(payload_tokens or 0)),
        output_reserve_tokens=reserve,
        cumulative_input_tokens=int(telemetry.get("model_input_tokens", 0) or 0),
        cumulative_budget_tokens=cumulative_budget,
        per_call_budget_tokens=per_call_budget,
        audit_calls_total=int(telemetry.get("total_audit_calls", 0) or 0),
        audit_call_budget=max(1, max_audits),
    )
    if not admission.admitted:
        telemetry.setdefault("budget_rejections", []).append({
            "kind": "audit_call",
            "wave_index": wave,
            "reason": admission.reason,
            "candidate_ids": list(candidate_ids),
        })
        _write_wave_telemetry(ctx, telemetry)
    return admission


def _cross_wave_state_path(ctx: SectionCoverageContext) -> Optional[Path]:
    path = getattr(ctx, "cross_wave_state_path", None)
    return Path(path) if path else None


def _read_cross_wave_state(ctx: SectionCoverageContext) -> Dict[str, Any]:
    path = _cross_wave_state_path(ctx)
    if path is None or not path.exists():
        return {
            "schema_version": "phase2.1.cross_wave_state.v1",
            "section_id": ctx.section_id,
            "candidate_outcomes": {},
            "material_identity_index": {},
            "attempted_candidate_ids": [],
            "attempted_material_identities": [],
            "reused_candidate_ids": [],
            "newly_inserted_paper_ids": [],
            "newly_inserted_chunk_ids": [],
            "scientific_components_closed": [],
            "no_progress_escalations": [],
            "invalid_chunk_ownership_warned": [],
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("cross-wave state must be an object")
    except Exception:
        value = {}
    value.setdefault("schema_version", "phase2.1.cross_wave_state.v1")
    value.setdefault("section_id", ctx.section_id)
    for key in (
        "candidate_outcomes", "material_identity_index", "attempted_candidate_ids",
        "attempted_material_identities", "reused_candidate_ids",
        "newly_inserted_paper_ids", "newly_inserted_chunk_ids",
        "scientific_components_closed", "no_progress_escalations",
        "invalid_chunk_ownership_warned",
    ):
        value.setdefault(key, {} if key in {"candidate_outcomes", "material_identity_index"} else [])
    return value


def _write_cross_wave_state(ctx: SectionCoverageContext, state: Dict[str, Any]) -> None:
    path = _cross_wave_state_path(ctx)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_artifact(path.parent, path.name, state)


def _candidate_identity(candidate: Dict[str, Any]) -> str:
    return canonical_material_identity(candidate)


def _candidate_audit_evidence_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Fingerprint the evidence payload, excluding the audit decision itself.

    A deferred/rejected decision may be revisited only when the candidate's
    scientific payload has materially changed.  The decision, action and audit
    prose are deliberately excluded: persisting those fields here would make
    a prior judgement look like new evidence after a restart.
    """

    list_fields = {
        "role_fit", "role_provenance", "query_texts", "backends",
        "scope_violations", "boundary_violations", "alternate_urls",
    }
    scalar_fields = (
        "candidate_id", "material_identity", "title", "doi", "year", "venue",
        "abstract", "tldr", "role", "is_oa", "semantic_scholar_id",
        "corpus_id", "openalex_id", "text_availability", "content_urls",
        "pdf_url", "oa_url", "open_access_url", "html_url", "repository_url",
    )
    payload: Dict[str, Any] = {}
    for field in scalar_fields:
        value = candidate.get(field)
        if value not in (None, "", [], {}):
            payload[field] = value
    for field in list_fields:
        value = candidate.get(field)
        if value not in (None, "", [], {}):
            payload[field] = value
    return stable_payload_fingerprint(payload)


def _staging_material_for_candidate(
    ctx: SectionCoverageContext,
    candidate: Dict[str, Any],
) -> tuple[str, List[str]]:
    """Return the durable staging identity and chunk IDs for one candidate."""

    if not ctx.temp_kb_sqlite or not ctx.temp_kb_sqlite.exists():
        return "", []
    doi = str(candidate.get("doi") or "").strip().lower()
    paper_id = "doi:" + doi if doi and not doi.startswith("doi:") else doi
    title = str(candidate.get("title") or "").strip()
    try:
        with sqlite3.connect(str(ctx.temp_kb_sqlite)) as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(papers)").fetchall()
            }
            if not columns:
                return "", []
            predicates: List[str] = []
            params: List[str] = []
            if paper_id and "paper_id" in columns:
                predicates.append("paper_id = ?")
                params.append(paper_id)
            if doi and "doi" in columns:
                predicates.append("lower(doi) = ?")
                params.append(doi.removeprefix("doi:"))
            if title and "title" in columns:
                predicates.append("title = ?")
                params.append(title)
            if not predicates:
                return "", []
            row = conn.execute(
                "SELECT paper_id FROM papers WHERE " + " OR ".join(predicates) + " LIMIT 1",
                tuple(params),
            ).fetchone()
            if not row:
                return "", []
            resolved_id = str(row[0] or "")
            chunk_ids: List[str] = []
            chunk_columns = {
                str(item[1])
                for item in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
            }
            if resolved_id and "paper_id" in chunk_columns:
                chunk_rows = conn.execute(
                    "SELECT chunk_id FROM text_chunks WHERE paper_id = ?",
                    (resolved_id,),
                ).fetchall()
                chunk_ids = [str(item[0]) for item in chunk_rows if item and item[0]]
            return resolved_id, chunk_ids
    except Exception:
        return "", []


def _candidate_state_outcome(
    state: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    cid = str(candidate.get("candidate_id") or "")
    identity = _candidate_identity(candidate)
    outcomes = state.setdefault("candidate_outcomes", {})
    outcome = outcomes.setdefault(
        cid,
        {
            "candidate_id": cid,
            "material_identity": identity,
            "attempted_waves": [],
            "materialization_attempts": 0,
            "new_chunk_ids": [],
            "reused_chunk_ids": [],
            "last_materialization_status": "",
            "no_progress": False,
            "no_progress_components": [],
        },
    )
    if identity:
        ids = state.setdefault("material_identity_index", {}).setdefault(identity, [])
        if cid and cid not in ids:
            ids.append(cid)
    return outcome


def _record_candidate_event(
    ctx: SectionCoverageContext,
    candidate: Dict[str, Any],
    *,
    status: str,
    new_chunk_ids: List[str] | None = None,
    reused_chunk_ids: List[str] | None = None,
    paper_id: str = "",
    paper_row_inserted: bool = False,
    no_progress_components: List[str] | None = None,
) -> Dict[str, Any]:
    """Persist one candidate outcome and update truthful telemetry."""

    state = _read_cross_wave_state(ctx)
    outcome = _candidate_state_outcome(state, candidate)
    wave = _current_wave_index(ctx)
    if wave and wave not in outcome["attempted_waves"]:
        outcome["attempted_waves"].append(wave)
    if status not in {
        "reused_candidate", "reused_after_attempt", "skipped_duplicate_identity",
        "no_progress_candidate_skipped",
    }:
        outcome["materialization_attempts"] = int(outcome.get("materialization_attempts", 0) or 0) + 1
        state.setdefault("attempted_candidate_ids", []).append(str(candidate.get("candidate_id") or ""))
        identity = _candidate_identity(candidate)
        if identity:
            state.setdefault("attempted_material_identities", []).append(identity)
    outcome["last_materialization_status"] = status
    outcome["new_chunk_ids"] = list(dict.fromkeys([
        *outcome.get("new_chunk_ids", []),
        *(str(item) for item in (new_chunk_ids or []) if str(item)),
    ]))
    outcome["reused_chunk_ids"] = list(dict.fromkeys([
        *outcome.get("reused_chunk_ids", []),
        *(str(item) for item in (reused_chunk_ids or []) if str(item)),
    ]))
    if no_progress_components:
        outcome["no_progress"] = True
        outcome["no_progress_components"] = list(dict.fromkeys([
            *outcome.get("no_progress_components", []),
            *(str(item) for item in no_progress_components if str(item)),
        ]))
    state["attempted_candidate_ids"] = list(dict.fromkeys(state.get("attempted_candidate_ids", [])))
    state["attempted_material_identities"] = list(dict.fromkeys(state.get("attempted_material_identities", [])))
    if paper_id and paper_row_inserted:
        state.setdefault("newly_inserted_paper_ids", []).append(paper_id)
    state["newly_inserted_paper_ids"] = list(dict.fromkeys(state.get("newly_inserted_paper_ids", [])))
    state["newly_inserted_chunk_ids"] = list(dict.fromkeys([
        *state.get("newly_inserted_chunk_ids", []),
        *(str(item) for item in (new_chunk_ids or []) if str(item)),
    ]))
    _write_cross_wave_state(ctx, state)
    telemetry = _phase2_telemetry(ctx)
    telemetry["newly_inserted_papers"] = len(set(state.get("newly_inserted_paper_ids", [])))
    telemetry["newly_inserted_chunks"] = len(set(state.get("newly_inserted_chunk_ids", [])))
    if status in {
        "reused_candidate", "reused_after_attempt", "no_progress_candidate_skipped",
    }:
        telemetry["reused_candidates"] = int(telemetry.get("reused_candidates", 0) or 0) + 1
        telemetry.setdefault("reused_candidate_ids", []).append(str(candidate.get("candidate_id") or ""))
    elif status == "skipped_duplicate_identity":
        telemetry["duplicate_candidate_skips"] = int(
            telemetry.get("duplicate_candidate_skips", 0) or 0
        ) + 1
    elif status == "no_progress_candidate_skipped":
        telemetry["no_progress_candidate_skips"] = int(
            telemetry.get("no_progress_candidate_skips", 0) or 0
        ) + 1
    else:
        telemetry["candidate_attempts"] = int(telemetry.get("candidate_attempts", 0) or 0) + 1
    if no_progress_components:
        telemetry.setdefault("no_progress_escalations", []).append({
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "components": list(no_progress_components),
        })
    if paper_id and (new_chunk_ids or reused_chunk_ids):
        _record_article_material(
            ctx,
            candidate,
            paper_id=str(paper_id),
            chunk_ids=[*(new_chunk_ids or []), *(reused_chunk_ids or [])],
        )
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", telemetry)
    return outcome

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_artifact(work_dir: Path, filename: str, data: Any) -> None:
    if hasattr(data, "model_dump"):
        payload = data.model_dump()
    elif isinstance(data, dict):
        payload = data
    else:
        payload = {"value": data}
    atomic_write_json(work_dir / filename, payload)


def _read_artifact(work_dir: Path, filename: str) -> Optional[Dict]:
    p = work_dir / filename
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


AGENT_PAYLOAD_STATE = "COVERAGE_AGENT_PAYLOAD_STATE.json"


def _read_agent_payload_state(ctx: SectionCoverageContext) -> Dict[str, Any]:
    state = _read_artifact(ctx.work_dir, AGENT_PAYLOAD_STATE) or {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("schema_version", "phase2.1.agent_payload_state.v1")
    state.setdefault("section_id", ctx.section_id)
    state.setdefault("candidate_fingerprints", {})
    state.setdefault("local_candidate_fingerprints", {})
    state.setdefault("audited_candidate_fingerprints", {})
    state.setdefault("audit_history", [])
    state.setdefault("payload_events", [])
    state.setdefault("payload_chars", 0)
    return state


def _write_agent_payload_state(ctx: SectionCoverageContext, state: Dict[str, Any]) -> None:
    _write_artifact(ctx.work_dir, AGENT_PAYLOAD_STATE, state)


def _record_agent_payload(ctx: SectionCoverageContext, payload: Any) -> None:
    """Track compact tool-result traffic for pre-next-call admission."""

    try:
        raw = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, default=str
        )
    except Exception:
        raw = str(payload)
    state = _read_agent_payload_state(ctx)
    chars = len(raw)
    state["payload_chars"] = int(state.get("payload_chars", 0) or 0) + chars
    state["payload_events"] = [
        *list(state.get("payload_events") or [])[-31:],
        {
            "chars": chars,
            "estimated_tokens": max(1, chars // 4),
            "fingerprint": stable_payload_fingerprint({"payload": raw}),
        },
    ]
    _write_agent_payload_state(ctx, state)
    setattr(
        ctx,
        "_coverage_payload_tokens",
        int(state.get("payload_chars", 0) or 0) // 4,
    )


def _payload_ledger_summary(ctx: SectionCoverageContext) -> Dict[str, Any]:
    state = _read_agent_payload_state(ctx)
    return {
        "payload_schema_version": state.get("schema_version"),
        "payload_events": len(state.get("payload_events") or []),
        "estimated_payload_tokens": int(state.get("payload_chars", 0) or 0) // 4,
        "delta_protocol": "changed_records_only",
    }


def _safe_str(v: Any, limit: int = 2000) -> str:
    s = str(v) if not isinstance(v, str) else v
    return s[:limit]


def _candidate_alignment_guard(
    candidate: Dict[str, Any],
    ctx: SectionCoverageContext,
) -> Dict[str, Any]:
    """Deterministically separate direct evidence from broad background.

    The LLM still performs the scientific judgement.  This guard only prevents
    a generic paper that mentions the broad field from being counted as a
    directly aligned source for the exact section.  It never upgrades a
    candidate and it does not reject useful adjacent/background literature.
    """

    topic_identity = ctx.section_data.get("topic_identity", {})
    scope_boundary = assess_explicit_scope_boundary(
        ctx.section_data,
        candidate,
    )
    if scope_boundary.get("incompatible"):
        return {
            "direct_eligible": False,
            "hard_reject": True,
            "topic_alignment": {"status": "explicit_scope_boundary_failed"},
            "scope_boundary": scope_boundary,
            "section_anchor_hits": [],
            "reason": str(
                scope_boundary.get("reason")
                or "candidate_conflicts_with_explicit_section_scope"
            ),
        }
    if not isinstance(topic_identity, dict) or not topic_identity.get("valid"):
        return {
            "direct_eligible": True,
            "topic_alignment": {"status": "not_available"},
            "scope_boundary": scope_boundary,
            "section_anchor_hits": [],
            "reason": "topic_identity_not_available",
        }

    title = str(candidate.get("title") or "")
    abstract = str(candidate.get("abstract") or "")
    candidate_text = f"{title} {abstract}"
    topic_alignment = assess_topic_alignment(
        candidate_text,
        topic_identity,
        strict=True,
    )
    core = {
        str(value).lower()
        for value in topic_identity.get("core_anchor_tokens", [])
        if str(value).strip()
    }
    section_text = " ".join(
        [
            str(ctx.section_data.get("title") or ""),
            str(ctx.section_data.get("chapter_argument") or ""),
            " ".join(
                str(value)
                for value in ctx.section_data.get("key_questions", [])
            ),
            str(ctx.section_data.get("synthesis_task") or ""),
        ]
    )
    section_anchors = [
        token
        for token in dict.fromkeys(topic_tokens(section_text))
        if token not in core
    ][:24]
    present = set(topic_tokens(candidate_text))
    section_hits = sorted(set(section_anchors) & present)
    core_hits = list(topic_alignment.get("core_hits") or [])
    required_core = int(topic_alignment.get("required_core_hits") or 0)
    # A paper is directly eligible when it preserves the scientific object
    # and either names it strongly or addresses at least two section-specific
    # concepts.  Broad field reviews remain usable as adjacent background.
    direct_eligible = (
        topic_alignment.get("status") == "passed"
        and (
            len(core_hits) >= max(3, required_core)
            or len(section_hits) >= 2
        )
    )
    return {
        "direct_eligible": bool(direct_eligible),
        "hard_reject": False,
        "topic_alignment": topic_alignment,
        "scope_boundary": scope_boundary,
        "section_anchor_hits": section_hits[:12],
        "reason": (
            "direct_topic_and_section_alignment"
            if direct_eligible
            else "broad_or_indirect_alignment_only"
        ),
    }


def _canonical_scope_restrictions(
    scope_fit: str,
    restrictions: List[Any],
) -> List[str]:
    """Add non-negotiable writing boundaries for non-direct sources.

    Adjacent literature is valuable for method transfer and synthesis, but it
    must not silently import a different application's measurements or examples
    into the review topic.  These restrictions travel with the exact chunk into
    the authoring trust graph.
    """

    normalized = [
        _safe_str(item, 300).strip()
        for item in restrictions
        if _safe_str(item, 300).strip()
    ]
    if scope_fit == "adjacent":
        normalized.extend(
            [
                (
                    "standalone examples or deployment claims from an "
                    "application domain outside the section scope"
                ),
                (
                    "paper-specific quantitative results whose measured "
                    "subject is outside the section scope"
                ),
            ]
        )
    elif scope_fit == "contextual":
        normalized.append(
            "pivotal factual claims, exact measurements, or causal conclusions"
        )
    return list(dict.fromkeys(normalized))


def _normalise_search_query(query: str) -> str:
    """Return a stable form used only for duplicate-round protection."""
    return " ".join(str(query).lower().split())


def _query_round_fingerprint(queries: List[str]) -> str:
    normalised = sorted(
        {_normalise_search_query(query) for query in queries if query.strip()}
    )
    return hashlib.sha1("\x1f".join(normalised).encode("utf-8")).hexdigest()


def _read_search_budget_ledger(ctx: SectionCoverageContext) -> Dict[str, Any]:
    ledger = _read_artifact(ctx.work_dir, SEARCH_BUDGET_LEDGER) or {}
    if not isinstance(ledger.get("rounds"), list):
        ledger["rounds"] = []
    ledger.setdefault("schema_version", "1.0")
    ledger.setdefault("section_id", ctx.section_id)
    ledger["max_rounds_per_role"] = max(
        1, int(ctx.max_search_rounds_per_role)
    )
    return ledger


def _write_search_budget_ledger(
    ctx: SectionCoverageContext,
    ledger: Dict[str, Any],
) -> None:
    ledger["updated_at_epoch"] = round(time.time(), 3)
    _write_artifact(ctx.work_dir, SEARCH_BUDGET_LEDGER, ledger)


def _candidate_decisions(ctx: SectionCoverageContext) -> Dict[str, Dict[str, Any]]:
    raw = _read_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json") or {}
    result = {
        str(candidate.get("candidate_id") or ""): dict(candidate)
        for candidate in raw.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("candidate_id")
    }
    for candidate in result.values():
        contract = canonical_candidate_decision(candidate)
        candidate["candidate_action"] = contract.action
        candidate["decision_state"] = contract.state
        candidate["can_materialize"] = contract.can_materialize
        candidate["candidate_action_provenance"] = _candidate_action_provenance(
            contract
        )
    return result


def _candidate_action_provenance(contract: Any) -> Dict[str, Any]:
    """Return the durable explanation for a canonical candidate action."""

    return {
        "decision": contract.decision,
        "scope_fit": contract.scope_fit,
        "action": contract.action,
        "state": contract.state,
        "route_available": bool(contract.route_available),
        "reason": contract.reason,
    }


def _candidate_action_audit_reason(
    candidate: Dict[str, Any],
    contract: Any,
) -> str:
    """Attach a route-clamp explanation without changing scientific approval."""

    reason = str(candidate.get("audit_reason") or "").strip()
    if not (
        contract.decision == "approved"
        and contract.scope_fit in {"direct", "adjacent"}
        and contract.action == "discovery_lead"
        and not contract.route_available
    ):
        return _safe_str(reason, 500)
    marker = f"candidate_action_provenance: {contract.reason}"
    if marker not in reason:
        reason = " | ".join(value for value in (reason, marker) if value)
    return _safe_str(reason, 500)


def _has_legal_structured_or_oa_route(candidate: Dict[str, Any]) -> bool:
    """Return whether deterministic code has a legal acquisition route."""
    # Discovery backend labels are not executable identities.  The shared
    # contract accepts only a typed S2 structured-body route, an explicitly
    # OA-marked URL, or a local full-text route.
    return candidate_has_legal_route(candidate)


def _deterministic_candidate_action(
    candidate: Dict[str, Any],
    requested: str = "",
) -> str:
    """Return the one canonical action for every candidate ledger path."""

    del requested
    return canonical_candidate_decision(candidate).action


def _phase2_telemetry(ctx: SectionCoverageContext) -> Dict[str, Any]:
    data = _read_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json") or {}
    data.setdefault("schema_version", "phase2.1.telemetry.v1")
    data.setdefault("section_id", ctx.section_id)
    for key in (
        "s2_search_calls", "s2_snippet_calls", "s2_reference_calls",
        "s2_citation_calls", "openalex_calls", "oa_resolution_probes",
        "materialization_attempts", "materialization_route_attempts",
        "candidate_attempts", "reused_candidates", "duplicate_candidate_skips",
        "no_progress_candidate_skips",
        "newly_inserted_papers", "newly_inserted_chunks",
        "invalid_chunk_ownership_rejection_count",
        "invalid_chunk_ownership_unique_count",
        "article_portfolio_candidate_reuse_hits",
        "article_portfolio_audit_reuse_hits",
        "article_portfolio_material_reuse_hits",
        "fulltext_escalations",
        "bounded_wave_stop_count",
        "deterministic_step_count",
        "batched_llm_calls",
        "batched_llm_input_tokens",
        "batched_llm_output_tokens",
        "batched_llm_cost_cny",
        "backend_call_count",
        "backend_failure_count",
        "accepted_s2_snippets",
    ):
        data.setdefault(key, 0)
    for key in (
        "skipped_backends", "reused_candidate_ids", "scientific_components_closed",
        "no_progress_escalations", "api_call_receipts",
        "invalid_chunk_ownership_rejections",
        "fulltext_escalation_reasons",
        "wave_stop_reasons",
        "deterministic_steps",
        "backend_calls",
        "stop_reasons",
    ):
        data.setdefault(key, [])
    data.setdefault("execution_mode", "")
    data.setdefault("stop_reason", "")
    data.setdefault("stop_reason_category", "unknown")
    data.setdefault("scientific_exhaustion", False)
    data.setdefault("engineering_failure", False)
    data.setdefault("cost_basis", "unavailable")
    data.setdefault("cost_is_estimated", False)
    data.setdefault("cache_reuse_hits", {})
    return data


def _record_invalid_chunk_ownership(
    ctx: SectionCoverageContext,
    paper_id: str,
    chunk_id: str,
) -> None:
    """Reject every mismatch but emit only one warning per identity tuple."""

    paper_id = str(paper_id or "")
    chunk_id = str(chunk_id or "")
    telemetry = _phase2_telemetry(ctx)
    rows = [
        dict(item)
        for item in telemetry.get("invalid_chunk_ownership_rejections") or []
        if isinstance(item, dict)
    ]
    existing = next(
        (
            item for item in rows
            if str(item.get("paper_id") or "") == paper_id
            and str(item.get("chunk_id") or "") == chunk_id
        ),
        None,
    )
    if existing is None:
        rows.append({
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "occurrences": 1,
        })
    else:
        existing["occurrences"] = int(existing.get("occurrences") or 0) + 1
    state = _read_cross_wave_state(ctx)
    identity = {"paper_id": paper_id, "chunk_id": chunk_id}
    warned = [
        dict(item)
        for item in state.get("invalid_chunk_ownership_warned") or []
        if isinstance(item, dict)
    ]
    globally_warned = any(
        str(item.get("paper_id") or "") == paper_id
        and str(item.get("chunk_id") or "") == chunk_id
        for item in warned
    )
    if not globally_warned and existing is None:
        warned.append(identity)
        state["invalid_chunk_ownership_warned"] = warned
        _write_cross_wave_state(ctx, state)
        logger.warning(
            "Ignoring local candidate with invalid chunk ownership: %s / %s",
            paper_id,
            chunk_id,
        )
    telemetry["invalid_chunk_ownership_rejections"] = rows
    telemetry["invalid_chunk_ownership_unique_count"] = len(rows)
    telemetry["invalid_chunk_ownership_rejection_count"] = sum(
        int(item.get("occurrences") or 0) for item in rows
    )
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", telemetry)


def _bump_phase2_telemetry(
    ctx: SectionCoverageContext,
    **increments: int,
) -> Dict[str, Any]:
    data = _phase2_telemetry(ctx)
    for key, value in increments.items():
        data[key] = int(data.get(key, 0) or 0) + int(value or 0)
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", data)
    return data


def _record_deterministic_step(
    ctx: SectionCoverageContext,
    step: str,
    *,
    status: str = "completed",
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Record controller progress without conflating it with model calls."""

    data = _phase2_telemetry(ctx)
    data["execution_mode"] = "deterministic_short_path"
    data["deterministic_step_count"] = int(
        data.get("deterministic_step_count", 0) or 0
    ) + 1
    row: Dict[str, Any] = {
        "step": str(step),
        "status": str(status),
        "at_epoch": round(time.time(), 3),
    }
    if details:
        row["details"] = dict(details)
    data["deterministic_steps"] = [
        *list(data.get("deterministic_steps") or [])[-99:],
        row,
    ]
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", data)
    return data


def _record_short_path_stop(
    ctx: SectionCoverageContext,
    reason: str,
    *,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist the truthful terminal reason for a bounded deterministic run."""

    data = _phase2_telemetry(ctx)
    value = str(reason or "deterministic_short_path_complete")[:300]
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "bounded_waves_exhausted",
            "no_candidates",
            "no_novel",
            "scientific_exhaustion",
            "local_pool_exhausted",
            "local_marginal_gain_exhausted",
            "local_soft_visibility_target_reached",
        )
    ):
        category = "scientific_exhaustion"
    elif "coverage_outcome_reached" in lowered or "coverage_sufficient" in lowered:
        category = "scientific_completion"
    elif any(
        marker in lowered
        for marker in (
            "error", "exception", "validation_failed", "audit_gap",
            "budget_rejected", "permission", "schema", "runtime",
        )
    ):
        category = "engineering_failure"
    else:
        category = "unknown"
    data["execution_mode"] = "deterministic_short_path"
    data["stop_reason"] = value
    data["stop_reason_category"] = category
    data["scientific_exhaustion"] = category == "scientific_exhaustion"
    data["engineering_failure"] = category == "engineering_failure"
    stops = list(data.get("stop_reasons") or [])
    row: Dict[str, Any] = {"reason": value, "at_epoch": round(time.time(), 3)}
    if details:
        row["details"] = dict(details)
    if not any(str(item.get("reason") or "") == value for item in stops if isinstance(item, dict)):
        stops.append(row)
    data["stop_reasons"] = stops[-50:]
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", data)
    return data


def _sync_article_portfolio_telemetry(ctx: SectionCoverageContext) -> Dict[str, Any]:
    """Expose cross-section portfolio reuse in the section telemetry."""

    data = _phase2_telemetry(ctx)
    portfolio = _read_article_portfolio(ctx)
    source = portfolio.get("telemetry") or {}
    reuse = {
        "candidate": int(source.get("candidate_reuse_hits", 0) or 0),
        "audit": int(source.get("audit_reuse_hits", 0) or 0),
        "material": int(source.get("material_reuse_hits", 0) or 0),
    }
    data["cache_reuse_hits"] = reuse
    data["portfolio_reuse"] = {
        "candidate_reuse_hits": reuse["candidate"],
        "audit_reuse_hits": reuse["audit"],
        "material_reuse_hits": reuse["material"],
    }
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", data)
    return data


def _record_skipped_backend(ctx: SectionCoverageContext, backend: str, reason: str) -> None:
    data = _phase2_telemetry(ctx)
    rows = list(data.get("skipped_backends") or [])
    rows.append({"backend": backend, "reason": reason})
    data["skipped_backends"] = rows[-50:]
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", data)


def _round_has_usable_candidate(
    round_record: Dict[str, Any],
    decisions: Dict[str, Dict[str, Any]],
) -> bool:
    for candidate_id in round_record.get("candidate_ids", []):
        candidate = decisions.get(str(candidate_id), {})
        if (
            candidate.get("candidate_action") in {
                CandidateAction.materialize_now.value,
                CandidateAction.discovery_lead.value,
            }
            or (
                candidate.get("decision") == "approved"
            and candidate.get("scope_fit") in ("direct", "adjacent")
            )
        ):
            return True
    return False


def _round_has_any_audit(
    round_record: Dict[str, Any],
    decisions: Dict[str, Dict[str, Any]],
) -> bool:
    candidate_ids = [
        str(value) for value in round_record.get("candidate_ids", [])
    ]
    if not candidate_ids:
        return True
    return any(
        decisions.get(candidate_id, {}).get("decision")
        in ("approved", "rejected")
        for candidate_id in candidate_ids
    )


def _active_planned_roles(ctx: SectionCoverageContext) -> set[str]:
    """Return roles the section actually intends to use.

    A role explicitly marked ``not_needed`` must never leak into the material
    package merely because a broad local query happened to find a hit.
    """
    plan = _read_artifact(ctx.work_dir, "SECTION_COVERAGE_PLAN.json") or {}
    roles = plan.get("roles") or {}
    if roles:
        active = {
            str(role)
            for role, item in roles.items()
            if isinstance(item, dict) and item.get("priority") != "not_needed"
        }
        active.update(str(role) for role in ctx.section_data.get("required_roles", []))
    else:
        active = set(str(role) for role in ctx.section_data.get("required_roles", []))
        active.update(str(role) for role in ctx.section_data.get("optional_roles", []))
    return {role for role in active if role in COVERAGE_ROLES} or set(COVERAGE_ROLES)


def _coverage_query_targets(ctx: SectionCoverageContext) -> List[Dict[str, Any]]:
    """Return the compact deterministic query ledger for unresolved needs."""

    request = ctx.phase3_coverage_request or {}
    request = request if isinstance(request, dict) else {}
    section_view = dict(ctx.section_data)
    section_view["phase3_coverage_request"] = request
    audit = _read_artifact(ctx.work_dir, "LOCAL_COVERAGE_AUDIT.json") or {}
    plan = _read_artifact(ctx.work_dir, "SECTION_COVERAGE_PLAN.json") or {}
    source_ledger = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
    existing_targets: List[Dict[str, Any]] = [
        dict(item)
        for item in request.get("query_targets") or []
        if isinstance(item, dict)
    ]
    roles = derive_uncovered_roles(
        section_view,
        audit=audit,
        plan=plan,
        source_ledger=source_ledger,
    )
    if getattr(ctx, "adaptive_coverage_enabled", False) or section_view.get(
        "adaptive_coverage_enabled"
    ):
        adaptive_contract = build_adaptive_coverage_contract(
            section_view,
            section_count=section_view.get("_review_section_count"),
        )
        explicit_roles = {
            str(item.get("role") or "").strip().casefold()
            for item in existing_targets
            if isinstance(item, dict) and str(item.get("role") or "").strip()
        }
        explicit_roles.update(
            str(item).strip().casefold()
            for item in ctx.targeted_missing_roles
            if str(item).strip()
        )
        allowed_roles = set(adaptive_contract.required_roles) | explicit_roles
        if allowed_roles:
            roles = [role for role in roles if role in allowed_roles]
            if not roles:
                roles = list(adaptive_contract.required_roles)
    components: List[Any] = []
    for key in ("missing_components", "components"):
        components.extend(request.get(key) or [])
    for query in request.get("queries") or []:
        value = query.get("query") if isinstance(query, dict) else query
        if str(value or "").strip():
            existing_targets.append({"query": value, "source": "phase3_request"})
    for role in roles:
        entry = (plan.get("roles") or {}).get(role) or {}
        if isinstance(entry, dict):
            for query in entry.get("queries") or []:
                if str(query or "").strip():
                    existing_targets.append({
                        "query": query,
                        "role": role,
                        "source": "section_coverage_plan",
                    })
    for target in existing_targets:
        components.extend(target.get("missing_components") or target.get("components") or [])
    # A breadth gap with no explicit component still gets a role-specific
    # query; it must never collapse into ``no_uncovered_component_queries``.
    if not roles and not components:
        roles = derive_uncovered_roles(
            {**section_view, "required_roles": list(ctx.section_data.get("required_roles") or ["foundation"])},
            audit={"blocking_gaps": ["coverage_breadth"]},
            plan=plan,
            source_ledger={"breadth_target_met": False, **source_ledger},
        ) or ["foundation"]
        components = ["independent source breadth"]
    return build_uncovered_query_targets(
        section_view,
        roles=roles,
        components=components,
        existing_targets=existing_targets,
        max_targets=max(8, int(getattr(ctx, "min_mode_max_queries", 4) or 4)),
    )


def _local_candidate_id(section_id: str, role: str, paper_id: str, chunk_id: str) -> str:
    raw = "\x1f".join((section_id, role, paper_id, chunk_id)).encode("utf-8")
    return "local_" + hashlib.sha1(raw).hexdigest()[:12]


def _read_local_candidate_ledger(ctx: SectionCoverageContext) -> Dict[str, Any]:
    value = _read_artifact(ctx.work_dir, LOCAL_CANDIDATE_LEDGER) or {}
    if not isinstance(value.get("candidates"), list):
        value = {}
    return value or {
        "schema_version": "2.1",
        "section_id": ctx.section_id,
        "candidates": [],
    }


def _write_local_candidate_ledger(ctx: SectionCoverageContext, ledger: Dict[str, Any]) -> None:
    ledger["schema_version"] = "2.1"
    ledger["section_id"] = ctx.section_id
    ledger["total_candidates"] = len(ledger.get("candidates", []))
    ledger["approved_candidates"] = sum(
        1 for item in ledger.get("candidates", [])
        if item.get("decision") == "approved"
    )
    _write_artifact(ctx.work_dir, LOCAL_CANDIDATE_LEDGER, ledger)


def _register_local_hits(
    ctx: SectionCoverageContext,
    role: str,
    hits: List[Dict[str, Any]],
    *,
    retrieval_query: str = "",
) -> List[Dict[str, Any]]:
    """Persist local recall separately from adoption decisions.

    Broad recall is intentionally cheap and permissive.  It becomes usable
    material only after ``submit_local_source_audit`` records an explicit
    scope and role judgement.
    """
    ledger = _read_local_candidate_ledger(ctx)
    existing = {
        str(item.get("candidate_id")): item
        for item in ledger.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id")
    }
    result: List[Dict[str, Any]] = []
    for hit in hits:
        paper_id = str(hit.get("paper_id") or "")
        chunk_id = str(hit.get("chunk_id") or "")
        if not paper_id:
            continue
        candidate_id = _local_candidate_id(ctx.section_id, role, paper_id, chunk_id)
        previous = existing.get(candidate_id, {})
        contract_present = (
            _coverage_bool(previous.get("permission_contract_present"))
            if "permission_contract_present" in previous
            else _coverage_contract_present(hit)
        )
        hit_depth = _coverage_depth(
            hit.get("content_depth")
            or hit.get("evidence_level")
            or hit.get("source_kind")
            or ("fulltext" if chunk_id and hit.get("text") else "metadata")
        )
        hit_allowed = _normalise_coverage_claim_kinds(
            hit.get("allowed_claim_kinds")
        )
        hit_route = _coverage_json_object(
            hit.get("route_provenance") or hit.get("provenance")
        )
        record = {
            "candidate_id": candidate_id,
            "section_id": ctx.section_id,
            "paper_id": paper_id,
            "title": str(hit.get("title") or ""),
            "year": hit.get("year"),
            "venue": str(hit.get("venue") or ""),
            "role": role,
            "chunk_id": chunk_id,
            "text_preview": str(hit.get("text") or "")[:1200],
            "topic_matches": list(hit.get("topic_matches") or []),
            "role_matches": list(hit.get("role_matches") or []),
            "retrieval_query": retrieval_query,
            "scope_fit": previous.get("scope_fit", hit.get("scope_fit", "unreviewed")),
            "use_permission": previous.get("use_permission", hit.get("use_permission", "")),
            "content_depth": previous.get("content_depth", hit_depth),
            "source_kind": previous.get("source_kind", hit.get("source_kind", "")),
            "discovery_route": previous.get("discovery_route", hit.get("discovery_route", "")),
            "materialization_route": previous.get("materialization_route", hit.get("materialization_route", "")),
            "context_complete": previous.get(
                "context_complete",
                hit.get("context_complete", not contract_present),
            ),
            "allowed_claim_kinds": list(
                previous.get("allowed_claim_kinds")
                or hit_allowed
            ),
            "route_provenance": dict(
                previous.get("route_provenance")
                or hit_route
            ),
            "permission_contract_present": contract_present,
            "canonical_chunk_ids": list(
                previous.get("canonical_chunk_ids")
                or hit.get("canonical_chunk_ids")
                or ([chunk_id] if chunk_id else [])
            ),
            "decision": previous.get("decision", "deferred"),
            "audit_reason": previous.get("audit_reason", ""),
            "not_usable_for": list(previous.get("not_usable_for") or []),
        }
        existing[candidate_id] = record
        result.append(record)
    ledger["candidates"] = list(existing.values())
    _write_local_candidate_ledger(ctx, ledger)
    return result


def _accepted_local_candidates(ctx: SectionCoverageContext) -> List[Dict[str, Any]]:
    return [
        item for item in _read_local_candidate_ledger(ctx).get("candidates", [])
        if (
            item.get("decision") == "approved"
            and item.get("scope_fit") in ("direct", "adjacent", "contextual")
        )
    ]


def _phase3_material_inventory(ctx: SectionCoverageContext) -> List[Dict[str, Any]]:
    """Read the section's already-selected Phase 3 material directly.

    This is deliberately not an FTS query.  Phase 3 has already selected the
    paper/chunk allowlist and recorded its permissions in the shared ledger
    and section overlay.  The coverage worker must see those records even if
    their vocabulary does not match a role keyword.
    """

    paths = [Path(p) for p in ctx.shared_kb_sqlite_paths if Path(p).exists()]
    if not paths and ctx.kb_sqlite and ctx.kb_sqlite.exists():
        paths = [ctx.kb_sqlite]
    selected_papers = {str(value) for value in ctx.selected_paper_ids if str(value)}
    selected_chunks = {str(value) for value in ctx.selected_chunk_ids if str(value)}
    if not paths or (not selected_papers and not selected_chunks):
        return []

    source_rows: List[Dict[str, Any]] = []
    if ctx.source_ledger_path and ctx.source_ledger_path.exists():
        try:
            raw = json.loads(ctx.source_ledger_path.read_text(encoding="utf-8"))
            source_rows = [
                dict(row) for row in raw.get("sources", [])
                if isinstance(row, dict)
            ]
        except Exception:
            source_rows = []
    source_by_chunk: Dict[str, Dict[str, Any]] = {}
    source_by_paper: Dict[str, Dict[str, Any]] = {}
    for row in source_rows:
        pid = str(row.get("paper_id") or "")
        if pid:
            source_by_paper.setdefault(pid, row)
        for chunk_id in row.get("canonical_chunk_ids") or []:
            if chunk_id:
                source_by_chunk[str(chunk_id)] = row

    overlay_chunks: Dict[str, Dict[str, Any]] = {}
    overlay_papers: Dict[str, Dict[str, Any]] = {}
    if ctx.section_overlay_path and ctx.section_overlay_path.exists():
        try:
            overlay = json.loads(ctx.section_overlay_path.read_text(encoding="utf-8"))
            overlay_chunks = {
                str(k): dict(v) for k, v in (overlay.get("chunk_overrides") or {}).items()
                if isinstance(v, dict)
            }
            overlay_papers = {
                str(k): dict(v) for k, v in (overlay.get("paper_overrides") or {}).items()
                if isinstance(v, dict)
            }
        except Exception:
            pass

    inventory: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for db_path in paths:
        try:
            with sqlite3.connect(str(db_path)) as conn:
                paper_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(papers)").fetchall()
                }
                chunk_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
                }
                paper_rows: Dict[str, tuple] = {}
                if selected_papers:
                    placeholders = ",".join("?" for _ in selected_papers)
                    fields = ["paper_id", "title", "year", "venue"]
                    for field in (
                        "doi", "content_depth", "use_permission", "scope_fit",
                        "discovery_route", "materialization_route", "context_complete",
                        "allowed_claim_kinds_json", "route_provenance_json",
                        "provenance_json",
                    ):
                        if field in paper_columns:
                            fields.append(field)
                    for row in conn.execute(
                        f"SELECT {','.join(fields)} FROM papers WHERE paper_id IN ({placeholders})",
                        tuple(selected_papers),
                    ).fetchall():
                        paper_rows[str(row[0])] = row

                fields = ["chunk_id", "paper_id", "doi", "title", "section_path", "text"]
                for field in (
                    "content_depth", "use_permission", "scope_fit", "source_kind",
                    "context_complete", "allowed_claim_kinds_json",
                    "route_provenance_json", "provenance_json",
                ):
                    if field in chunk_columns:
                        fields.append(field)
                where: List[str] = []
                params: List[str] = []
                if selected_chunks:
                    placeholders = ",".join("?" for _ in selected_chunks)
                    where.append(f"chunk_id IN ({placeholders})")
                    params.extend(sorted(selected_chunks))
                if selected_papers:
                    placeholders = ",".join("?" for _ in selected_papers)
                    where.append(f"paper_id IN ({placeholders})")
                    params.extend(sorted(selected_papers))
                if not where:
                    continue
                rows = conn.execute(
                    f"SELECT {','.join(fields)} FROM text_chunks WHERE {' OR '.join(where)}",
                    tuple(params),
                ).fetchall()
                for row in rows:
                    item = dict(zip(fields, row))
                    pid = str(item.get("paper_id") or "")
                    cid = str(item.get("chunk_id") or "")
                    if not pid or not cid or (selected_chunks and cid not in selected_chunks):
                        continue
                    key = (pid, cid)
                    if key in seen:
                        continue
                    seen.add(key)
                    source = source_by_chunk.get(cid) or source_by_paper.get(pid) or {}
                    paper = paper_rows.get(pid, ())
                    paper_map = dict(zip(
                        ["paper_id", "title", "year", "venue"] + [
                            field for field in (
                                "doi", "content_depth", "use_permission", "scope_fit",
                                "discovery_route", "materialization_route",
                                "context_complete", "allowed_claim_kinds_json",
                                "route_provenance_json", "provenance_json",
                            )
                            if field in paper_columns
                        ],
                        paper,
                    )) if paper else {}
                    override = overlay_chunks.get(cid) or overlay_papers.get(pid) or {}
                    role_text = str(source.get("literature_role") or override.get("literature_role") or "")
                    roles = [
                        value.strip() for value in re.split(r"[,;/]", role_text)
                        if value.strip() in COVERAGE_ROLES
                    ]
                    if not roles:
                        roles = [str(ctx.section_data.get("required_roles", ["foundation"])[0])]
                    route = _coverage_json_object(
                        item.get("route_provenance_json")
                        or item.get("provenance_json")
                        or source.get("route_provenance")
                    )
                    allowed = _normalise_coverage_claim_kinds(
                        item.get("allowed_claim_kinds_json")
                        or source.get("allowed_claim_kinds")
                        or route.get("allowed_claim_kinds")
                    )
                    depth = _coverage_depth(
                        ctx.selected_content_depths.get(cid)
                        or ctx.selected_content_depths.get(pid)
                        or override.get("content_depth")
                        or source.get("content_depth")
                        or item.get("content_depth")
                        or "fulltext"
                    )
                    context_complete = (
                        ctx.selected_content_depths.get(cid) is not None
                        or item.get("context_complete") is not None
                    )
                    if item.get("context_complete") is not None:
                        context_complete = _coverage_bool(item.get("context_complete"))
                    elif "context_complete" in route:
                        context_complete = _coverage_bool(route.get("context_complete"))
                    else:
                        context_complete = depth == "fulltext"
                    contract_present = _coverage_contract_present({
                        **item,
                        "route_provenance": route,
                        "allowed_claim_kinds": allowed,
                    }) or bool(source.get("content_depth") or source.get("use_permission"))
                    inventory.append({
                        "paper_id": pid,
                        "chunk_id": cid,
                        "title": item.get("title") or paper_map.get("title") or source.get("title", ""),
                        "year": paper_map.get("year", source.get("year")),
                        "venue": paper_map.get("venue", source.get("venue", "")),
                        "doi": item.get("doi") or paper_map.get("doi") or source.get("doi", ""),
                        "text": item.get("text", ""),
                        "roles": list(dict.fromkeys(roles)),
                        "scope_fit": str(
                            override.get("scope_fit")
                            or source.get("scope_fit")
                            or item.get("scope_fit")
                            or "adjacent"
                        ),
                        "use_permission": str(
                            ctx.selected_permissions.get(cid)
                            or ctx.selected_permissions.get(pid)
                            or override.get("use_permission")
                            or source.get("use_permission")
                            or item.get("use_permission")
                            or "contextual_or_qualified_support"
                        ),
                        "content_depth": depth,
                        "context_complete": context_complete,
                        "allowed_claim_kinds": sorted(allowed),
                        "route_provenance": route,
                        "permission_contract_present": contract_present,
                        "canonical_chunk_ids": [cid],
                        "source_kind": str(item.get("source_kind") or source.get("source_kind") or "local_review_kb"),
                        "discovery_route": str(source.get("discovery_route") or "phase3_selected_material"),
                        "materialization_route": str(source.get("materialization_route") or "reused_local_asset"),
                        "not_usable_for": list(override.get("not_usable_for") or source.get("not_usable_for") or []),
                    })
        except Exception as exc:
            logger.warning("Phase 3 material inventory failed for %s: %s", db_path, exc)
    return inventory


def _register_phase3_inventory(ctx: SectionCoverageContext, inventory: List[Dict[str, Any]]) -> None:
    """Persist selected Phase 3 chunks as approved local candidates once."""

    for item in inventory:
        for role in item.get("roles") or []:
            _register_local_hits(
                ctx,
                role,
                [{
                    **item,
                    "role": role,
                    "topic_matches": [],
                    "role_matches": [],
                }],
                retrieval_query="phase3_selected_material",
            )
    ledger = _read_local_candidate_ledger(ctx)
    changed = False
    for item in ledger.get("candidates", []):
        if item.get("retrieval_query") != "phase3_selected_material":
            continue
        if item.get("decision") != "approved" or item.get("scope_fit") not in ("direct", "adjacent"):
            item["decision"] = "approved"
            item["scope_fit"] = item.get("scope_fit") if item.get("scope_fit") in ("direct", "adjacent") else "adjacent"
            item["audit_reason"] = "phase3_selected_allowlist_material"
            changed = True
    if changed:
        _write_local_candidate_ledger(ctx, ledger)


def _source_route_fields(
    *,
    candidate: Dict[str, Any] | None = None,
    materialized: Dict[str, Any] | None = None,
    scope_fit: str = "unreviewed",
    local: bool = False,
    abstract_only: bool = False,
    chunk_ids: List[str] | None = None,
) -> Dict[str, Any]:
    """Build route provenance at source creation time.

    This keeps the normal section ledger self-describing.  The historical
    route report remains useful for auditing old runs, but new sources no
    longer depend on a later multi-ledger reconstruction.
    """

    candidate = candidate or {}
    materialized = materialized or {}
    chunks = list(chunk_ids or [])
    has_s2_chunk = any(str(item).startswith("s2chunk:") for item in chunks)
    if abstract_only:
        depth = "abstract"
    elif has_s2_chunk:
        depth = "structured_snippet"
    elif local or str(materialized.get("acquisition_status") or "") == "fulltext":
        depth = "fulltext"
    else:
        depth = "metadata"
    backends = [str(item).casefold() for item in candidate.get("backends", [])]
    discovery = str(candidate.get("discovery_route") or "").strip()
    if not discovery:
        discovery = (
            "semantic_scholar_graph"
            if any("semantic" in item or item == "s2" for item in backends)
            else "academic_backend_search"
        )
    materialization = str(candidate.get("materialization_route") or "").strip() or (
        "s2_structured_body_snippet"
        if has_s2_chunk
        else
        "reused_local_abstract"
        if abstract_only and local
        else "reused_local_fulltext"
        if local
        else "legacy_oa_fulltext_fallback"
        if depth == "fulltext"
        else "not_materialized"
    )
    route_record = source_route_record(
        discovery_route=discovery,
        materialization_route=materialization,
        content_depth=depth,
        scope_fit=normalize_scope_fit(scope_fit),
        context_complete=depth in {"fulltext", "structured_snippet"},
        events=[
            {
                "event": "source_adopted",
                "route": discovery,
                "materialization": materialization,
            }
        ],
        metadata_conflicts=candidate.get("metadata_conflicts", []),
    )
    # Preserve explicit Phase 3 route facts when they exist.  The fallback
    # route inference above remains for legacy local candidates.
    if candidate.get("discovery_route"):
        route_record["discovery_route"] = str(candidate["discovery_route"])
    if candidate.get("materialization_route"):
        route_record["materialization_route"] = str(candidate["materialization_route"])
    if candidate.get("content_depth"):
        route_record["content_depth"] = str(candidate["content_depth"])
    if candidate.get("use_permission"):
        route_record["use_permission"] = str(candidate["use_permission"])
    if candidate.get("allowed_claim_kinds") is not None:
        route_record["allowed_claim_kinds"] = sorted(
            _normalise_coverage_claim_kinds(candidate.get("allowed_claim_kinds"))
        )
    if candidate.get("context_complete") is not None:
        route_record["context_complete"] = _coverage_bool(
            candidate.get("context_complete")
        )
        route_record["route_events"] = [
            {
                **event,
                "context_complete": route_record["context_complete"],
            }
            for event in route_record.get("route_events", [])
            if isinstance(event, dict)
        ]
    return route_record


# ---------------------------------------------------------------------------
# 1. load_section_context
# ---------------------------------------------------------------------------

def _make_load_section_context(ctx: SectionCoverageContext):
    def load_section_context() -> str:
        """Load and persist the section context (goal, argument, scope) to SECTION_CONTEXT.json.

        Returns a JSON summary of the section being researched.
        No arguments required.
        """
        try:
            paper_count = 0
            chunk_count = 0
            phase3_inventory = _phase3_material_inventory(ctx)
            if phase3_inventory:
                paper_count = len({item.get("paper_id") for item in phase3_inventory if item.get("paper_id")})
                chunk_count = len({item.get("chunk_id") for item in phase3_inventory if item.get("chunk_id")})
            if not phase3_inventory and ctx.kb_sqlite and ctx.kb_sqlite.exists():
                try:
                    with sqlite3.connect(str(ctx.kb_sqlite)) as conn:
                        r = conn.execute("SELECT COUNT(*) FROM papers").fetchone()
                        paper_count = r[0] if r else 0
                        r2 = conn.execute("SELECT COUNT(*) FROM text_chunks").fetchone()
                        chunk_count = r2[0] if r2 else 0
                except Exception:
                    pass

            breadth_targets = ctx.coverage_breadth_targets()
            sc = SectionContext(
                section_id=ctx.section_id,
                section_title=ctx.section_title,
                chapter_argument=ctx.chapter_argument,
                scope_description=ctx.section_data.get("scope_description", ""),
                scope_guardrails=ctx.scope_guardrails,
                required_roles=ctx.section_data.get("required_roles", list(COVERAGE_ROLES[:4])),
                optional_roles=ctx.section_data.get("optional_roles", list(COVERAGE_ROLES[4:])),
                kb_sqlite_path=str(ctx.kb_sqlite) if ctx.kb_sqlite else None,
                existing_paper_count=paper_count,
                existing_chunk_count=chunk_count,
                minimum_unique_sources=breadth_targets[
                    "minimum_unique_sources"
                ],
                minimum_direct_sources=breadth_targets[
                    "minimum_direct_sources"
                ],
                topic_identity=(
                    ctx.section_data.get("topic_identity", {})
                    if isinstance(
                        ctx.section_data.get("topic_identity", {}),
                        dict,
                    )
                    else {}
                ),
                shared_kb_sqlite_paths=[str(path) for path in ctx.shared_kb_sqlite_paths],
                source_ledger_path=str(ctx.source_ledger_path) if ctx.source_ledger_path else None,
                section_overlay_path=str(ctx.section_overlay_path) if ctx.section_overlay_path else None,
                selected_paper_ids=list(ctx.selected_paper_ids),
                selected_chunk_ids=list(ctx.selected_chunk_ids),
            )
            _write_artifact(ctx.work_dir, "SECTION_CONTEXT.json", sc)
            feedback = ctx.section_data.get("author_coverage_feedback", {})
            return json.dumps({
                "status": "ok",
                "section_id": sc.section_id,
                "section_title": sc.section_title,
                "chapter_argument": sc.chapter_argument[:300],
                "scope_guardrails": sc.scope_guardrails,
                "required_roles": sc.required_roles,
                "optional_roles": sc.optional_roles,
                "existing_paper_count": sc.existing_paper_count,
                "existing_chunk_count": sc.existing_chunk_count,
                "minimum_unique_sources": sc.minimum_unique_sources,
                "minimum_direct_sources": sc.minimum_direct_sources,
                "topic_identity": {
                    "fingerprint": sc.topic_identity.get("fingerprint", ""),
                    "core_anchor_tokens": list(
                        sc.topic_identity.get("core_anchor_tokens", [])
                    ),
                    "anchor_phrases": list(
                        sc.topic_identity.get("anchor_phrases", [])
                    )[:6],
                    "valid": bool(sc.topic_identity.get("valid")),
                },
                "breadth_policy": (
                    "Required roles and source breadth are separate gates. "
                    "Continue local recall or targeted OA search until both "
                    "targets are met, or document why further retrieval has "
                    "reached a defensible stop condition."
                ),
                "author_coverage_feedback": (
                    feedback if isinstance(feedback, dict) else {}
                ),
                "phase3_coverage_request": {
                    "queries": ctx.targeted_queries,
                    "query_targets": ctx.targeted_query_targets,
                    "missing_claim_ids": ctx.targeted_missing_claim_ids,
                    "missing_roles": ctx.targeted_missing_roles,
                    "expected_new_papers": ctx.targeted_expected_new_papers,
                    "stop_condition": ctx.phase3_coverage_request.get("stop_condition", {}),
                },
                "uncovered_query_targets": build_uncovered_query_targets(
                    {**ctx.section_data, "phase3_coverage_request": ctx.phase3_coverage_request},
                    roles=(ctx.targeted_missing_roles or sc.required_roles),
                    components=ctx.phase3_coverage_request.get("missing_components", []),
                    existing_targets=ctx.targeted_query_targets,
                ),
                "ledger_summary": _payload_ledger_summary(ctx),
                "artifact": "SECTION_CONTEXT.json",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:300]})

    return load_section_context


# ---------------------------------------------------------------------------
# 2. inspect_section_local_coverage
# ---------------------------------------------------------------------------

def _make_inspect_section_local_coverage(ctx: SectionCoverageContext):
    def inspect_section_local_coverage() -> str:
        """Query the local ReviewKnowledgeBase for each coverage role and return a gap analysis.

        Writes LOCAL_COVERAGE_AUDIT.json. No arguments required.
        """
        required_roles = ctx.section_data.get("required_roles", list(COVERAGE_ROLES))
        phase3_inventory = _phase3_material_inventory(ctx)
        if phase3_inventory:
            _register_phase3_inventory(ctx, phase3_inventory)
        local_db_available = bool(
            phase3_inventory
            or (ctx.kb_sqlite is not None and ctx.kb_sqlite.exists())
            or any(Path(path).exists() for path in ctx.shared_kb_sqlite_paths)
        )
        if not local_db_available:
            audit = LocalCoverageAudit(
                section_id=ctx.section_id,
                blocking_gaps=list(required_roles),
                important_gaps=[],
                sufficient_roles=[],
            )
            _write_artifact(ctx.work_dir, "LOCAL_COVERAGE_AUDIT.json", audit)
            return json.dumps({
                "status": "ok",
                "message": "No local KB found. All roles require external search.",
                "blocking_gaps": list(required_roles),
                "uncovered_roles": list(required_roles),
                "uncovered_query_targets": build_uncovered_query_targets(
                    ctx.section_data,
                    roles=required_roles,
                    components=(ctx.phase3_coverage_request or {}).get("missing_components", []),
                ),
                "artifact": "LOCAL_COVERAGE_AUDIT.json",
            })

        role_audits: Dict[str, LocalRoleAudit] = {}
        total_papers = set()
        total_chunks = 0
        provisional_by_role: Dict[str, Dict[str, Any]] = {}
        candidate_ids_by_role: Dict[str, List[str]] = {}
        approved = _accepted_local_candidates(ctx)
        approved_by_role: Dict[str, List[Dict[str, Any]]] = {
            role: [item for item in approved if item.get("role") == role]
            for role in COVERAGE_ROLES
        }

        for role in COVERAGE_ROLES:
            try:
                phase3_hits = [
                    {**item, "role": role}
                    for item in phase3_inventory
                    if role in set(item.get("roles") or [])
                ]
                # Phase 3 selected material is authoritative for the local
                # inventory.  FTS is only a fallback for roles not represented
                # in that allowlist; it must not make an existing six-paper
                # selection appear to be zero.
                fts_hits = []
                if not phase3_hits and ctx.kb_sqlite and ctx.kb_sqlite.exists():
                    fts_hits = _query_kb_for_role(
                        ctx.kb_sqlite, ctx.section_id, role, top_k=8,
                        section_data=ctx.section_data,
                    )
                hits = phase3_hits + fts_hits
                registered = _register_local_hits(ctx, role, hits)
                candidate_ids_by_role[role] = [
                    item["candidate_id"] for item in registered[:8]
                ]
                raw_paper_ids = list({
                    h.get("paper_id", "") for h in hits if h.get("paper_id")
                })
                provisional_by_role[role] = {
                    "recalled_paper_count": len(raw_paper_ids),
                    "sample_titles": list({
                        h.get("title", "") for h in hits if h.get("title")
                    })[:3],
                    "candidate_ids": candidate_ids_by_role[role],
                    "status": "awaiting_scope_and_role_audit",
                }

                accepted = [
                    item for item in approved_by_role[role]
                    if _role_material_is_coverage_eligible(item, role)
                ]
                paper_ids = list({
                    item.get("paper_id", "") for item in accepted
                    if item.get("paper_id")
                    and item.get("scope_fit") in ("direct", "adjacent")
                })
                chunk_ids = [
                    item.get("chunk_id", "") for item in accepted
                    if item.get("chunk_id")
                    and item.get("scope_fit") in ("direct", "adjacent")
                ]
                sample_titles = list({
                    item.get("title", "") for item in accepted if item.get("title")
                })[:3]
                total_papers.update(raw_paper_ids)
                total_chunks += len({h.get("chunk_id") for h in hits if h.get("chunk_id")})

                n = len(paper_ids)
                if n >= 3:
                    verdict, severity = "sufficient", "minor"
                elif n >= 1:
                    verdict, severity = "partial", "important"
                else:
                    verdict = "none"
                    severity = "blocking" if role in required_roles else "important"

                role_audits[role] = LocalRoleAudit(
                    role=role,
                    paper_count=len(paper_ids),
                    chunk_count=len(chunk_ids),
                    top_paper_ids=paper_ids[:5],
                    coverage_verdict=verdict,
                    gap_severity=severity,
                    sample_titles=sample_titles,
                )
            except Exception as exc:
                logger.warning("inspect_section_local_coverage role=%s error: %s", role, exc)
                role_audits[role] = LocalRoleAudit(
                    role=role, coverage_verdict="none", gap_severity="blocking"
                )

        # Only required_roles escalate to blocking; optional roles max out at important
        blocking = [r for r, a in role_audits.items()
                    if a.gap_severity == "blocking" and r in required_roles]
        important = [r for r, a in role_audits.items() if a.gap_severity == "important"]
        sufficient = [r for r, a in role_audits.items() if a.coverage_verdict == "sufficient"]

        audit = LocalCoverageAudit(
            section_id=ctx.section_id,
            role_audits=role_audits,
            total_local_papers=len(total_papers),
            total_local_chunks=total_chunks,
            blocking_gaps=blocking,
            important_gaps=important,
            sufficient_roles=sufficient,
        )
        _write_artifact(ctx.work_dir, "LOCAL_COVERAGE_AUDIT.json", audit)

        uncovered_targets = _coverage_query_targets(ctx)
        return json.dumps({
            "status": "ok",
            "total_local_papers": audit.total_local_papers,
            "total_local_chunks": audit.total_local_chunks,
            "blocking_gaps": blocking,
            "important_gaps": important,
            "sufficient_roles": sufficient,
            "provisional_recall": provisional_by_role,
            "candidate_ids_by_role": candidate_ids_by_role,
            "local_candidates_require_audit": True,
            "local_candidate_ledger": LOCAL_CANDIDATE_LEDGER,
            "role_summary": {r: a.coverage_verdict for r, a in role_audits.items()},
            "uncovered_roles": derive_uncovered_roles(
                {**ctx.section_data, "phase3_coverage_request": ctx.phase3_coverage_request},
                audit=audit.model_dump(),
                plan=_read_artifact(ctx.work_dir, "SECTION_COVERAGE_PLAN.json") or {},
            ),
            "uncovered_query_targets": uncovered_targets,
            "ledger_summary": _payload_ledger_summary(ctx),
            "artifact": "LOCAL_COVERAGE_AUDIT.json",
        }, ensure_ascii=False)

    return inspect_section_local_coverage


def _query_kb_for_role(
    kb_sqlite: Path,
    section_id: str,
    role: str,
    top_k: int = 8,
    section_data: Optional[Dict] = None,
) -> List[Dict]:
    """Two-phase BM25 FTS5 query against local KB.

    Phase 1 — topic relevance: query only section-derived anchor terms (OR).
               Retrieves papers that are about this section's subject matter.
    Phase 2 — role fitness: filter Phase-1 hits to those whose text contains
               at least one role-specific keyword.

    Separation ensures the same topically-relevant papers cannot
    simultaneously satisfy all six roles.
    """
    import re as _re

    # --- Role-specific keywords (phase-2 filter only, never in FTS query) ---
    # No hyphens, no year numbers — those break FTS5 or match irrelevant noise.
    _ROLE_KW: Dict[str, List[str]] = {
        "foundation":   ["foundational", "theoretical", "seminal", "review",
                         "foundation", "principles", "history", "overview"],
        "mechanism":    ["mechanism", "physical", "process", "interaction",
                         "underlying", "theory"],
        "method":       ["method", "technique", "fabrication", "measurement",
                         "experimental", "characterization", "procedure"],
        "frontier":     ["novel", "emerging", "advance", "recent", "state",
                         "cutting"],
        "controversy":  ["debate", "controversy", "limitation", "challenge",
                         "criticism", "inconsistent", "disputed"],
        "application":  ["application", "device", "practical", "deployment",
                         "system", "integrated", "circuit"],
    }
    role_kw = _ROLE_KW.get(role, [role])

    # --- Phase 1: section-topic anchor query ---
    topic_tokens: List[str] = []
    if section_data:
        title = section_data.get("title", "")
        chapter_arg = section_data.get("chapter_argument", "")
        scope_desc = section_data.get("scope_description", "")
        combined = f"{title} {chapter_arg} {scope_desc}"
        _STOP = {
            'this', 'that', 'with', 'from', 'have', 'been', 'they', 'were',
            'will', 'which', 'their', 'section', 'using', 'established',
            'enable', 'enabled', 'advances', 'between', 'about', 'also',
            'into', 'these', 'competitive', 'such', 'each', 'more', 'when',
            'than', 'over', 'only', 'some', 'both', 'very', 'high',
        }
        raw = _re.findall(r'[a-zA-Z]{4,}', combined)
        seen_tok: set = set()
        topic_identity = section_data.get("topic_identity", {})
        if isinstance(topic_identity, dict) and topic_identity.get("valid"):
            for value in topic_identity.get("core_anchor_tokens", []):
                token = str(value or "").lower().strip()
                if token and token not in seen_tok:
                    seen_tok.add(token)
                    topic_tokens.append(token)
        for t in raw:
            tl = t.lower()
            if tl not in _STOP and tl not in seen_tok:
                seen_tok.add(tl)
                topic_tokens.append(tl)
    topic_tokens = topic_tokens[:12]

    # If no section data, fall back to role keywords as topic seed
    if not topic_tokens:
        topic_tokens = role_kw[:6]

    topic_query = " OR ".join(topic_tokens)

    # --- Execute phase 1 + phase 2 ---
    results: List[Dict] = []
    try:
        with sqlite3.connect(str(kb_sqlite)) as conn:
            fetch_k = min(top_k * 3, 60)
            phase1: List[Dict] = []
            chunk_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
            }
            paper_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(papers)").fetchall()
            }
            chunk_optional = [
                field for field in (
                    "evidence_level",
                    "content_depth",
                    "use_permission",
                    "source_kind",
                    "discovery_route",
                    "materialization_route",
                    "route_provenance",
                    "route_provenance_json",
                    "provenance_json",
                    "context_complete",
                    "allowed_claim_kinds",
                    "allowed_claim_kinds_json",
                    "scope_fit",
                )
                if field in chunk_columns
            ]
            chunk_fields = ["chunk_id", "paper_id", "text", *chunk_optional]
            chunk_select = ", ".join(
                f"tc.{field} AS {field}" for field in chunk_fields
            )
            paper_text_field = (
                "search_text"
                if "search_text" in paper_columns
                else "abstract"
                if "abstract" in paper_columns
                else "title"
            )
            paper_optional = [
                field for field in (
                    "content_depth",
                    "use_permission",
                    "source_kind",
                    "discovery_route",
                    "materialization_route",
                    "route_provenance",
                    "route_provenance_json",
                    "provenance_json",
                    "context_complete",
                    "allowed_claim_kinds",
                    "allowed_claim_kinds_json",
                    "scope_fit",
                )
                if field in paper_columns
            ]
            paper_fields = ["paper_id", "title", paper_text_field, "year", "venue", *paper_optional]

            def _hit_from_row(
                row: tuple[Any, ...],
                fields: List[str],
                *,
                has_chunk: bool,
            ) -> Dict[str, Any]:
                raw = dict(zip(fields, row))
                route = _coverage_json_object(
                    raw.get("route_provenance")
                    or raw.get("route_provenance_json")
                    or raw.get("provenance_json")
                )
                allowed = _normalise_coverage_claim_kinds(
                    raw.get("allowed_claim_kinds")
                    or raw.get("allowed_claim_kinds_json")
                    or route.get("allowed_claim_kinds")
                )
                source_kind = str(raw.get("source_kind") or "").strip()
                evidence_level = str(raw.get("evidence_level") or "").strip()
                depth = _coverage_depth(
                    raw.get("content_depth")
                    or route.get("content_depth")
                    or evidence_level
                    or (source_kind if source_kind else "")
                    or ("fulltext" if has_chunk else "metadata")
                )
                if raw.get("discovery_route"):
                    route.setdefault("discovery_route", str(raw["discovery_route"]))
                if raw.get("materialization_route"):
                    route.setdefault(
                        "materialization_route",
                        str(raw["materialization_route"]),
                    )
                if raw.get("context_complete") is not None:
                    route.setdefault(
                        "context_complete",
                        _coverage_bool(raw.get("context_complete")),
                    )
                if allowed:
                    route.setdefault("allowed_claim_kinds", sorted(allowed))
                contract = _coverage_contract_present({
                    **raw,
                    "route_provenance": route,
                    "allowed_claim_kinds": allowed,
                })
                return {
                    "chunk_id": str(raw.get("chunk_id") or ""),
                    "paper_id": str(raw.get("paper_id") or ""),
                    "text_raw": str(
                        raw.get("text")
                        or raw.get(paper_text_field)
                        or ""
                    )[:800],
                    "title_raw": str(raw.get("title") or ""),
                    "text_lc": str(
                        raw.get("text")
                        or raw.get(paper_text_field)
                        or ""
                    ).lower(),
                    "title_lc": str(raw.get("title") or "").lower(),
                    "year": raw.get("year"),
                    "venue": raw.get("venue") or "",
                    "content_depth": depth,
                    "use_permission": str(raw.get("use_permission") or ""),
                    "source_kind": source_kind or evidence_level,
                    "evidence_level": evidence_level,
                    "discovery_route": str(
                        raw.get("discovery_route")
                        or route.get("discovery_route")
                        or ""
                    ),
                    "materialization_route": str(
                        raw.get("materialization_route")
                        or route.get("materialization_route")
                        or ""
                    ),
                    "route_provenance": route,
                    "context_complete": (
                        _coverage_bool(raw.get("context_complete"))
                        if raw.get("context_complete") is not None
                        else _coverage_context_complete(
                            {"route_provenance": route}, depth
                        )
                    ),
                    "allowed_claim_kinds": sorted(allowed),
                    "scope_fit": str(raw.get("scope_fit") or ""),
                    "permission_contract_present": contract,
                }

            try:
                rows = conn.execute(
                    f"SELECT {chunk_select}, p.title AS title, p.year AS year, p.venue AS venue "
                    "FROM text_chunk_fts fts "
                    "JOIN text_chunks tc ON tc.chunk_id = fts.chunk_id "
                    "LEFT JOIN papers p ON p.paper_id = tc.paper_id "
                    "WHERE text_chunk_fts MATCH ? ORDER BY rank LIMIT ?",
                    (topic_query, fetch_k),
                ).fetchall()
                for row in rows:
                    chunk_width = len(chunk_fields)
                    raw_chunk = _hit_from_row(
                        row[:chunk_width], chunk_fields, has_chunk=True
                    )
                    raw_chunk["title_raw"] = str(row[chunk_width] or "")
                    raw_chunk["title_lc"] = raw_chunk["title_raw"].lower()
                    raw_chunk["year"] = row[chunk_width + 1]
                    raw_chunk["venue"] = row[chunk_width + 2] or ""
                    phase1.append(raw_chunk)
            except Exception:
                phase1 = []

            # ``papers`` has no abstract column.  Its FTS ``search_text``
            # already contains title, DOI and abstract metadata.  Query it
            # when chunk search is unavailable or simply has no hit, while
            # retaining an empty chunk_id so metadata-only papers stay
            # discovery leads rather than masquerading as evidence.
            if not phase1:
                try:
                    paper_select = ", ".join(
                        f"p.{field} AS {field}" for field in paper_fields
                    )
                    rows = conn.execute(
                        f"SELECT {paper_select} "
                        "FROM paper_fts fts "
                        "JOIN papers p ON p.paper_id = fts.paper_id "
                        "WHERE paper_fts MATCH ? ORDER BY rank LIMIT ?",
                        (topic_query, fetch_k),
                    ).fetchall()
                    for row in rows:
                        phase1.append(_hit_from_row(
                            row, paper_fields, has_chunk=False
                        ))
                except Exception:
                    pass

            # Phase 2: require at least one role keyword in title or text
            for h in phase1:
                combined_lc = h["text_lc"] + " " + h["title_lc"]
                role_matches = [kw for kw in role_kw if kw in combined_lc]
                topic_matches = [term for term in topic_tokens if term in combined_lc]
                if role_matches:
                    results.append({
                        "chunk_id": h["chunk_id"],
                        "paper_id": h["paper_id"],
                        "text": h["text_raw"],
                        "title": h["title_raw"],
                        "year": h.get("year"),
                        "venue": h.get("venue", ""),
                        "topic_matches": topic_matches,
                        "role_matches": role_matches,
                        "content_depth": h.get("content_depth", "metadata"),
                        "use_permission": h.get("use_permission", ""),
                        "source_kind": h.get("source_kind", ""),
                        "evidence_level": h.get("evidence_level", ""),
                        "discovery_route": h.get("discovery_route", ""),
                        "materialization_route": h.get("materialization_route", ""),
                        "route_provenance": dict(h.get("route_provenance") or {}),
                        "context_complete": h.get("context_complete", False),
                        "allowed_claim_kinds": list(h.get("allowed_claim_kinds") or []),
                        "scope_fit": h.get("scope_fit", ""),
                        "permission_contract_present": bool(
                            h.get("permission_contract_present")
                        ),
                    })
                    if len(results) >= top_k:
                        break

    except Exception as exc:
        logger.warning("_query_kb_for_role failed: %s", exc)
    return results


# ---------------------------------------------------------------------------
# 3. query_review_knowledge_base
# ---------------------------------------------------------------------------

def _make_query_review_knowledge_base(ctx: SectionCoverageContext):
    def query_review_knowledge_base(query: str, top_k: int = 10, role: str = "") -> str:
        """Run a free-text BM25 query against the local ReviewKnowledgeBase.

        Args:
            query: Free-text search query targeting the section's coverage need.
            top_k: Maximum results to return (default 10).
            role: Optional coverage role hint (foundation/mechanism/method/frontier/controversy/application).

        Returns JSON list of matching papers and chunks.
        """
        if not query or not query.strip():
            return json.dumps({"status": "error", "error": "query must not be empty"})

        topic_identity = ctx.section_data.get("topic_identity", {})
        topic_identity = (
            topic_identity if isinstance(topic_identity, dict) else {}
        )
        if (
            topic_identity.get("valid")
            and not (ctx.work_dir / "SECTION_COVERAGE_PLAN.json").exists()
        ):
            return json.dumps({
                "status": "error",
                "error_code": "coverage_plan_required",
                "error": (
                    "Submit SECTION_COVERAGE_PLAN.json before issuing focused "
                    "local or external retrieval queries."
                ),
            })

        query, topic_correction = anchor_retrieval_query(
            query,
            topic_identity,
        )
        if topic_correction["after"]["status"] == "failed":
            return json.dumps({
                "status": "error",
                "error_code": "retrieval_query_topic_drift",
                "error": "The query does not preserve the scientific object.",
                "topic_alignment": topic_correction["after"],
            })

        if ctx.kb_sqlite is None or not ctx.kb_sqlite.exists():
            return json.dumps({"status": "ok", "results": [], "message": "No local KB available."})

        normalized_role = role if role in COVERAGE_ROLES else ""
        hits = _query_kb_for_role(
            ctx.kb_sqlite,
            ctx.section_id,
            normalized_role or query,
            top_k=min(top_k, 20),
            section_data=ctx.section_data,
        )
        # Also run the literal query
        raw_hits = []
        try:
            with sqlite3.connect(str(ctx.kb_sqlite)) as conn:
                try:
                    rows = conn.execute(
                        "SELECT tc.chunk_id, tc.paper_id, tc.text, p.title, p.year, p.venue "
                        "FROM text_chunk_fts fts "
                        "JOIN text_chunks tc ON tc.chunk_id = fts.chunk_id "
                        "LEFT JOIN papers p ON p.paper_id = tc.paper_id "
                        "WHERE text_chunk_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (query, min(top_k, 20)),
                    ).fetchall()
                    for row in rows:
                        raw_hits.append({
                            "chunk_id": row[0],
                            "paper_id": row[1],
                            "text": (row[2] or "")[:300],
                            "title": row[3] or "",
                            "year": row[4],
                            "venue": row[5] or "",
                        })
                except Exception:
                    pass
        except Exception:
            pass

        # Merge and deduplicate
        seen_chunks: set = set()
        merged = []
        for h in hits + raw_hits:
            k = h.get("chunk_id") or h.get("paper_id")
            if k and k not in seen_chunks:
                seen_chunks.add(k)
                merged.append(h)
        merged = merged[: min(top_k, 8)]
        for item in merged:
            if "text" in item:
                item["text"] = _safe_str(item.get("text", ""), 280)
        registered = (
            _register_local_hits(
                ctx,
                normalized_role,
                merged,
                retrieval_query=query,
            )
            if normalized_role else []
        )
        candidate_by_key = {
            (item.get("paper_id"), item.get("chunk_id")): item.get("candidate_id")
            for item in registered
        }
        for item in merged:
            item["candidate_id"] = candidate_by_key.get(
                (item.get("paper_id"), item.get("chunk_id")), ""
            )

        payload_state = _read_agent_payload_state(ctx)
        query_fingerprints = payload_state.setdefault("query_fingerprints", {})
        query_key = stable_payload_fingerprint({
            "query": query,
            "role": normalized_role,
            "ids": [item.get("candidate_id") for item in merged],
        })
        unchanged = query_fingerprints.get(query_key) == stable_payload_fingerprint({
            "results": merged,
        })
        query_fingerprints[query_key] = stable_payload_fingerprint({"results": merged})
        _write_agent_payload_state(ctx, payload_state)
        results_for_agent = [] if unchanged else merged

        return json.dumps({
            "status": "ok",
            "query": query,
            "topic_query_correction": topic_correction,
            "role": role,
            "result_count": len(merged),
            "delta_result_count": len(results_for_agent),
            "results": results_for_agent,
            "unchanged": unchanged,
            "audit_required_before_adoption": bool(normalized_role),
            "ledger_summary": _payload_ledger_summary(ctx),
        }, ensure_ascii=False)

    return query_review_knowledge_base


# ---------------------------------------------------------------------------
# 4. inspect / audit local candidates
# ---------------------------------------------------------------------------

def _make_inspect_local_candidate_batch(ctx: SectionCoverageContext):
    def inspect_local_candidate_batch(candidate_ids: str) -> str:
        """Inspect locally recalled paper/chunk candidates before adoption.

        Args:
            candidate_ids: JSON array of ``local_*`` IDs returned by
                inspect_section_local_coverage or query_review_knowledge_base.

        Returns candidate title, role, matched terms, and the canonical chunk
        preview.  Recall is not approval.
        """
        try:
            ids = json.loads(candidate_ids) if candidate_ids.strip().startswith("[") else [candidate_ids]
        except Exception:
            ids = [candidate_ids]
        ids = [str(item).strip() for item in ids if str(item).strip()]
        ledger = _read_local_candidate_ledger(ctx)
        by_id = {
            item.get("candidate_id"): item
            for item in ledger.get("candidates", [])
            if isinstance(item, dict)
        }
        found = []
        unchanged: List[str] = []
        payload_state = _read_agent_payload_state(ctx)
        fingerprints = payload_state.setdefault("local_candidate_fingerprints", {})
        for item in ids[:LOCAL_AUDIT_MAX_INSPECTION_CANDIDATES]:
            if item not in by_id:
                continue
            candidate = dict(by_id[item])
            candidate = {
                "candidate_id": candidate.get("candidate_id"),
                "paper_id": candidate.get("paper_id"),
                "chunk_id": candidate.get("chunk_id"),
                "title": compact_text(candidate.get("title"), 160),
                "year": candidate.get("year"),
                "venue": compact_text(candidate.get("venue"), 80),
                "role": candidate.get("role"),
                "scope_fit": candidate.get("scope_fit", "unreviewed"),
                "decision": candidate.get("decision", "deferred"),
                "topic_matches": list(candidate.get("topic_matches") or [])[:6],
                "role_matches": list(candidate.get("role_matches") or [])[:6],
                "text_preview": compact_text(candidate.get("text_preview"), 520),
                "audit_reason": compact_text(candidate.get("audit_reason"), 240),
                "not_usable_for": list(candidate.get("not_usable_for") or [])[:4],
            }
            fingerprint = stable_payload_fingerprint(candidate)
            if fingerprints.get(item) == fingerprint:
                unchanged.append(item)
                continue
            fingerprints[item] = fingerprint
            found.append(candidate)
        missing = [item for item in ids if item not in by_id]
        _write_agent_payload_state(ctx, payload_state)
        return json.dumps({
            "status": "ok",
            "found": len(found),
            "available": len(found) + len(unchanged),
            "missing": missing,
            "unchanged_ids": unchanged,
            "candidates": found,
            "role_definitions": {
                role: ROLE_DEFINITIONS[role]
                for role in sorted({
                    str(item.get("role")) for item in found
                    if item.get("role") in ROLE_DEFINITIONS
                })
            },
            "audit_instruction": (
                "Judge whether this exact paper/chunk helps the stated section and "
                "whether it genuinely performs the proposed literature role. "
                "Topical vocabulary overlap, generic recency, or merely mentioning "
                "a challenge is insufficient. Direct scope_fit is reserved for the "
                "exact section question; cross-platform or cross-regime transfer is adjacent."
            ),
            "ledger_summary": _payload_ledger_summary(ctx),
        }, ensure_ascii=False)

    return inspect_local_candidate_batch


def _make_submit_local_source_audit(ctx: SectionCoverageContext):
    def submit_local_source_audit(audit_json: str) -> str:
        """Record explicit adoption decisions for local KB candidates.

        Args:
            audit_json: JSON array.  Every object requires ``candidate_id``,
                ``scope_fit`` (direct/adjacent/contextual/out_of_scope),
                ``decision`` (approved/rejected/deferred), and
                ``audit_reason``.  ``not_usable_for`` is optional.

        Only explicitly approved direct/adjacent candidates count toward
        section coverage.  Approved contextual candidates remain background
        material and cannot satisfy a required literature role.
        """
        decoded = decode_json_payload(
            audit_json,
            expected="list",
            allow_single_object_for_list=True,
        )
        if decoded.error:
            return json.dumps({"status": "error", "error": decoded.error})
        records = decoded.value
        if not isinstance(records, list):
            return json.dumps({"status": "error", "error": "audit_json must be a JSON array"})

        ledger = _read_local_candidate_ledger(ctx)
        candidates = {
            item.get("candidate_id"): item
            for item in ledger.get("candidates", [])
            if isinstance(item, dict) and item.get("candidate_id")
        }
        errors: List[str] = []
        updated = 0
        approved = 0
        for record in records:
            if not isinstance(record, dict):
                errors.append("audit record must be an object")
                continue
            candidate_id = str(record.get("candidate_id") or "")
            candidate = candidates.get(candidate_id)
            if candidate is None:
                errors.append(f"Unknown candidate_id: {candidate_id}")
                continue
            scope_fit = str(record.get("scope_fit") or "").strip().casefold()
            decision = str(record.get("decision") or "").strip().casefold()
            reason = _safe_str(record.get("audit_reason", ""), 500).strip()
            if scope_fit not in {item.value for item in ScopeFit}:
                errors.append(f"{candidate_id}: invalid scope_fit={scope_fit!r}")
                continue
            if decision not in {item.value for item in CandidateDecision}:
                errors.append(f"{candidate_id}: invalid decision={decision!r}")
                continue
            if decision == "approved" and (
                scope_fit not in ("direct", "adjacent", "contextual") or not reason
            ):
                errors.append(
                    f"{candidate_id}: approval requires direct/adjacent/contextual "
                    "scope_fit and a non-empty audit_reason"
                )
                continue
            restrictions_raw = record.get("not_usable_for", [])
            if not isinstance(restrictions_raw, list):
                errors.append(f"{candidate_id}: not_usable_for must be a list")
                continue
            restrictions = _canonical_scope_restrictions(
                scope_fit,
                list(restrictions_raw),
            )
            candidate["scope_fit"] = scope_fit
            candidate["decision"] = decision
            candidate["audit_reason"] = reason
            candidate["not_usable_for"] = restrictions
            role_fit_raw = record.get("role_fit")
            role_fit: List[str] = []
            if isinstance(role_fit_raw, list):
                role_fit = [
                    str(item).strip().casefold()
                    for item in role_fit_raw
                    if str(item).strip().casefold() in COVERAGE_ROLES
                ]
            if not role_fit:
                role_fit = [
                    str(candidate.get("role") or "foundation")
                    .strip()
                    .casefold()
                ]
            candidate["role_fit"] = role_fit
            score = record.get(
                "semantic_score",
                record.get("relevance_score"),
            )
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                candidate["semantic_score"] = round(
                    max(0.0, min(1.0, float(score))), 4
                )
            elif "semantic_score" not in candidate:
                fallback = candidate.get("relevance_score")
                candidate["semantic_score"] = round(
                    max(0.0, min(1.0, float(fallback or 0.5))), 4
                )
            action_raw = str(
                record.get("candidate_decision") or ""
            ).strip().casefold()
            if action_raw in {"materialize_now", "discovery_lead", "reject"}:
                candidate["candidate_decision"] = action_raw
            updated += 1
            approved += int(decision == "approved")

        ledger["candidates"] = list(candidates.values())
        _write_local_candidate_ledger(ctx, ledger)
        return json.dumps({
            "status": "ok" if not errors else "partial",
            "updated": updated,
            "approved": approved,
            "errors": errors,
            "artifact": LOCAL_CANDIDATE_LEDGER,
            "json_recovered": bool(decoded.recovered),
        }, ensure_ascii=False)

    return submit_local_source_audit


# ---------------------------------------------------------------------------
# 5. submit_literature_role_plan
# ---------------------------------------------------------------------------

def _make_submit_literature_role_plan(ctx: SectionCoverageContext):
    def submit_literature_role_plan(plan_json: str) -> str:
        """Validate and save the agent's coverage plan to SECTION_COVERAGE_PLAN.json.

        Args:
            plan_json: JSON string with keys for each coverage role.
                Each role entry must have: priority, coverage_question, intended_synthesis, queries (list).
                Example:
                {
                  "foundation": {"priority": "required", "coverage_question": "...",
                                 "intended_synthesis": "...", "queries": ["..."]},
                  "mechanism": {"priority": "important", ...},
                  ...
                }

        Returns confirmation or validation errors.
        """
        decoded = decode_json_payload(plan_json, expected="object")
        if decoded.error:
            return json.dumps({"status": "error", "error": decoded.error})
        data = decoded.value
        if not isinstance(data, dict):
            return json.dumps({"status": "error", "error": "plan_json must be a JSON object"})

        roles_dict: Dict[str, RolePlan] = {}
        errors = []
        topic_identity = ctx.section_data.get("topic_identity", {})
        topic_identity = (
            topic_identity if isinstance(topic_identity, dict) else {}
        )
        query_topic_corrections: List[Dict[str, Any]] = []
        for role in COVERAGE_ROLES:
            if role not in data:
                errors.append(f"Missing role: {role}")
                continue
            entry = data[role]
            if not isinstance(entry, dict):
                errors.append(f"Role {role} must be a dict")
                continue
            priority_val = entry.get("priority", "useful")
            try:
                priority = RolePriority(priority_val)
            except (TypeError, ValueError):
                errors.append(f"Role {role} has invalid priority={priority_val!r}")
                continue
            raw_queries_value = entry.get("queries", [])
            if not isinstance(raw_queries_value, list):
                errors.append(f"Role {role} queries must be a list")
                raw_queries_value = []
            raw_queries = [
                " ".join(str(query or "").split()).strip()
                for query in raw_queries_value[:6]
                if str(query or "").strip()
            ]
            normalized_queries: List[str] = []
            for raw_query in raw_queries:
                normalized_query, correction = anchor_retrieval_query(
                    raw_query,
                    topic_identity,
                )
                if correction["after"]["status"] == "failed":
                    errors.append(
                        f"Role {role} query loses the scientific object: "
                        f"{raw_query!r}"
                    )
                    continue
                normalized_queries.append(normalized_query)
                if correction["changed"]:
                    query_topic_corrections.append({
                        "role": role,
                        "original_query": raw_query,
                        "normalized_query": normalized_query,
                        "reason": "scientific_object_anchor_restored",
                        "topic_alignment": correction["after"],
                    })
            if (
                priority != RolePriority.not_needed
                and not normalized_queries
            ):
                errors.append(
                    f"Role {role} with priority={priority.value} requires at "
                    "least one topic-preserving query."
                )
            try:
                roles_dict[role] = RolePlan(
                    role=role,
                    priority=priority,
                    coverage_question=_safe_str(entry.get("coverage_question", ""), 500),
                    intended_synthesis=_safe_str(entry.get("intended_synthesis", ""), 500),
                    queries=normalized_queries,
                    local_hit_count=entry.get("local_hit_count", 0),
                    gap_severity=entry.get("gap_severity", "unknown"),
                )
            except Exception as exc:
                errors.append(f"Role {role} has invalid plan fields: {str(exc)[:180]}")

        if errors:
            return json.dumps({"status": "error", "errors": errors})

        plan = SectionCoveragePlan(
            section_id=ctx.section_id,
            chapter_argument=ctx.chapter_argument,
            roles=roles_dict,
            topic_fingerprint=str(topic_identity.get("fingerprint") or ""),
            query_topic_corrections=query_topic_corrections,
        )
        _write_artifact(ctx.work_dir, "SECTION_COVERAGE_PLAN.json", plan)
        return json.dumps({
            "status": "ok",
            "artifact": "SECTION_COVERAGE_PLAN.json",
            "roles_planned": list(roles_dict.keys()),
            "required_roles": [r for r, p in roles_dict.items() if p.priority == RolePriority.required],
            "query_topic_corrections": query_topic_corrections,
            "json_recovered": bool(decoded.recovered),
        }, ensure_ascii=False)

    return submit_literature_role_plan


# ---------------------------------------------------------------------------
# 5. search_oa_candidates
# ---------------------------------------------------------------------------

def _is_s2_transport_failure(error: BaseException) -> bool:
    """Classify S2 availability failures without exposing provider secrets."""

    try:
        from optomind_research.s2_intelligence_gateway import S2AvailabilityError
        if isinstance(error, S2AvailabilityError):
            return True
    except Exception:
        pass
    text = str(error or "").casefold()
    return any(
        marker in text
        for marker in (
            "429", "too many requests", "timeout", "timed out", "winerror 10054",
            "connection reset", "connection aborted", "connectionerror", "networkerror",
            "temporarily unavailable", "availability_delay", "service unavailable",
        )
    )

def _make_search_oa_candidates(ctx: SectionCoverageContext):
    def search_oa_candidates(
        role: str,
        queries: str,
        max_per_backend: int = 5,
    ) -> str:
        """Search OpenAlex and Semantic Scholar for OA candidates for a coverage role.

        No LLM calls. Returns raw metadata for agent inspection.
        Registers candidates in the session store and appends to OA_CANDIDATE_LEDGER.json.

        Args:
            role: Coverage role (foundation/mechanism/method/frontier/controversy/application).
            queries: JSON array of query strings, e.g. ["query1", "query2"].
            max_per_backend: Max results per backend per query (default 5, max 10).
        """
        if role not in COVERAGE_ROLES:
            return json.dumps({"status": "error", "error": f"Unknown role: {role}. Must be one of {COVERAGE_ROLES}"})

        topic_identity = ctx.section_data.get("topic_identity", {})
        topic_identity = (
            topic_identity if isinstance(topic_identity, dict) else {}
        )
        if (
            topic_identity.get("valid")
            and not (ctx.work_dir / "SECTION_COVERAGE_PLAN.json").exists()
        ):
            return json.dumps({
                "status": "error",
                "error_code": "coverage_plan_required",
                "error": (
                    "Submit SECTION_COVERAGE_PLAN.json before external "
                    "retrieval."
                ),
            })

        # Hard budget: role restriction
        if ctx.min_mode_allowed_role is not None and role != ctx.min_mode_allowed_role:
            return json.dumps({
                "status": "error",
                "error": (
                    f"Budget constraint: only role '{ctx.min_mode_allowed_role}' is allowed "
                    f"in this run. Requested: '{role}'."
                ),
            })
        if ctx.targeted_missing_roles and role not in set(ctx.targeted_missing_roles):
            return json.dumps({
                "status": "error",
                "error_code": "phase3_role_not_requested",
                "error": (
                    f"Phase 3 requested roles {ctx.targeted_missing_roles}; "
                    f"role '{role}' is not an approved target for this run."
                ),
                "requested_missing_claim_ids": ctx.targeted_missing_claim_ids,
            })

        try:
            query_list = json.loads(queries) if queries.strip().startswith("[") else [queries]
        except Exception:
            query_list = [queries]
        # Hard budget: clip to min_mode_max_queries (default 4 = no restriction in normal mode)
        query_list = [
            str(q).strip() for q in query_list if str(q).strip()
        ][:ctx.min_mode_max_queries]

        # A missing role must never depend on the model inventing a query in a
        # later turn.  Derive a compact, topic-anchored query from the durable
        # audit/plan/component ledgers when the caller provides an empty list.
        deterministic_targets = _coverage_query_targets(ctx)
        if not query_list:
            query_list = [
                str(item.get("query") or "").strip()
                for item in deterministic_targets
                if (
                    str(item.get("query") or "").strip()
                    and (
                        not item.get("role")
                        or str(item.get("role")) == role
                    )
                )
            ][:ctx.min_mode_max_queries]

        # A Phase-3 CoverageRequest is authoritative.  The worker may choose
        # the role and inspect candidates, but it cannot replace the focused
        # query set with a broad chapter question.
        phase3_queries = ctx.targeted_queries
        if phase3_queries:
            allowed = {
                _normalized_target_text(query): query
                for query in phase3_queries
                if _normalized_target_text(query)
            }
            requested_subset = [
                allowed[_normalized_target_text(query)]
                for query in query_list
                if _normalized_target_text(query) in allowed
            ]
            query_list = list(dict.fromkeys(
                requested_subset or phase3_queries
            ))[:ctx.min_mode_max_queries]

        if not query_list:
            return json.dumps({"status": "error", "error": "queries must contain at least one query string"})

        corrected_queries: List[str] = []
        topic_query_corrections: List[Dict[str, Any]] = []
        for raw_query in query_list:
            corrected, correction = anchor_retrieval_query(
                raw_query,
                topic_identity,
            )
            if correction["after"]["status"] == "failed":
                return json.dumps({
                    "status": "error",
                    "error_code": "retrieval_query_topic_drift",
                    "error": (
                        "An external retrieval query does not preserve the "
                        "scientific object."
                    ),
                    "query": raw_query,
                    "topic_alignment": correction["after"],
                })
            target_components: List[Any] = []
            for target in deterministic_targets:
                target_query = str(target.get("query") or "").strip()
                if (
                    _normalized_target_text(target_query)
                    == _normalized_target_text(raw_query)
                    or _normalized_target_text(target_query)
                    == _normalized_target_text(corrected)
                ):
                    target_components.extend(
                        target.get("components") or target.get("missing_components") or []
                    )
            normalized = normalize_scientific_query(
                corrected,
                section_data=ctx.section_data,
                components=target_components,
                role=role,
            )
            corrected_queries.append(normalized)
            if correction["changed"] or normalized != corrected:
                topic_query_corrections.append({
                    "original_query": raw_query,
                    "normalized_query": normalized,
                    "reason": (
                        "scientific_object_anchor_restored"
                        if correction["changed"]
                        else "provider_query_budget_normalized"
                    ),
                    "topic_alignment": correction["after"],
                })
        query_list = corrected_queries

        # Hard budget: clip max_per_backend to min_mode_max_per_backend
        max_per_backend = min(max(1, max_per_backend), 10, ctx.min_mode_max_per_backend)

        # Durable search-round control.  This is intentionally enforced below
        # the model: prompt instructions alone cannot prevent a long ReAct
        # context from repeatedly reopening the same role.
        with ctx._store_lock:
            search_ledger = _read_search_budget_ledger(ctx)
            existing_rounds = [
                item for item in search_ledger.get("rounds", [])
                if isinstance(item, dict)
            ]
            request_wave = (ctx.phase3_coverage_request or {}).get("wave_index")
            if request_wave is None:
                wave_index = max(
                    [int(item.get("wave_index") or 0) for item in existing_rounds]
                    or [-1]
                ) + 1
            else:
                try:
                    wave_index = max(0, int(request_wave))
                except (TypeError, ValueError):
                    wave_index = 0
            max_waves = int(getattr(ctx, "max_coverage_waves", 0) or 0)
            if max_waves > 0 and wave_index >= max_waves:
                telemetry = _read_wave_telemetry(ctx)
                telemetry.setdefault("stop_reasons", []).append({
                    "wave_index": wave_index,
                    "reason": "bounded_wave_budget_reached",
                })
                _write_wave_telemetry(ctx, telemetry)
                return json.dumps({
                    "status": "error",
                    "error_code": "coverage_wave_budget_reached",
                    "wave_index": wave_index,
                    "max_waves": max_waves,
                    "error": "Bounded coverage waves exhausted; document optional gaps or merge the section.",
                })
            role_rounds = [
                item for item in search_ledger["rounds"]
                if isinstance(item, dict) and item.get("role") == role
            ]
            max_rounds = max(1, int(ctx.max_search_rounds_per_role))
            if len(role_rounds) >= max_rounds:
                return json.dumps({
                    "status": "error",
                    "error_code": "role_search_round_limit_reached",
                    "error": (
                        f"Search stop condition reached for role '{role}': "
                        f"{len(role_rounds)}/{max_rounds} rounds already used. "
                        "Document the remaining gap and validate the package; "
                        "do not invent or force a weak source."
                    ),
                    "rounds_used": len(role_rounds),
                    "max_rounds": max_rounds,
                })

            fingerprint = _query_round_fingerprint(query_list)
            if any(
                item.get("query_fingerprint") == fingerprint
                for item in role_rounds
            ):
                return json.dumps({
                    "status": "error",
                    "error_code": "duplicate_search_round",
                    "error": (
                        f"These queries repeat an earlier search round for role "
                        f"'{role}'. Audit existing candidates or use a genuinely "
                        "different inferential route."
                    ),
                    "rounds_used": len(role_rounds),
                })

            decisions = _candidate_decisions(ctx)
            if (
                role_rounds
                and not _round_has_any_audit(role_rounds[-1], decisions)
            ):
                return json.dumps({
                    "status": "error",
                    "error_code": "candidate_audit_required",
                    "error": (
                        f"Audit at least the promising candidates from search "
                        f"round {role_rounds[-1].get('round_index')} for role "
                        f"'{role}' before spending another search round."
                    ),
                })

            # If two fully adjudicated rounds produced no usable candidate,
            # further broad search has low expected value.  The correct action
            # is to document the gap, not to weaken scope until something fits.
            no_yield_rounds = []
            for item in role_rounds[-2:]:
                ids = [str(v) for v in item.get("candidate_ids", [])]
                fully_audited = bool(ids) and all(
                    decisions.get(candidate_id, {}).get("decision")
                    in ("approved", "rejected")
                    for candidate_id in ids
                )
                if (
                    not ids
                    or (
                        fully_audited
                        and not _round_has_usable_candidate(item, decisions)
                    )
                ):
                    no_yield_rounds.append(item)
            if len(no_yield_rounds) >= 2:
                return json.dumps({
                    "status": "error",
                    "error_code": "consecutive_no_yield_stop",
                    "error": (
                        f"Two completed search rounds for role '{role}' produced "
                        "no direct or adjacent approved source. Document the "
                        "evidence gap and validate instead of searching again."
                    ),
                })

            round_record = {
                "round_index": len(role_rounds) + 1,
                "wave_index": wave_index,
                "role": role,
                "queries": query_list,
                "query_targets": [
                    target for target in deterministic_targets
                    if str(target.get("query") or "").strip() in set(query_list)
                ],
                "query_fingerprint": fingerprint,
                "status": "admitted",
                "candidate_ids": [],
                "candidate_count": 0,
                "backend_stats": {},
                "started_at_epoch": round(time.time(), 3),
            }
            search_ledger["rounds"].append(round_record)
            # Persist admission before network I/O so a crash/restart cannot
            # silently spend the same round twice.
            _write_search_budget_ledger(ctx, search_ledger)

        topic_fp = str(topic_identity.get("fingerprint") or "") if isinstance(topic_identity, dict) else ""
        cached_entries = [
            get_global_query(ctx.global_coverage_ledger_path, topic_fp, role, query)
            for query in query_list
        ]
        cached_rows_by_query = [
            _reusable_cached_candidates(
                ctx,
                entry.get("candidates", []) if isinstance(entry, dict) else [],
                role=role,
                queries=[query],
                topic_fingerprint=topic_fp,
            )
            for entry, query in zip(cached_entries, query_list)
        ]
        replay_global_cache = bool(cached_entries) and all(
            isinstance(entry, dict) and bool(rows)
            for entry, rows in zip(cached_entries, cached_rows_by_query)
        )
        raw_candidates: List[Dict] = []
        backend_stats: Dict[str, int] = {}
        article_portfolio_candidates = _article_candidates_for_role(
            ctx,
            role,
            query_list,
        )
        if article_portfolio_candidates:
            raw_candidates.extend(article_portfolio_candidates)
            backend_stats["article_portfolio_cache_hits"] = len(article_portfolio_candidates)
            _bump_phase2_telemetry(
                ctx,
                article_portfolio_candidate_reuse_hits=len(article_portfolio_candidates),
            )
        elif replay_global_cache:
            for rows in cached_rows_by_query:
                raw_candidates.extend(dict(item) for item in rows)
            backend_stats["global_query_cache_hits"] = len(cached_entries)
            increment_global_stat(
                ctx.global_coverage_ledger_path,
                "query_cache_hits",
                len(cached_entries),
            )

        # S2-first: use the typed lightweight discovery endpoint before older
        # backends.  A provider availability failure is not a scientific zero.
        # Count each backend invocation per query; when the compatibility
        # backend is actually invoked after an empty rich result, that is a
        # second real S2 call rather than synthetic fallback telemetry.
        s2_call_count = 0
        if not replay_global_cache and not article_portfolio_candidates:
            try:
                if ctx.s2_first_enabled:
                    s2_call_count += len(query_list)
                    _s2_results = _search_s2_first(query_list, max_per_backend)
                    if not _s2_results:
                        s2_call_count += len(query_list)
                        _s2_results = _search_semantic_scholar(
                            query_list, max_per_backend
                        )
                        backend_stats["semantic_scholar_mode"] = "legacy_fallback"
                    else:
                        backend_stats["semantic_scholar_mode"] = "s2_first"
                else:
                    s2_call_count += len(query_list)
                    _s2_results = _search_semantic_scholar(
                        query_list, max_per_backend
                    )
                    backend_stats["semantic_scholar_mode"] = "legacy_compatible"
                raw_candidates.extend(_s2_results)
                backend_stats["semantic_scholar"] = len(_s2_results)
                backend_stats["semantic_scholar_calls"] = s2_call_count
                _bump_phase2_telemetry(ctx, s2_search_calls=s2_call_count)
            except Exception as exc:
                backend_stats["semantic_scholar_error"] = str(exc)[:100]
                transport_failure = _is_s2_transport_failure(exc)
                backend_stats["semantic_scholar_result_state"] = (
                    "transport_failure" if transport_failure else "request_failure"
                )
                if transport_failure:
                    backend_stats["semantic_scholar_transport_failure"] = 1
                # Legacy fallback is retained for a deterministic request
                # failure/empty result, but never duplicates an unavailable
                # S2 API request (429, timeout, connection reset, auth outage).
                if ctx.s2_first_enabled and not transport_failure:
                    try:
                        s2_call_count += len(query_list)
                        _s2_results = _search_semantic_scholar(
                            query_list, max_per_backend
                        )
                        raw_candidates.extend(_s2_results)
                        backend_stats["semantic_scholar_mode"] = "legacy_fallback_after_error"
                        backend_stats["semantic_scholar"] = len(_s2_results)
                    except Exception as fallback_exc:
                        backend_stats["semantic_scholar_fallback_error"] = str(
                            fallback_exc
                        )[:100]
                else:
                    _s2_results = []
                backend_stats["semantic_scholar_calls"] = s2_call_count
                _bump_phase2_telemetry(ctx, s2_search_calls=s2_call_count)
        elif article_portfolio_candidates:
            backend_stats["semantic_scholar_mode"] = "article_portfolio_cache"
            backend_stats["semantic_scholar"] = 0
            backend_stats["semantic_scholar_calls"] = 0
        else:
            backend_stats["semantic_scholar_mode"] = "global_query_cache"
            backend_stats["semantic_scholar"] = 0
            backend_stats["semantic_scholar_calls"] = 0

        # OpenAlex is complementary/fallback rather than an unconditional
        # duplicate search.  It fills identity/OA gaps when S2 recall is thin.
        if (
            not replay_global_cache
            and not article_portfolio_candidates
            and len(raw_candidates) < max_per_backend
        ):
            try:
                _oa_results = _search_openalex(query_list, max_per_backend)
                raw_candidates.extend(_oa_results)
                backend_stats["openalex"] = len(_oa_results)
                backend_stats["openalex_calls"] = len(query_list)
                _bump_phase2_telemetry(ctx, openalex_calls=len(query_list))
            except Exception as exc:
                backend_stats["openalex_error"] = str(exc)[:100]
                backend_stats["openalex_calls"] = len(query_list)
                _bump_phase2_telemetry(ctx, openalex_calls=len(query_list))
        elif not replay_global_cache and not article_portfolio_candidates:
            backend_stats["openalex"] = 0
            backend_stats["openalex_mode"] = "skipped_s2_sufficient"
            _record_skipped_backend(ctx, "openalex", "semantic_scholar_sufficient")

        # Deduplicate by DOI then title
        deduped = _dedup_raw_candidates(raw_candidates)

        # Quality gate before the model sees a candidate.  This prevents a
        # broad optics hit from becoming an "adjacent" filler source merely
        # because it shares words such as optical, wavefront, or beam.
        query_components = [
            item
            for target in deterministic_targets
            if not target.get("role") or str(target.get("role")) == role
            for item in (target.get("components") or target.get("missing_components") or [])
        ]
        quality_checked: List[Dict[str, Any]] = []
        quality_rejections = 0
        for raw in deduped:
            quality = evaluate_candidate_topic_affinity(
                raw,
                ctx.section_data,
                queries=query_list,
                components=query_components,
            )
            if not quality.get("accepted"):
                quality_rejections += 1
                continue
            enriched_raw = dict(raw)
            enriched_raw["topic_quality"] = quality
            if quality.get("scope_fit") == "adjacent":
                enriched_raw["explicit_topic_bridge"] = True
            quality_checked.append(enriched_raw)
        deduped = quality_checked
        if quality_rejections:
            backend_stats["quality_gate_rejections"] = quality_rejections

        if not replay_global_cache and not article_portfolio_candidates:
            for query in query_list:
                record_global_query(
                    ctx.global_coverage_ledger_path,
                    topic_fingerprint=topic_fp,
                    role=role,
                    query=query,
                    candidates=deduped,
                )

        # Build OACandidate objects and register
        oa_candidates = []
        for raw in deduped:
            cand_dict = {
                "section_id": ctx.section_id,
                "role": role,
                **raw,
                "topic_fingerprint": topic_fp,
            }
            cand_dict["role_fit"] = _role_union(
                role, raw.get("role"), raw.get("role_fit")
            )
            cand_dict["role_provenance"] = _merge_role_provenance(
                raw.get("role_provenance") or {},
                {role: list(raw.get("query_texts") or query_list)},
            )
            if _article_portfolio_path(ctx) is not None:
                cand_dict = _upsert_article_candidate(ctx, cand_dict)
            if not cand_dict.get("candidate_id"):
                cand_dict["candidate_id"] = "cand_" + uuid.uuid4().hex[:10]
            cand_dict = _apply_article_audit(ctx, cand_dict)
            oa_candidates.append(cand_dict)

        registered_ids = ctx.register_candidates(oa_candidates)

        # Append to OA_CANDIDATE_LEDGER.json
        _append_candidates_to_ledger(ctx.work_dir, ctx.section_id, oa_candidates)

        # Complete the durable round record after both backends finish.
        with ctx._store_lock:
            search_ledger = _read_search_budget_ledger(ctx)
            matching = [
                item for item in search_ledger["rounds"]
                if (
                    isinstance(item, dict)
                    and item.get("role") == role
                    and item.get("query_fingerprint") == fingerprint
                )
            ]
            if matching:
                matching[-1].update({
                    "status": "completed",
                    "wave_index": int(matching[-1].get("wave_index") or 0),
                    "candidate_ids": registered_ids,
                    "candidate_count": len(registered_ids),
                    "backend_stats": backend_stats,
                    "completed_at_epoch": round(time.time(), 3),
                })
            _write_search_budget_ledger(ctx, search_ledger)
        _mark_search_wave(
            ctx,
            wave_index=wave_index,
            backend_stats=backend_stats,
            candidate_count=len(registered_ids),
        )

        # Return summaries for agent (no full abstracts — keep token count low)
        _sync_article_portfolio_telemetry(ctx)

        def discovery_rank(cand: Dict[str, Any]) -> tuple:
            relevance = float(cand.get("relevance_score") or 0.0)
            citations = int(cand.get("citation_count") or 0)
            year = int(cand.get("year") or 0)
            if role == "foundation":
                primary = citations
            elif role == "frontier":
                primary = year
            else:
                primary = relevance
            return (
                -primary,
                -relevance,
                -citations,
                -year,
                -int(bool(cand.get("is_oa"))),
                str(cand.get("title") or ""),
            )

        ranked_candidates = sorted(oa_candidates, key=discovery_rank)
        visible_candidate_ids = [
            str(cand.get("candidate_id") or "")
            for cand in ranked_candidates[:12]
            if cand.get("candidate_id")
        ]
        summaries = []
        for cand in ranked_candidates[:12]:
            summaries.append({
                "candidate_id": cand["candidate_id"],
                "title": cand.get("title", "")[:120],
                "doi": cand.get("doi", ""),
                "year": cand.get("year"),
                "venue": cand.get("venue", "")[:60],
                "is_oa": cand.get("is_oa", False),
                "citation_count": cand.get("citation_count", 0),
                "abstract_snippet": cand.get("abstract", "")[:200],
                "backends": cand.get("backends", []),
            })

        return json.dumps({
            "status": "ok",
            "role": role,
            "queries_used": query_list,
            "wave_index": int(
                next(
                    (
                        item.get("wave_index")
                        for item in reversed(_read_search_budget_ledger(ctx).get("rounds", []))
                        if isinstance(item, dict)
                        and item.get("role") == role
                        and item.get("query_fingerprint") == fingerprint
                    ),
                    0,
                )
            ),
            "query_targets": [
                target for target in _coverage_query_targets(ctx)
                if str(target.get("query") or "").strip() in set(query_list)
            ],
            "deterministic_uncovered_query_targets": [
                target for target in deterministic_targets
                if str(target.get("query") or "").strip() in set(query_list)
            ],
            "backend_stats": backend_stats,
            "candidate_count": len(registered_ids),
            "candidate_ids": visible_candidate_ids,
            "candidates": summaries,
            "candidate_summaries_returned": len(summaries),
            "topic_query_corrections": topic_query_corrections,
            "phase3_request_consumed": bool(ctx.phase3_coverage_request),
            "phase3_missing_claim_ids": ctx.targeted_missing_claim_ids,
            "phase3_missing_roles": ctx.targeted_missing_roles,
            "phase3_expected_new_papers": ctx.targeted_expected_new_papers,
            "phase3_stop_condition": ctx.phase3_coverage_request.get("stop_condition", {}),
        }, ensure_ascii=False)

    return search_oa_candidates


def _search_openalex(queries: List[str], max_per_q: int) -> List[Dict]:
    """Search OpenAlex via GapOAEvidenceExpander backend with correct field mapping.

    OpenAlex backend returns: abstract_or_snippet (not abstract), journal_or_venue (not venue),
    open_access_url (not oa_url), cited_by_count (top-level), is_oa (top-level),
    source_id or openalex_id for the OA identifier.
    """
    try:
        from optomind_research.gap_oa_expander import OpenAlexBackend
        backend = OpenAlexBackend()
        results = []
        seen_dois: set = set()
        for q in queries:
            hits = backend.search(q, max_results=max_per_q)
            for h in hits:
                doi = (h.get("doi") or "").lower().strip()
                key = doi or (h.get("title") or "")[:60].lower()
                if key and key not in seen_dois:
                    seen_dois.add(key)
                    # Handle citation count: top-level cited_by_count OR raw_metadata
                    raw_meta = h.get("raw_metadata") or {}
                    cit = (h.get("cited_by_count")
                           or h.get("citation_count")
                           or raw_meta.get("cited_by_count")
                           or raw_meta.get("citation_count")
                           or 0)
                    # OA URL: open_access_url (OA) or oa_url fallback
                    oa_url = (h.get("open_access_url") or h.get("oa_url") or "")
                    # Abstract: abstract_or_snippet field
                    abstract = (h.get("abstract_or_snippet")
                                or h.get("abstract")
                                or h.get("snippet")
                                or "")
                    # Venue: journal_or_venue field
                    venue = (h.get("journal_or_venue")
                             or h.get("venue")
                             or h.get("journal")
                             or "")
                    # OpenAlex ID: openalex_id or source_id
                    openalex_id = (h.get("openalex_id")
                                   or h.get("source_id")
                                   or "")
                    # OpenAlex content URLs (pdf + grobid_xml) — passed to KBIngester
                    content_urls = {}
                    raw_content = h.get("content_urls") or raw_meta.get("content_urls") or {}
                    if isinstance(raw_content, dict):
                        content_urls = {k: v for k, v in raw_content.items() if v}
                    # url_for_pdf from primary_location
                    url_for_pdf = (h.get("url_for_pdf")
                                   or raw_meta.get("primary_location", {}).get("url_for_pdf", "")
                                   or "")
                    # best_oa_url from best_oa_location
                    best_oa = raw_meta.get("best_oa_location") or {}
                    best_oa_url = (h.get("best_oa_url")
                                   or best_oa.get("url_for_pdf")
                                   or best_oa.get("url")
                                   or "")
                    # alternate_urls: collect all oa_locations PDF URLs
                    alternate_urls = []
                    for loc in (raw_meta.get("oa_locations") or []):
                        for fld in ("url_for_pdf", "url"):
                            u = loc.get(fld, "")
                            if u and u not in alternate_urls and u not in (oa_url, url_for_pdf, best_oa_url):
                                alternate_urls.append(u)
                    results.append({
                        "title": h.get("title", ""),
                        "doi": h.get("doi", ""),
                        "year": h.get("year") or h.get("publication_year"),
                        "venue": venue,
                        "authors": h.get("authors", [])[:3],
                        "abstract": abstract,
                        "is_oa": bool(h.get("is_oa", False)),
                        "oa_url": oa_url,
                        "pdf_url": h.get("pdf_url", ""),
                        "url_for_pdf": url_for_pdf,
                        "best_oa_url": best_oa_url,
                        "open_access_url": oa_url,
                        "content_urls": content_urls,
                        "alternate_urls": alternate_urls,
                        "citation_count": int(cit or 0),
                        "backends": ["openalex"],
                        "query_texts": [q],
                        "relevance_score": float(h.get("relevance_score") or 0.0),
                        "openalex_id": openalex_id,
                    })
        return results
    except Exception as exc:
        logger.warning("OpenAlex search failed: %s", exc)
        raise RuntimeError(f"openalex_backend_failure: {str(exc)[:180]}") from exc


def _search_s2_first(queries: List[str], max_per_q: int) -> List[Dict]:
    """Search S2 cheaply, then enrich only a bounded identity shortlist.

    S2 backend returns: abstract_or_snippet (not abstract), journal_or_venue (not venue),
    NO top-level is_oa/oa_url — OA info lives in raw_metadata.open_access_pdf.
    semantic_scholar_paper_id (not semantic_scholar_id).
    """
    try:
        from optomind_research.s2_intelligence_gateway import (
            S2AvailabilityError,
            S2IntelligenceGateway,
            S2RequestContractError,
        )
        gateway = S2IntelligenceGateway()
        results = []
        seen_dois: set = set()
        for q in queries:
            papers, response = gateway.search_papers(
                q,
                limit=max_per_q,
                enrich_limit=min(3, max_per_q),
            )
            if not response.ok:
                detail = getattr(response, "error", "") or getattr(
                    response, "message", ""
                ) or getattr(response, "status_code", "") or "request failed"
                message = f"s2_backend_failure: {str(detail)[:180]}"
                if (
                    response.status_category in {
                        "availability_delay", "authentication_failure",
                    }
                    or response.status_code == 0
                    or response.status_code == 429
                    or response.status_code >= 500
                ):
                    raise S2AvailabilityError(message, response)
                if response.status_category == "request_contract_failure" or response.status_code in {
                    400, 404, 422,
                }:
                    raise S2RequestContractError(message, response)
                raise RuntimeError(message)
            for paper in papers:
                doi = paper.doi.lower().strip()
                key = doi or paper.paper_id or paper.title[:60].lower()
                if key and key not in seen_dois:
                    seen_dois.add(key)
                    pdf_url = paper.s2_open_access_candidate_url
                    results.append({
                        "title": paper.title,
                        "doi": paper.doi,
                        "year": paper.year,
                        "venue": paper.venue,
                        "authors": paper.authors[:3],
                        "abstract": paper.abstract,
                        "tldr": paper.tldr,
                        "is_oa": paper.is_oa,
                        "oa_url": pdf_url,
                        "pdf_url": pdf_url,
                        "url_for_pdf": pdf_url,
                        "best_oa_url": "",
                        "open_access_url": pdf_url,
                        "html_url": "",
                        "repository_url": "",
                        "content_urls": {},
                        "alternate_urls": [],
                        "citation_count": paper.citation_count,
                        "influential_citation_count": paper.influential_citation_count,
                        "backends": ["semantic_scholar"],
                        "query_texts": [q],
                        "relevance_score": 0.0,
                        "semantic_scholar_id": paper.paper_id,
                        "corpus_id": paper.corpus_id,
                        "text_availability": paper.text_availability,
                        "specter2_available": bool(paper.specter2_vector),
                        "raw_metadata": {
                            **paper.to_dict(),
                            "s2_gateway_audit": dict(response.audit),
                        },
                    })
        return results
    except (S2AvailabilityError, S2RequestContractError):
        raise
    except Exception as exc:
        logger.warning("S2 search failed: %s", exc)
        raise RuntimeError(f"semantic_scholar_backend_failure: {str(exc)[:180]}") from exc


def _search_semantic_scholar(queries: List[str], max_per_q: int) -> List[Dict]:
    """Legacy S2 backend adapter retained as an explicit fallback."""
    try:
        from optomind_research.gap_oa_expander import SemanticScholarBackend

        backend = SemanticScholarBackend()
        results = []
        seen_dois: set = set()
        for q in queries:
            hits = backend.search(q, max_results=max_per_q)
            for h in hits:
                doi = (h.get("doi") or "").lower().strip()
                key = doi or (h.get("title") or "")[:60].lower()
                if key and key not in seen_dois:
                    seen_dois.add(key)
                    raw_meta = h.get("raw_metadata") or {}
                    citation_count = (
                        h.get("citation_count")
                        or raw_meta.get("citation_count")
                        or raw_meta.get("citationCount")
                        or 0
                    )
                    oa_pdf = raw_meta.get("open_access_pdf") or {}
                    pdf_url = h.get("pdf_url") or oa_pdf.get("url") or ""
                    oa_url = (
                        h.get("open_access_url")
                        or h.get("oa_url")
                        or pdf_url
                        or ""
                    )
                    results.append(
                        {
                            "title": h.get("title", ""),
                            "doi": h.get("doi", ""),
                            "year": h.get("year") or h.get("publication_year"),
                            "venue": h.get("journal_or_venue")
                            or h.get("venue")
                            or h.get("journal")
                            or "",
                            "authors": h.get("authors", [])[:3],
                            "abstract": h.get("abstract_or_snippet")
                            or h.get("abstract")
                            or h.get("snippet")
                            or "",
                            "is_oa": bool(pdf_url or h.get("is_oa", False)),
                            "oa_url": oa_url,
                            "pdf_url": pdf_url,
                            "url_for_pdf": pdf_url,
                            "best_oa_url": "",
                            "open_access_url": oa_url,
                            "html_url": "",
                            "repository_url": "",
                            "content_urls": {},
                            "alternate_urls": [],
                            "citation_count": int(citation_count or 0),
                            "backends": ["semantic_scholar"],
                            "query_texts": [q],
                            "relevance_score": float(
                                h.get("relevance_score") or 0.0
                            ),
                            "semantic_scholar_id": h.get(
                                "semantic_scholar_id"
                            )
                            or h.get("semantic_scholar_paper_id")
                            or h.get("source_id")
                            or "",
                        }
                    )
        return results
    except Exception as exc:
        logger.warning("Legacy S2 search failed: %s", exc)
        raise RuntimeError(f"legacy_semantic_scholar_backend_failure: {str(exc)[:180]}") from exc


def _dedup_raw_candidates(candidates: List[Dict]) -> List[Dict]:
    """Merge duplicate candidates from multiple backends.

    P0-B: Never discard complementary metadata from the second backend.
    Union URL routes, backends, query_texts; preserve best abstract; keep highest citation count.
    """
    seen: Dict[str, int] = {}  # key → index
    merged: List[Dict] = []
    _url_fields = (
        "oa_url", "pdf_url", "url_for_pdf", "best_oa_url",
        "open_access_url", "html_url", "repository_url",
    )
    for c in candidates:
        doi = (c.get("doi") or "").lower().strip()
        title_key = (c.get("title") or "")[:60].lower()
        key = doi if doi else title_key
        if not key:
            merged.append(dict(c))
            continue
        if key in seen:
            existing = merged[seen[key]]
            # Union list fields
            existing["backends"] = list(dict.fromkeys(
                existing.get("backends", []) + c.get("backends", [])
            ))
            existing["query_texts"] = list(dict.fromkeys(
                existing.get("query_texts", []) + c.get("query_texts", [])
            ))
            existing["role_fit"] = _role_union(
                existing.get("role"), existing.get("role_fit"),
                c.get("role"), c.get("role_fit"),
            )
            existing["role_provenance"] = _merge_role_provenance(
                existing.get("role_provenance") or {},
                c.get("role_provenance") or {},
            )
            # Keep higher citation count
            if int(c.get("citation_count", 0) or 0) > int(existing.get("citation_count", 0) or 0):
                existing["citation_count"] = c["citation_count"]
            # Fill missing scalar metadata from second backend
            for field in ("doi", "year", "venue", "semantic_scholar_id", "openalex_id"):
                if not existing.get(field) and c.get(field):
                    existing[field] = c[field]
            # Keep better (longer) abstract
            ex_abs = (existing.get("abstract") or "")
            c_abs = (c.get("abstract") or "")
            if len(c_abs) > len(ex_abs):
                existing["abstract"] = c_abs
            # Fill missing URL fields, while retaining every distinct route in
            # alternate_urls even when both backends populated the same field.
            all_routes: List[str] = []
            for source in (existing, c):
                for uf in _url_fields:
                    value = source.get(uf)
                    if isinstance(value, str) and value.startswith("http"):
                        all_routes.append(value)
                all_routes.extend(
                    u for u in (source.get("alternate_urls") or [])
                    if isinstance(u, str) and u.startswith("http")
                )
                all_routes.extend(
                    u for u in (source.get("content_urls") or {}).values()
                    if isinstance(u, str) and u.startswith("http")
                )
            for uf in _url_fields:
                if not existing.get(uf) and c.get(uf):
                    existing[uf] = c[uf]
            # Propagate is_oa if either backend flagged it
            if c.get("is_oa"):
                existing["is_oa"] = True
            # Union authors without duplicating names/records.
            existing["authors"] = list(dict.fromkeys(
                str(a) for a in (list(existing.get("authors") or []) + list(c.get("authors") or []))
                if str(a).strip()
            ))
            # Merge content_urls dicts
            ex_content = dict(existing.get("content_urls") or {})
            for ck, cv in (c.get("content_urls") or {}).items():
                if cv and not ex_content.get(ck):
                    ex_content[ck] = cv
            existing["content_urls"] = ex_content
            existing["alternate_urls"] = list(dict.fromkeys(all_routes))
        else:
            seen[key] = len(merged)
            merged.append(dict(c))
    return merged


def _append_candidates_to_ledger(work_dir: Path, section_id: str, new_cands: List[Dict]) -> None:
    ledger_path = work_dir / "OA_CANDIDATE_LEDGER.json"
    if ledger_path.exists():
        try:
            existing = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger = OACandidateLedger.model_validate(existing)
        except Exception:
            ledger = OACandidateLedger(section_id=section_id)
    else:
        ledger = OACandidateLedger(section_id=section_id)

    existing_ids = {c.candidate_id for c in ledger.candidates}
    for cand in new_cands:
        candidate_id = str(cand.get("candidate_id") or "")
        identity = _candidate_identity(cand)
        existing = next(
            (
                item for item in ledger.candidates
                if item.candidate_id == candidate_id
                or (identity and item.material_identity == identity)
            ),
            None,
        )
        if existing is not None:
            existing.role_fit = _role_union(
                existing.role, existing.role_fit, cand.get("role"), cand.get("role_fit")
            )
            existing.role_provenance = _merge_role_provenance(
                existing.role_provenance, cand.get("role_provenance") or {},
            )
            existing.scope_violations = normalize_scope_violation_records([
                *existing.scope_violations, *(cand.get("scope_violations") or []),
            ])
            existing.boundary_violations = normalize_scope_violation_records([
                *existing.boundary_violations, *(cand.get("boundary_violations") or []),
            ])
            if (
                str(cand.get("scope_fit") or "").casefold() == ScopeFit.out_of_scope.value
                and str(cand.get("decision") or "").casefold() == CandidateDecision.rejected.value
            ):
                existing.scope_fit = ScopeFit.out_of_scope
                existing.decision = CandidateDecision.rejected
                existing.candidate_action = CandidateAction.reject
                existing.audit_reason = str(
                    cand.get("audit_reason") or existing.audit_reason
                )[:500]
                existing.not_usable_for = list(dict.fromkeys([
                    *existing.not_usable_for, *(cand.get("not_usable_for") or []),
                ]))
            existing.query_texts = list(dict.fromkeys([
                *existing.query_texts, *(cand.get("query_texts") or []),
            ]))
            existing.backends = list(dict.fromkeys([
                *existing.backends, *(cand.get("backends") or []),
            ]))
            if identity and existing.material_identity != identity:
                existing.material_identity = identity
            continue
        if candidate_id not in existing_ids:
            try:
                contract = canonical_candidate_decision(cand)
                raw_text_availability = cand.get("text_availability")
                text_availability = (
                    dict(raw_text_availability)
                    if isinstance(raw_text_availability, dict)
                    else {}
                )
                # OACandidate is intentionally a compact schema.  Preserve
                # local-route hints inside its existing typed availability
                # field so a restart does not turn a usable local asset back
                # into a discovery-only record.
                for key in (
                    "local_fulltext_path", "local_download_path",
                    "fulltext_path", "parsed_text_path", "local_file_path",
                    "local_fulltext", "has_local_fulltext",
                    "content_depth", "materialization_route", "local_prior",
                ):
                    if cand.get(key) is not None and key not in text_availability:
                        text_availability[key] = cand.get(key)
                ledger.candidates.append(OACandidate(
                    candidate_id=cand.get("candidate_id", ""),
                    section_id=cand.get("section_id", section_id),
                    role=cand.get("role", ""),
                    title=cand.get("title", ""),
                    doi=cand.get("doi", ""),
                    year=cand.get("year"),
                    venue=cand.get("venue", ""),
                    authors=cand.get("authors", []),
                    abstract=cand.get("abstract", ""),
                    is_oa=cand.get("is_oa", False),
                    oa_url=cand.get("oa_url", ""),
                    pdf_url=cand.get("pdf_url", ""),
                    url_for_pdf=cand.get("url_for_pdf", ""),
                    best_oa_url=cand.get("best_oa_url", ""),
                    open_access_url=cand.get("open_access_url", ""),
                    html_url=cand.get("html_url", ""),
                    repository_url=cand.get("repository_url", ""),
                    content_urls=cand.get("content_urls") or {},
                    alternate_urls=cand.get("alternate_urls") or [],
                    semantic_scholar_id=cand.get("semantic_scholar_id", ""),
                    corpus_id=cand.get("corpus_id"),
                    openalex_id=cand.get("openalex_id", ""),
                    tldr=cand.get("tldr", ""),
                    text_availability=text_availability,
                    citation_count=cand.get("citation_count", 0),
                    backends=cand.get("backends", []),
                    query_texts=cand.get("query_texts", []),
                    relevance_score=cand.get("relevance_score", 0.0),
                    scope_fit=cand.get("scope_fit", ScopeFit.unreviewed),
                    role_fit=_role_union(cand.get("role"), cand.get("role_fit")),
                    role_provenance=_merge_role_provenance(
                        cand.get("role_provenance") or {},
                        {
                            str(cand.get("role") or "").casefold(): list(
                                cand.get("query_texts") or []
                            )
                        },
                    ),
                    scope_violations=normalize_scope_violation_records(
                        cand.get("scope_violations") or []
                    ),
                    boundary_violations=normalize_scope_violation_records(
                        cand.get("boundary_violations") or []
                    ),
                    decision=cand.get("decision", CandidateDecision.deferred),
                    candidate_action=contract.action,
                    audit_reason=_candidate_action_audit_reason(cand, contract),
                    not_usable_for=cand.get("not_usable_for", []),
                    material_identity=_candidate_identity(cand),
                    attempted_waves=list(cand.get("attempted_waves") or []),
                    materialization_attempts=int(cand.get("materialization_attempts", 0) or 0),
                    last_materialization_status=str(cand.get("last_materialization_status") or ""),
                    no_progress=bool(cand.get("no_progress", False)),
                    no_progress_components=list(cand.get("no_progress_components") or []),
                ))
                existing_ids.add(candidate_id)
            except Exception as exc:
                logger.warning("Failed to add candidate to ledger: %s", exc)

    # Repair legacy ledgers at the persistence boundary.  In particular,
    # never preserve an action that is inconsistent with approval or the
    # currently represented legal route after a restart/backend merge.
    for candidate in ledger.candidates:
        candidate_data = candidate.model_dump()
        contract = canonical_candidate_decision(candidate_data)
        desired = contract.action
        if candidate.candidate_action.value != desired:
            candidate.candidate_action = CandidateAction(desired)
        repaired_reason = _candidate_action_audit_reason(candidate_data, contract)
        if candidate.audit_reason != repaired_reason:
            candidate.audit_reason = repaired_reason

    _write_artifact(work_dir, "OA_CANDIDATE_LEDGER.json", ledger)


def _restore_candidates_from_ledger(ctx: SectionCoverageContext) -> int:
    """Re-populate the in-memory candidate store from the persisted OA_CANDIDATE_LEDGER.json.

    Called at the start of any tool that reads from the candidate store so that
    restart/resume works correctly — candidates registered in a prior session are
    not lost when the process restarts.

    Returns the number of candidates that were newly loaded (already-present
    candidates are not re-added).
    """
    ledger_path = ctx.work_dir / "OA_CANDIDATE_LEDGER.json"
    if not ledger_path.exists():
        return 0
    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger = OACandidateLedger.model_validate(raw)
    except Exception as exc:
        logger.warning("_restore_candidates_from_ledger: failed to read ledger: %s", exc)
        return 0

    existing_ids = set(ctx.all_candidate_ids())
    action_changed = False
    cross_state = _read_cross_wave_state(ctx)
    outcomes = cross_state.get("candidate_outcomes") or {}
    identity_index = cross_state.get("material_identity_index") or {}
    for candidate in ledger.candidates:
        candidate_data = candidate.model_dump()
        cached_article_audit = _article_audit_for_candidate(ctx, candidate_data)
        if cached_article_audit:
            updated_candidate = _apply_article_audit(ctx, candidate_data)
            for field in (
                "scope_fit", "role_fit", "decision", "candidate_action",
                "role_provenance", "scope_violations", "boundary_violations",
                "audit_reason", "not_usable_for", "material_identity",
            ):
                if field not in updated_candidate:
                    continue
                current = getattr(candidate, field, None)
                desired_value = updated_candidate[field]
                if current == desired_value:
                    continue
                try:
                    if field == "scope_fit":
                        desired_value = ScopeFit(str(desired_value))
                    elif field == "decision":
                        desired_value = CandidateDecision(str(desired_value))
                    elif field == "candidate_action":
                        desired_value = CandidateAction(str(desired_value))
                    setattr(candidate, field, desired_value)
                    action_changed = True
                except (TypeError, ValueError):
                    pass
            candidate_data = candidate.model_dump()
        contract = canonical_candidate_decision(candidate_data)
        desired = contract.action
        if candidate.candidate_action.value != desired:
            candidate.candidate_action = CandidateAction(desired)
            action_changed = True
        repaired_reason = _candidate_action_audit_reason(candidate_data, contract)
        if candidate.audit_reason != repaired_reason:
            candidate.audit_reason = repaired_reason
            action_changed = True
        identity = _candidate_identity(candidate.model_dump())
        outcome = outcomes.get(candidate.candidate_id) or {}
        if not outcome and identity:
            prior_ids = identity_index.get(identity) or []
            outcome = next(
                (outcomes.get(str(item)) for item in prior_ids if outcomes.get(str(item))),
                {},
            )
        if identity and candidate.material_identity != identity:
            candidate.material_identity = identity
            action_changed = True
        if outcome:
            for field, value in (
                ("attempted_waves", list(outcome.get("attempted_waves") or [])),
                ("materialization_attempts", int(outcome.get("materialization_attempts", 0) or 0)),
                ("last_materialization_status", str(outcome.get("last_materialization_status") or "")),
                ("no_progress", bool(outcome.get("no_progress", False))),
                ("no_progress_components", list(outcome.get("no_progress_components") or [])),
            ):
                if getattr(candidate, field) != value:
                    setattr(candidate, field, value)
                    action_changed = True
    if action_changed:
        _write_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json", ledger)
    to_restore = [c.model_dump() for c in ledger.candidates if c.candidate_id not in existing_ids]
    if to_restore:
        ctx.register_candidates(to_restore)
    return len(to_restore)


# ---------------------------------------------------------------------------
# 6. inspect_candidate_batch
# ---------------------------------------------------------------------------

def _make_inspect_candidate_batch(ctx: SectionCoverageContext):
    def inspect_candidate_batch(candidate_ids: str) -> str:
        """Return full metadata + abstract for a batch of candidates for agent review.

        Args:
            candidate_ids: JSON array of candidate_id strings from a previous search_oa_candidates call.

        Returns JSON list of candidate records for agent scope/role judgement.
        """
        try:
            ids = json.loads(candidate_ids) if candidate_ids.strip().startswith("[") else [candidate_ids]
        except Exception:
            ids = [candidate_ids]

        ids = [i.strip() for i in ids if i.strip()]
        if not ids:
            return json.dumps({"status": "error", "error": "No candidate_ids provided"})
        # The durable ledger retains the full abstract.  The ReAct transcript
        # receives a bounded scientific preview because every later model call
        # replays prior tool results.
        ids = ids[:MAX_INSPECTION_CANDIDATES]

        _restore_candidates_from_ledger(ctx)

        results = []
        unchanged: List[str] = []
        missing = []
        payload_state = _read_agent_payload_state(ctx)
        fingerprints = payload_state.setdefault("candidate_fingerprints", {})
        for cid in ids:
            cand = ctx.get_candidate(cid)
            if cand is None:
                missing.append(cid)
                continue
            abstract = str(cand.get("abstract", "") or "")
            alignment = _candidate_alignment_guard(cand, ctx)
            result = {
                "candidate_id": cid,
                "title": compact_text(cand.get("title"), 180),
                "doi": compact_text(cand.get("doi"), 120),
                "year": cand.get("year"),
                "venue": compact_text(cand.get("venue"), 90),
                "authors": [
                    compact_text(author, 80)
                    for author in (cand.get("authors") or [])[:5]
                ],
                "abstract": _safe_str(
                    abstract,
                    min(700, MAX_ABSTRACT_PREVIEW_CHARS),
                ),
                "abstract_truncated": (
                    len(abstract) > min(700, MAX_ABSTRACT_PREVIEW_CHARS)
                ),
                "is_oa": cand.get("is_oa", False),
                "citation_count": cand.get("citation_count", 0),
                "backends": [
                    compact_text(backend, 40)
                    for backend in (cand.get("backends") or [])[:4]
                ],
                "role": compact_text(cand.get("role"), 40),
                "direct_eligibility": alignment,
            }
            compact_record = {
                **result,
                "abstract": _safe_str(result.get("abstract", ""), 700),
            }
            fingerprint = stable_payload_fingerprint(compact_record)
            if fingerprints.get(cid) == fingerprint:
                unchanged.append(cid)
                continue
            fingerprints[cid] = fingerprint
            topic = ctx.section_data.get("topic_identity", {})
            topic_fp = str(topic.get("fingerprint") or "") if isinstance(topic, dict) else ""
            cached_audit = get_global_audit(
                ctx.global_coverage_ledger_path,
                topic_fp,
                str(cand.get("role") or ""),
                _candidate_identity(cand),
            )
            if cached_audit:
                result["global_audit_cache"] = cached_audit
                increment_global_stat(
                    ctx.global_coverage_ledger_path,
                    "audit_cache_hits",
                )
            article_audit = _article_audit_for_candidate(ctx, cand)
            if article_audit:
                result["article_audit_cache"] = article_audit
            results.append(result)

        wave_index = _current_wave_index(ctx)
        compact_payload = build_compact_batched_audit_payload(
            section=ctx.section_data,
            candidates=results,
            wave_index=wave_index,
            max_candidates=MAX_AUDIT_CANDIDATES_PER_WAVE,
            components=(ctx.phase3_coverage_request or {}).get("missing_components", []),
        )
        payload_state["last_inspection_ids"] = [
            str(item.get("candidate_id") or "")
            for item in results
            if str(item.get("candidate_id") or "")
        ]
        payload_state["last_inspection_wave"] = wave_index
        payload_state["last_inspection_payload_tokens"] = int(
            compact_payload.get("estimated_input_tokens") or 0
        )
        payload_state["last_inspection_fingerprint"] = compact_payload.get(
            "payload_fingerprint", ""
        )

        _write_agent_payload_state(ctx, payload_state)

        return json.dumps({
            "status": "ok",
            "found": len(results),
            "available": len(results) + len(unchanged),
            "missing": missing,
            "unchanged_ids": unchanged,
            "candidates": results,
            "delta_only": True,
            "wave_index": wave_index,
            "estimated_input_tokens": int(
                compact_payload.get("estimated_input_tokens") or 0
            ),
            "payload_fingerprint": compact_payload.get("payload_fingerprint", ""),
            "ledger_summary": _payload_ledger_summary(ctx),
        }, ensure_ascii=False)

    return inspect_candidate_batch


# ---------------------------------------------------------------------------
# 7. submit_candidate_audit
# ---------------------------------------------------------------------------

def _make_submit_candidate_audit(ctx: SectionCoverageContext):
    def submit_candidate_audit(audit_json: str) -> str:
        """Record the agent's scope/role audit decisions for a batch of candidates.

        Updates OA_CANDIDATE_LEDGER.json with agent decisions.

        Args:
            audit_json: JSON array of audit records. Each record must have:
                - candidate_id: str
                - scope_fit: "direct" | "adjacent" | "contextual" | "out_of_scope"
                - role_fit: list of roles this paper serves
                - decision: "approved" | "rejected" | "deferred"
                - audit_reason: str (brief justification)
                - not_usable_for: list of claim types this cannot support (optional)
                - scope_violations / boundary_violations: optional structured
                  records with code, severity (hard|soft), and evidence
        """
        decoded = decode_json_payload(
            audit_json,
            expected="list",
            allow_single_object_for_list=True,
        )
        if decoded.error:
            # Recovery is intentionally transport-only.  No candidate ledger
            # is touched when the decision records are still malformed.
            return json.dumps({"status": "error", "error": decoded.error})
        records = decoded.value
        if not isinstance(records, list):
            return json.dumps({"status": "error", "error": "audit_json must be a JSON array"})

        # Load current ledger
        ledger_path = ctx.work_dir / "OA_CANDIDATE_LEDGER.json"
        if ledger_path.exists():
            try:
                ledger = OACandidateLedger.model_validate(
                    json.loads(ledger_path.read_text(encoding="utf-8"))
                )
            except Exception:
                ledger = OACandidateLedger(section_id=ctx.section_id)
        else:
            ledger = OACandidateLedger(section_id=ctx.section_id)

        cand_by_id = {c.candidate_id: c for c in ledger.candidates}
        errors = []
        updated = 0
        approved_ids = []
        audited_ids: List[str] = []
        action_provenance: Dict[str, Dict[str, Any]] = {}

        record_ids = [
            str(item.get("candidate_id") or "").strip()
            for item in records
            if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()
        ]
        if record_ids and all(
            cid in cand_by_id
            and cand_by_id[cid].decision in {
                CandidateDecision.approved,
                CandidateDecision.rejected,
            }
            for cid in record_ids
        ):
            transition = {
                "status": "audit_reused",
                "materialize_now": [],
                "materialization_attempted": False,
            }
            if any(
                cand_by_id[cid].candidate_action == CandidateAction.materialize_now
                for cid in record_ids
                if cid in cand_by_id
            ):
                transition = _deterministic_post_audit_transition(ctx, ledger)
            return json.dumps({
                "status": "ok",
                "updated": 0,
                "approved": sum(
                    cand_by_id[cid].decision == CandidateDecision.approved
                    for cid in record_ids if cid in cand_by_id
                ),
                "approved_ids": [
                    cid for cid in record_ids
                    if cid in cand_by_id
                    and cand_by_id[cid].decision == CandidateDecision.approved
                ],
                "materialize_now_ids": [
                    cid for cid in record_ids
                    if cid in cand_by_id
                    and cand_by_id[cid].candidate_action == CandidateAction.materialize_now
                ],
                "candidate_actions": {
                    action.value: sum(
                        candidate.candidate_action == action
                        for candidate in ledger.candidates
                    )
                    for action in CandidateAction
                },
                "candidate_action_provenance": {},
                "post_audit_transition": transition,
                "errors": [],
                "scope_guard_applied": True,
                "audit_reused": True,
                "artifact": "OA_CANDIDATE_LEDGER.json",
                "json_recovered": bool(decoded.recovered),
            }, ensure_ascii=False)

        payload_state = _read_agent_payload_state(ctx)
        inspected_ids = set(str(item) for item in payload_state.get("last_inspection_ids") or [])
        enforce_batch_protocol = bool(
            getattr(ctx, "enforce_batched_audit_protocol", False)
        )
        if enforce_batch_protocol and record_ids and not set(record_ids).issubset(inspected_ids):
            return json.dumps({
                "status": "error",
                "error_code": "audit_delta_payload_required",
                "error": (
                    "Audit only the compact delta returned by the latest batch inspection; "
                    "do not start a per-candidate chat loop."
                ),
                "uninspected_candidate_ids": sorted(set(record_ids) - inspected_ids),
            })
        audit_payload_tokens = int(
            payload_state.get("last_inspection_payload_tokens") or 0
        )
        if not audit_payload_tokens:
            compact_payload = build_compact_batched_audit_payload(
                section=ctx.section_data,
                candidates=[cand.model_dump() for cand in ledger.candidates if cand.candidate_id in record_ids],
                wave_index=_current_wave_index(ctx),
                max_candidates=MAX_AUDIT_CANDIDATES_PER_WAVE,
                components=(ctx.phase3_coverage_request or {}).get("missing_components", []),
            )
            audit_payload_tokens = int(compact_payload.get("estimated_input_tokens") or 0)
        admission = _audit_call_preflight(ctx, record_ids, audit_payload_tokens)
        if not admission.admitted:
            return json.dumps({
                "status": "error",
                "error_code": admission.reason,
                "error": (
                    "The bounded audit call was not admitted. Use the existing "
                    "delta, document the remaining gap, or advance to a new wave."
                ),
                "wave_index": admission.wave_index,
                "predicted_input_tokens": admission.predicted_input_tokens,
                "output_reserve_tokens": admission.output_reserve_tokens,
            })

        for rec in records:
            if not isinstance(rec, dict):
                errors.append("Each audit record must be a JSON object")
                continue
            cid = str(rec.get("candidate_id", "") or "").strip()
            if not cid:
                errors.append("Record missing candidate_id")
                continue
            if cid not in cand_by_id:
                errors.append(f"Unknown candidate_id: {cid}")
                continue

            cand = cand_by_id[cid]
            # Apply only complete, enumerated decisions.  The old path coerced
            # arbitrary values to deferred/unreviewed and then persisted them,
            # which made malformed model output look like an accepted audit.
            scope_val = str(rec.get("scope_fit", "") or "").strip().casefold()
            dec_val = str(rec.get("decision", "") or "").strip().casefold()
            if scope_val not in {
                ScopeFit.direct.value,
                ScopeFit.adjacent.value,
                ScopeFit.contextual.value,
                ScopeFit.out_of_scope.value,
            }:
                errors.append(f"{cid}: invalid scope_fit={scope_val!r}")
                continue
            if dec_val not in {
                CandidateDecision.approved.value,
                CandidateDecision.rejected.value,
                CandidateDecision.deferred.value,
            }:
                errors.append(f"{cid}: invalid decision={dec_val!r}")
                continue
            reason = str(rec.get("audit_reason", "") or "").strip()
            if not reason:
                errors.append(f"{cid}: audit_reason is required")
                continue
            role_fit = rec.get("role_fit", [cand.role])
            if not isinstance(role_fit, list) or any(
                str(role).strip().casefold() not in COVERAGE_ROLES
                for role in role_fit
            ):
                errors.append(f"{cid}: role_fit must be a list of known coverage roles")
                continue
            not_usable_for = rec.get("not_usable_for", [])
            if not isinstance(not_usable_for, list):
                errors.append(f"{cid}: not_usable_for must be a list")
                continue
            scope_violations = normalize_scope_violation_records(
                rec.get("scope_violations")
            )
            boundary_violations = normalize_scope_violation_records(
                rec.get("boundary_violations")
            )
            violation_state = scope_violation_outcome({
                "scope_violations": scope_violations,
                "boundary_violations": boundary_violations,
            })

            alignment = _candidate_alignment_guard(
                cand.model_dump(),
                ctx,
            )
            scope_adjustment = ""
            if alignment.get("hard_reject"):
                # A clear conflict with an explicit spectral/modality/
                # application guardrail is not eligible even as materialized
                # adjacent evidence.  Keep the candidate auditable, but make
                # the executable state a rejection/discovery stop.
                scope_val = ScopeFit.out_of_scope.value
                dec_val = CandidateDecision.rejected.value
                scope_adjustment = (
                    "deterministic_scope_boundary_rejection: "
                    + alignment["reason"]
                )
            elif violation_state["hard_violation"]:
                scope_val = ScopeFit.out_of_scope.value
                dec_val = CandidateDecision.rejected.value
                scope_adjustment = (
                    "deterministic_structured_scope_violation_rejection"
                )
            elif scope_val == "direct" and not alignment["direct_eligible"]:
                # Preserve useful literature, but do not let a broad
                # metasurface/optics paper satisfy the direct-source target.
                scope_val = "adjacent"
                scope_adjustment = (
                    "deterministic_scope_downgrade: direct->adjacent; "
                    + alignment["reason"]
                )
            try:
                cand.scope_fit = ScopeFit(scope_val)
            except ValueError:
                cand.scope_fit = ScopeFit.unreviewed
            try:
                cand.decision = CandidateDecision(dec_val)
            except ValueError:
                errors.append(f"{cid}: invalid decision={dec_val!r}")
                continue

            # An approved out-of-scope or unreviewed record is not a valid
            # state.  Clamp it to rejected before persistence; this is the
            # canonical state-machine boundary, not a model preference.
            if cand.scope_fit in {ScopeFit.out_of_scope, ScopeFit.unreviewed}:
                cand.decision = CandidateDecision.rejected
                scope_adjustment = (
                    scope_adjustment + " | " if scope_adjustment else ""
                ) + "deterministic_scope_decision_clamp: approval->rejected"

            cand.role_fit = _role_union(cand.role, role_fit)
            cand.role_provenance = _merge_role_provenance(
                cand.role_provenance,
                rec.get("role_provenance") or {},
                {
                    str(cand.role or "").casefold(): list(cand.query_texts or [])
                },
            )
            cand.scope_violations = scope_violations
            cand.boundary_violations = boundary_violations
            cand.audit_reason = _safe_str(
                " | ".join(
                    value
                    for value in (
                        reason,
                        scope_adjustment,
                    )
                    if value
                ),
                500,
            )
            cand.not_usable_for = _canonical_scope_restrictions(
                cand.scope_fit.value,
                list(not_usable_for),
            )

            # The model may express a preference, but the executable action
            # is decided here from the audited scope, decision and legal route.
            # This is the boundary that prevents a relevant non-OA record from
            # being mistaken for downloaded evidence.  Keep the clamp reason
            # in the audit provenance so a later resume is self-explanatory.
            contract = canonical_candidate_decision(
                cand.model_dump(),
                rec.get("candidate_decision") or rec.get("candidate_action", ""),
            )
            cand.candidate_action = CandidateAction(contract.action)
            cand.audit_reason = _candidate_action_audit_reason(
                cand.model_dump(), contract
            )
            action_provenance[cid] = _candidate_action_provenance(contract)

            if cand.decision == CandidateDecision.approved:
                approved_ids.append(cid)
            topic = ctx.section_data.get("topic_identity", {})
            topic_fp = str(topic.get("fingerprint") or "") if isinstance(topic, dict) else ""
            record_global_audit(
                ctx.global_coverage_ledger_path,
                topic_fingerprint=topic_fp,
                role=str(cand.role or rec.get("role") or ""),
                identity=_candidate_identity(cand.model_dump()),
                decision=cand.decision.value,
                scope_fit=cand.scope_fit.value,
                role_fit=list(cand.role_fit or []),
                audit_reason=cand.audit_reason,
                not_usable_for=list(cand.not_usable_for or []),
            )
            _record_article_audit(ctx, cand.model_dump())
            updated += 1
            audited_ids.append(cid)

        _write_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json", ledger)
        payload_state = _read_agent_payload_state(ctx)
        audited_fingerprints = payload_state.setdefault(
            "audited_candidate_fingerprints", {}
        )
        history = list(payload_state.get("audit_history") or [])
        for cid in audited_ids:
            candidate = cand_by_id.get(cid)
            if candidate is None:
                continue
            fingerprint = _candidate_audit_evidence_fingerprint(
                candidate.model_dump()
            )
            audited_fingerprints[cid] = fingerprint
            history.append({
                "candidate_id": cid,
                "evidence_fingerprint": fingerprint,
                "decision": candidate.decision.value,
                "scope_fit": candidate.scope_fit.value,
                "wave_index": int(admission.wave_index),
                "inspection_fingerprint": str(
                    payload_state.get("last_inspection_fingerprint") or ""
                ),
            })
        payload_state["audit_history"] = history[-100:]
        _write_agent_payload_state(ctx, payload_state)
        _mark_audit_wave(
            ctx,
            wave_index=admission.wave_index,
            candidate_ids=record_ids,
            payload_tokens=admission.predicted_input_tokens,
            output_tokens=max(1, estimate_json_tokens(records)),
        )

        action_counts = {
            action.value: sum(
                1 for candidate in ledger.candidates
                if candidate.candidate_action == action
            )
            for action in CandidateAction
        }
        materialize_now_ids = [
            candidate.candidate_id
            for candidate in ledger.candidates
            if candidate.candidate_action == CandidateAction.materialize_now
            and candidate.decision == CandidateDecision.approved
        ]
        if any(
            candidate.candidate_action == CandidateAction.materialize_now
            for candidate in ledger.candidates
        ):
            transition = _deterministic_post_audit_transition(ctx, ledger)
        else:
            transition = {
                "status": "no_materializable_candidate",
                "materialize_now": [],
                "materialization_attempted": False,
            }

        return json.dumps({
            "status": "ok",
            "updated": updated,
            "approved": len(approved_ids),
            "approved_ids": approved_ids,
            "materialize_now_ids": materialize_now_ids,
            "candidate_actions": action_counts,
            "candidate_action_provenance": action_provenance,
            "audited_ids": audited_ids,
            "post_audit_transition": transition,
            "errors": errors,
            "scope_guard_applied": True,
            "artifact": "OA_CANDIDATE_LEDGER.json",
            "json_recovered": bool(decoded.recovered),
        }, ensure_ascii=False)

    return submit_candidate_audit


def _normalized_target_text(value: Any) -> str:
    return " ".join(topic_tokens(str(value or "")))


def _quarantine_retrieved_scope_candidate(
    ctx: SectionCoverageContext,
    candidate: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    """Persist a paper-level S2 boundary quarantine before any ingest."""

    candidate_id = str(candidate.get("candidate_id") or "")
    violation = {
        "code": "retrieved_paper_scope_boundary",
        "severity": "hard",
        "evidence": str(report.get("reason") or "retrieved snippet conflicts with section boundary"),
        "incompatible_regimes": list(report.get("incompatible_regimes") or []),
        "snippet_evidence": list(report.get("snippet_evidence") or []),
    }
    live = ctx.get_candidate(candidate_id) or dict(candidate)
    live.update({
        "scope_fit": ScopeFit.out_of_scope.value,
        "decision": CandidateDecision.rejected.value,
        "candidate_action": CandidateAction.reject.value,
        "scope_violations": normalize_scope_violation_records(
            [*(live.get("scope_violations") or []), violation]
        ),
        "audit_reason": "deterministic_retrieved_paper_scope_quarantine",
        "not_usable_for": list(dict.fromkeys([
            *(live.get("not_usable_for") or []),
            "all retrieved snippets from this paper",
        ])),
        "last_materialization_status": "scope_quarantined",
        "no_progress": True,
        "no_progress_components": list(dict.fromkeys([
            *(live.get("no_progress_components") or []), "scope_boundary"
        ])),
    })
    ctx.register_candidates([live])
    _append_candidates_to_ledger(ctx.work_dir, ctx.section_id, [live])
    _record_article_audit(ctx, live)
    _record_candidate_event(
        ctx,
        live,
        status="s2_scope_quarantined",
        no_progress_components=["scope_boundary"],
    )


def _candidate_target_keys(
    candidate: OACandidate,
    query_targets: List[Dict[str, Any]],
) -> set[str]:
    """Map a candidate to explicit request targets without domain vocabulary."""

    candidate_queries = [
        set(topic_tokens(str(query)))
        for query in candidate.query_texts
        if topic_tokens(str(query))
    ]
    keys: set[str] = set()
    for target in query_targets:
        query = str(target.get("query") or "").strip()
        query_tokens = set(topic_tokens(query))
        matched = False
        for candidate_tokens in candidate_queries:
            if not query_tokens or not candidate_tokens:
                continue
            overlap = len(query_tokens & candidate_tokens)
            matched = (
                query_tokens == candidate_tokens
                or overlap / max(1, min(len(query_tokens), len(candidate_tokens)))
                >= 0.6
            )
            if matched:
                break
        if not matched:
            continue
        query_key = _normalized_target_text(query)
        if query_key:
            keys.add("query:" + query_key)
        for field in ("missing_components", "components"):
            for component in target.get(field) or []:
                component_key = _normalized_target_text(component)
                if component_key:
                    keys.add("component:" + component_key)
    if not keys:
        keys.update(
            "query:" + normalized
            for normalized in (
                _normalized_target_text(query) for query in candidate.query_texts
            )
            if normalized
        )
    return keys


def _rank_budget_fill_candidates(
    candidates: List[OACandidate],
    query_targets: List[Dict[str, Any]],
) -> List[OACandidate]:
    """Rank safely, then diversify papers within each scope class."""

    scope_order = {
        ScopeFit.direct: 0,
        ScopeFit.adjacent: 1,
        ScopeFit.unreviewed: 2,
    }
    unique: Dict[str, OACandidate] = {}
    for candidate in candidates:
        if candidate.scope_fit == ScopeFit.out_of_scope:
            continue
        identity = _candidate_identity(candidate.model_dump())
        key = identity or "candidate:" + candidate.candidate_id
        existing = unique.get(key)
        if existing is None:
            unique[key] = candidate
            continue
        old_key = (
            scope_order.get(existing.scope_fit, 3),
            -float(existing.relevance_score or 0.0),
            existing.candidate_id,
        )
        new_key = (
            scope_order.get(candidate.scope_fit, 3),
            -float(candidate.relevance_score or 0.0),
            candidate.candidate_id,
        )
        if new_key < old_key:
            unique[key] = candidate

    ranked: List[OACandidate] = []
    for scope_rank in range(4):
        pool = [
            candidate for candidate in unique.values()
            if scope_order.get(candidate.scope_fit, 3) == scope_rank
        ]
        seen_targets: set[str] = set()
        while pool:
            pool.sort(key=lambda candidate: (
                0 if (
                    _candidate_target_keys(candidate, query_targets)
                    - seen_targets
                ) else 1,
                -len(
                    _candidate_target_keys(candidate, query_targets)
                    - seen_targets
                ),
                0 if (
                    candidate.semantic_scholar_id or candidate.corpus_id
                ) else 1,
                -float(candidate.relevance_score or 0.0),
                -int(candidate.citation_count or 0),
                str(candidate.title or "").casefold(),
                candidate.candidate_id,
            ))
            chosen = pool.pop(0)
            ranked.append(chosen)
            seen_targets.update(
                _candidate_target_keys(chosen, query_targets)
            )
    return ranked


def _deterministically_admit_searched_candidates(
    ctx: SectionCoverageContext,
    candidate_ids: List[str],
) -> List[OACandidate]:
    """Conservatively audit newly searched candidates for bounded routing."""

    raw = _read_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json") or {}
    try:
        ledger = OACandidateLedger.model_validate(raw)
    except Exception:
        return []
    wanted = set(candidate_ids)
    admitted: List[OACandidate] = []
    changed = False
    for candidate in ledger.candidates:
        if candidate.candidate_id not in wanted:
            continue
        if candidate.scope_fit == ScopeFit.out_of_scope:
            candidate.decision = CandidateDecision.rejected
            candidate.candidate_action = CandidateAction.reject
            candidate.audit_reason = "deterministic_topic_guard_rejected"
            changed = True
            continue
        alignment = _candidate_alignment_guard(candidate.model_dump(), ctx)
        alignment_status = str(
            (alignment.get("topic_alignment") or {}).get("status") or ""
        )
        if alignment.get("direct_eligible"):
            candidate.scope_fit = ScopeFit.direct
        elif alignment_status == "passed" or alignment.get("section_anchor_hits"):
            candidate.scope_fit = ScopeFit.adjacent
        else:
            candidate.scope_fit = ScopeFit.out_of_scope
        candidate.decision = (
            CandidateDecision.approved
            if candidate.scope_fit in {ScopeFit.direct, ScopeFit.adjacent}
            else CandidateDecision.rejected
        )
        if candidate.decision == CandidateDecision.approved:
            candidate.role_fit = _role_union(candidate.role, candidate.role_fit)
        else:
            candidate.role_fit = []
        candidate_data = candidate.model_dump()
        contract = canonical_candidate_decision(candidate_data)
        candidate.candidate_action = CandidateAction(contract.action)
        candidate.audit_reason = _safe_str(
            " | ".join(
                value
                for value in (
                    (
                        "deterministic_component_search_alignment"
                        if candidate.decision == CandidateDecision.approved
                        else "deterministic_component_search_not_scientifically_aligned"
                    ),
                    _candidate_action_audit_reason(candidate_data, contract),
                )
                if value
            ),
            500,
        )
        changed = True
        if candidate.candidate_action == CandidateAction.materialize_now:
            admitted.append(candidate)
    if changed:
        _write_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json", ledger)
        ctx.register_candidates([
            candidate.model_dump() for candidate in ledger.candidates
            if candidate.candidate_id in wanted
        ])
    return admitted


def _search_component_candidates_for_budget_fill(
    ctx: SectionCoverageContext,
    *,
    successful_target_keys: set[str],
    remaining_slots: int,
) -> Dict[str, Any]:
    """Open one focused S2-first search round for targets without a success."""

    query_targets = _coverage_query_targets(ctx)
    remaining_queries: List[str] = []
    for target in query_targets:
        query = str(target.get("query") or "").strip()
        query_key = "query:" + _normalized_target_text(query)
        if query and query_key not in successful_target_keys:
            remaining_queries.append(query)
    if not remaining_queries:
        remaining_queries = [
            str(item.get("query") or "").strip()
            for item in query_targets
            if str(item.get("query") or "").strip()
        ]
    if not remaining_queries:
        remaining_queries = list(ctx.targeted_queries)
    remaining_queries = list(dict.fromkeys(remaining_queries))[
        :max(1, min(ctx.min_mode_max_queries, remaining_slots * 2))
    ]
    if not remaining_queries:
        return {
            "status": "error",
            "error_code": "no_uncovered_component_queries",
            "candidate_ids": [],
            "queries_used": [],
            "uncovered_query_targets": query_targets,
        }
    role = next(
        (
            item for item in ctx.targeted_missing_roles
            if item in COVERAGE_ROLES
        ),
        next(
            (
                str(item.get("role"))
                for item in query_targets
                if str(item.get("role") or "") in COVERAGE_ROLES
            ),
            str(ctx.min_mode_allowed_role or "foundation"),
        ),
    )
    try:
        result = json.loads(_make_search_oa_candidates(ctx)(
            role,
            json.dumps(remaining_queries),
            max_per_backend=max(
                1,
                min(ctx.min_mode_max_per_backend, remaining_slots * 2),
            ),
        ))
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "component_search_exception",
            "error": str(exc)[:240],
            "candidate_ids": [],
            "queries_used": remaining_queries,
        }
    result.setdefault("queries_used", remaining_queries)
    return result


def _deterministic_post_audit_transition(
    ctx: SectionCoverageContext,
    ledger: OACandidateLedger,
) -> Dict[str, Any]:
    """Fill the bounded successful-paper budget from audited candidates.

    Candidate judgement remains upstream.  This deterministic transition
    enforces scope, identity and successful-paper budgets, tries each novel
    candidate's structured S2 route before legal OA full text, and only opens
    one component-targeted discovery round when the audited queue cannot fill
    the remaining slots.
    """

    if ctx._post_audit_transition_done:
        return _read_artifact(ctx.work_dir, "POST_AUDIT_TRANSITION.json") or {
            "status": "already_done"
        }
    ctx._post_audit_transition_done = True
    ctx._post_audit_materialization_attempted = True

    persisted = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
    persisted_successes = len({
        _candidate_identity(row) or str(row.get("paper_id") or row.get("candidate_id") or "")
        for row in persisted.get("papers", [])
        if isinstance(row, dict)
        and bool(row.get("new_paper"))
        and int(row.get("new_chunks") or 0) > 0
    } - {""})
    with ctx._store_lock:
        ctx._papers_materialized_total = max(
            int(ctx._papers_materialized_total), persisted_successes
        )
        successful_before = int(ctx._papers_materialized_total)
    requested = ctx.targeted_expected_new_papers or ctx.min_mode_max_total_papers
    slots_requested = max(0, min(
        int(requested),
        int(ctx.min_mode_max_total_papers) - successful_before,
    ))

    transition: Dict[str, Any] = {
        "schema_version": "phase2.1.post_audit_transition.v2",
        "section_id": ctx.section_id,
        "status": "started",
        "candidate_ids": [],
        "candidates_considered": [],
        "snippet_attempts": [],
        "oa_fallback_attempts": [],
        "search_escalation": {
            "invoked": False,
            "queries": [],
            "candidate_ids": [],
            "s2_calls": 0,
            "openalex_calls": 0,
            "status": "not_needed",
        },
        "successful_paper_ids": [],
        "successful_chunk_ids": [],
        "skipped_candidates": [],
        "slots_requested": slots_requested,
        "successful_slots_before": successful_before,
        "successful_slots_filled": 0,
        "successful_slots_remaining": slots_requested,
        "candidate_attempts": 0,
        "route_attempts": 0,
        "failed_attempts": 0,
        "reused_or_skipped": 0,
        "coverage_target_met": False,
        "budget_remaining": max(
            0, int(ctx.min_mode_max_total_papers) - successful_before
        ),
        "stop_reason": "not_started",
        "citation_trace_allowed_after": False,
    }
    if slots_requested <= 0:
        transition.update({
            "status": "budget_reached",
            "stop_reason": "successful_paper_budget_reached",
            "citation_trace_allowed_after": True,
        })
        _write_artifact(ctx.work_dir, "POST_AUDIT_TRANSITION.json", transition)
        return transition

    hard_rejected = [
        candidate for candidate in ledger.candidates
        if candidate.candidate_action == CandidateAction.materialize_now
        and candidate.scope_fit == ScopeFit.out_of_scope
    ]
    for candidate in hard_rejected:
        candidate_dict = candidate.model_dump()
        transition["skipped_candidates"].append({
            "candidate_id": candidate.candidate_id,
            "status": "rejected_out_of_scope",
            "material_identity": _candidate_identity(candidate_dict),
        })

    candidates = _rank_budget_fill_candidates(
        [
            candidate for candidate in ledger.candidates
            if candidate.candidate_action == CandidateAction.materialize_now
            and candidate.decision == CandidateDecision.approved
            and candidate.scope_fit != ScopeFit.out_of_scope
        ],
        _coverage_query_targets(ctx),
    )[:MAX_INSPECTION_CANDIDATES]
    transition["candidate_ids"] = [candidate.candidate_id for candidate in candidates]

    try:
        from optomind_research.s2_text_chunk_retriever import S2TextChunkRetriever
        from optomind_research.s2_kb_bridge import S2KnowledgeBaseBridge
    except Exception as exc:
        logger.warning("S2 structured snippet route unavailable: %s", exc)
        S2TextChunkRetriever = None  # type: ignore[assignment]
        S2KnowledgeBaseBridge = None  # type: ignore[assignment]

    cross_state = _read_cross_wave_state(ctx)
    prior_identities = set(cross_state.get("attempted_material_identities") or [])
    identity_index = cross_state.get("material_identity_index") or {}
    seen_identities: set[str] = set()
    queued_ids: set[str] = set()
    queue_candidates: List[OACandidate] = []
    successful_target_keys: set[str] = set()

    def enqueue_novel(rows: List[OACandidate]) -> None:
        for candidate in rows:
            if candidate.candidate_id in queued_ids:
                continue
            candidate_dict = candidate.model_dump()
            identity = _candidate_identity(candidate_dict)
            outcome = (cross_state.get("candidate_outcomes") or {}).get(
                candidate.candidate_id
            ) or {}
            if not outcome and identity:
                outcome = next((
                    (cross_state.get("candidate_outcomes") or {}).get(str(cid)) or {}
                    for cid in identity_index.get(identity, [])
                    if (cross_state.get("candidate_outcomes") or {}).get(str(cid))
                ), {})
            existing_paper_id, existing_chunk_ids = _staging_material_for_candidate(
                ctx, candidate_dict
            )
            if identity and identity in seen_identities:
                status = "skipped_duplicate_identity"
            elif (
                (identity and identity in prior_identities)
                or outcome.get("no_progress")
                or existing_paper_id
            ):
                status = (
                    "no_progress_candidate_skipped"
                    if outcome.get("no_progress")
                    else "reused_candidate"
                )
            else:
                if identity:
                    seen_identities.add(identity)
                queued_ids.add(candidate.candidate_id)
                queue_candidates.append(candidate)
                continue
            transition["skipped_candidates"].append({
                "candidate_id": candidate.candidate_id,
                "status": status,
                "material_identity": identity,
                "paper_id": existing_paper_id,
                "chunk_count": len(existing_chunk_ids),
                "no_progress_components": list(
                    outcome.get("no_progress_components") or []
                ),
            })
            _record_candidate_event(
                ctx,
                candidate_dict,
                status=status,
                reused_chunk_ids=existing_chunk_ids,
                paper_id=existing_paper_id,
                no_progress_components=list(
                    outcome.get("no_progress_components") or []
                ),
            )

    enqueue_novel(candidates)
    search_used = False
    queue_index = 0
    stop_reason = "approved_candidates_exhausted"

    while transition["successful_slots_filled"] < slots_requested:
        if queue_index >= len(queue_candidates):
            if search_used:
                stop_reason = "bounded_novel_candidates_exhausted"
                break
            search_used = True
            before_telemetry = _phase2_telemetry(ctx)
            search_result = _search_component_candidates_for_budget_fill(
                ctx,
                successful_target_keys=successful_target_keys,
                remaining_slots=(
                    slots_requested - transition["successful_slots_filled"]
                ),
            )
            after_telemetry = _phase2_telemetry(ctx)
            new_ids = [str(item) for item in search_result.get("candidate_ids", []) if str(item)]
            transition["search_escalation"] = {
                "invoked": True,
                "queries": list(search_result.get("queries_used") or []),
                "candidate_ids": new_ids,
                "s2_calls": max(
                    0,
                    int(after_telemetry.get("s2_search_calls", 0) or 0)
                    - int(before_telemetry.get("s2_search_calls", 0) or 0),
                ),
                "openalex_calls": max(
                    0,
                    int(after_telemetry.get("openalex_calls", 0) or 0)
                    - int(before_telemetry.get("openalex_calls", 0) or 0),
                ),
                "backend_stats": dict(search_result.get("backend_stats") or {}),
                "status": str(search_result.get("status") or "error"),
                "error_code": str(search_result.get("error_code") or ""),
            }
            if not new_ids:
                stop_reason = "component_search_exhausted"
                break
            searched = _deterministically_admit_searched_candidates(ctx, new_ids)
            ranked = _rank_budget_fill_candidates(
                searched, _coverage_query_targets(ctx)
            )
            transition["candidate_ids"].extend(
                candidate.candidate_id for candidate in ranked
                if candidate.candidate_id not in transition["candidate_ids"]
            )
            enqueue_novel(ranked)
            if queue_index >= len(queue_candidates):
                stop_reason = "component_search_produced_no_materializable_candidate"
                break
            continue

        candidate = queue_candidates[queue_index]
        queue_index += 1
        candidate_dict = candidate.model_dump()
        transition["candidates_considered"].append(candidate.candidate_id)
        candidate_attempted = False
        candidate_succeeded = False
        candidate_reused = False
        candidate_event_recorded = False
        coverage_target_met_after_success = False

        snippet_result: Dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "status": "not_available",
        }
        scope_quarantined = False
        s2_id = (
            candidate_dict.get("semantic_scholar_id")
            or candidate_dict.get("semantic_scholar_paper_id")
            or (
                f"CorpusId:{candidate_dict['corpus_id']}"
                if candidate_dict.get("corpus_id") else ""
            )
        )
        article_material = _article_material_for_candidate(ctx, candidate_dict)
        if article_material:
            reused_chunk_ids = [
                str(item) for item in article_material.get("chunk_ids") or [] if str(item)
            ]
            candidate_reused = bool(reused_chunk_ids)
            snippet_result.update({
                "status": "article_portfolio_reused",
                "paper_id": str(article_material.get("paper_id") or ""),
                "reused_chunk_ids": reused_chunk_ids,
            })
            _record_candidate_event(
                ctx,
                candidate_dict,
                status="reused_candidate",
                reused_chunk_ids=reused_chunk_ids,
                paper_id=str(article_material.get("paper_id") or ""),
            )
            candidate_event_recorded = True
            transition["reused_or_skipped"] += 1
        elif S2TextChunkRetriever is not None and s2_id:
            candidate_attempted = True
            transition["route_attempts"] += 1
            _bump_phase2_telemetry(
                ctx, materialization_attempts=1,
                materialization_route_attempts=1,
            )
            role_name = str(candidate_dict.get("role") or "")
            role_queries = [
                str(item.get("query") or "")
                for item in _coverage_query_targets(ctx)
                if isinstance(item, dict)
                and (
                    not item.get("role")
                    or str(item.get("role") or "") == role_name
                )
            ]
            queries = list(dict.fromkeys([
                *role_queries,
                *(candidate_dict.get("query_texts") or []),
                *ctx.targeted_queries,
            ]))[:2]
            try:
                result = S2TextChunkRetriever(min_chars=500).retrieve(
                    queries,
                    paper_ids=[str(s2_id)],
                    limit_per_query=6,
                    requested_roles=[str(candidate_dict.get("role") or "")],
                    scope_context={
                        "section_context": str(
                            ctx.section_data.get("chapter_argument") or ""
                        ),
                    },
                )
                _bump_phase2_telemetry(
                    ctx, s2_snippet_calls=len(result.query_runs)
                )
                scope_report = assess_retrieved_paper_scope_boundary(
                    ctx.section_data,
                    candidate_dict,
                    [
                        {
                            "chunk_id": getattr(chunk, "chunk_id", ""),
                            "text": getattr(chunk, "text", ""),
                            "section_path": getattr(chunk, "section", ""),
                            "content_kind": getattr(chunk, "content_kind", ""),
                            "text_provenance": getattr(chunk, "text_provenance", ""),
                            "source_locator": getattr(chunk, "source_locator", {}) or {},
                            "raw_metadata": getattr(chunk, "raw_metadata", {}) or {},
                            "route_provenance": getattr(chunk, "route_provenance", {}) or {},
                        }
                        for chunk in result.accepted_chunks
                    ],
                )
                scope_quarantined = bool(scope_report.get("quarantine_all_snippets"))
                if scope_quarantined:
                    _bump_phase2_telemetry(ctx, s2_scope_quarantines=1)
                    _quarantine_retrieved_scope_candidate(
                        ctx, candidate_dict, scope_report
                    )
                    result.rejected_items.extend([
                        {
                            "paper_title": candidate_dict.get("title", ""),
                            "chunk_id": item.get("chunk_id", ""),
                            "reason": "paper_level_scope_quarantine",
                            "scope_report": dict(scope_report),
                        }
                        for item in (scope_report.get("decisive_conflicts") or [{}])
                    ])
                    result.accepted_chunks = []
                    route_decisions = []
                else:
                    visual_required = _visual_first_evidence_required(ctx.section_data)
                    route_decisions = [
                        structured_snippet_route_decision(
                            text=getattr(chunk, "text", ""),
                            scope_fit=getattr(chunk, "scope_fit", ""),
                            context_complete=getattr(chunk, "context_complete", False),
                            use_permission=getattr(chunk, "use_permission", ""),
                            visual_required=visual_required,
                            context_limitations=getattr(chunk, "context_limitations", []) or [],
                        )
                        for chunk in result.accepted_chunks
                    ]
                accepted = [
                    chunk for chunk, decision in zip(result.accepted_chunks, route_decisions)
                    if decision.get("accepted_as_peer_text_evidence")
                ]
                if accepted:
                    _bump_phase2_telemetry(
                        ctx,
                        accepted_s2_snippets=len(accepted),
                    )
                escalation_reasons = [
                    str(decision.get("reason") or "")
                    for decision in route_decisions
                    if decision.get("fulltext_escalation_required")
                ]
                if escalation_reasons:
                    telemetry = _phase2_telemetry(ctx)
                    telemetry["fulltext_escalation_reasons"] = list(dict.fromkeys([
                        *(telemetry.get("fulltext_escalation_reasons") or []),
                        *escalation_reasons,
                    ]))[-40:]
                    telemetry["fulltext_escalations"] = int(
                        telemetry.get("fulltext_escalations", 0) or 0
                    ) + 1
                    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", telemetry)
                snippet_result.update({
                    "status": "scope_quarantined" if scope_quarantined else "accepted" if accepted else "insufficient",
                    "query_runs": result.query_runs,
                    "accepted_count": len(accepted),
                    "rejected_count": len(result.rejected_items),
                    "scope_boundary_quarantine": dict(scope_report) if scope_quarantined else {},
                    "fulltext_escalation_required": bool(escalation_reasons),
                    "fulltext_escalation_reasons": list(dict.fromkeys(escalation_reasons)),
                    "is_peer_text_evidence": bool(accepted),
                })
                if accepted and S2KnowledgeBaseBridge is not None:
                    from optomind_research.s2_schemas import S2PaperRecord

                    # S2 snippet responses expose provider parents such as
                    # ``CorpusId:<n>`` while section ledgers use the stable
                    # S2 paper hash.  Upsert the canonical paper in the same
                    # bridge transaction as its chunks; the bridge retains
                    # provider/S2 aliases in identity provenance.
                    canonical_paper_id = str(
                        candidate_dict.get("semantic_scholar_id")
                        or candidate_dict.get("semantic_scholar_paper_id")
                        or candidate_dict.get("corpus_id")
                        or getattr(accepted[0], "paper_id", "")
                    ).strip()
                    corpus_id = candidate_dict.get("corpus_id")
                    if corpus_id in (None, ""):
                        corpus_id = getattr(accepted[0], "corpus_id", None)
                    try:
                        corpus_id = int(corpus_id) if corpus_id not in (None, "") else None
                    except (TypeError, ValueError):
                        corpus_id = None
                    canonical_title = str(
                        candidate_dict.get("title")
                        or getattr(accepted[0], "title", "")
                    )
                    provider_aliases = list(dict.fromkeys(
                        str(value).strip()
                        for value in [
                            *(
                                str(getattr(chunk, "paper_id", "") or "")
                                for chunk in accepted
                            ),
                            *(
                                [f"CorpusId:{corpus_id}"]
                                if corpus_id is not None else []
                            ),
                            str(candidate_dict.get("semantic_scholar_id") or ""),
                        ]
                        if str(value).strip()
                    ))
                    canonical_paper = S2PaperRecord(
                        paper_id=canonical_paper_id,
                        corpus_id=corpus_id,
                        doi=str(candidate_dict.get("doi") or ""),
                        title=canonical_title,
                        authors=[
                            str(value)
                            for value in (candidate_dict.get("authors") or [])
                            if str(value).strip()
                        ],
                        year=candidate_dict.get("year"),
                        venue=str(candidate_dict.get("venue") or ""),
                        abstract=str(candidate_dict.get("abstract") or ""),
                        tldr=str(candidate_dict.get("tldr") or ""),
                        citation_count=int(candidate_dict.get("citation_count") or 0),
                        is_oa=bool(candidate_dict.get("is_oa")),
                        external_ids={
                            "semantic_scholar_id": canonical_paper_id,
                            "corpus_id": corpus_id,
                        },
                        discovery_route="semantic_scholar_graph",
                        materialization_route="s2_structured_body_snippet",
                        content_depth="structured_snippet",
                        use_permission="factual_support",
                        scope_fit=normalize_scope_fit(
                            candidate_dict.get("scope_fit") or "direct"
                        ),
                        literature_roles=_role_union(
                            candidate_dict.get("role"),
                            candidate_dict.get("role_fit"),
                        ) or ["foundation"],
                        route_events=[{
                            "event": "s2_structured_body_snippet_materialized",
                            "provider_aliases": provider_aliases,
                            "canonical_paper_id": canonical_paper_id,
                            "role_provenance": candidate_dict.get(
                                "role_provenance", {}
                            ),
                        }],
                        raw_metadata={
                            "candidate_id": candidate_dict.get("candidate_id"),
                            "semantic_scholar_id": candidate_dict.get("semantic_scholar_id"),
                            "corpus_id": corpus_id,
                        },
                    )
                    bridge_result = S2KnowledgeBaseBridge(
                        ctx.temp_kb_sqlite
                    ).ingest(papers=[canonical_paper], chunks=accepted)
                    paper_id = canonical_paper_id
                    chunk_ids = [chunk.chunk_id for chunk in accepted]
                    snippet_result["canonical_paper_id"] = paper_id
                    snippet_result["identity_rebindings"] = list(
                        bridge_result.get("identity_rebindings") or []
                    )
                    inserted_chunk_ids = list(dict.fromkeys(
                        str(item)
                        for item in bridge_result.get("inserted_chunk_ids") or []
                        if str(item)
                    ))
                    inserted_count = int(
                        bridge_result.get("chunks_inserted", 0) or 0
                    )
                    if not inserted_chunk_ids and inserted_count:
                        inserted_chunk_ids = chunk_ids[:inserted_count]
                    if inserted_chunk_ids:
                        _append_structured_snippet_manifest(
                            ctx,
                            candidate_dict,
                            paper_id=paper_id,
                            chunk_ids=chunk_ids,
                            new_chunk_ids=inserted_chunk_ids,
                            new_chunks=len(inserted_chunk_ids),
                            paper_row_inserted=bool(
                                bridge_result.get("papers_inserted", 0)
                            ),
                        )
                        _record_candidate_event(
                            ctx,
                            candidate_dict,
                            status="structured_snippet",
                            new_chunk_ids=inserted_chunk_ids,
                            paper_id=paper_id,
                            paper_row_inserted=bool(
                                bridge_result.get("papers_inserted", 0)
                            ),
                        )
                        candidate_event_recorded = True
                        candidate_succeeded = True
                        transition["successful_paper_ids"].append(paper_id)
                        transition["successful_chunk_ids"].extend(
                            inserted_chunk_ids
                        )
                    else:
                        candidate_reused = True
                        snippet_result["status"] = "reused_no_new_chunks"
                        _record_candidate_event(
                            ctx,
                            candidate_dict,
                            status="reused_candidate",
                            reused_chunk_ids=chunk_ids,
                            paper_id=paper_id,
                        )
                        candidate_event_recorded = True
            except Exception as exc:
                snippet_result.update({
                    "status": "error", "error": str(exc)[:240]
                })
        else:
            _record_skipped_backend(
                ctx, "semantic_scholar_snippet", "candidate has no S2 identity"
            )
        transition["snippet_attempts"].append(snippet_result)

        has_oa_route = bool(
            candidate.is_oa
            and any(
                str(candidate_dict.get(key) or "").startswith("http")
                for key in (
                    "pdf_url", "oa_url", "open_access_url", "url_for_pdf",
                    "best_oa_url", "html_url", "repository_url",
                )
            )
        )
        if (
            not candidate_succeeded
            and not candidate_reused
            and not scope_quarantined
            and has_oa_route
        ):
            candidate_attempted = True
            transition["route_attempts"] += 1
            _bump_phase2_telemetry(ctx, oa_resolution_probes=1)
            try:
                oa_result = json.loads(
                    _make_acquire_and_materialize_oa_papers(ctx)(
                        str(candidate.role or "foundation"),
                        json.dumps([candidate.candidate_id]),
                        max_papers=1,
                    )
                )
            except Exception as exc:
                oa_result = {"status": "error", "error": str(exc)[:240]}
            rows = list(oa_result.get("papers_this_call") or [])
            coverage_target_met_after_success = bool(
                oa_result.get("coverage_target_met")
            )
            if int(oa_result.get("attempted_this_call", 0) or 0) > 0 or rows:
                candidate_event_recorded = True
            transition["oa_fallback_attempts"].append({
                "candidate_id": candidate.candidate_id,
                "status": oa_result.get("status", "unknown"),
                "papers": rows,
                "attempted": int(oa_result.get("attempted_this_call", 0) or 0),
                "successful": int(oa_result.get("successful_this_call", 0) or 0),
            })
            successful_rows = [
                row for row in rows
                if row.get("acquisition_status") in {
                    "fulltext", "structured_snippet"
                }
                and bool(row.get("new_paper"))
                and int(row.get("new_chunks") or 0) > 0
            ]
            if successful_rows:
                candidate_succeeded = True
                manifest = _read_artifact(
                    ctx.work_dir, "MATERIALIZATION_MANIFEST.json"
                ) or {}
                for row in successful_rows:
                    paper_id = str(row.get("paper_id") or "")
                    if paper_id:
                        transition["successful_paper_ids"].append(paper_id)
                    matching = next((
                        item for item in manifest.get("papers", [])
                        if item.get("candidate_id") == row.get("candidate_id")
                    ), None)
                    if matching:
                        transition["successful_chunk_ids"].extend(
                            str(chunk_id)
                            for chunk_id in (
                                matching.get("new_chunk_ids")
                                or matching.get("chunk_ids", [])
                            )
                            if chunk_id
                        )
            elif any(
                int(row.get("new_chunks") or 0) <= 0 for row in rows
            ):
                candidate_reused = True
        else:
            transition["oa_fallback_attempts"].append({
                "candidate_id": candidate.candidate_id,
                "status": (
                    "skipped_after_snippet_success"
                    if candidate_succeeded
                    else "skipped_after_scope_quarantine"
                    if scope_quarantined
                    else "skipped_reused_identity"
                    if candidate_reused
                    else "not_available"
                ),
            })

        if candidate_attempted:
            transition["candidate_attempts"] += 1
        if candidate_succeeded:
            ctx._post_audit_materialization_succeeded = True
            transition["successful_slots_filled"] += 1
            successful_target_keys.update(
                _candidate_target_keys(candidate, _coverage_query_targets(ctx))
            )
            try:
                refreshed = json.loads(_make_refresh_section_coverage(ctx)())
            except Exception:
                refreshed = {}
            coverage_target_met_after_success = bool(
                coverage_target_met_after_success
                or (
                    refreshed.get("status") == "ok"
                    and not refreshed.get("blocking_gaps")
                )
            )
            _make_validate_section_coverage_package(ctx)()
            if coverage_target_met_after_success:
                transition["coverage_target_met"] = True
                stop_reason = "section_coverage_target_met"
                break
        else:
            if candidate_attempted and not candidate_event_recorded:
                _record_candidate_event(
                    ctx,
                    candidate_dict,
                    status="materialization_failed_or_insufficient",
                )
            if candidate_attempted:
                transition["failed_attempts"] += 1
            if candidate_reused:
                transition["reused_or_skipped"] += 1

    transition["successful_paper_ids"] = list(dict.fromkeys(
        transition["successful_paper_ids"]
    ))
    transition["successful_chunk_ids"] = list(dict.fromkeys(
        transition["successful_chunk_ids"]
    ))
    transition["successful_slots_remaining"] = max(
        0, slots_requested - transition["successful_slots_filled"]
    )
    with ctx._store_lock:
        current_successes = int(ctx._papers_materialized_total)
    transition["budget_remaining"] = max(
        0, int(ctx.min_mode_max_total_papers) - current_successes
    )
    transition["reused_or_skipped"] += len(transition["skipped_candidates"])
    if (
        not transition["coverage_target_met"]
        and transition["successful_slots_filled"] >= slots_requested
    ):
        stop_reason = "successful_paper_slots_filled"
    transition["stop_reason"] = stop_reason
    transition["status"] = (
        "materialized"
        if transition["successful_slots_filled"] > 0
        else "materialization_failed_or_insufficient"
    )
    transition["citation_trace_allowed_after"] = True

    telemetry = _phase2_telemetry(ctx)
    telemetry.setdefault("budget_fill_runs", []).append({
        key: transition[key]
        for key in (
            "slots_requested", "successful_slots_before",
            "successful_slots_filled", "successful_slots_remaining",
            "candidate_attempts", "route_attempts", "failed_attempts",
            "reused_or_skipped", "coverage_target_met",
            "budget_remaining", "stop_reason",
        )
    })
    telemetry["budget_fill_runs"][-1]["candidates_considered"] = list(
        transition["candidates_considered"]
    )
    telemetry["budget_fill_runs"][-1]["search_escalation"] = dict(
        transition["search_escalation"]
    )
    _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", telemetry)
    _write_artifact(ctx.work_dir, "POST_AUDIT_TRANSITION.json", transition)
    return transition


def _append_structured_snippet_manifest(
    ctx: SectionCoverageContext,
    candidate: Dict[str, Any],
    *,
    paper_id: str,
    chunk_ids: List[str],
    new_chunks: int,
    new_chunk_ids: Optional[List[str]] = None,
    paper_row_inserted: bool = False,
) -> None:
    """Append an auditable structured-snippet acquisition row idempotently."""

    path = ctx.work_dir / "MATERIALIZATION_MANIFEST.json"
    try:
        manifest = MaterializationManifest.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        ) if path.exists() else MaterializationManifest(section_id=ctx.section_id)
    except Exception:
        manifest = MaterializationManifest(section_id=ctx.section_id)
    if any(row.candidate_id == candidate.get("candidate_id") and row.chunk_ids for row in manifest.papers):
        return
    unique_chunk_ids = list(dict.fromkeys(str(item) for item in chunk_ids if str(item)))
    inserted_ids = list(dict.fromkeys(
        str(item) for item in (new_chunk_ids or []) if str(item)
    ))
    if not inserted_ids and new_chunks:
        # Older bridge implementations only returned a count.  The accepted
        # IDs are still the only bounded candidate chunks, so retain a
        # deterministic prefix rather than inventing IDs.
        inserted_ids = unique_chunk_ids[:int(new_chunks)]
    actual_new_chunks = len(inserted_ids)
    manifest.papers.append(MaterializedPaper(
        candidate_id=str(candidate.get("candidate_id") or ""),
        paper_id=paper_id,
        doi=str(candidate.get("doi") or ""),
        title=str(candidate.get("title") or ""),
        year=candidate.get("year"),
        venue=str(candidate.get("venue") or ""),
        acquisition_status=AcquisitionStatus.structured_snippet,
        chunk_ids=unique_chunk_ids,
        new_chunk_ids=inserted_ids,
        new_paper=bool(actual_new_chunks),
        paper_row_inserted=bool(paper_row_inserted),
        new_chunks=actual_new_chunks,
        section_id=ctx.section_id,
        role=str(candidate.get("role") or "foundation"),
        role_fit=_role_union(candidate.get("role"), candidate.get("role_fit")),
        role_provenance=_merge_role_provenance(
            candidate.get("role_provenance") or {},
            {
                str(candidate.get("role") or "").casefold(): list(
                    candidate.get("query_texts") or []
                )
            },
        ),
        scope_fit=ScopeFit(
            normalize_scope_fit(candidate.get("scope_fit"))
        ),
        materialization_route="s2_structured_body_snippet",
        chunk_count=len(unique_chunk_ids),
    ))
    manifest.total_new_papers = sum(1 for row in manifest.papers if row.new_paper)
    manifest.total_new_chunks = sum(int(row.new_chunks or 0) for row in manifest.papers)
    _write_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json", manifest)
    topic = ctx.section_data.get("topic_identity", {})
    record_global_material(
        ctx.global_coverage_ledger_path,
        topic_fingerprint=(
            str(topic.get("fingerprint") or "")
            if isinstance(topic, dict) else ""
        ),
        role=str(candidate.get("role") or "foundation"),
        identity=_candidate_identity(candidate),
        paper_id=paper_id,
        chunk_ids=unique_chunk_ids,
    )
    with ctx._store_lock:
        ctx._papers_materialized_total += 1


# ---------------------------------------------------------------------------
# 8. trace_seed_references
# ---------------------------------------------------------------------------

def _make_trace_seed_references(ctx: SectionCoverageContext):
    def trace_seed_references(doi_or_candidate_id: str, max_refs: int = 10) -> str:
        """Fetch references for a seed paper via Semantic Scholar.

        Args:
            doi_or_candidate_id: A DOI string (bare or DOI:xxx) or candidate_id.
            max_refs: Maximum references to return (default 10, max 20).

        Returns JSON list of reference records for agent review.
        """
        max_refs = min(max(1, max_refs), 20)

        # Restore candidates from ledger first (restart recovery)
        _restore_candidates_from_ledger(ctx)

        # Resolve input to a bare DOI and an S2-compatible paper_id
        raw = doi_or_candidate_id.strip()
        doi = ""
        s2_paper_id = ""  # "DOI:10.xxx" form accepted by S2 API

        if raw.startswith("cand_"):
            cand = ctx.get_candidate(raw)
            if cand is None:
                return json.dumps({"status": "error", "error": f"Unknown candidate_id: {raw}"})
            doi = cand.get("doi", "").strip()
            s2_paper_id = cand.get("semantic_scholar_id", "").strip()
            if not doi and not s2_paper_id:
                return json.dumps({
                    "status": "error",
                    "error": f"Candidate has no DOI or S2 ID; cannot trace references. "
                             f"Title: {cand.get('title', '')[:80]}",
                })
        else:
            # Accept bare DOI "10.xxxx" or prefixed "DOI:10.xxxx"
            if raw.upper().startswith("DOI:"):
                doi = raw[4:].strip()
            else:
                doi = raw

        # Build the S2-API paper_id: prefer explicit S2 ID, else use DOI:xxx prefix
        if not s2_paper_id:
            s2_paper_id = f"DOI:{doi}" if doi else ""
        if not s2_paper_id:
            return json.dumps({"status": "error", "error": "Could not resolve a paper ID for S2"})

        if ctx.short_path_mode:
            decisions = _candidate_decisions(ctx)
            materializable = [
                item for item in decisions.values()
                if item.get("candidate_action") == CandidateAction.materialize_now.value
            ]
            if materializable and not ctx._post_audit_transition_done:
                return json.dumps({
                    "status": "error",
                    "error_code": "materialization_required_before_citation_trace",
                    "error": "Complete the deterministic structured-snippet/OA acquisition attempt before citation tracing.",
                })
            trace_count = int(_phase2_telemetry(ctx).get("s2_reference_calls", 0) or 0) + int(_phase2_telemetry(ctx).get("s2_citation_calls", 0) or 0)
            if trace_count >= 1:
                return json.dumps({
                    "status": "error",
                    "error_code": "citation_trace_wave_limit_reached",
                    "error": "At most one citation-tracing seed is allowed in a bounded wave.",
                })
            max_refs = min(int(max_refs or 0), 5)
        try:
            from tools.academic_backends.semantic_scholar_backend import SemanticScholarBackend
            s2 = SemanticScholarBackend()
            # S2 get_references accepts "DOI:10.xxx" as paper_id
            refs = s2.get_references(s2_paper_id, max_results=max_refs)
            _bump_phase2_telemetry(ctx, s2_reference_calls=1)

            raw_refs: List[Dict] = []
            for r in refs[:max_refs]:
                # Normalized S2 result fields (see normalize_s2_result)
                raw_meta = r.get("raw_metadata") or {}
                oa_pdf = raw_meta.get("open_access_pdf") or {}
                pdf_url = r.get("pdf_url") or oa_pdf.get("url") or ""
                is_oa = bool(pdf_url)
                cit = (raw_meta.get("citation_count")
                       or raw_meta.get("citationCount")
                       or 0)
                raw_refs.append({
                    "title": r.get("title", ""),
                    "doi": r.get("doi", ""),
                    "year": r.get("year"),
                    "venue": r.get("journal_or_venue") or r.get("venue", ""),
                    "authors": r.get("authors", [])[:3],
                    "abstract_snippet": (r.get("abstract_or_snippet")
                                         or r.get("abstract", ""))[:200],
                    "is_oa": is_oa,
                    "pdf_url": pdf_url,
                    "citation_count": int(cit or 0),
                    "semantic_scholar_id": r.get("semantic_scholar_paper_id", ""),
                })

            # Register as candidates so agent can call submit_candidate_audit
            new_cands: List[Dict] = []
            for r in raw_refs:
                if r.get("doi") or r.get("title"):
                    cand_dict = {
                        "section_id": ctx.section_id,
                        "role": "foundation",
                        "backends": ["citation_chase"],
                        "query_texts": [f"references of {doi or s2_paper_id}"],
                        "title": r.get("title", ""),
                        "doi": r.get("doi", ""),
                        "year": r.get("year"),
                        "venue": r.get("venue", ""),
                        "authors": r.get("authors", []),
                        "abstract": r.get("abstract_snippet", ""),
                        "is_oa": r.get("is_oa", False),
                        "oa_url": r.get("pdf_url", ""),
                        "pdf_url": r.get("pdf_url", ""),
                        "citation_count": r.get("citation_count", 0),
                        "semantic_scholar_id": r.get("semantic_scholar_id", ""),
                    }
                    new_cands.append(cand_dict)

            registered_ids = ctx.register_candidates(new_cands)
            if new_cands:
                _append_candidates_to_ledger(ctx.work_dir, ctx.section_id, new_cands)

            for i, ref in enumerate(raw_refs):
                if i < len(registered_ids):
                    ref["candidate_id"] = registered_ids[i]

            return json.dumps({
                "status": "ok",
                "seed_doi": doi,
                "s2_paper_id_used": s2_paper_id,
                "reference_count": len(raw_refs),
                "references": raw_refs,
            }, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:300]})

    return trace_seed_references


# ---------------------------------------------------------------------------
# 9. acquire_and_materialize_oa_papers
# ---------------------------------------------------------------------------

def _make_acquire_and_materialize_oa_papers(ctx: SectionCoverageContext):
    def acquire_and_materialize_oa_papers(
        role: str,
        candidate_ids: str,
        max_papers: int = 6,
    ) -> str:
        """Download, normalize, chunk, and ingest approved OA candidates into the staging KB.

        Only candidates with decision="approved" and scope_fit in {direct, adjacent} are ingested.
        Writes MATERIALIZATION_MANIFEST.json. Candidates are processed in
        direct-first order. After every paper, the runtime rebuilds coverage
        for auditability. The caller's successful-paper budget is filled even
        when an earlier candidate fails or adds no chunks; Phase 3 decides
        whether a later wave is needed.

        Args:
            role: Coverage role these candidates serve.
            candidate_ids: JSON array of approved candidate_ids.
            max_papers: Compatibility ceiling requested by the caller. The
                runtime processes the supplied approved batch until this many
                papers add new chunks, candidates are exhausted, or the
                section/time cap is reached.
        """
        try:
            ids = json.loads(candidate_ids) if candidate_ids.strip().startswith("[") else [candidate_ids]
        except Exception:
            ids = [candidate_ids]
        ids = [i.strip() for i in ids if i.strip()]

        if not ids:
            return json.dumps({"status": "error", "error": "No candidate_ids provided"})

        _restore_candidates_from_ledger(ctx)

        # Load ledger to check decision status
        ledger_path = ctx.work_dir / "OA_CANDIDATE_LEDGER.json"
        approved_set: set = set()
        approved_scope_by_id: Dict[str, str] = {}
        if ledger_path.exists():
            try:
                ledger = OACandidateLedger.model_validate(
                    json.loads(ledger_path.read_text(encoding="utf-8"))
                )
                for c in ledger.candidates:
                    candidate_data = c.model_dump()
                    # Older/manual ledgers may contain only the audit fields,
                    # while the in-memory discovery record still carries the
                    # typed OA URL/S2/local route.  Merge route metadata only;
                    # approval and scope remain authoritative from the
                    # durable ledger.
                    runtime_candidate = ctx.get_candidate(c.candidate_id)
                    if isinstance(runtime_candidate, dict):
                        for field in (
                            "is_oa", "oa_status", "oa_url", "pdf_url",
                            "url_for_pdf", "best_oa_url", "open_access_url",
                            "html_url", "repository_url", "semantic_scholar_id",
                            "semantic_scholar_paper_id", "corpus_id",
                            "local_fulltext_path", "local_download_path",
                            "fulltext_path", "parsed_text_path", "local_file_path",
                            "local_fulltext", "has_local_fulltext",
                            "content_depth", "materialization_route", "source_kind",
                        ):
                            if not candidate_data.get(field) and runtime_candidate.get(field):
                                candidate_data[field] = runtime_candidate[field]
                        if not candidate_data.get("alternate_urls"):
                            candidate_data["alternate_urls"] = list(
                                runtime_candidate.get("alternate_urls") or []
                            )
                        if not candidate_data.get("content_urls"):
                            candidate_data["content_urls"] = dict(
                                runtime_candidate.get("content_urls") or {}
                            )
                        availability = dict(candidate_data.get("text_availability") or {})
                        availability.update({
                            key: value
                            for key, value in dict(
                                runtime_candidate.get("text_availability") or {}
                            ).items()
                            if value and not availability.get(key)
                        })
                        candidate_data["text_availability"] = availability
                    # The action is derived again at this boundary so an old
                    # or hand-edited ledger can never turn a non-approved
                    # record into executable material.
                    if candidate_is_materializable(candidate_data):
                        approved_set.add(c.candidate_id)
                        approved_scope_by_id[c.candidate_id] = (
                            c.scope_fit.value
                        )
            except Exception:
                pass

        # Load existing manifest for idempotency
        manifest_path = ctx.work_dir / "MATERIALIZATION_MANIFEST.json"
        if manifest_path.exists():
            try:
                manifest = MaterializationManifest.model_validate(
                    json.loads(manifest_path.read_text(encoding="utf-8"))
                )
            except Exception:
                manifest = MaterializationManifest(section_id=ctx.section_id)
        else:
            manifest = MaterializationManifest(section_id=ctx.section_id)

        manifest.temp_kb_path = str(ctx.temp_kb_sqlite)
        # A resumed process begins with a fresh in-memory context.  Successful
        # papers, not route attempts or metadata-only rows, consume the paper
        # budget.  Failed/reused identities remain durable in cross-wave state
        # but leave their successful slot available to the next candidate.
        persisted_successes = len({
            _candidate_identity(p.model_dump())
            or str(p.paper_id or p.candidate_id)
            for p in manifest.papers
            if p.new_paper and int(p.new_chunks or 0) > 0
        })
        with ctx._store_lock:
            ctx._papers_materialized_total = max(
                int(ctx._papers_materialized_total), persisted_successes
            )
            already_done = ctx._papers_materialized_total
        remaining_budget = ctx.min_mode_max_total_papers - already_done
        if remaining_budget <= 0:
            return json.dumps({
                "status": "error",
                "error": (
                    "Budget constraint reached; retrieval is terminal for "
                    "this section."
                ),
                "budget_reached": True,
                "stop_retrieval": True,
                "papers_materialized": already_done,
                "paper_budget": ctx.min_mode_max_total_papers,
                "next_required_actions": [
                    "refresh_section_coverage",
                    "submit_section_gap_report_if_needed",
                    "validate_section_coverage_package",
                ],
            })
        # The model has already audited this exact candidate list.  Requiring
        # another model turn for every approved paper causes cumulative ReAct
        # context to grow quadratically and can hit max_iters one paper before
        # the breadth target.  Process the approved batch deterministically,
        # with a refresh after every paper and the existing paper/time caps.
        max_papers = min(
            max(1, int(max_papers or 1), len(ids)),
            remaining_budget,
        )
        if ctx.targeted_expected_new_papers > 0:
            max_papers = min(max_papers, ctx.targeted_expected_new_papers)
        call_started = time.monotonic()
        call_time_cap = max(
            10,
            int(
                getattr(
                    ctx,
                    "max_materialization_seconds_per_call",
                    MAX_MATERIALIZATION_SECONDS_PER_CALL,
                )
                or MAX_MATERIALIZATION_SECONDS_PER_CALL
            ),
        )
        # P1-E: Only skip candidates that were already successfully acquired (fulltext).
        # Failed/metadata_only entries are retryable — replace them if a new route appears.
        successfully_materialized = {
            p.candidate_id for p in manifest.papers
            if p.acquisition_status == AcquisitionStatus.fulltext
            and int(p.new_chunks or 0) > 0
        }
        cross_state = _read_cross_wave_state(ctx)
        prior_identities = set(cross_state.get("attempted_material_identities") or [])
        identity_index = cross_state.get("material_identity_index") or {}
        seen_identities: set[str] = set()
        # Build index for in-place replacement of retryable entries
        retryable_index: Dict[str, int] = {
            p.candidate_id: i for i, p in enumerate(manifest.papers)
            if p.acquisition_status != AcquisitionStatus.fulltext
        }
        adjacent_materialized = sum(
            1
            for paper in manifest.papers
            if (
                paper.acquisition_status == AcquisitionStatus.fulltext
                and approved_scope_by_id.get(paper.candidate_id) == "adjacent"
            )
        )
        # Directly aligned papers are always attempted first.  Adjacent
        # literature remains available for background synthesis but is capped
        # so it cannot crowd out the section's actual scientific object.
        ids = sorted(
            ids,
            key=lambda value: (
                approved_scope_by_id.get(value) != "direct",
                value,
            ),
        )

        attempts_done = 0
        successes_done = 0
        materialization_time_cap_reached = False
        coverage_target_met = False
        latest_coverage: Dict[str, Any] = {}
        skipped_adjacent_ids: List[str] = []
        skipped_candidates: List[Dict[str, Any]] = []
        papers_this_call: List[Dict[str, Any]] = []
        for cid in ids:
            if successes_done >= max_papers:
                break
            if (
                attempts_done > 0
                and time.monotonic() - call_started >= call_time_cap
            ):
                materialization_time_cap_reached = True
                break
            if cid in successfully_materialized:
                continue  # already successfully acquired — idempotent

            if cid not in approved_set:
                # Strict fail-closed: always require explicit approval in ledger.
                # A candidate with no ledger entry or non-approved status is never ingested.
                continue
            if (
                approved_scope_by_id.get(cid) == "adjacent"
                and adjacent_materialized
                >= MAX_ADJACENT_MATERIALIZED_PER_SECTION
            ):
                skipped_adjacent_ids.append(cid)
                continue

            cand = ctx.get_candidate(cid)
            if cand is None:
                continue

            identity = _candidate_identity(cand)
            outcome = (cross_state.get("candidate_outcomes") or {}).get(cid) or {}
            if not outcome and identity:
                outcome = next(
                    (
                        (cross_state.get("candidate_outcomes") or {}).get(str(previous_id)) or {}
                        for previous_id in identity_index.get(identity, [])
                        if (cross_state.get("candidate_outcomes") or {}).get(str(previous_id))
                    ),
                    {},
                )
            if identity and identity in seen_identities:
                skipped_candidates.append({
                    "candidate_id": cid,
                    "status": "skipped_duplicate_identity",
                    "material_identity": identity,
                })
                _record_candidate_event(
                    ctx, cand, status="skipped_duplicate_identity"
                )
                continue
            if identity:
                seen_identities.add(identity)
            existing_paper_id, existing_chunk_ids = _staging_material_for_candidate(
                ctx, cand
            )
            if (
                (identity and identity in prior_identities)
                or outcome.get("no_progress")
                or existing_paper_id
            ):
                skip_status = (
                    "no_progress_candidate_skipped"
                    if outcome.get("no_progress")
                    else "reused_candidate"
                )
                skipped_candidates.append({
                    "candidate_id": cid,
                    "status": skip_status,
                    "material_identity": identity,
                    "paper_id": existing_paper_id,
                    "chunk_count": len(existing_chunk_ids),
                    "no_progress_components": list(
                        outcome.get("no_progress_components") or []
                    ),
                })
                _record_candidate_event(
                    ctx,
                    cand,
                    status=(
                        "reused_candidate"
                        if skip_status == "reused_candidate"
                        else "no_progress_candidate_skipped"
                    ),
                    reused_chunk_ids=existing_chunk_ids,
                    paper_id=existing_paper_id,
                    no_progress_components=list(
                        outcome.get("no_progress_components") or []
                    ),
                )
                continue

            paper_rec = MaterializedPaper(
                candidate_id=cid,
                paper_id="",
                doi=cand.get("doi", ""),
                title=cand.get("title", ""),
                year=cand.get("year"),
                venue=cand.get("venue", ""),
                section_id=ctx.section_id,
                role=role,
                role_fit=_role_union(cand.get("role"), cand.get("role_fit")),
                role_provenance=_merge_role_provenance(
                    cand.get("role_provenance") or {},
                    {str(cand.get("role") or role).casefold(): list(
                        cand.get("query_texts") or []
                    )},
                ),
                scope_fit=ScopeFit(normalize_scope_fit(cand.get("scope_fit"))),
                materialization_route=str(
                    cand.get("materialization_route") or "legacy_oa_fulltext_fallback"
                ),
                new_paper=False,
                paper_row_inserted=False,
            )

            result: Dict[str, Any] = {}
            _bump_phase2_telemetry(
                ctx,
                materialization_attempts=1,
                materialization_route_attempts=1,
            )
            try:
                ingest_candidate = dict(cand)
                ingest_candidate["_visual_first_evidence"] = bool(
                    _visual_first_evidence_required(ctx.section_data)
                )
                result = _ingest_single_candidate_bounded(
                    ingest_candidate,
                    ctx.temp_kb_sqlite,
                    ctx.work_dir,
                )
                paper_rec.paper_id = result.get("paper_id", "")
                paper_rec.chunk_ids = result.get("chunk_ids", [])
                paper_rec.new_chunk_ids = result.get("new_chunk_ids", [])
                paper_rec.new_chunks = result.get("new_chunks", 0)
                paper_rec.new_paper = bool(
                    result.get("new_paper", False)
                    and int(paper_rec.new_chunks or 0) > 0
                )
                paper_rec.paper_row_inserted = bool(
                    result.get("paper_row_inserted", False)
                )
                paper_rec.reused_chunks = result.get("reused_chunks", 0)
                paper_rec.chunk_count = len(paper_rec.chunk_ids)
                paper_rec.materialization_route = str(
                    result.get("materialization_route")
                    or paper_rec.materialization_route
                )
                paper_rec.scope_fit = ScopeFit(
                    normalize_scope_fit(
                        result.get("scope_fit") or cand.get("scope_fit")
                    )
                )
                paper_rec.acquisition_status = AcquisitionStatus(
                    result.get("acquisition_status", "metadata_only")
                )
                paper_rec.download_url = result.get("download_url", "")
                paper_rec.download_error = result.get("download_error", "")
                paper_rec.attempted_urls = result.get("attempted_urls", [])
                paper_rec.download_errors_by_url = result.get("download_errors_by_url", {})
                paper_rec.content_type_detected = result.get("content_type_detected", "")
                paper_rec.parse_failure_reason = result.get("parse_failure_reason", "")
                paper_rec.visual_ingest_status = result.get(
                    "visual_ingest_status", ""
                )
                paper_rec.visual_candidate_count = int(
                    result.get("visual_candidate_count", 0) or 0
                )
                paper_rec.visual_composite_parent_excluded_count = int(
                    result.get(
                        "visual_composite_parent_excluded_count", 0
                    )
                    or 0
                )
                paper_rec.visual_ingest_report_path = result.get(
                    "visual_ingest_report_path", ""
                )
            except Exception as exc:
                paper_rec.acquisition_status = AcquisitionStatus.failed
                paper_rec.download_error = str(exc)[:200]

            event_status = paper_rec.acquisition_status.value
            if int(paper_rec.new_chunks or 0) <= 0:
                event_status = (
                    "reused_after_attempt"
                    if paper_rec.reused_chunks or paper_rec.chunk_ids
                    else "no_new_chunks"
                )
            _record_candidate_event(
                ctx,
                cand,
                status=event_status,
                new_chunk_ids=list(paper_rec.new_chunk_ids or []),
                reused_chunk_ids=list(result.get("reused_chunk_ids", []) or []),
                paper_id=paper_rec.paper_id,
                paper_row_inserted=paper_rec.paper_row_inserted,
            )

            # P1-E: Replace retryable entry in-place; otherwise append new entry
            if cid in retryable_index:
                manifest.papers[retryable_index[cid]] = paper_rec
            else:
                manifest.papers.append(paper_rec)
            attempts_done += 1
            if paper_rec.new_paper and int(paper_rec.new_chunks or 0) > 0:
                successes_done += 1
            if (
                approved_scope_by_id.get(cid) == "adjacent"
                and paper_rec.acquisition_status
                == AcquisitionStatus.fulltext
            ):
                adjacent_materialized += 1
            papers_this_call.append({
                "candidate_id": paper_rec.candidate_id,
                "paper_id": paper_rec.paper_id,
                "title": paper_rec.title[:100],
                "scope_fit": approved_scope_by_id.get(cid, ""),
                "acquisition_status": paper_rec.acquisition_status.value,
                "chunk_count": len(paper_rec.chunk_ids),
                "new_chunk_ids": list(paper_rec.new_chunk_ids),
                "new_chunks": int(paper_rec.new_chunks or 0),
                "new_paper": bool(paper_rec.new_paper),
                "paper_row_inserted": bool(paper_rec.paper_row_inserted),
            })
            if paper_rec.chunk_ids:
                topic = ctx.section_data.get("topic_identity", {})
                record_global_material(
                    ctx.global_coverage_ledger_path,
                    topic_fingerprint=(
                        str(topic.get("fingerprint") or "")
                        if isinstance(topic, dict) else ""
                    ),
                    role=str(cand.get("role") or ""),
                    identity=_candidate_identity(cand),
                    paper_id=str(paper_rec.paper_id or ""),
                    chunk_ids=list(paper_rec.chunk_ids),
                )

            # Persist immediately so the deterministic coverage rebuild sees
            # the newly acquired source.  This also makes an interrupted batch
            # restart-safe without repeating completed acquisition work.
            manifest.total_new_papers = sum(
                1 for p in manifest.papers if p.new_paper
            )
            manifest.total_new_chunks = sum(
                p.new_chunks for p in manifest.papers
            )
            manifest.total_reused_chunks = sum(
                p.reused_chunks for p in manifest.papers
            )
            manifest.total_failed = sum(
                1
                for p in manifest.papers
                if p.acquisition_status == AcquisitionStatus.failed
            )
            _write_artifact(
                ctx.work_dir,
                "MATERIALIZATION_MANIFEST.json",
                manifest,
            )
            try:
                latest_coverage = json.loads(
                    _make_refresh_section_coverage(ctx)()
                )
            except Exception:
                latest_coverage = {}
            coverage_target_met = (
                latest_coverage.get("status") == "ok"
                and not latest_coverage.get("blocking_gaps")
            )
            # A failed/reused/no-new-chunk route leaves the successful-paper
            # slot open.  A genuine insertion may, however, satisfy the
            # current section target before the requested slot ceiling is
            # reached, so stop immediately after that successful recheck.
            if (
                paper_rec.new_paper
                and int(paper_rec.new_chunks or 0) > 0
                and coverage_target_met
            ):
                break

        # Update session-level materialization counter (enforces min_mode_max_total_papers)
        if successes_done > 0:
            with ctx._store_lock:
                ctx._papers_materialized_total += successes_done

        # Recount totals
        manifest.total_new_papers = sum(1 for p in manifest.papers if p.new_paper)
        manifest.total_new_chunks = sum(p.new_chunks for p in manifest.papers)
        manifest.total_reused_chunks = sum(p.reused_chunks for p in manifest.papers)
        manifest.total_failed = sum(
            1 for p in manifest.papers if p.acquisition_status == AcquisitionStatus.failed
        )

        _write_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json", manifest)

        with ctx._store_lock:
            current_total = ctx._papers_materialized_total
        budget_reached = (
            current_total >= ctx.min_mode_max_total_papers
        )
        targeted_expected_met = (
            ctx.targeted_expected_new_papers > 0
            and (
                successes_done >= ctx.targeted_expected_new_papers
                or current_total >= ctx.targeted_expected_new_papers
            )
        )
        stop_retrieval = budget_reached or materialization_time_cap_reached or targeted_expected_met
        return json.dumps({
            "status": "ok",
            "budget_reached": budget_reached,
            "materialization_time_cap_reached": materialization_time_cap_reached,
            "coverage_target_met": coverage_target_met,
            "coverage_after_batch": latest_coverage,
            "materialization_elapsed_seconds": round(
                time.monotonic() - call_started, 3
            ),
            "materialized_this_call": successes_done,
            "attempted_this_call": attempts_done,
            "successful_this_call": successes_done,
            "failed_this_call": max(0, attempts_done - successes_done),
            "reused_this_call": sum(
                1 for item in papers_this_call
                if int(item.get("new_chunks") or 0) <= 0
                and (
                    item.get("chunk_count")
                    or item.get("acquisition_status") != "failed"
                )
            ),
            "successful_slots_requested": max_papers,
            "successful_slots_remaining": max(0, max_papers - successes_done),
            "total_materialized": len(manifest.papers),
            "total_new_papers": manifest.total_new_papers,
            "total_new_chunks": manifest.total_new_chunks,
            "total_failed": manifest.total_failed,
            "papers_this_call": papers_this_call,
            "skipped_candidates": skipped_candidates,
            "skipped_adjacent_ids": skipped_adjacent_ids,
            "adjacent_materialized": adjacent_materialized,
            "adjacent_cap": MAX_ADJACENT_MATERIALIZED_PER_SECTION,
            "stop_retrieval": stop_retrieval,
            "phase3_request_consumed": bool(ctx.phase3_coverage_request),
            "phase3_missing_claim_ids": ctx.targeted_missing_claim_ids,
            "phase3_missing_roles": ctx.targeted_missing_roles,
            "phase3_expected_new_papers": ctx.targeted_expected_new_papers,
            "phase3_stop_condition": ctx.phase3_coverage_request.get("stop_condition", {}),
            "next_required_actions": (
                [
                    "refresh_section_coverage",
                    "submit_section_gap_report_if_needed",
                    "validate_section_coverage_package",
                ]
                if stop_retrieval
                else []
            ),
            "artifact": "MATERIALIZATION_MANIFEST.json",
        }, ensure_ascii=False)

    return acquire_and_materialize_oa_papers


def _enrich_candidate_oa_routes(cand: Dict) -> Dict:
    """Lazily enrich one approved finalist through independent legal OA resolvers."""
    from optomind_research.m3_kb_ingest import normalize_doi

    normalized_doi = normalize_doi(cand.get("doi") or "")
    if not normalized_doi:
        return cand

    enriched = dict(cand)
    enriched["content_urls"] = dict(cand.get("content_urls") or {})
    all_routes: List[str] = []

    def add_route(value: Any) -> None:
        url = str(value or "").strip()
        if url.startswith("http") and url not in all_routes:
            all_routes.append(url)

    for key in ("pdf_url", "url_for_pdf", "best_oa_url", "oa_url",
                "open_access_url", "html_url", "repository_url"):
        add_route(enriched.get(key))
    for value in enriched.get("alternate_urls") or []:
        add_route(value)
    for value in enriched["content_urls"].values():
        add_route(value)

    # Resolver failures are deliberately independent: Unpaywall failure must
    # not prevent the single bounded OpenAlex DOI lookup below (and vice versa).
    try:
        from tools.academic_backends.unpaywall_backend import UnpaywallBackend
        uw_result = UnpaywallBackend().lookup(normalized_doi) or {}
        if normalize_doi(uw_result.get("doi") or normalized_doi) == normalized_doi:
            add_route(uw_result.get("best_oa_url"))
            for key in ("oa_url", "pdf_url", "repository_url", "html_url"):
                add_route(uw_result.get(key))
            for location in uw_result.get("oa_locations") or []:
                if not isinstance(location, dict):
                    continue
                for key in ("url_for_pdf", "pdf_url", "url", "landing_page_url"):
                    add_route(location.get(key))
            if not enriched.get("best_oa_url") and uw_result.get("best_oa_url"):
                enriched["best_oa_url"] = uw_result["best_oa_url"]
            if uw_result.get("is_oa"):
                enriched["is_oa"] = True
            if uw_result.get("oa_status"):
                enriched["oa_status"] = uw_result["oa_status"]
    except Exception:
        pass

    try:
        from tools.academic_backends.openalex_backend import OpenAlexBackend
        oa_result = OpenAlexBackend().get_work(normalized_doi) or {}
        if normalize_doi(oa_result.get("doi") or "") == normalized_doi:
            raw_meta = oa_result.get("raw_metadata") or {}
            if not enriched.get("openalex_id") and oa_result.get("openalex_id"):
                enriched["openalex_id"] = oa_result["openalex_id"]
            if oa_result.get("is_oa"):
                enriched["is_oa"] = True
            if oa_result.get("oa_status"):
                enriched["oa_status"] = oa_result["oa_status"]
            if not enriched.get("abstract") and oa_result.get("abstract_or_snippet"):
                enriched["abstract"] = oa_result["abstract_or_snippet"]
            if not enriched.get("venue") and oa_result.get("journal_or_venue"):
                enriched["venue"] = oa_result["journal_or_venue"]
            for key in ("pdf_url", "open_access_url", "source_url"):
                add_route(oa_result.get(key))
            oa_content = oa_result.get("content_urls") or raw_meta.get("content_urls") or {}
            if isinstance(oa_content, dict):
                for key, value in oa_content.items():
                    add_route(value)
                    if value and not enriched["content_urls"].get(key):
                        enriched["content_urls"][key] = value
            for location in raw_meta.get("oa_locations") or []:
                if isinstance(location, dict):
                    for key in ("pdf_url", "url_for_pdf", "url"):
                        add_route(location.get(key))
    except Exception:
        pass

    primary_routes = {
        str(enriched.get(key) or "") for key in
        ("pdf_url", "url_for_pdf", "best_oa_url", "oa_url", "open_access_url",
         "html_url", "repository_url")
    }
    enriched["alternate_urls"] = [url for url in all_routes if url not in primary_routes]
    return enriched


_SENSITIVE_URL_KEYS = {
    "api_key", "apikey", "key", "token", "access_token", "auth",
    "authorization", "signature", "sig", "x-api-key",
}


def _redact_diagnostic_url(url: str) -> str:
    """Remove credentials and secret query values before persisting diagnostics."""
    try:
        parts = urlsplit(str(url or ""))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        safe_query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            safe_query.append((key, "REDACTED" if key.lower() in _SENSITIVE_URL_KEYS else value))
        return urlunsplit((parts.scheme, host, parts.path, urlencode(safe_query), ""))
    except Exception:
        return str(url or "").split("?", 1)[0]


def _redact_diagnostic_text(text: str) -> str:
    value = str(text or "")
    for marker in ("api_key=", "apikey=", "token=", "access_token=", "key="):
        lower = value.lower()
        pos = lower.find(marker)
        if pos >= 0:
            end = value.find("&", pos)
            if end < 0:
                end = len(value)
            value = value[: pos + len(marker)] + "REDACTED" + value[end:]
    return value[:200]


def _probe_url_waterfall(
    url_candidates: List[str],
    doi: str,
    download_dir: Path,
) -> tuple:
    """P0-D: Try URLs in priority order; record per-URL diagnostics.

    Returns (success_raw_bytes_or_None, source_url, attempted_urls, errors_by_url, content_type).
    Saves successful download to download_dir so KBIngester can reuse via local_download_path.
    Rejects challenge pages (HTML too short / no scholarly structure).
    """
    from optomind_research.m3_kb_ingest import _try_download_bytes, doi_to_slug, sha1_hex

    attempted: List[str] = []
    errors_by_url: Dict[str, str] = {}
    content_type_detected = ""
    saved_path = ""

    for url in url_candidates:
        safe_url = _redact_diagnostic_url(url)
        attempted.append(safe_url)
        try:
            raw = _try_download_bytes(url)
        except Exception as exc:
            errors_by_url[safe_url] = f"download_error:{_redact_diagnostic_text(str(exc))[:120]}"
            continue

        if raw is None or len(raw) == 0:
            errors_by_url[safe_url] = "empty_response_or_connection_failed"
            continue

        # Detect content type
        if raw[:4] == b"%PDF":
            if len(raw) < MIN_VALID_PDF_BYTES:
                errors_by_url[safe_url] = f"pdf_too_short:{len(raw)} bytes"
                content_type_detected = "invalid_pdf"
                continue
            ct = "application/pdf"
        elif len(raw) > 50 and (b"<html" in raw[:1024].lower() or b"<!doctype" in raw[:1024].lower()):
            ct = "text/html"
            probe_text = raw.decode("utf-8", errors="replace").lower()
            structure_hits = sum(
                1 for kw in ("abstract", "introduction", "method", "result", "discussion", "reference")
                if kw in probe_text
            )
            if len(raw) < 3000 or structure_hits < 2:
                errors_by_url[safe_url] = "challenge_page_or_too_short"
                content_type_detected = "challenge_page"
                continue
        else:
            errors_by_url[safe_url] = "unrecognized_content_type"
            content_type_detected = "application/octet-stream"
            continue

        # Save file for KBIngester reuse
        content_type_detected = ct
        if download_dir:
            try:
                download_dir.mkdir(parents=True, exist_ok=True)
                slug = doi_to_slug(doi) if doi else sha1_hex(url)[:12]
                suffix = ".pdf" if ct == "application/pdf" else ".html"
                fpath = download_dir / f"{slug}{suffix}"
                fpath.write_bytes(raw)
                saved_path = str(fpath)
            except Exception:
                pass
        return raw, url, attempted, errors_by_url, content_type_detected

    return None, "", attempted, errors_by_url, content_type_detected


def _ingest_candidate_process(
    cand: Dict,
    temp_kb: str,
    work_dir: str,
    result_queue: Any,
) -> None:
    """Process target for one bounded acquisition.

    A paper can traverse several legal OA routes, a document parser, and local
    figure extraction.  Individual HTTP timeouts do not bound that composed
    operation.  Running one paper in a child process gives the section worker a
    real wall-clock stop condition and prevents a single hostile endpoint or
    parser from consuming the whole ReAct budget.
    """

    try:
        result_queue.put(
            {
                "ok": True,
                "result": _ingest_single_candidate(
                    cand,
                    Path(temp_kb),
                    Path(work_dir),
                ),
            },
        )
    except BaseException as exc:
        result_queue.put(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            },
        )


def _ingest_single_candidate_bounded(
    cand: Dict,
    temp_kb: Path,
    work_dir: Path,
    timeout_seconds: int = MAX_MATERIALIZATION_SECONDS_PER_PAPER,
) -> Dict:
    """Run one full-text materialization with a process-level hard timeout."""

    # Deterministic tests monkeypatch _ingest_single_candidate heavily.  Keep
    # those tests in-process; the production path is what needs isolation from
    # network/parser hangs.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return _ingest_single_candidate(cand, temp_kb, work_dir)

    mp = multiprocessing.get_context("spawn")
    result_queue = mp.Queue(maxsize=1)
    process = mp.Process(
        target=_ingest_candidate_process,
        args=(dict(cand), str(temp_kb), str(work_dir), result_queue),
        daemon=False,
    )
    process.start()
    process.join(max(10, int(timeout_seconds)))
    if process.is_alive():
        process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        result = {
            "paper_id": _make_paper_id(cand),
            "chunk_ids": [],
            "new_chunk_ids": [],
            "new_paper": False,
            "paper_row_inserted": False,
            "new_chunks": 0,
            "reused_chunks": 0,
            "acquisition_status": "failed",
            "download_url": "",
            "download_error": (
                "paper_materialization_timeout:"
                f"{int(timeout_seconds)}s"
            ),
            "attempted_urls": [],
            "download_errors_by_url": {},
            "content_type_detected": "",
            "parse_failure_reason": (
                "paper-level acquisition, parsing, or visual extraction "
                f"exceeded {int(timeout_seconds)} seconds"
            ),
            "materialization_timeout": True,
        }
    else:
        try:
            payload = result_queue.get(timeout=2)
        except queue.Empty:
            payload = {
                "ok": False,
                "error": (
                    "materialization child exited without returning a result "
                    f"(exit_code={process.exitcode})"
                ),
            }
        if payload.get("ok"):
            result = dict(payload.get("result") or {})
        else:
            result = {
                "paper_id": _make_paper_id(cand),
                "chunk_ids": [],
                "new_chunk_ids": [],
                "new_paper": False,
                "paper_row_inserted": False,
                "new_chunks": 0,
                "reused_chunks": 0,
                "acquisition_status": "failed",
                "download_url": "",
                "download_error": str(payload.get("error") or "")[:300],
                "attempted_urls": [],
                "download_errors_by_url": {},
                "content_type_detected": "",
                "parse_failure_reason": str(
                    payload.get("error") or "materialization_child_failed"
                )[:300],
            }
    result_queue.close()
    result_queue.join_thread()
    return result


def _visual_first_evidence_required(section_data: Dict[str, Any]) -> bool:
    """Read only explicit visual-first declarations from a section contract."""

    if not isinstance(section_data, dict):
        return False
    if any(
        bool(section_data.get(key))
        for key in ("visual_first_evidence", "requires_visual_first_evidence")
    ):
        return True
    mode = str(section_data.get("visual_evidence_mode") or "").strip().casefold()
    if mode in {"visual_first", "visual-first", "required_visual_first"}:
        return True
    visual_contract = section_data.get("visual_contract")
    return isinstance(visual_contract, dict) and bool(
        visual_contract.get("visual_first_evidence")
    )


def _textual_evidence_closure_exists(
    work_dir: Path,
    *,
    candidate_id: str,
    new_chunk_ids: List[str],
) -> bool:
    """Require a durable textual adoption receipt before expensive extraction."""

    if not new_chunk_ids:
        return False
    package = _read_artifact(work_dir, "SECTION_MATERIAL_PACKAGE.json") or {}
    if str(package.get("coverage_status") or "") == "coverage_sufficient":
        return True
    ledger = _read_artifact(work_dir, "SECTION_SOURCE_LEDGER.json") or {}
    return any(
        isinstance(source, dict)
        and str(source.get("candidate_id") or "") == str(candidate_id)
        and source.get("canonical_chunk_ids")
        for source in ledger.get("sources", []) or []
    )


def _ingest_single_candidate(cand: Dict, temp_kb: Path, work_dir: Path) -> Dict:
    """Attempt download + parse + chunk + write to temp KB. Returns status dict.

    KBIngester(kb_sqlite=...) is the correct constructor — NOT db_path.
    Acquisition status is derived from chunk ID pattern (no .stats field on IngestResult).
    P0-C: enriches DOI routes via Unpaywall before attempting download.
    P0-D: probes URLs to collect per-URL diagnostics; KBIngester reuses local file.
    """
    from optomind_research.m3_kb_ingest import KBIngester

    download_dir = work_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    # Resolve extra DOI routes only when the candidate does not already carry
    # enough legal OA options.  Re-querying Unpaywall and OpenAlex for every
    # approved paper adds up to 45 seconds of avoidable latency per paper.
    existing_routes = {
        str(value).strip()
        for key in (
            "pdf_url", "url_for_pdf", "best_oa_url", "oa_url",
            "open_access_url", "html_url", "repository_url",
        )
        if (value := cand.get(key)) and str(value).startswith("http")
    }
    existing_routes.update(
        str(value).strip()
        for value in cand.get("alternate_urls") or []
        if str(value).startswith("http")
    )
    existing_routes.update(
        str(value).strip()
        for value in (cand.get("content_urls") or {}).values()
        if str(value).startswith("http")
    )
    if len(existing_routes) < 2:
        cand = _enrich_candidate_oa_routes(cand)

    scope_fit_val = cand.get("scope_fit", "adjacent")
    _scope_map = {
        "direct": "in_domain",
        "adjacent": "cross_domain_analogy",
        "contextual": "background",
        "out_of_scope": "off_domain",
    }
    llm_scope = _scope_map.get(str(scope_fit_val), "cross_domain_analogy")

    # P0-A: use canonical labels (direct/adjacent) accepted by _candidate_relevance_tier
    _grade_map = {
        "direct": "direct",
        "adjacent": "adjacent",
        "contextual": "background",
        "out_of_scope": "off_domain",
    }
    llm_grade = _grade_map.get(str(scope_fit_val), "adjacent")

    # Build full URL waterfall from all candidate URL fields
    _url_candidates: list = []
    for _fld in ("pdf_url", "url_for_pdf", "best_oa_url", "open_access_url",
                 "oa_url", "html_url", "repository_url"):
        _u = cand.get(_fld, "") or ""
        if _u and _u not in _url_candidates:
            _url_candidates.append(_u)
    for _u in (cand.get("alternate_urls") or []):
        if _u and _u not in _url_candidates:
            _url_candidates.append(_u)
    for _u in (cand.get("content_urls") or {}).values():
        if _u and _u not in _url_candidates:
            _url_candidates.append(_u)
    # Each route already has an HTTP timeout.  Without a route ceiling, one
    # inaccessible paper can serially consume many timeouts and freeze the
    # whole section worker.  The remaining legal routes stay in the candidate
    # ledger for a later targeted retry or human download.
    _all_url_candidates = list(_url_candidates)
    _url_candidates = _url_candidates[:MAX_OA_ROUTES_PER_CANDIDATE]

    doi = cand.get("doi", "")
    _primary_url = _url_candidates[0] if _url_candidates else ""
    _alternate_urls = _all_url_candidates[1:]

    # P0-D: probe URL waterfall for truthful diagnostics
    from optomind_research.m3_kb_ingest import doi_to_slug, sha1_hex

    reusable_download = ""
    if doi:
        slug = doi_to_slug(doi)
        for suffix in (".pdf", ".html"):
            candidate_path = download_dir / f"{slug}{suffix}"
            if (
                candidate_path.is_file()
                and candidate_path.stat().st_size
                >= (MIN_VALID_PDF_BYTES if suffix == ".pdf" else 3000)
            ):
                reusable_download = str(candidate_path)
                break
    if reusable_download:
        probe_raw = None
        probe_url = _primary_url
        attempted_urls = []
        errors_by_url = {}
        content_type_detected = (
            "application/pdf"
            if reusable_download.lower().endswith(".pdf")
            else "text/html"
        )
    else:
        (
            probe_raw,
            probe_url,
            attempted_urls,
            errors_by_url,
            content_type_detected,
        ) = (
            _probe_url_waterfall(_url_candidates, doi, download_dir)
            if _url_candidates else (None, "", [], {}, "")
        )
    probe_local_path = ""
    if reusable_download:
        probe_local_path = reusable_download
    elif probe_url and content_type_detected in ("application/pdf", "text/html"):
        slug = doi_to_slug(doi) if doi else sha1_hex(probe_url)[:12]
        suffix = ".pdf" if content_type_detected == "application/pdf" else ".html"
        candidate_path = download_dir / f"{slug}{suffix}"
        if candidate_path.is_file():
            probe_local_path = str(candidate_path)
    safe_source_url = _redact_diagnostic_url(probe_url if probe_url else _primary_url)

    gap_cand_dict: Dict[str, Any] = {
        "candidate_id": cand.get("candidate_id", ""),
        "title": cand.get("title", ""),
        "doi": doi,
        "year": cand.get("year"),
        "venue": cand.get("venue", ""),
        "authors": cand.get("authors", []),
        "abstract": cand.get("abstract", ""),
        "is_oa": cand.get("is_oa", False),
        "oa_status": "yes" if cand.get("is_oa") else "unknown",
        "pdf_url": safe_source_url,
        "open_access_url": safe_source_url,
        "source_url": safe_source_url,
        "alternate_urls": [_redact_diagnostic_url(u) for u in _alternate_urls],
        # P0-D: pass local file so KBIngester reuses it without re-downloading
        "local_download_path": probe_local_path,
        "download_attempts_complete": True,
        "citation_count": cand.get("citation_count", 0),
        "llm_scope_fit": llm_scope,
        "llm_retrieval_role": "evidence_candidate",
        "llm_relevance_grade": llm_grade,
        "llm_relevance_score": 0.7,
        "llm_relevance_confidence": "medium",
        "llm_support_status": "supporting",
        "llm_supported_clause": "",
        "llm_abstract_evidence_span": "",
        "likely_contribution": "",
        "backends": cand.get("backends", []),
        "raw_records": [],
        "download_status": "pending",
        "download_error": "",
        "download_attempted_urls": [],
        "semantic_scholar_id": cand.get("semantic_scholar_id", ""),
        "openalex_id": cand.get("openalex_id", ""),
        "relevance_score": cand.get("relevance_score", 0.5),
        "selected_reason": "section_coverage_gap",
        "query_ids": [],
        "query_texts": cand.get("query_texts", []),
    }

    dummy_claim = {
        "claim_id": f"sc_{cand.get('candidate_id', 'unknown')}",
        "statement": cand.get("title", ""),
        "missing_evidence_components": [],
        "planned_queries": cand.get("query_texts", []),
    }

    ingester = KBIngester(kb_sqlite=temp_kb, download_dir=download_dir)

    try:
        ingest_result = ingester.ingest_oa_candidates(
            [gap_cand_dict],
            dummy_claim,
            max_successes=1,
        )
        new_chunk_ids = list(ingest_result.new_chunk_ids or [])
        reused_chunk_ids = list(ingest_result.reused_chunk_ids or [])
        new_paper_ids = list(ingest_result.new_paper_ids or [])
        all_chunks = new_chunk_ids + reused_chunk_ids
        paper_id = new_paper_ids[0] if new_paper_ids else _make_paper_id(cand)

        # Determine acquisition status from chunk ID pattern (no .stats on IngestResult)
        if not all_chunks:
            acq = "metadata_only"
        elif len(all_chunks) == 1 and all_chunks[0].endswith(":abstract"):
            acq = "abstract_only"
        else:
            acq = "fulltext"

        # P0-D: build truthful diagnostics
        if acq == "fulltext":
            # Only URLs tried up to (and including) the one that succeeded
            reported_attempted = attempted_urls if attempted_urls else (
                [probe_url] if probe_url else ([_primary_url] if _primary_url else [])
            )
            # The successful URL is not an error
            clean_errors = {u: e for u, e in errors_by_url.items() if u != probe_url}
        else:
            reported_attempted = attempted_urls if attempted_urls else _url_candidates
            clean_errors = dict(errors_by_url)
            if probe_url:
                # The route downloaded plausible content but downstream parsing
                # produced no fulltext chunks. Keep that route in the audit.
                ingest_stats = getattr(ingest_result, "stats", {}) or {}
                parse_count = int(ingest_stats.get("parse_failed", 0) or 0)
                clean_errors[_redact_diagnostic_url(probe_url)] = (
                    f"parse_failed_after_download:parser_produced_no_fulltext_chunks"
                    f";parse_failed_count={parse_count}"
                )
            if acq == "abstract_only" and not content_type_detected:
                content_type_detected = "abstract_fallback"

        if acq == "fulltext":
            parse_failure_reason = ""
            download_error = ""
        elif acq == "abstract_only":
            parse_failure_reason = "fulltext unavailable; abstract-only fallback retained"
            download_error = parse_failure_reason
        else:
            if clean_errors:
                reasons = list(dict.fromkeys(clean_errors.values()))
                parse_failure_reason = "; ".join(reasons[:3])
            else:
                parse_failure_reason = "no parseable fulltext from any attempted URL"
            download_error = parse_failure_reason

        visual_report: Dict[str, Any] = {}
        if acq == "fulltext" and probe_local_path:
            visual_first = bool(cand.get("_visual_first_evidence"))
            textual_closed = _textual_evidence_closure_exists(
                work_dir,
                candidate_id=str(cand.get("candidate_id") or ""),
                new_chunk_ids=new_chunk_ids,
            )
            if not visual_first and not textual_closed:
                visual_report = {
                    "status": "deferred_until_textual_evidence_closure",
                    "deferred_reason": (
                        "Textual material was normalized, but section evidence "
                        "closure has not yet been accepted."
                    ),
                }
            else:
                try:
                    from optomind_research.runtime.supplemental_visual_ingest import (
                        extract_visual_candidates,
                    )

                    visual_report = extract_visual_candidates(
                        source_path=Path(probe_local_path),
                        staging_kb=temp_kb,
                        output_dir=work_dir / "visual_candidates",
                        paper_id=paper_id,
                        doi=doi,
                        title=str(cand.get("title") or ""),
                    )
                except Exception as exc:
                    # Figure extraction is valuable but must never discard a
                    # successfully normalized full text.
                    visual_report = {
                        "status": "extractor_failed_nonblocking",
                        "extraction_errors": [
                            f"{type(exc).__name__}: {str(exc)[:240]}"
                        ],
                    }

        return {
            "paper_id": paper_id,
            "chunk_ids": all_chunks,
            "new_chunk_ids": new_chunk_ids,
            # ``new_paper`` is a successful new-material receipt.  A paper
            # row can be upserted while every chunk is already present; that
            # case is tracked separately and must not count as new success.
            "new_paper": bool(new_paper_ids and new_chunk_ids),
            "paper_row_inserted": bool(new_paper_ids),
            "new_chunks": len(new_chunk_ids),
            "reused_chunks": len(reused_chunk_ids),
            "acquisition_status": acq,
            "download_url": safe_source_url,
            "download_error": download_error,
            "attempted_urls": reported_attempted,
            "download_errors_by_url": clean_errors,
            "content_type_detected": content_type_detected,
            "parse_failure_reason": parse_failure_reason,
            "visual_ingest_status": visual_report.get("status", ""),
            "visual_candidate_count": int(
                visual_report.get("eligible_visual_chunks", 0) or 0
            ),
            "visual_composite_parent_excluded_count": int(
                visual_report.get("composite_parents_excluded", 0) or 0
            ),
            "visual_ingest_report_path": visual_report.get(
                "report_path", ""
            ),
        }
    except Exception as exc:
        return {
            "paper_id": _make_paper_id(cand),
            "chunk_ids": [],
            "new_chunk_ids": [],
            "new_paper": False,
            "paper_row_inserted": False,
            "new_chunks": 0,
            "reused_chunks": 0,
            "acquisition_status": "failed",
            "download_url": safe_source_url,
            "download_error": _redact_diagnostic_text(str(exc)),
            "attempted_urls": attempted_urls or [_redact_diagnostic_url(u) for u in _url_candidates],
            "download_errors_by_url": (
                dict(errors_by_url) if errors_by_url
                else ({_redact_diagnostic_url(_primary_url): _redact_diagnostic_text(str(exc))} if _primary_url else {})
            ),
            "content_type_detected": content_type_detected,
            "parse_failure_reason": _redact_diagnostic_text(str(exc)),
        }


def _make_paper_id(cand: Dict) -> str:
    doi = cand.get("doi", "").strip()
    if doi:
        import re
        return "doi:" + re.sub(r"[^a-z0-9._/-]", "", doi.lower())
    title = cand.get("title", "unknown")
    slug = "".join(c if c.isalnum() else "_" for c in title.lower())[:40]
    return f"title:{slug}"


# ---------------------------------------------------------------------------
# 10. refresh_section_coverage
# ---------------------------------------------------------------------------

def _make_refresh_section_coverage(ctx: SectionCoverageContext):
    def refresh_section_coverage() -> str:
        """Rebuild coverage from explicitly adopted local and OA sources.

        Broad search hits never count as coverage until they have passed a
        scope/role audit.  Updates LOCAL_COVERAGE_AUDIT.json.
        No arguments required.
        """
        _build_source_ledger(ctx)
        source_ledger = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
        accepted_sources = [
            source for source in source_ledger.get("sources", [])
            if source.get("scope_fit") in ("direct", "adjacent")
            and source.get("canonical_chunk_ids")
        ]
        required_roles = set(ctx.section_data.get("required_roles", []))
        merged_roles: Dict[str, LocalRoleAudit] = {}
        for role in COVERAGE_ROLES:
            role_sources = _role_coverage_sources(accepted_sources, role)
            total_papers = len({
                source.get("paper_id") for source in role_sources
                if source.get("paper_id")
            })
            total_chunks = len({
                chunk_id for source in role_sources
                for chunk_id in source.get("canonical_chunk_ids", [])
            })
            if total_papers >= 3:
                verdict, severity = "sufficient", "minor"
            elif total_papers >= 1:
                verdict, severity = "partial", "important"
            else:
                verdict = "none"
                severity = "blocking" if role in required_roles else "important"
            merged_roles[role] = LocalRoleAudit(
                role=role,
                paper_count=total_papers,
                chunk_count=total_chunks,
                coverage_verdict=verdict,
                gap_severity=severity,
            )

        blocking = [
            r for r, a in merged_roles.items()
            if a.gap_severity == "blocking" and r in required_roles
        ]
        important = [r for r, a in merged_roles.items() if a.gap_severity == "important"]
        sufficient = [r for r, a in merged_roles.items() if a.coverage_verdict == "sufficient"]
        breadth_targets = ctx.coverage_breadth_targets()
        unique_papers = {
            str(source.get("paper_id"))
            for source in accepted_sources
            if source.get("paper_id")
        }
        direct_papers = {
            str(source.get("paper_id"))
            for source in accepted_sources
            if source.get("paper_id") and source.get("scope_fit") == "direct"
        }
        breadth_target_met = (
            len(unique_papers) >= breadth_targets["minimum_unique_sources"]
            and len(direct_papers) >= breadth_targets["minimum_direct_sources"]
        )
        if not breadth_target_met:
            blocking.append("coverage_breadth")

        all_adopted_chunk_ids = {
            str(chunk_id)
            for source in accepted_sources
            for chunk_id in source.get("canonical_chunk_ids", [])
            if chunk_id
        }
        audit = LocalCoverageAudit(
            section_id=ctx.section_id,
            role_audits=merged_roles,
            total_local_papers=len(unique_papers),
            total_local_chunks=len(all_adopted_chunk_ids),
            blocking_gaps=blocking,
            important_gaps=important,
            sufficient_roles=sufficient,
        )
        _write_artifact(ctx.work_dir, "LOCAL_COVERAGE_AUDIT.json", audit)

        query_targets = _coverage_query_targets(ctx)
        coverage_sources = [
            source for source in accepted_sources
            if _role_material_is_coverage_eligible(
                source, str(source.get("literature_role") or "")
            )
        ]
        closed_components = closed_scientific_components(
            query_targets,
            coverage_sources,
        )
        cross_state = _read_cross_wave_state(ctx)
        cross_state["scientific_components_closed"] = list(dict.fromkeys([
            *list(cross_state.get("scientific_components_closed") or []),
            *closed_components,
        ]))
        _write_cross_wave_state(ctx, cross_state)
        telemetry = _phase2_telemetry(ctx)
        telemetry["scientific_components_closed"] = list(
            cross_state["scientific_components_closed"]
        )
        _write_artifact(ctx.work_dir, "PHASE2_TELEMETRY.json", telemetry)

        return json.dumps({
            "status": "ok",
            "blocking_gaps": blocking,
            "important_gaps": important,
            "sufficient_roles": sufficient,
            "source_breadth": {
                "unique_sources": len(unique_papers),
                "direct_sources": len(direct_papers),
                **breadth_targets,
                "target_met": breadth_target_met,
            },
            "role_summary": {r: a.coverage_verdict for r, a in merged_roles.items()},
            "scientific_components_closed": closed_components,
            "uncovered_query_targets": query_targets,
            "ledger_summary": _payload_ledger_summary(ctx),
            "artifact": "LOCAL_COVERAGE_AUDIT.json",
        }, ensure_ascii=False)

    return refresh_section_coverage


# ---------------------------------------------------------------------------
# 11. validate_section_coverage_package
# ---------------------------------------------------------------------------

def _make_validate_section_coverage_package(ctx: SectionCoverageContext):
    def validate_section_coverage_package(expected_roles: str = "") -> str:
        """Validate all required artifacts exist and build SECTION_MATERIAL_PACKAGE.json.

        Returns VALIDATION_PASSED if:
          - SECTION_CONTEXT.json exists
          - SECTION_COVERAGE_PLAN.json exists
          - LOCAL_COVERAGE_AUDIT.json exists
          - No blocking gaps remain (or all gaps are documented in SECTION_GAP_REPORT.json)
          - At least one source entry exists in SECTION_SOURCE_LEDGER.json OR blocking_gaps == []

        Returns VALIDATION_FAILED:<reason> otherwise.

        Args:
            expected_roles: JSON array of role names that must be covered (optional).
                If empty, uses the required_roles from SECTION_CONTEXT.json.
        """
        missing_artifacts = []
        for fname in ("SECTION_CONTEXT.json", "SECTION_COVERAGE_PLAN.json", "LOCAL_COVERAGE_AUDIT.json"):
            if not (ctx.work_dir / fname).exists():
                missing_artifacts.append(fname)

        if missing_artifacts:
            return f"VALIDATION_FAILED: Missing required artifacts: {missing_artifacts}. " \
                   "Call load_section_context, inspect_section_local_coverage, and submit_literature_role_plan first."

        # A source ledger is not sufficient provenance on its own: every
        # staged chunk must have a real canonical paper parent.  Keep this
        # audit deterministic and fail closed before package readiness is
        # evaluated.
        if ctx.temp_kb_sqlite and Path(ctx.temp_kb_sqlite).exists():
            try:
                from optomind_research.s2_kb_bridge import (
                    validate_foreign_parent_consistency,
                )

                identity_audit = validate_foreign_parent_consistency(
                    ctx.temp_kb_sqlite
                )
                _write_artifact(
                    ctx.work_dir,
                    "S2_IDENTITY_AUDIT.json",
                    {
                        "schema_version": "phase2.s2_identity_audit.v1",
                        "section_id": ctx.section_id,
                        **identity_audit,
                    },
                )
                if not identity_audit.get("valid"):
                    return (
                        "VALIDATION_FAILED: S2 text_chunks contain orphan parents: "
                        + json.dumps(identity_audit, ensure_ascii=False)
                    )
            except Exception as exc:
                return (
                    "VALIDATION_FAILED: S2 identity audit could not run: "
                    + str(exc)[:240]
                )

        # Parse expected roles
        try:
            req_roles = json.loads(expected_roles) if expected_roles.strip().startswith("[") else []
        except Exception:
            req_roles = []
        if not req_roles:
            sc = _read_artifact(ctx.work_dir, "SECTION_CONTEXT.json") or {}
            req_roles = sc.get("required_roles", [])
        if not req_roles:
            req_roles = list(COVERAGE_ROLES)

        # The adaptive contract is enabled by the orchestrator for production
        # runs.  Direct legacy callers retain their historical target fields
        # unless they opt in explicitly; this keeps old ledgers readable while
        # making the new contract the single runtime path for new runs.
        adaptive_enabled = bool(
            getattr(ctx, "adaptive_coverage_enabled", False)
            or (ctx.section_data or {}).get("adaptive_coverage_enabled")
        )

        # Check coverage audit
        audit_data = _read_artifact(ctx.work_dir, "LOCAL_COVERAGE_AUDIT.json") or {}
        blocking = audit_data.get("blocking_gaps", [])
        required_blocking = [r for r in blocking if r in req_roles]

        # Check gap report — if exists, blocking gaps may be documented as acceptable
        gap_report_data = _read_artifact(ctx.work_dir, "SECTION_GAP_REPORT.json")
        documented_gaps: set = set()
        if gap_report_data:
            for gap in gap_report_data.get("gaps", []):
                if not isinstance(gap, dict):
                    continue
                if gap.get("stop_reason"):
                    documented_gaps.add(str(gap.get("role", "")))
                # ``coverage_material`` is an adaptive contract label rather
                # than one of the six literature roles.  A bounded empty or
                # unavailable backend is still a normal, package-producing
                # outcome, so its explicit stop receipt must suppress the
                # corresponding validator blocker without pretending that
                # evidence exists.
                for label in gap.get("documented_labels") or []:
                    if str(label).strip():
                        documented_gaps.add(str(label).strip())
            if gap_report_data.get("stop_conditions_met"):
                documented_gaps.update(
                    str(label).strip()
                    for label in gap_report_data.get("documented_labels") or []
                    if str(label).strip()
                )

        # Build source ledger from audited materialization and local-candidate ledgers.
        _build_source_ledger(ctx)

        ledger_data = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
        sources = ledger_data.get("sources", [])
        unique_source_ids = {
            str(source.get("paper_id"))
            for source in sources
            if source.get("paper_id")
            and source.get("scope_fit") in ("direct", "adjacent")
            and source.get("canonical_chunk_ids")
        }
        direct_source_ids = {
            str(source.get("paper_id"))
            for source in sources
            if source.get("paper_id")
            and source.get("scope_fit") == "direct"
            and source.get("canonical_chunk_ids")
        }
        breadth_targets = ctx.coverage_breadth_targets()
        breadth_target_met = (
            len(unique_source_ids) >= breadth_targets["minimum_unique_sources"]
            and len(direct_source_ids) >= breadth_targets["minimum_direct_sources"]
        )
        adopted_for_coverage = [
            source for source in sources
            if (
                source.get("scope_fit") in ("direct", "adjacent")
                and source.get("canonical_chunk_ids")
                and source.get("literature_role") in req_roles
                and _role_material_is_coverage_eligible(
                    source, str(source.get("literature_role") or "")
                )
            )
        ]
        covered_required_roles = {
            source.get("literature_role") for source in adopted_for_coverage
        }
        missing_required_roles = [
            role for role in req_roles if role not in covered_required_roles
        ]
        unresolved_blocking = [
            role for role in missing_required_roles if role not in documented_gaps
        ]
        if (
            not breadth_target_met
            and "coverage_breadth" not in documented_gaps
        ):
            unresolved_blocking.append("coverage_breadth")

        sc_data = _read_artifact(ctx.work_dir, "SECTION_CONTEXT.json") or {}
        adaptive_readiness = evaluate_adaptive_coverage(
            {
                **dict(ctx.section_data or {}),
                **dict(sc_data),
            },
            sources,
            claims=(ctx.section_data or {}).get("load_bearing_claims") or (),
            legacy_targets=breadth_targets,
        )
        if adaptive_enabled:
            # Only load-bearing failures remain blockers.  Optional role
            # breadth and the legacy article-wide count are recorded as
            # limitations or merge instructions by the adaptive contract.
            adaptive_blocking: List[str] = []
            if adaptive_readiness.outcome == "needs_more_literature":
                adaptive_blocking.extend(adaptive_readiness.missing_required_roles)
                if adaptive_readiness.unsupported_load_bearing_claims:
                    adaptive_blocking.append("load_bearing_claims")
                if not adaptive_readiness.visual_asset_ready:
                    adaptive_blocking.append("visual_asset")
                if not sources or not adaptive_readiness.unique_sources:
                    adaptive_blocking.append("coverage_material")
            unresolved_blocking = list(dict.fromkeys(
                item for item in adaptive_blocking
                if item not in documented_gaps
            ))
            blocking = list(unresolved_blocking)
        else:
            # A documented bounded stop is no longer accidentally converted
            # back into a blocker by the old ``missing_required_roles`` list.
            blocking = list(dict.fromkeys(unresolved_blocking))

        # Build material package
        role_counts: Dict[str, int] = {}
        chunk_ids_by_role: Dict[str, list] = {}
        for src in sources:
            if src.get("scope_fit") not in ("direct", "adjacent"):
                continue
            r = src.get("literature_role", "unknown")
            role_counts[r] = role_counts.get(r, 0) + 1
            chunk_ids_by_role.setdefault(r, []).extend(
                src.get("canonical_chunk_ids", [])
            )
        chunk_ids_by_role = {
            role: list(dict.fromkeys(chunk_ids))
            for role, chunk_ids in chunk_ids_by_role.items()
        }

        manifest_data = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
        new_sources = sum(1 for s in sources if s.get("new_this_run"))
        local_prior = sum(1 for s in sources if s.get("local_prior"))
        accepted_sources = [
            source for source in sources
            if source.get("scope_fit") in ("direct", "adjacent")
            and source.get("canonical_chunk_ids")
        ]

        topic_identity = ctx.section_data.get("topic_identity", {})
        if isinstance(topic_identity, dict) and topic_identity.get("valid"):
            source_topic_alignment = assess_topic_alignment(
                sources,
                topic_identity,
                strict=False,
            )
            _write_artifact(
                ctx.work_dir,
                "SECTION_TOPIC_ALIGNMENT.json",
                source_topic_alignment,
            )
            if source_topic_alignment.get("status") != "passed":
                if accepted_sources:
                    return (
                        "VALIDATION_FAILED: adopted literature does not preserve "
                        "the confirmed scientific object; revise the local audit "
                        "or run a topic-specific OA search before authoring."
                    )
                # No accepted source means there is no scientific material to
                # misclassify.  A bounded search must still close with an
                # honest empty/limited package so downstream stages can see the
                # gap and decide whether to retry, merge, or reframe.
                source_topic_alignment = {
                    **source_topic_alignment,
                    "status": "not_applicable_no_adopted_sources",
                    "reason": (
                        "No direct or adjacent material was adopted; topic "
                        "alignment is deferred until a source is available."
                    ),
                    "scientific_coverage_ready": False,
                }
                _write_artifact(
                    ctx.work_dir,
                    "SECTION_TOPIC_ALIGNMENT.json",
                    source_topic_alignment,
                )
        adaptive_limitations = list(adaptive_readiness.limitations)
        if adaptive_enabled and adaptive_readiness.outcome == "merge_required":
            adaptive_limitations.append("merge_or_simplify_section_before_authoring")
        open_gap_labels = list(dict.fromkeys([
            *blocking,
            *([] if breadth_target_met else ["coverage_breadth"]),
            *(adaptive_limitations if adaptive_enabled else []),
        ]))
        if adaptive_enabled and adaptive_readiness.outcome == "needs_more_literature":
            open_gap_labels.extend(
                reason for reason in adaptive_readiness.reasons
                if reason not in open_gap_labels
            )
            open_gap_labels = list(dict.fromkeys(open_gap_labels))
        coverage_status = (
            "blocking_gaps_remain"
            if unresolved_blocking
            else (
                "coverage_sufficient"
                if (
                    (not adaptive_enabled and breadth_target_met and not open_gap_labels)
                    or (
                        adaptive_enabled
                        and adaptive_readiness.outcome == "material_ready"
                        and not open_gap_labels
                    )
                )
                else "completed_with_open_gaps"
            )
        )
        gap_summary = (
            ", ".join(f"{role}: blocking" for role in unresolved_blocking)
            if unresolved_blocking
            else (
                ", ".join(
                    f"{role}: documented_open_gap"
                    for role in open_gap_labels
                )
                if open_gap_labels
                else "no blocking or open gaps"
            )
        )

        package = SectionMaterialPackage(
            section_id=ctx.section_id,
            section_title=sc_data.get("section_title", ctx.section_id),
            chapter_argument=sc_data.get("chapter_argument", ""),
            coverage_status=coverage_status,
            total_sources=len(sources),
            unique_sources=len(unique_source_ids),
            direct_sources=len(direct_source_ids),
            minimum_unique_sources=breadth_targets["minimum_unique_sources"],
            minimum_direct_sources=breadth_targets["minimum_direct_sources"],
            breadth_target_met=breadth_target_met,
            new_sources_this_run=new_sources,
            local_prior_sources=local_prior,
            sources_by_role=role_counts,
            chunk_ids_by_role=chunk_ids_by_role,
            blocking_gaps_remain=bool(unresolved_blocking),
            gap_summary=gap_summary,
            artifacts={
                "SECTION_CONTEXT": "SECTION_CONTEXT.json",
                "SECTION_COVERAGE_PLAN": "SECTION_COVERAGE_PLAN.json",
                "LOCAL_COVERAGE_AUDIT": "LOCAL_COVERAGE_AUDIT.json",
                "OA_CANDIDATE_LEDGER": "OA_CANDIDATE_LEDGER.json",
                "LOCAL_CANDIDATE_LEDGER": LOCAL_CANDIDATE_LEDGER,
                "SEARCH_BUDGET_LEDGER": SEARCH_BUDGET_LEDGER,
                "MATERIALIZATION_MANIFEST": "MATERIALIZATION_MANIFEST.json",
                "SECTION_SOURCE_LEDGER": "SECTION_SOURCE_LEDGER.json",
                "SECTION_SOURCE_SELECTION": "SECTION_SOURCE_SELECTION.json",
                "SECTION_COVERAGE_PACKAGE": "SECTION_COVERAGE_PACKAGE.json",
                "SECTION_GAP_REPORT": "SECTION_GAP_REPORT.json",
            },
        )
        package_payload = package.model_dump()
        if adaptive_enabled:
            package_payload.update({
                "coverage_outcome": adaptive_readiness.outcome,
                "adaptive_coverage_contract": adaptive_readiness.contract.to_dict(),
                "adaptive_readiness": adaptive_readiness.to_dict(),
                "adaptive_breadth_target_met": adaptive_readiness.outcome in {
                    "material_ready", "material_ready_with_limits"
                },
                "coverage_limitations": list(adaptive_readiness.limitations),
                "permission_failures": list(adaptive_readiness.permission_failures),
                "factual_permission_sources": adaptive_readiness.factual_permission_sources,
                "scoped_direct_sources": adaptive_readiness.scoped_direct_sources,
                "factual_direct_sources": adaptive_readiness.factual_direct_sources,
                "merge_required": adaptive_readiness.outcome == "merge_required",
            })
        _write_artifact(ctx.work_dir, "SECTION_MATERIAL_PACKAGE.json", package_payload)
        # Keep the historical material-package filename for downstream
        # authoring, while emitting the explicit Phase-2 coverage-package
        # contract requested by the deterministic controller.
        _write_artifact(
            ctx.work_dir,
            "SECTION_COVERAGE_PACKAGE.json",
            package_payload,
        )
        readiness = evaluate_coverage_readiness(
            required_artifacts=(
                "SECTION_CONTEXT.json",
                "SECTION_COVERAGE_PLAN.json",
                "LOCAL_COVERAGE_AUDIT.json",
                "SECTION_SOURCE_LEDGER.json",
            ),
            work_dir_exists=all(
                (ctx.work_dir / name).exists()
                for name in (
                    "SECTION_CONTEXT.json",
                    "SECTION_COVERAGE_PLAN.json",
                    "LOCAL_COVERAGE_AUDIT.json",
                    "SECTION_SOURCE_LEDGER.json",
                )
            ),
            package=package_payload,
        )
        _write_artifact(
            ctx.work_dir,
            "COVERAGE_DECISION.json",
            {
                "schema_version": "phase2.1.coverage_decision.v1",
                "section_id": ctx.section_id,
                **readiness.to_dict(),
                "coverage_status": coverage_status,
                "coverage_outcome": adaptive_readiness.outcome if adaptive_enabled else readiness.outcome,
                "adaptive_coverage_enabled": adaptive_enabled,
                "adaptive_readiness": adaptive_readiness.to_dict(),
                "breadth_target_met": breadth_target_met,
                "blocking_gaps": list(unresolved_blocking),
                "open_gaps": list(open_gap_labels),
                "structural_completion_is_not_scientific_readiness": True,
            },
        )

        if unresolved_blocking:
            return (
                f"VALIDATION_FAILED: {len(unresolved_blocking)} blocking gaps remain "
                f"with no documented stop reason: {unresolved_blocking}. "
                "Use refresh_section_coverage then document gaps via write_task_note "
                "as SECTION_GAP_REPORT.json, or acquire more papers."
            )

        return (
            f"VALIDATION_PASSED: Section coverage package complete. "
            f"Status={coverage_status}, sources={len(sources)}, "
            f"unique_sources={len(unique_source_ids)}/"
            f"{breadth_targets['minimum_unique_sources']}, "
            f"direct_sources={len(direct_source_ids)}/"
            f"{breadth_targets['minimum_direct_sources']}, "
            f"new_this_run={new_sources}, "
            f"open_gaps={len(open_gap_labels)}."
        )

    return validate_section_coverage_package


def _build_source_ledger(ctx: SectionCoverageContext) -> None:
    """Build the adopted-source ledger from audited OA and local candidates.

    Same paper may appear once per role it serves — dedup key is (paper_id, role),
    Recall is deliberately broader than adoption. Unreviewed local hits and
    roles explicitly marked ``not_needed`` never enter the writing graph.
    """
    manifest_data = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
    ledger_data = _read_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json") or {}
    cand_map = {c.get("candidate_id", ""): c for c in ledger_data.get("candidates", [])}

    sources: List[SourceEntry] = []
    # Dedup key: (paper_id, role) — same paper may serve multiple roles
    seen_pid_role: set = set()
    active_roles = _active_planned_roles(ctx)

    # Preserve previously adopted entries across checkpoint/resume, but never
    # preserve legacy unreviewed recall.  Subsequent manifest/local-ledger
    # passes may enrich these entries with newly materialized chunks.
    previous_ledger = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
    for raw_source in previous_ledger.get("sources", []):
        if (
            not isinstance(raw_source, dict)
            or raw_source.get("scope_fit") not in ("direct", "adjacent", "contextual")
            or raw_source.get("literature_role") not in active_roles
            or raw_source.get("literature_role")
            in set(raw_source.get("not_usable_for") or [])
            or not raw_source.get("canonical_chunk_ids")
        ):
            continue
        try:
            source = SourceEntry.model_validate(raw_source)
        except Exception:
            continue
        # One-time in-memory migration for legacy ledgers.  The next write
        # persists the derived route fields, so downstream authoring no longer
        # mistakes an old full-text source for metadata-only content.
        if not raw_source.get("content_depth") or not raw_source.get(
            "discovery_route"
        ):
            legacy_route = _source_route_fields(
                candidate={
                    "backends": str(
                        raw_source.get("retrieval_backend") or ""
                    ).split(","),
                },
                materialized={
                    "acquisition_status": str(
                        raw_source.get("acquisition_status") or ""
                    ),
                },
                scope_fit=str(raw_source.get("scope_fit") or "unreviewed"),
                local=bool(raw_source.get("local_prior")),
                abstract_only=str(
                    raw_source.get("acquisition_status") or ""
                )
                == AcquisitionStatus.abstract_only.value,
                chunk_ids=list(raw_source.get("canonical_chunk_ids") or []),
            )
            model_fields = set(
                getattr(SourceEntry, "model_fields", {})
                or getattr(SourceEntry, "__fields__", {})
            )
            for key, value in legacy_route.items():
                if key in model_fields:
                    if key == "scope_fit":
                        try:
                            value = ScopeFit(str(value))
                        except ValueError:
                            value = ScopeFit.unreviewed
                    setattr(source, key, value)
        key = (source.paper_id, source.literature_role)
        if key not in seen_pid_role:
            seen_pid_role.add(key)
            sources.append(source)

    # --- From materialized papers ---
    for p in manifest_data.get("papers", []):
        # Failed or metadata-only attempts remain fully auditable in the
        # materialization manifest, but they are not adopted writing sources.
        # A source must expose at least one canonical chunk to enter coverage
        # counts or a downstream evidence packet.
        if (
            p.get("acquisition_status") == AcquisitionStatus.failed.value
            or not p.get("chunk_ids")
        ):
            continue
        pid = p.get("paper_id") or p.get("candidate_id", "")
        if not pid:
            continue
        cand = cand_map.get(p.get("candidate_id", ""), {})
        # Collect all roles this paper serves (primary role + role_fit list from audit)
        primary_role = p.get("role", cand.get("role", ""))
        extra_roles = [r for r in cand.get("role_fit", []) if isinstance(r, str)]
        all_roles = [
            role for role in dict.fromkeys([primary_role] + extra_roles)
            if (
                role in active_roles
                and role not in set(cand.get("not_usable_for") or [])
            )
        ]

        acq_val = p.get("acquisition_status", "metadata_only")
        acq = AcquisitionStatus(acq_val) if acq_val in (
            "fulltext", "structured_snippet", "abstract_only", "metadata_only", "failed") else AcquisitionStatus.metadata_only
        scope_val = normalize_scope_fit(cand.get("scope_fit", "unreviewed"))
        scope = ScopeFit(scope_val) if scope_val in (
            "direct", "adjacent", "contextual", "out_of_scope") else ScopeFit.unreviewed

        for role in all_roles:
            key = (pid, role)
            if key in seen_pid_role:
                continue
            seen_pid_role.add(key)
            sources.append(SourceEntry(
                paper_id=pid,
                doi=p.get("doi", cand.get("doi", "")),
                title=p.get("title", cand.get("title", "")),
                year=p.get("year", cand.get("year")),
                venue=p.get("venue", cand.get("venue", "")),
                authors=cand.get("authors", []),
                literature_role=role,
                role_provenance=_merge_role_provenance(
                    cand.get("role_provenance") or {},
                    {str(role).casefold(): list(cand.get("query_texts") or [])},
                ),
                retrieval_query=", ".join(cand.get("query_texts", []))[:200],
                retrieval_backend=", ".join(cand.get("backends", []))[:100],
                adoption_reason=cand.get("audit_reason", ""),
                canonical_chunk_ids=p.get("chunk_ids", []),
                local_prior=False,
                new_this_run=True,
                acquisition_status=acq,
                section_id=ctx.section_id,
                not_usable_for=cand.get("not_usable_for", []),
                **_source_route_fields(
                    candidate=cand,
                    materialized=p,
                    scope_fit=scope.value,
                    chunk_ids=p.get("chunk_ids", []),
                ),
            ))

    # --- Approved adjacent/contextual OA candidates that were not
    # materialized remain available as background sources.  They carry the
    # original provenance/permission and never count toward coverage because
    # they have no canonical chunks. ---
    for cand in ledger_data.get("candidates", []):
        if not isinstance(cand, dict):
            continue
        if str(cand.get("decision") or "").casefold() != "approved":
            continue
        scope_val = normalize_scope_fit(cand.get("scope_fit"))
        if scope_val not in ("adjacent", "contextual"):
            continue
        role = str(cand.get("role") or "").strip().casefold()
        if (
            role not in active_roles
            or role in set(cand.get("not_usable_for") or [])
        ):
            continue
        pid = str(cand.get("paper_id") or cand.get("candidate_id") or "")
        if not pid:
            continue
        key = (pid, role)
        if key in seen_pid_role:
            continue
        seen_pid_role.add(key)
        abstract_only = bool(str(cand.get("abstract") or "").strip())
        sources.append(SourceEntry(
            paper_id=pid,
            doi=str(cand.get("doi") or ""),
            title=str(cand.get("title") or ""),
            year=cand.get("year"),
            venue=str(cand.get("venue") or ""),
            authors=[
                str(author)
                for author in (cand.get("authors") or [])[:5]
                if str(author)
            ],
            literature_role=role,
            role_provenance=_merge_role_provenance(
                cand.get("role_provenance") or {},
                {role: list(cand.get("query_texts") or [])},
            ),
            retrieval_query=", ".join(
                str(item)
                for item in (cand.get("query_texts") or [])
            )[:200],
            retrieval_backend=", ".join(
                str(item)
                for item in (cand.get("backends") or [])
            )[:100],
            adoption_reason=str(
                cand.get("audit_reason")
                or "approved background source"
            )[:500],
            canonical_chunk_ids=[],
            local_prior=False,
            new_this_run=False,
            acquisition_status=(
                AcquisitionStatus.abstract_only
                if abstract_only
                else AcquisitionStatus.metadata_only
            ),
            section_id=ctx.section_id,
            not_usable_for=list(cand.get("not_usable_for") or []),
            **_source_route_fields(
                candidate=cand,
                scope_fit=scope_val,
                abstract_only=abstract_only,
                chunk_ids=[],
            ),
        ))

    # --- Phase 3 selected material: direct allowlist bridge ---
    # These entries are added before the legacy single-KB adapter.  The
    # ``seen_pid_role`` guard below prevents the fallback adapter from
    # duplicating them, while preserving the complete section overlay.
    phase3_inventory = _phase3_material_inventory(ctx)
    phase3_by_key: Dict[tuple[str, str], SourceEntry] = {}
    existing_by_key: Dict[tuple[str, str], SourceEntry] = {
        (source.paper_id, source.literature_role): source
        for source in sources
    }
    for item in phase3_inventory:
        pid = str(item.get("paper_id") or "")
        if not pid:
            continue
        for role in item.get("roles") or []:
            if role not in active_roles:
                continue
            key = (pid, role)
            chunk_id = str(item.get("chunk_id") or "")
            if key in existing_by_key:
                existing_by_key[key].canonical_chunk_ids = list(dict.fromkeys([
                    *existing_by_key[key].canonical_chunk_ids,
                    chunk_id,
                ]))
                continue
            if key in phase3_by_key:
                phase3_by_key[key].canonical_chunk_ids = list(dict.fromkeys([
                    *phase3_by_key[key].canonical_chunk_ids,
                    chunk_id,
                ]))
                continue
            scope_text = str(item.get("scope_fit") or "adjacent")
            scope = ScopeFit(scope_text) if scope_text in {
                "direct", "adjacent", "contextual", "out_of_scope", "unreviewed"
            } else ScopeFit.unreviewed
            depth = str(item.get("content_depth") or "fulltext")
            abstract_only = depth in {"abstract", "abstract_claim"}
            phase3_by_key[key] = SourceEntry(
                paper_id=pid,
                doi=str(item.get("doi") or ""),
                title=str(item.get("title") or ""),
                year=item.get("year"),
                venue=str(item.get("venue") or ""),
                literature_role=role,
                retrieval_query="phase3_selected_material",
                retrieval_backend="phase3_argument_layer",
                adoption_reason="Existing Phase 3 selected material bridged into Phase 2",
                canonical_chunk_ids=[chunk_id],
                local_prior=True,
                new_this_run=False,
                acquisition_status=(
                    AcquisitionStatus.abstract_only if abstract_only
                    else AcquisitionStatus.structured_snippet
                    if depth == "structured_snippet"
                    else AcquisitionStatus.fulltext
                ),
                section_id=ctx.section_id,
                not_usable_for=list(item.get("not_usable_for") or []),
                **_source_route_fields(
                    candidate=item,
                    scope_fit=scope.value,
                    local=True,
                    abstract_only=abstract_only,
                    chunk_ids=[chunk_id],
                ),
            )
            seen_pid_role.add(key)
    sources.extend(phase3_by_key.values())

    # --- From local KB — real chunk_ids fetched from DB, one entry per (paper_id, role) ---
    if ctx.kb_sqlite and ctx.kb_sqlite.exists():
        local_candidates = [
            item for item in _accepted_local_candidates(ctx)
            if (
                item.get("role") in active_roles
                and item.get("role")
                not in set(item.get("not_usable_for") or [])
            )
        ]
        try:
            with sqlite3.connect(str(ctx.kb_sqlite)) as conn:
                chunk_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(text_chunks)").fetchall()
                }
                source_kind_expr = "source_kind" if "source_kind" in chunk_columns else "''"
                evidence_level_expr = "evidence_level" if "evidence_level" in chunk_columns else "''"
                raw_json_expr = "raw_json" if "raw_json" in chunk_columns else "'{}'"
                source_by_key = {
                    (source.paper_id, source.literature_role): source
                    for source in sources
                }
                for candidate in local_candidates:
                    pid = str(candidate.get("paper_id") or "")
                    role = str(candidate.get("role") or "")
                    matched_chunk_id = str(candidate.get("chunk_id") or "")
                    if not pid or not role or not matched_chunk_id:
                        continue
                    ownership = conn.execute(
                        f"SELECT {source_kind_expr}, {evidence_level_expr}, {raw_json_expr} "
                        "FROM text_chunks WHERE chunk_id = ? AND paper_id = ?",
                        (matched_chunk_id, pid),
                    ).fetchone()
                    if not ownership:
                        _record_invalid_chunk_ownership(
                            ctx,
                            pid,
                            matched_chunk_id,
                        )
                        continue
                    source_kind = str(ownership[0] or "").lower()
                    evidence_level = str(ownership[1] or "").lower()
                    try:
                        chunk_raw = json.loads(ownership[2] or "{}")
                    except Exception:
                        chunk_raw = {}
                    ingest_source = str(chunk_raw.get("ingest_source") or "").lower()
                    is_abstract = (
                        source_kind == "abstract"
                        or evidence_level == "abstract"
                        or matched_chunk_id.endswith(":abstract")
                        or ingest_source == "m3_real_abstract_fallback"
                    )
                    row = conn.execute(
                        "SELECT title, year, venue FROM papers WHERE paper_id = ?",
                        (pid,),
                    ).fetchone()
                    title = row[0] if row else candidate.get("title", "")
                    year = row[1] if row else candidate.get("year")
                    venue = row[2] if row else candidate.get("venue", "")
                    key = (pid, role)
                    if key in source_by_key:
                        source = source_by_key[key]
                        source.canonical_chunk_ids = list(dict.fromkeys([
                            *source.canonical_chunk_ids,
                            matched_chunk_id,
                        ]))
                        continue
                    source = SourceEntry(
                        paper_id=pid,
                        title=title,
                        year=year,
                        venue=venue,
                        literature_role=role,
                        retrieval_query=str(candidate.get("retrieval_query") or "")[:200],
                        retrieval_backend="local_review_kb",
                        adoption_reason=str(candidate.get("audit_reason") or "")[:500],
                        canonical_chunk_ids=[matched_chunk_id],
                        local_prior=True,
                        new_this_run=False,
                        acquisition_status=(
                            AcquisitionStatus.abstract_only
                            if is_abstract else AcquisitionStatus.fulltext
                        ),
                        section_id=ctx.section_id,
                        not_usable_for=list(candidate.get("not_usable_for") or []),
                        **_source_route_fields(
                            candidate=candidate,
                            scope_fit=str(candidate.get("scope_fit") or "unreviewed"),
                            local=True,
                            abstract_only=is_abstract,
                            chunk_ids=[matched_chunk_id],
                        ),
                    )
                    sources.append(source)
                    source_by_key[key] = source
                    seen_pid_role.add(key)
        except Exception as exc:
            logger.warning("_build_source_ledger: audited local KB entries failed: %s", exc)

    # All approved adjacent sources remain in the section artifact for
    # downstream explanation.  The local controller never discards useful
    # adjacent/contextual material; coverage counting still distinguishes
    # direct from adjacent/contextual scope.
    adjacent_paper_ids = {
        source.paper_id
        for source in sources
        if source.scope_fit == ScopeFit.adjacent
    }
    direct_paper_ids = {
        source.paper_id
        for source in sources
        if source.scope_fit == ScopeFit.direct
    }
    _write_artifact(
        ctx.work_dir,
        "SECTION_SOURCE_SELECTION.json",
        {
            "schema_version": "research_harness.source_selection.v1",
            "section_id": ctx.section_id,
            "direct_paper_count": len(direct_paper_ids),
            "adjacent_paper_limit": None,
            "selected_adjacent_paper_ids": sorted(adjacent_paper_ids),
            "excluded_adjacent_paper_ids": [],
            "exclusion_reason": (
                "Approved adjacent records are retained in the section "
                "artifacts for background/explanatory synthesis; none are "
                "discarded by the local controller."
            ),
        },
    )

    ledger = SectionSourceLedger(
        section_id=ctx.section_id,
        sources=sources,
        total_sources=len(sources),
        new_sources=sum(1 for s in sources if s.new_this_run),
        local_prior_sources=sum(1 for s in sources if s.local_prior),
    )
    _write_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json", ledger)


# ---------------------------------------------------------------------------
# 12. submit_section_gap_report
# ---------------------------------------------------------------------------

def _make_submit_section_gap_report(ctx: SectionCoverageContext):
    def submit_section_gap_report(gap_report_json: str) -> str:
        """Write or merge-update SECTION_GAP_REPORT.json with structured gap entries.

        Accepts a JSON object matching the SectionGapReport schema. If the file
        already exists, new gap entries are appended (de-duplicated by role).
        The overall_coverage_status field is always overwritten by the caller's value.

        Args:
            gap_report_json: JSON string with fields:
                - gaps: list of {role, severity, description, queries_attempted,
                         candidates_found, candidates_approved, candidates_materialized,
                         stop_reason, suggested_followup, is_blocking}
                - overall_coverage_status: "coverage_sufficient" | "completed_with_open_gaps"
                                           | "blocking_gaps_remain"
                - stop_conditions_met: list of strings (optional)

        Returns JSON with {"status": "ok", "blocking_gap_count": N, "open_gap_count": M}.
        """
        decoded = decode_json_payload(gap_report_json, expected="object")
        if decoded.error:
            return json.dumps({"status": "error", "error": decoded.error})
        incoming = decoded.value
        if not isinstance(incoming, dict):
            return json.dumps({"status": "error", "error": "gap_report_json must be a JSON object"})
        raw_gaps = incoming.get("gaps", [])
        if not isinstance(raw_gaps, list):
            return json.dumps({"status": "error", "error": "gaps must be a JSON array"})

        report_path = ctx.work_dir / "SECTION_GAP_REPORT.json"
        if report_path.exists():
            try:
                existing = SectionGapReport.model_validate(
                    json.loads(report_path.read_text(encoding="utf-8"))
                )
            except Exception:
                existing = SectionGapReport(section_id=ctx.section_id)
        else:
            existing = SectionGapReport(section_id=ctx.section_id)

        existing_roles = {g.role for g in existing.gaps}
        errors: List[str] = []
        for gap_raw in raw_gaps:
            try:
                ge = GapEntry(**gap_raw)
                if ge.role not in existing_roles:
                    existing.gaps.append(ge)
                    existing_roles.add(ge.role)
                else:
                    # Overwrite existing entry for same role
                    existing.gaps = [ge if g.role == ge.role else g for g in existing.gaps]
            except Exception as exc:
                errors.append(f"invalid gap entry: {str(exc)[:180]}")

        if errors and not existing.gaps and raw_gaps:
            return json.dumps({"status": "error", "errors": errors})

        if "overall_coverage_status" in incoming:
            existing.overall_coverage_status = incoming["overall_coverage_status"]
        if "stop_conditions_met" in incoming:
            existing.stop_conditions_met = incoming["stop_conditions_met"]

        existing.blocking_gap_count = sum(1 for g in existing.gaps if g.is_blocking)
        existing.open_gap_count = len(existing.gaps)

        _write_artifact(ctx.work_dir, "SECTION_GAP_REPORT.json", existing)
        return json.dumps({
            "status": "ok",
            "blocking_gap_count": existing.blocking_gap_count,
            "open_gap_count": existing.open_gap_count,
            "artifact": "SECTION_GAP_REPORT.json",
            "errors": errors,
            "json_recovered": bool(decoded.recovered),
        })

    return submit_section_gap_report


# ---------------------------------------------------------------------------
# Deterministic closure helpers
# ---------------------------------------------------------------------------

def _persisted_materialization_attempts(ctx: SectionCoverageContext) -> int:
    """Return the durable unique-paper count for the current section.

    The in-memory counter is fast during one ReAct pass; the manifest is the
    source of truth across checkpoint/resume. Count attempts rather than only
    successes so a repeatedly inaccessible OA paper cannot consume an
    unbounded number of retries.
    """

    manifest = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
    persisted = len({
        str(paper.get("candidate_id") or "").strip()
        for paper in manifest.get("papers", [])
        if isinstance(paper, dict)
        and str(paper.get("candidate_id") or "").strip()
    })
    with ctx._store_lock:
        ctx._papers_materialized_total = max(
            int(ctx._papers_materialized_total), persisted
        )
        return int(ctx._papers_materialized_total)


def _bounded_materialization_limit_reached(ctx: SectionCoverageContext) -> bool:
    return (
        _persisted_materialization_attempts(ctx)
        >= int(ctx.min_mode_max_total_papers)
    )


def _document_bounded_materialization_gaps(
    ctx: SectionCoverageContext,
) -> bool:
    """Record unresolved roles honestly once the legal-OA paper cap is hit.

    This is an operational stop, not an evidentiary upgrade. It converts the
    residual absence into explicit, non-blocking open gaps so a downstream
    author can preserve the limitation or a later feedback run can reopen it.
    It never runs for topic-misalignment or malformed-artifact failures.
    """

    context_data = _read_artifact(ctx.work_dir, "SECTION_CONTEXT.json") or {}
    required_roles = list(context_data.get("required_roles") or [])
    if not required_roles:
        required_roles = list(ctx.section_data.get("required_roles") or [])
    if not required_roles:
        required_roles = list(COVERAGE_ROLES)

    source_ledger = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
    accepted_sources = [
        source
        for source in source_ledger.get("sources", [])
        if isinstance(source, dict)
        and source.get("scope_fit") in ("direct", "adjacent")
        and source.get("canonical_chunk_ids")
    ]
    covered_roles = {
        str(source.get("literature_role") or "")
        for source in accepted_sources
        if _role_material_is_coverage_eligible(
            source, str(source.get("literature_role") or "")
        )
    }
    unresolved_roles = [
        role for role in required_roles if role not in covered_roles
    ]

    unique_sources = {
        str(source.get("paper_id") or "")
        for source in accepted_sources
        if str(source.get("paper_id") or "")
    }
    direct_sources = {
        str(source.get("paper_id") or "")
        for source in accepted_sources
        if (
            str(source.get("paper_id") or "")
            and source.get("scope_fit") == "direct"
        )
    }
    breadth = ctx.coverage_breadth_targets()
    if (
        len(unique_sources) < breadth["minimum_unique_sources"]
        or len(direct_sources) < breadth["minimum_direct_sources"]
    ):
        unresolved_roles.append("coverage_breadth")
    unresolved_roles = list(dict.fromkeys(unresolved_roles))
    if not unresolved_roles:
        return False

    candidate_ledger = _read_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json") or {}
    candidates = [
        item for item in candidate_ledger.get("candidates", [])
        if isinstance(item, dict)
    ]
    manifest = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
    papers = [
        item for item in manifest.get("papers", [])
        if isinstance(item, dict)
    ]
    search_budget = _read_artifact(ctx.work_dir, SEARCH_BUDGET_LEDGER) or {}
    rounds = [
        item for item in search_budget.get("rounds", [])
        if isinstance(item, dict)
    ]
    cap = int(ctx.min_mode_max_total_papers)
    attempts = _persisted_materialization_attempts(ctx)
    gaps: List[Dict[str, Any]] = []
    for role in unresolved_roles:
        role_rounds = rounds if role == "coverage_breadth" else [
            item for item in rounds if str(item.get("role") or "") == role
        ]
        queries = list(dict.fromkeys(
            str(query).strip()
            for item in role_rounds
            for query in (item.get("queries") or [])
            if str(query).strip()
        ))
        if not queries:
            queries = list(dict.fromkeys(
                str(item.get("query") or "").strip()
                for item in _coverage_query_targets(ctx)
                if str(item.get("query") or "").strip()
                and (
                    role == "coverage_breadth"
                    or str(item.get("role") or "") == role
                )
            ))
        role_candidates = candidates if role == "coverage_breadth" else [
            candidate
            for candidate in candidates
            if (
                str(candidate.get("role") or "") == role
                or role in (candidate.get("role_fit") or [])
            )
        ]
        role_papers = papers if role == "coverage_breadth" else [
            paper for paper in papers
            if str(paper.get("role") or "") == role
        ]
        if role == "coverage_breadth":
            description = (
                "The bounded legal-OA acquisition pass ended before the "
                "chapter reached its independent-source breadth target."
            )
            followup = (
                "A later targeted return may add a small number of directly "
                "relevant OA papers; retain the present breadth limitation "
                "until then."
            )
        else:
            description = (
                f"The bounded legal-OA acquisition pass did not establish "
                f"defensible {role} coverage for this section."
            )
            followup = (
                "Reopen only if this role becomes load-bearing in the "
                "article argument, using a targeted OA or human-supplied "
                "full-text route."
            )
        gaps.append({
            "role": role,
            "severity": "important",
            "description": description,
            "queries_attempted": queries,
            "candidates_found": len(role_candidates),
            "candidates_approved": sum(
                canonical_candidate_decision(candidate).decision == "approved"
                for candidate in role_candidates
            ),
            "candidates_materialized": len(role_papers),
            "stop_reason": (
                "bounded_oa_materialization_limit_reached: "
                f"{attempts}/{cap} unique candidate attempts"
            ),
            "suggested_followup": followup,
            "is_blocking": False,
        })

    submitted = _make_submit_section_gap_report(ctx)(
        json.dumps(
            {
                "gaps": gaps,
                "overall_coverage_status": "completed_with_open_gaps",
                "stop_conditions_met": [
                    "bounded_oa_materialization_limit_reached",
                    "open_gaps_preserved_without_fabrication",
                ],
            },
            ensure_ascii=False,
        )
    )
    try:
        return json.loads(submitted).get("status") == "ok"
    except Exception:
        return False


def _document_context_budget_gaps(ctx: SectionCoverageContext) -> bool:
    """Close the current structural package before the next model call.

    This is deliberately separate from the OA-paper cap: a context admission
    stop must not be reported as a materialisation stop.  All entries are
    non-blocking open gaps, so downstream status can be ``needs_more_literature``
    while preserving a usable, structurally validated package.
    """

    source_ledger = _read_artifact(ctx.work_dir, "SECTION_SOURCE_LEDGER.json") or {}
    accepted = [
        item for item in source_ledger.get("sources", []) or []
        if isinstance(item, dict)
        and item.get("scope_fit") in {"direct", "adjacent"}
        and item.get("canonical_chunk_ids")
    ]
    required_roles = list(
        (_read_artifact(ctx.work_dir, "SECTION_CONTEXT.json") or {}).get("required_roles")
        or ctx.section_data.get("required_roles")
        or []
    )
    covered_roles = {
        str(item.get("literature_role") or "")
        for item in accepted
        if _role_material_is_coverage_eligible(
            item, str(item.get("literature_role") or "")
        )
    }
    unresolved = [role for role in required_roles if role not in covered_roles]
    breadth = ctx.coverage_breadth_targets()
    unique = {str(item.get("paper_id") or "") for item in accepted if item.get("paper_id")}
    direct = {
        str(item.get("paper_id") or "")
        for item in accepted
        if item.get("paper_id") and item.get("scope_fit") == "direct"
    }
    if len(unique) < breadth["minimum_unique_sources"] or len(direct) < breadth["minimum_direct_sources"]:
        unresolved.append("coverage_breadth")
    unresolved = list(dict.fromkeys(unresolved))
    if not unresolved:
        return False
    targets = _coverage_query_targets(ctx)
    candidate_ledger = _read_artifact(ctx.work_dir, "OA_CANDIDATE_LEDGER.json") or {}
    candidates = [item for item in candidate_ledger.get("candidates", []) or [] if isinstance(item, dict)]
    manifest = _read_artifact(ctx.work_dir, "MATERIALIZATION_MANIFEST.json") or {}
    papers = [item for item in manifest.get("papers", []) or [] if isinstance(item, dict)]
    gaps: List[Dict[str, Any]] = []
    for role in unresolved:
        role_targets = [
            item for item in targets
            if role == "coverage_breadth" or str(item.get("role") or "") == role
        ]
        role_candidates = [
            item for item in candidates
            if role == "coverage_breadth"
            or str(item.get("role") or "") == role
            or role in (item.get("role_fit") or [])
        ]
        role_papers = [
            item for item in papers
            if role == "coverage_breadth" or str(item.get("role") or "") == role
        ]
        gaps.append({
            "role": role,
            "severity": "important",
            "description": (
                "The compact context budget was admitted before another model "
                f"call; defensible {role} evidence remains unresolved."
            ),
            "queries_attempted": list(dict.fromkeys(
                str(item.get("query") or "") for item in role_targets if item.get("query")
            )),
            "candidates_found": len(role_candidates),
            "candidates_approved": sum(
                1 for item in role_candidates if canonical_candidate_decision(item).decision == "approved"
            ),
            "candidates_materialized": len(role_papers),
            "stop_reason": "context_budget_admission_before_next_model_call",
            "suggested_followup": (
                "Run a targeted coverage wave or provide human-supplied full text; "
                "do not treat structural completion as scientific readiness."
            ),
            "is_blocking": False,
        })
    submitted = _make_submit_section_gap_report(ctx)(
        json.dumps({
            "gaps": gaps,
            "overall_coverage_status": "completed_with_open_gaps",
            "stop_conditions_met": [
                "context_budget_admission_before_next_model_call",
                "open_gaps_preserved_without_fabrication",
            ],
        }, ensure_ascii=False)
    )
    try:
        return json.loads(submitted).get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Provider class — assembles all 12 tools
# ---------------------------------------------------------------------------

SECTION_COVERAGE_TOOL_NAMES = [
    "load_section_context",
    "inspect_section_local_coverage",
    "query_review_knowledge_base",
    "inspect_local_candidate_batch",
    "submit_local_source_audit",
    "submit_literature_role_plan",
    "search_oa_candidates",
    "inspect_candidate_batch",
    "submit_candidate_audit",
    "trace_seed_references",
    "acquire_and_materialize_oa_papers",
    "refresh_section_coverage",
    "validate_section_coverage_package",
    "submit_section_gap_report",
]


class SectionCoverageToolProvider(ToolProvider):
    """Builds all 11 section-coverage FunctionTools bound to a SectionCoverageContext."""

    def __init__(self, ctx: SectionCoverageContext) -> None:
        self._ctx = ctx

    def get_tools(self, work_dir: Path) -> list:
        ctx = self._ctx
        def budgeted(function):
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                result = function(*args, **kwargs)
                if isinstance(result, str):
                    _record_agent_payload(ctx, result)
                return result
            return wrapped

        return [
            FunctionTool(budgeted(_make_load_section_context(ctx))),
            FunctionTool(budgeted(_make_inspect_section_local_coverage(ctx))),
            FunctionTool(budgeted(_make_query_review_knowledge_base(ctx))),
            FunctionTool(budgeted(_make_inspect_local_candidate_batch(ctx))),
            FunctionTool(budgeted(_make_submit_local_source_audit(ctx))),
            FunctionTool(budgeted(_make_submit_literature_role_plan(ctx))),
            FunctionTool(budgeted(_make_search_oa_candidates(ctx))),
            FunctionTool(budgeted(_make_inspect_candidate_batch(ctx))),
            FunctionTool(budgeted(_make_submit_candidate_audit(ctx))),
            FunctionTool(budgeted(_make_trace_seed_references(ctx))),
            FunctionTool(budgeted(_make_acquire_and_materialize_oa_papers(ctx))),
            FunctionTool(budgeted(_make_refresh_section_coverage(ctx))),
            FunctionTool(budgeted(_make_validate_section_coverage_package(ctx))),
            FunctionTool(budgeted(_make_submit_section_gap_report(ctx))),
        ]

    def get_allowed_tool_names(self) -> List[str]:
        return list(SECTION_COVERAGE_TOOL_NAMES)

    def try_auto_finalize(self) -> Optional[str]:
        """Close a coverage package as soon as durable evidence is sufficient.

        This hook runs after every completed tool invocation. It is the
        deterministic counterpart to the researcher's stop instruction: once
        an approved source has been materialized, refresh and validate before
        the model can request another slow OA download. If the bounded
        acquisition cap has been exhausted, it records transparent open gaps
        and validates again; it never documents around a topic-alignment or
        artifact-integrity failure.
        """

        required = (
            "SECTION_CONTEXT.json",
            "SECTION_COVERAGE_PLAN.json",
            "LOCAL_COVERAGE_AUDIT.json",
        )
        if not all((self._ctx.work_dir / name).exists() for name in required):
            return None
        try:
            cumulative_budget = int(
                getattr(self._ctx, "context_cumulative_budget_tokens", 0) or 0
            )
            per_call_budget = int(
                getattr(self._ctx, "context_per_call_budget_tokens", 0) or 0
            )
            payload_tokens = int(
                getattr(self._ctx, "_coverage_payload_tokens", 0) or 0
            )
            reserve = int(
                getattr(self._ctx, "context_output_reserve_tokens", 0) or 0
            )
            if (
                cumulative_budget > 0
                and per_call_budget > 0
                and payload_tokens + per_call_budget + reserve >= cumulative_budget
            ):
                if _document_context_budget_gaps(self._ctx):
                    _make_refresh_section_coverage(self._ctx)()
                    validation = _make_validate_section_coverage_package(self._ctx)()
                    if "VALIDATION_PASSED" in validation:
                        return validation
            _make_refresh_section_coverage(self._ctx)()
            validation = _make_validate_section_coverage_package(self._ctx)()
        except Exception:
            logger.exception(
                "SectionCoverageToolProvider auto-finalization failed for %s",
                self._ctx.section_id,
            )
            return None
        if "VALIDATION_PASSED" in validation:
            return validation
        if (
            "VALIDATION_FAILED" in validation
            and _bounded_materialization_limit_reached(self._ctx)
            and "no documented stop reason:" in validation
        ):
            if _document_bounded_materialization_gaps(self._ctx):
                _make_refresh_section_coverage(self._ctx)()
                validation = _make_validate_section_coverage_package(self._ctx)()
                if "VALIDATION_PASSED" in validation:
                    return validation
        return None


def build_section_coverage_toolkit(ctx: SectionCoverageContext) -> tuple:
    """Convenience: return (tools_list, tool_name_list) for a SectionCoverageContext."""
    provider = SectionCoverageToolProvider(ctx)
    tools = provider.get_tools(ctx.work_dir)
    return tools, provider.get_allowed_tool_names()
