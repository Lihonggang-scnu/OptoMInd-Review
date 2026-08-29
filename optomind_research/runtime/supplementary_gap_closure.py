"""Durable one-wave supplementary gap-closure coordination layer.

The coordinator is a separate SQLite-backed ingress/worker queue.  Enqueueing
a review gap only writes durable job state: no query generation, network,
model, retrieval, materialization, cache merge, progress recheck, or revision
path runs until ``process_next`` executes.  The existing
:class:`SupplementaryRetrievalPipeline` remains the retrieval engine,
``merge_material_cache`` remains the only cache merge path, and both progress
assessment and the revision/review path are injected and never invoked from
enqueue.

Approved one-wave policy
------------------------
Every job is allowed exactly one retrieval wave (``retrieval_wave_count``
with ``max_retrieval_waves=1``).  Progress is outcome-level, never
paper-count: a wave that closes a claim ends ``closed``; a wave that gains
material without closing ends ``improved_stop`` with residual reviewer
comments retained; a wave with no outcome-level progress never searches
again and ends ``revision_required`` after the injected revision/review path
narrows, qualifies, rewrites, or deletes the claim using current evidence.
Recheck/revision results are persisted per affected target.  Terminal
statuses are never automatically requeued; crash recovery returns only
nonterminal jobs to ``queued`` and the pipeline/service idempotency handles
safe reuse of already-committed work.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

from .material_cache_merge import (
    MaterialCacheIncrement,
    merge_material_cache,
)
from .supplementary_retrieval_contract import (
    CONTEXT_FIELD_CATALOG,
    DEFAULT_PORTFOLIO_LIMITS,
    GAP_TYPE_REQUIRED_CONTEXT_FIELDS,
    ContextRegistry,
    SupplementaryRetrievalTask,
    task_fingerprint,
    validate_task_context,
)


SCHEMA_VERSION = "optomind.supplementary_gap_closure.v2"

STATUS_QUEUED = "queued"
STATUS_SUBMITTING = "submitting"
STATUS_RETRIEVING = "retrieving"
STATUS_MERGING = "merging"
STATUS_REVALIDATING = "revalidating"
STATUS_CLOSED = "closed"
STATUS_IMPROVED_STOP = "improved_stop"
STATUS_REVISION_REQUIRED = "revision_required"
STATUS_STILL_OPEN = "still_open"
STATUS_FAILED = "failed"
STATUS_NO_PROGRESS = "no_progress"

NONTERMINAL_STATUSES = {
    STATUS_SUBMITTING,
    STATUS_RETRIEVING,
    STATUS_MERGING,
    STATUS_REVALIDATING,
}

TERMINAL_STATUSES = {
    STATUS_CLOSED,
    STATUS_IMPROVED_STOP,
    STATUS_REVISION_REQUIRED,
    STATUS_STILL_OPEN,
    STATUS_FAILED,
    STATUS_NO_PROGRESS,
}

MAX_RETRIEVAL_WAVES = 1

NOTIFICATION_PENDING = "pending"
NOTIFICATION_ACKED = "acked"

PROGRESS_CLOSED = "closed"
PROGRESS_IMPROVED = "improved"
PROGRESS_NO_PROGRESS = "no_progress"

_DDL = """
CREATE TABLE IF NOT EXISTS gap_closure_jobs (
    idempotency_key TEXT PRIMARY KEY,
    source_record_id TEXT NOT NULL,
    task_json TEXT NOT NULL,
    registry_json TEXT NOT NULL,
    source_provenance_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    attempts_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT,
    error TEXT,
    retrieval_wave_count INTEGER NOT NULL DEFAULT 0,
    max_retrieval_waves INTEGER NOT NULL DEFAULT 1,
    progress_assessment TEXT,
    next_action TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gap_closure_affected_targets (
    job_key TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    PRIMARY KEY (job_key, target_id, target_type)
);
CREATE TABLE IF NOT EXISTS gap_closure_notifications (
    notification_id TEXT PRIMARY KEY,
    job_key TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    status TEXT NOT NULL,
    cache_version TEXT NOT NULL,
    cache_path TEXT NOT NULL,
    closure_result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acked_at TEXT
);
CREATE TABLE IF NOT EXISTS gap_closure_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gap_jobs_status
    ON gap_closure_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_gap_targets_job
    ON gap_closure_affected_targets(job_key);
CREATE INDEX IF NOT EXISTS idx_gap_notifications_target
    ON gap_closure_notifications(target_id, status);
"""

_JOB_SCHEMA_ADDITIONS = {
    "retrieval_wave_count": "INTEGER NOT NULL DEFAULT 0",
    "max_retrieval_waves": "INTEGER NOT NULL DEFAULT 1",
    "progress_assessment": "TEXT",
    "next_action": "TEXT",
}


class GapClosureError(RuntimeError):
    """Raised for invalid coordinator usage or durable state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _slug(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return text or "gap"


def _normalize_targets(
    targets: Iterable[Any],
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in targets:
        if isinstance(raw, Mapping):
            target_id = str(raw.get("target_id") or "").strip()
            target_type = str(raw.get("target_type") or "").strip()
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            target_id = str(raw[0]).strip()
            target_type = str(raw[1]).strip()
        else:
            continue
        if target_id and target_type:
            key = (target_id, target_type)
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result


def _job_idempotency_key(
    *,
    source_record_id: str,
    task: SupplementaryRetrievalTask,
    registry: ContextRegistry,
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_record_id": source_record_id,
        "task_fingerprint": task_fingerprint(task, registry),
        "registry_fingerprint": hashlib.sha256(
            _canonical_json(registry.to_dict()).encode("utf-8")
        ).hexdigest(),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return "gap:" + digest[:40]


def _task_from_dict(payload: Mapping[str, Any]) -> SupplementaryRetrievalTask:
    return SupplementaryRetrievalTask(
        task_id=str(payload.get("task_id") or ""),
        gap_type=str(payload.get("gap_type") or ""),
        context_refs=tuple(payload.get("context_refs") or ()),
        priority=int(payload.get("priority") or 0),
        source_provenance=dict(payload.get("source_provenance") or {}),
        history_refs=tuple(payload.get("history_refs") or ()),
        success_criteria=tuple(payload.get("success_criteria") or ()),
        material_requirements=tuple(
            payload.get("material_requirements") or ()
        ),
        retrieval_queries=tuple(payload.get("retrieval_queries") or ()),
        visual_route=bool(payload.get("visual_route")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _context_registry_from_dict(payload: Mapping[str, Any]) -> ContextRegistry:
    return ContextRegistry.from_dict(payload)


def _materialization_policy(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and value:
        return dict(value)
    return {
        "priority": ["s2_structured_body", "public_oa_fulltext", "abstract_claim"],
        "abstract_background_only": True,
    }


_MISSING = object()


def _take(record: Mapping[str, Any], shared: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record:
            return copy.deepcopy(record[key])
        if key in shared:
            return copy.deepcopy(shared[key])
    return _MISSING


def _importance_priority(value: Any) -> int:
    return {"high": 8, "medium": 5, "low": 3}.get(
        str(value or "").strip().lower(), 5
    )


def _bounded_evidence_entries(summary: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not isinstance(summary, list):
        return entries
    for item in summary:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("raw_text") or item.get("text") or ""
        entries.append(
            {
                "chunk_id": str(item.get("chunk_id") or ""),
                "paper_id": str(item.get("paper_id") or ""),
                "title": str(item.get("title") or ""),
                "permission": str(item.get("permission") or ""),
                "source_kind": str(item.get("source_kind") or ""),
                "evidence": str(raw)[:2000],
            }
        )
    return entries


def _paper_identities(summary: Any) -> list[str]:
    identities: list[str] = []
    seen: set[str] = set()
    if not isinstance(summary, list):
        return identities
    for item in summary:
        if not isinstance(item, Mapping):
            continue
        paper_id = str(item.get("paper_id") or "").strip()
        if paper_id and paper_id not in seen:
            seen.add(paper_id)
            identities.append(paper_id)
    return identities


def _normalize_evidence_gap_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize real and legacy evidence-gap field aliases into one shape."""

    normalized = dict(record)
    normalized.setdefault(
        "failure_reason",
        str(
            record.get("why_current_evidence_fails")
            or record.get("reviewer_reason")
            or record.get("failure_reason")
            or ""
        ),
    )
    if not normalized.get("author_revision_suggestion"):
        normalized["author_revision_suggestion"] = str(
            record.get("required_revision_or_qualification") or ""
        )
    follow_up = record.get("follow_up_retrieval_task")
    follow_up = follow_up if isinstance(follow_up, Mapping) else {}
    if not (
        normalized.get("success_criteria")
        or normalized.get("retrieval_success_criteria")
    ) and follow_up.get("success_criteria"):
        normalized["success_criteria"] = [
            str(follow_up["success_criteria"]).strip()
        ]
    if not normalized.get("bound_papers_and_quotes"):
        normalized["bound_papers_and_quotes"] = _bounded_evidence_entries(
            record.get("current_evidence_summary")
        )
    if not normalized.get("existing_paper_identities"):
        normalized["existing_paper_identities"] = _paper_identities(
            record.get("current_evidence_summary")
        )
    strength = dict(record.get("required_material_strength") or {})
    required_evidence = str(record.get("required_evidence") or "")
    if required_evidence and not strength.get("required_evidence"):
        strength["required_evidence"] = required_evidence
    normalized["required_material_strength"] = strength
    feedback = dict(record.get("reviewer_feedback") or {})
    for key in (
        "disposition",
        "stop_reason",
        "iteration_count",
        "required_evidence",
        "current_evidence_ids",
        "importance",
        "suggested_routes",
        "suggested_source_types",
        "why_current_evidence_fails",
    ):
        if key in record and key not in feedback:
            feedback[key] = copy.deepcopy(record[key])
    if follow_up:
        feedback["follow_up_retrieval_task"] = {
            "objective": str(follow_up.get("objective") or ""),
            "search_intent": str(follow_up.get("search_intent") or ""),
            "query_hints": list(follow_up.get("query_hints") or []),
            "success_criteria": str(follow_up.get("success_criteria") or ""),
        }
    normalized["reviewer_feedback"] = feedback
    normalized["target_claim_or_sentence"] = dict(
        record.get("target_claim_or_sentence")
        or {
            "claim_id": str(record.get("claim_id") or ""),
            "component_id": str(record.get("component_id") or ""),
            "statement": str(
                record.get("claim_statement")
                or record.get("statement")
                or ""
            ),
        }
    )
    return normalized


def _task_and_registry_from_spec(
    spec: Mapping[str, Any],
) -> tuple[SupplementaryRetrievalTask, ContextRegistry]:
    registry = ContextRegistry()
    record = spec.get("record") if isinstance(spec.get("record"), Mapping) else {}
    shared = (
        spec.get("shared_context")
        if isinstance(spec.get("shared_context"), Mapping)
        else {}
    )
    gap_type = str(spec.get("gap_type") or "")
    if gap_type == "visual_material_gap":
        raise GapClosureError(
            "visual_material_gap is not supported by the textual coordinator"
        )
    provided: dict[str, Any] = {}
    value = _take(record, shared, "user_question")
    if value is not _MISSING:
        provided["user_question"] = str(value or "")
    value = _take(record, shared, "topic_scope")
    if value is not _MISSING:
        provided["topic_scope"] = dict(value or {})
    value = _take(record, shared, "dynamic_axes")
    if value is not _MISSING:
        provided["dynamic_axes"] = list(value or [])
    value = _take(record, shared, "missing_fact_units", "missing_fact_units_json")
    if value is not _MISSING:
        provided["missing_fact_units"] = list(value or [])
    value = _take(
        record, shared, "bound_papers_and_quotes", "bound_papers_quotes"
    )
    if value is not _MISSING:
        provided["bound_papers_and_quotes"] = list(value or [])
    strength_value = _take(
        record,
        shared,
        "required_material_strength",
        "required_factual_support_strength",
    )
    required_evidence = str(record.get("required_evidence") or "")
    if strength_value is not _MISSING or required_evidence:
        strength = (
            dict(strength_value or {})
            if strength_value is not _MISSING
            else {}
        )
        if required_evidence and not strength.get("required_evidence"):
            strength["required_evidence"] = required_evidence
        provided["required_material_strength"] = strength
    value = _take(record, shared, "success_criteria", "retrieval_success_criteria")
    if value is not _MISSING:
        provided["retrieval_success_criteria"] = list(value or [])
    value = _take(
        record, shared, "existing_paper_identities", "existing_identities"
    )
    if value is not _MISSING:
        provided["existing_paper_identities"] = list(value or [])
    provided["materialization_policy"] = _materialization_policy(
        record.get("materialization_policy")
        or shared.get("materialization_policy")
    )
    provided["portfolio_limits"] = dict(
        record.get("portfolio_limits")
        or shared.get("portfolio_limits")
        or DEFAULT_PORTFOLIO_LIMITS
    )
    if any(
        key in record
        for key in (
            "reviewer_feedback",
            "failure_reason",
            "reviewer_reason",
            "why_current_evidence_fails",
        )
    ):
        provided["reviewer_feedback"] = {
            "failure_reason": str(
                record.get("failure_reason")
                or record.get("reviewer_reason")
                or record.get("why_current_evidence_fails")
                or ""
            ),
            **dict(record.get("reviewer_feedback") or {}),
        }
    if any(
        key in record
        for key in (
            "author_revision_history",
            "author_revision_suggestion",
            "required_revision_or_qualification",
        )
    ):
        revision_history = record.get("author_revision_history")
        if not isinstance(revision_history, list):
            suggestion = str(
                record.get("author_revision_suggestion")
                or record.get("required_revision_or_qualification")
                or ""
            )
            revision_history = (
                [{"revision": 1, "outcome": "suggested", "note": suggestion}]
                if suggestion
                else []
            )
        provided["author_revision_history"] = list(revision_history)
    if any(
        key in record or key in spec
        for key in (
            "target_claim_or_sentence",
            "claim_id",
            "claim_statement",
            "statement",
            "component_id",
        )
    ):
        provided["target_claim_or_sentence"] = dict(
            record.get("target_claim_or_sentence")
            or {
                "claim_id": str(
                    record.get("claim_id") or spec.get("claim_id") or ""
                ),
                "component_id": str(record.get("component_id") or ""),
                "statement": str(
                    record.get("claim_statement")
                    or record.get("statement")
                    or ""
                ),
            }
        )
    for field_id in (
        "section_task",
        "argument_role",
        "current_review_structure",
        "paper_introduction_conclusion_excerpts",
        "whole_review_feedback",
        "visual_slots",
        "visual_gaps",
    ):
        if record.get(field_id) or shared.get(field_id):
            provided[field_id] = copy.deepcopy(
                record.get(field_id) or shared.get(field_id)
            )
    required = set(GAP_TYPE_REQUIRED_CONTEXT_FIELDS.get(gap_type, ()))
    missing = sorted(required - set(provided))
    if missing:
        raise GapClosureError(
            "job spec missing required projected context fields: "
            + ",".join(missing)
        )
    for field_id, value in provided.items():
        if field_id in CONTEXT_FIELD_CATALOG:
            registry.set(field_id, value)
    task_id = str(
        spec.get("task_id")
        or record.get("task_id")
        or f"gap-{record.get('gap_id') or spec.get('source_record_id') or uuid.uuid4().hex[:8]}"
    )
    task = SupplementaryRetrievalTask(
        task_id=task_id,
        gap_type=gap_type,
        context_refs=tuple(sorted(required)),
        priority=int(spec.get("priority") or 5),
        source_provenance={
            "producer": str(
                spec.get("producer") or "supplementary_gap_closure"
            ),
            "stage": "gap_closure_enqueue",
            "source_record": str(
                spec.get("source_record_id")
                or record.get("gap_id")
                or record.get("id")
                or ""
            ),
        },
        success_criteria=tuple(provided["retrieval_success_criteria"]),
        material_requirements=(
            "s2_structured_body",
            "public_oa_fulltext",
            "abstract_claim",
        ),
        retrieval_queries=(),
        visual_route=(gap_type == "visual_material_gap"),
        metadata=dict(spec.get("metadata") or {}),
    )
    errors = list(task.validate())
    errors.extend(validate_task_context(task, registry))
    if errors:
        raise GapClosureError("invalid gap job spec: " + "; ".join(errors))
    return task, registry.freeze()


def claim_evidence_gap_job_spec(
    record: Mapping[str, Any],
    *,
    shared_context: Mapping[str, Any] | None = None,
    producer: str = "supplementary_gap_closure",
) -> dict[str, Any]:
    """Generic adapter from a source-faithfulness evidence-gap record."""

    normalized = _normalize_evidence_gap_record(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "gap_type": "claim_evidence_gap",
        "source_record_id": str(
            normalized.get("gap_id") or normalized.get("id") or ""
        ),
        "claim_id": str(normalized.get("claim_id") or ""),
        "record": normalized,
        "shared_context": dict(shared_context or {}),
        "producer": producer,
        "priority": int(
            normalized.get("priority")
            or _importance_priority(normalized.get("importance"))
        ),
        "affected_targets": _normalize_targets(
            normalized.get("affected_targets")
            or [
                {
                    "target_id": str(normalized.get("claim_id") or ""),
                    "target_type": "claim",
                }
            ]
        ),
    }


def typed_gap_job_spec(
    record: Mapping[str, Any],
    *,
    shared_context: Mapping[str, Any] | None = None,
    producer: str = "supplementary_gap_closure",
) -> dict[str, Any]:
    """Generic adapter for already-typed section/structure/whole-review gaps."""

    gap_type = str(record.get("gap_type") or "").strip()
    if gap_type == "visual_material_gap":
        raise GapClosureError(
            "visual_material_gap is not supported by the textual coordinator"
        )
    if gap_type not in {
        "claim_evidence_gap",
        "section_argument_gap",
        "review_structure_gap",
        "whole_review_gap",
    }:
        raise GapClosureError(f"unsupported gap_type: {gap_type}")
    spec = claim_evidence_gap_job_spec(
        record,
        shared_context=shared_context,
        producer=producer,
    )
    spec["gap_type"] = gap_type
    return spec


def _report_fingerprint(payload: Mapping[str, Any]) -> str:
    subset = {
        "schema_version": payload.get("schema_version"),
        "probe_timestamp": payload.get("probe_timestamp"),
        "section_id": payload.get("section_id"),
        "section_title": payload.get("section_title"),
        "research_context": payload.get("research_context"),
        "evidence_gap_records": payload.get("evidence_gap_records"),
    }
    return hashlib.sha256(
        _canonical_json(subset).encode("utf-8")
    ).hexdigest()


def v19_claim_evidence_gap_job_specs(
    report: str | Path | Mapping[str, Any],
    *,
    shared_context_override: Mapping[str, Any] | None = None,
    producer: str = "v19_evidence_gap_adapter",
) -> list[dict[str, Any]]:
    """Build job specs from the authoritative S04 v19 probe report.

    v19 gap records carry no ``gap_id``/``id``.  Source and task identity are
    derived deterministically from the report fingerprint plus section,
    claim, and component ids, so re-enqueue is idempotent and a different
    (v20-like) report can never supersede v19's durable jobs.
    """

    if isinstance(report, (str, Path)):
        report_path = Path(report)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GapClosureError(
                f"cannot read gap-closure report {report_path}: {exc}"
            ) from exc
    elif isinstance(report, Mapping):
        payload = dict(report)
        report_path = None
    else:
        raise GapClosureError("report must be a path or mapping")
    if not isinstance(payload, Mapping):
        raise GapClosureError("report payload must be a mapping")
    section_id = str(payload.get("section_id") or "").strip()
    if not section_id:
        raise GapClosureError("report payload missing section_id")
    records = payload.get("evidence_gap_records")
    if not isinstance(records, list) or not records:
        raise GapClosureError("report payload has no evidence_gap_records")
    research = payload.get("research_context")
    research = research if isinstance(research, Mapping) else {}
    claims = payload.get("final_claims")
    by_claim: dict[str, Mapping[str, Any]] = {}
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, Mapping):
                claim_id = str(claim.get("claim_id") or "")
                if claim_id:
                    by_claim[claim_id] = claim
    fingerprint = _report_fingerprint(payload)
    shared = dict(shared_context_override or {})
    shared.setdefault(
        "user_question", str(research.get("user_question") or "")
    )
    shared.setdefault(
        "topic_scope",
        {
            "main_scope": str(
                research.get("scope_definition")
                or research.get("user_question")
                or ""
            ),
            "section_id": section_id,
            "section_title": str(payload.get("section_title") or ""),
            "problem_understanding": str(
                research.get("problem_understanding") or ""
            ),
            "provisional_section_title": str(
                research.get("provisional_section_title") or ""
            ),
            "provisional_argument_role": str(
                research.get("provisional_argument_role") or ""
            ),
            "key_questions": list(research.get("key_questions") or []),
            "report_fingerprint": fingerprint,
            "report_schema_version": str(payload.get("schema_version") or ""),
            "report_probe_timestamp": str(payload.get("probe_timestamp") or ""),
        },
    )
    shared.setdefault(
        "dynamic_axes",
        [
            {
                "axis_id": f"KQ{index + 1:02d}",
                "description": str(question),
            }
            for index, question in enumerate(research.get("key_questions") or [])
        ],
    )
    shared.setdefault("materialization_policy", None)
    specs: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, Mapping):
            raise GapClosureError(
                "evidence_gap_records must contain mappings"
            )
        record = dict(raw)
        claim_id = str(record.get("claim_id") or "").strip()
        component_id = str(
            record.get("component_id") or claim_id
        ).strip()
        if not claim_id or not component_id:
            raise GapClosureError(
                "evidence gap record missing claim_id/component_id"
            )
        claim = by_claim.get(claim_id) or by_claim.get(component_id) or {}
        record.setdefault(
            "claim_statement", str(claim.get("statement") or "")
        )
        record.setdefault("claim_role", str(claim.get("role") or ""))
        record.setdefault(
            "evidence_strength",
            str(
                record.get("evidence_strength")
                or record.get("required_evidence")
                or ""
            ),
        )
        record["report_fingerprint"] = fingerprint
        record["report_section_id"] = section_id
        record["report_probe_timestamp"] = str(
            payload.get("probe_timestamp") or ""
        )
        record["report_schema_version"] = str(payload.get("schema_version") or "")
        if report_path is not None:
            record["report_path"] = str(report_path)
        source_record_id = (
            f"{fingerprint}:{section_id}:{claim_id}:{component_id}"
        )
        spec = claim_evidence_gap_job_spec(
            record,
            shared_context=shared,
            producer=producer,
        )
        spec["source_record_id"] = source_record_id
        spec["task_id"] = (
            f"gap-{fingerprint[:10]}-{_slug(section_id)}-{_slug(component_id)}"
        )
        spec["priority"] = _importance_priority(record.get("importance"))
        spec["affected_targets"] = [
            {"target_id": component_id, "target_type": "claim"}
        ]
        spec["metadata"] = {
            "report_fingerprint": fingerprint,
            "report_section_id": section_id,
            "report_claim_id": claim_id,
            "report_component_id": component_id,
            "adapter": "v19_evidence_gap_adapter",
        }
        specs.append(spec)
    return specs


SIBLING_CONTEXT_MAX_SIBLINGS = 8
SIBLING_CONTEXT_MAX_QUOTES_PER_SIBLING = 3
SIBLING_CONTEXT_MAX_QUOTE_CHARS = 1200
SIBLING_CONTEXT_MAX_TOTAL_CHARS = 10000
_CLAIM_QUOTE_KEYS = (
    "verified_quotes",
    "bound_papers_and_quotes",
    "evidence_quotes",
    "quotes",
)
_CLAIM_CAVEAT_KEYS = (
    "caveats",
    "qualified_wording",
    "limitations",
    "reviewer_comments",
    "residual_reviewer_comments",
)


def _claim_verified_quote_records(
    claim: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Extract source-grounded verified quote records from a final claim."""

    records: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for key in _CLAIM_QUOTE_KEYS:
        raw = claim.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            quote = str(
                item.get("quote")
                or item.get("evidence")
                or item.get("raw_text")
                or item.get("text")
                or ""
            ).strip()
            if not quote:
                continue
            identity = item.get("identity")
            identity = identity if isinstance(identity, Mapping) else {}
            paper_id = str(
                item.get("paper_id")
                or item.get("doi")
                or identity.get("paper_id")
                or identity.get("doi")
                or ""
            )
            chunk_id = str(
                item.get("chunk_id")
                or identity.get("chunk_id")
                or ""
            )
            key_tuple = (paper_id, chunk_id, quote)
            if key_tuple in seen:
                continue
            seen.add(key_tuple)
            records.append({
                "quote": quote,
                "paper_id": paper_id,
                "chunk_id": chunk_id,
                "title": str(
                    item.get("title")
                    or identity.get("title")
                    or ""
                ),
            })
    return records


def _claim_caveats(claim: Mapping[str, Any]) -> list[str]:
    caveats: list[str] = []
    for key in _CLAIM_CAVEAT_KEYS:
        raw = claim.get(key)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            text = str(value or "").strip()
            if text and text not in caveats:
                caveats.append(text)
    return caveats[:10]


def _compact_sibling_verified_context(
    payload: Mapping[str, Any],
    claim: Mapping[str, Any],
    *,
    parent_claim_id: str = "",
) -> list[dict[str, Any]]:
    """Compact same-parent verified sibling context for one failed claim."""

    parent = str(parent_claim_id or claim.get("parent_claim_id") or "").strip()
    if not parent:
        return []
    claims = payload.get("final_claims")
    if not isinstance(claims, list):
        return []
    self_id = str(claim.get("claim_id") or "")
    siblings: list[dict[str, Any]] = []
    for candidate in claims:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = str(candidate.get("claim_id") or "")
        if not candidate_id or candidate_id == self_id:
            continue
        if str(candidate.get("parent_claim_id") or "").strip() != parent:
            continue
        quotes = _claim_verified_quote_records(candidate)
        if not quotes:
            continue
        siblings.append({
            "claim_id": candidate_id,
            "statement": str(candidate.get("statement") or ""),
            "role": str(
                candidate.get("role")
                or candidate.get("argument_role")
                or ""
            ),
            "evidence_strength": str(
                candidate.get("evidence_strength")
                or candidate.get("evidence_level")
                or ""
            ),
            "caveats": _claim_caveats(candidate),
            "verified_quotes": quotes[
                :SIBLING_CONTEXT_MAX_QUOTES_PER_SIBLING
            ],
        })
        if len(siblings) >= SIBLING_CONTEXT_MAX_SIBLINGS:
            break
    bounded: list[dict[str, Any]] = []
    total_chars = 0
    for entry in siblings:
        for quote in entry.get("verified_quotes") or []:
            text = str(quote.get("quote") or "")
            if len(text) > SIBLING_CONTEXT_MAX_QUOTE_CHARS:
                quote["quote"] = text[:SIBLING_CONTEXT_MAX_QUOTE_CHARS]
        size = len(_canonical_json(entry))
        if bounded and total_chars + size > SIBLING_CONTEXT_MAX_TOTAL_CHARS:
            break
        bounded.append(entry)
        total_chars += size
    return bounded


def _probe_shared_context(
    payload: Mapping[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    """Version-agnostic shared context cells from a frozen blueprint probe."""

    research = payload.get("research_context")
    research = research if isinstance(research, Mapping) else {}
    return {
        "user_question": str(research.get("user_question") or ""),
        "topic_scope": {
            "main_scope": str(
                research.get("scope_definition")
                or research.get("user_question")
                or ""
            ),
            "section_id": str(payload.get("section_id") or ""),
            "section_title": str(payload.get("section_title") or ""),
            "problem_understanding": str(
                research.get("problem_understanding") or ""
            ),
            "provisional_section_title": str(
                research.get("provisional_section_title") or ""
            ),
            "provisional_argument_role": str(
                research.get("provisional_argument_role") or ""
            ),
            "key_questions": list(research.get("key_questions") or []),
            "report_fingerprint": fingerprint,
            "report_schema_version": str(
                payload.get("schema_version") or ""
            ),
            "report_probe_timestamp": str(
                payload.get("probe_timestamp") or ""
            ),
        },
        "dynamic_axes": [
            {
                "axis_id": f"KQ{index + 1:02d}",
                "description": str(question),
            }
            for index, question in enumerate(
                research.get("key_questions") or []
            )
        ],
        "materialization_policy": None,
    }


def _selected_claim_gap_record(
    record: Mapping[str, Any],
    *,
    blocking_only: bool,
    include_nonblocking: bool,
    include_medium: bool,
    blocker_claim_ids: set[str] | None,
) -> bool:
    """Default blocker selection; ``include_nonblocking`` widens it."""

    gap_type = str(record.get("gap_type") or "").strip()
    if gap_type and gap_type != "claim_evidence_gap":
        return False
    disposition = str(record.get("disposition") or "").strip()
    importance = str(record.get("importance") or "").strip().lower()
    claim_id = str(record.get("claim_id") or "").strip()
    component_id = str(
        record.get("component_id") or claim_id
    ).strip()
    is_blocker = bool(
        blocker_claim_ids is not None
        and (
            claim_id in blocker_claim_ids
            or component_id in blocker_claim_ids
        )
    )
    if is_blocker:
        return disposition == "requires_new_evidence"
    if not include_nonblocking:
        if disposition != "requires_new_evidence":
            return False
        if blocker_claim_ids is not None:
            return is_blocker
        return importance == "high"
    if disposition not in {
        "requires_new_evidence",
        "salvageable_by_narrowing",
    }:
        return False
    if importance != "high" and not include_medium:
        return False
    return True


def build_claim_evidence_gap_specs_from_probe(
    report: str | Path | Mapping[str, Any],
    *,
    shared_context_override: Mapping[str, Any] | None = None,
    producer: str = "supplementary_gap_closure",
    blocking_only: bool = True,
    include_nonblocking: bool = False,
    include_medium: bool = False,
    blocker_claim_ids: set[str] | None = None,
    component_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Version-agnostic probe-to-job-spec builder for claim evidence gaps.

    Unlike the S04-v19 wrapper, this helper is not tied to a report schema
    version.  It reads ``evidence_gap_records`` (or the
    ``claim_evidence_gap_records`` alias), selects important
    ``requires_new_evidence`` blockers by default, and derives deterministic
    ``source_record_id``/``task_id`` from the probe fingerprint plus
    section/claim/component identity so re-enqueue is idempotent.
    """

    if isinstance(report, (str, Path)):
        report_path = Path(report)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GapClosureError(
                f"cannot read probe report {report_path}: {exc}"
            ) from exc
    elif isinstance(report, Mapping):
        payload = dict(report)
        report_path = None
    else:
        raise GapClosureError("report must be a path or mapping")
    if not isinstance(payload, Mapping):
        raise GapClosureError("probe payload must be a mapping")
    section_id = str(payload.get("section_id") or "").strip()
    if not section_id:
        raise GapClosureError("probe payload missing section_id")
    raw_records = payload.get("evidence_gap_records")
    if not isinstance(raw_records, list):
        raw_records = payload.get("claim_evidence_gap_records")
    if not isinstance(raw_records, list) or not raw_records:
        return []
    claims = payload.get("final_claims")
    by_claim: dict[str, Mapping[str, Any]] = {}
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, Mapping):
                claim_id = str(claim.get("claim_id") or "")
                if claim_id:
                    by_claim[claim_id] = claim
    fingerprint = _report_fingerprint(payload)
    identity_fingerprint = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    shared = dict(shared_context_override or {})
    for key, value in _probe_shared_context(payload, fingerprint).items():
        shared.setdefault(key, value)
    selected_ids = (
        {str(value).strip() for value in component_ids}
        if component_ids
        else None
    )
    specs: list[dict[str, Any]] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        claim_id = str(record.get("claim_id") or "").strip()
        component_id = str(
            record.get("component_id") or claim_id
        ).strip()
        if not claim_id or not component_id:
            continue
        if selected_ids is not None and component_id not in selected_ids:
            continue
        if not _selected_claim_gap_record(
            record,
            blocking_only=blocking_only,
            include_nonblocking=include_nonblocking,
            include_medium=include_medium,
            blocker_claim_ids=blocker_claim_ids,
        ):
            continue
        claim = by_claim.get(claim_id) or by_claim.get(component_id) or {}
        record.setdefault(
            "claim_statement", str(claim.get("statement") or "")
        )
        record.setdefault(
            "claim_role",
            str(
                record.get("claim_role")
                or claim.get("role")
                or claim.get("argument_role")
                or ""
            ),
        )
        record.setdefault(
            "evidence_strength",
            str(
                record.get("evidence_strength")
                or record.get("required_evidence")
                or claim.get("evidence_strength")
                or claim.get("evidence_level")
                or ""
            ),
        )
        record.setdefault(
            "parent_claim_id",
            str(claim.get("parent_claim_id") or ""),
        )
        record["sibling_verified_context"] = (
            _compact_sibling_verified_context(
                payload,
                claim,
                parent_claim_id=record.get("parent_claim_id") or "",
            )
        )
        record["report_fingerprint"] = fingerprint
        record["report_section_id"] = section_id
        record["report_probe_timestamp"] = str(
            payload.get("probe_timestamp") or ""
        )
        record["report_schema_version"] = str(
            payload.get("schema_version") or ""
        )
        if report_path is not None:
            record["report_path"] = str(report_path)
        if not (
            record.get("success_criteria")
            or record.get("retrieval_success_criteria")
            or (record.get("follow_up_retrieval_task") or {}).get(
                "success_criteria"
            )
        ):
            record["success_criteria"] = [
                "Exact verified source text supporting the missing fact units."
            ]
        source_record_id = (
            f"{identity_fingerprint}:{section_id}:{claim_id}:{component_id}"
        )
        spec = claim_evidence_gap_job_spec(
            record,
            shared_context=shared,
            producer=producer,
        )
        spec["source_record_id"] = source_record_id
        spec["task_id"] = (
            f"gap-{identity_fingerprint[:16]}-{_slug(section_id)}-"
            f"{_slug(component_id)}"
        )
        spec["priority"] = _importance_priority(record.get("importance"))
        spec["affected_targets"] = [
            {"target_id": component_id, "target_type": "claim"}
        ]
        spec["metadata"] = {
            "report_fingerprint": fingerprint,
            "report_section_id": section_id,
            "report_claim_id": claim_id,
            "report_component_id": component_id,
            "adapter": producer,
        }
        specs.append(spec)
    return specs


def _verify_run_identity(
    submission: Any,
    run: Any,
    task: SupplementaryRetrievalTask,
) -> tuple[bool, str]:
    """Require an unambiguous match between run result and dispatched task."""

    submission_key = str(
        getattr(submission, "idempotency_key", "") or ""
    ).strip()
    run_key = str(getattr(run, "idempotency_key", "") or "").strip()
    task_id = str(getattr(task, "task_id", "") or "").strip()
    run_task_id = str(getattr(run, "task_id", "") or "").strip()
    checks: list[tuple[bool, str]] = []
    if submission_key and run_key:
        checks.append(
            (
                run_key == submission_key,
                f"idempotency_key {run_key!r} != {submission_key!r}",
            )
        )
    if task_id and run_task_id:
        checks.append(
            (
                run_task_id == task_id,
                f"task_id {run_task_id!r} != {task_id!r}",
            )
        )
    if not checks:
        return False, (
            "run result carries no matching task identity "
            "(idempotency_key/task_id missing)"
        )
    failed = [reason for ok, reason in checks if not ok]
    if failed:
        return False, "run result identity mismatch: " + "; ".join(failed)
    return True, ""


def _overall_progress_verdict(
    per_target: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    values = {
        str(item.get("progress") or "")
        for item in per_target.values()
    }
    if values and values == {PROGRESS_CLOSED}:
        return STATUS_CLOSED
    if PROGRESS_NO_PROGRESS in values:
        return STATUS_REVISION_REQUIRED
    return STATUS_IMPROVED_STOP


def _next_action_from_revision(
    revision_results: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    actions = sorted(
        {
            str(item.get("next_action") or "").strip()
            for item in revision_results.values()
        }
        - {""}
    )
    return "revision:" + ",".join(actions) if actions else "revision_required"


_REVISION_ELIGIBLE_ACTIONS = frozenset(
    {"narrow", "qualify", "rewrite", "accept_reasoned_inference"}
)


def _revision_status_from_results(
    revision_results: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    """Return improved_stop only when every revision target is eligible."""

    if not revision_results:
        return STATUS_REVISION_REQUIRED
    for item in revision_results.values():
        action = str(item.get("next_action") or "").strip().casefold()
        revised = str(item.get("revised_claim") or "").strip()
        if action not in _REVISION_ELIGIBLE_ACTIONS or not revised:
            return STATUS_REVISION_REQUIRED
    return STATUS_IMPROVED_STOP


def _claim_revision_status_from_results(
    targets: Sequence[tuple[str, str]],
    revision_results: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str:
    """Promote scoped revisions only for claim-level gap targets."""

    if not targets or any(target_type != "claim" for _, target_type in targets):
        return STATUS_REVISION_REQUIRED
    return _revision_status_from_results(revision_results)


def _merge_per_target_revision(
    per_target: Mapping[tuple[str, str], Mapping[str, Any]],
    revision_results: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Merge revision output into revalidation results without overwriting.

    Revalidation fields such as ``progress``, ``reason``, exact quote matches,
    and locally validated quotes are preserved; revision fields fill only the
    gaps (``next_action``, ``revised_claim``, inference rationale, etc.).
    """

    combined = {
        target: dict(item) for target, item in per_target.items()
    }
    for target, revision_item in revision_results.items():
        if target not in combined:
            combined[target] = dict(revision_item)
            continue
        merged = dict(combined[target])
        for field, value in revision_item.items():
            if field not in merged or field in {
                "next_action",
                "revised_claim",
                "inference_rationale",
            }:
                merged[field] = value
        combined[target] = merged
    return combined


class SupplementaryGapClosureCoordinator:
    """SQLite-backed durable ingress and worker for gap closure."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        base_units_path: str | Path,
        base_vectors_path: str | Path,
        snapshot_root: str | Path,
        pipeline: Any | None = None,
        revalidator: Any | None = None,
        revision_callback: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_units_path = Path(base_units_path)
        self.base_vectors_path = Path(base_vectors_path)
        self.snapshot_root = Path(snapshot_root)
        self.pipeline = pipeline
        self.revalidator = revalidator
        self.revision_callback = revision_callback
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_DDL)
        self._ensure_job_schema()
        self._conn.execute(
            "INSERT OR IGNORE INTO gap_closure_meta(key, value) VALUES (?, ?)",
            ("current_snapshot", str(self.base_units_path.parent)),
        )
        self._conn.commit()

    def _ensure_job_schema(self) -> None:
        columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(gap_closure_jobs)")
        }
        for name, declaration in _JOB_SCHEMA_ADDITIONS.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE gap_closure_jobs ADD COLUMN {name} {declaration}"
                )

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def _transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def enqueue_gap_closure(
        self,
        *,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        affected_targets: Iterable[Any],
        source_provenance: Mapping[str, Any],
        source_record_id: str = "",
    ) -> dict[str, Any]:
        """Durably enqueue a job without any retrieval side effects."""

        if task.gap_type == "visual_material_gap":
            raise GapClosureError(
                "visual_material_gap is not supported by the textual coordinator"
            )
        targets = _normalize_targets(affected_targets)
        if not targets:
            raise GapClosureError("gap closure requires at least one affected target")
        key = _job_idempotency_key(
            source_record_id=source_record_id,
            task=task,
            registry=registry,
        )
        now = _now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM gap_closure_jobs WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                for target_id, target_type in targets:
                    connection.execute(
                        "INSERT OR IGNORE INTO gap_closure_affected_targets "
                        "(job_key, target_id, target_type) VALUES (?, ?, ?)",
                        (key, target_id, target_type),
                    )
                connection.execute(
                    "UPDATE gap_closure_jobs SET updated_at=? "
                    "WHERE idempotency_key=?",
                    (now, key),
                )
                return self._row_to_job(existing)
            connection.execute(
                "INSERT INTO gap_closure_jobs("
                "idempotency_key, source_record_id, task_json, registry_json, "
                "source_provenance_json, status, attempt_count, attempts_json, "
                "result_json, error, retrieval_wave_count, max_retrieval_waves, "
                "progress_assessment, next_action, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    source_record_id,
                    _json_dumps(task.to_dict()),
                    _json_dumps(registry.to_dict()),
                    _json_dumps(dict(source_provenance)),
                    STATUS_QUEUED,
                    0,
                    "[]",
                    None,
                    None,
                    0,
                    MAX_RETRIEVAL_WAVES,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            for target_id, target_type in targets:
                connection.execute(
                    "INSERT INTO gap_closure_affected_targets "
                    "(job_key, target_id, target_type) VALUES (?, ?, ?)",
                    (key, target_id, target_type),
                )
        row = self._conn.execute(
            "SELECT * FROM gap_closure_jobs WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        return self._row_to_job(row)

    def enqueue_gap_closure_spec(
        self,
        spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Enqueue a factory-produced job spec without external side effects."""

        task, registry = _task_and_registry_from_spec(spec)
        targets = _normalize_targets(spec.get("affected_targets"))
        if not targets:
            raise GapClosureError(
                "gap closure job spec requires at least one affected target"
            )
        return self.enqueue_gap_closure(
            task=task,
            registry=registry,
            affected_targets=targets,
            source_provenance={
                "producer": str(
                    spec.get("producer") or "supplementary_gap_closure"
                ),
                "stage": "gap_closure_enqueue",
                "source_record": str(
                    spec.get("source_record_id")
                    or (spec.get("record") or {}).get("gap_id")
                    or ""
                ),
            },
            source_record_id=str(spec.get("source_record_id") or ""),
        )

    def _row_to_job(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "idempotency_key": str(row["idempotency_key"]),
            "source_record_id": str(row["source_record_id"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"] or 0),
            "attempts": _json_loads(row["attempts_json"], []),
            "result": _json_loads(row["result_json"], None),
            "error": row["error"],
            "retrieval_wave_count": int(row["retrieval_wave_count"] or 0),
            "max_retrieval_waves": int(row["max_retrieval_waves"] or 1),
            "progress_assessment": _json_loads(
                row["progress_assessment"], None
            ),
            "next_action": row["next_action"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def get_job(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM gap_closure_jobs WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM gap_closure_jobs ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM gap_closure_jobs WHERE status=? "
                "ORDER BY created_at",
                (status,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def _append_event(
        self,
        connection: sqlite3.Connection,
        key: str,
        event: str,
        *,
        status: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT attempts_json FROM gap_closure_jobs WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        attempts = _json_loads(row["attempts_json"] if row else None, [])
        record: dict[str, Any] = {
            "event": event,
            "status": status,
            "at": _now(),
        }
        if extra:
            record.update(dict(extra))
        attempts.append(record)
        connection.execute(
            "UPDATE gap_closure_jobs SET attempts_json=?, updated_at=? "
            "WHERE idempotency_key=?",
            (_json_dumps(attempts), _now(), key),
        )

    def _set_status(
        self,
        connection: sqlite3.Connection,
        key: str,
        status: str,
        event: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
        extra: Mapping[str, Any] | None = None,
        progress_assessment: Mapping[str, Any] | None = None,
        next_action: str = "",
    ) -> None:
        self._append_event(connection, key, event, status=status, extra=extra)
        connection.execute(
            "UPDATE gap_closure_jobs SET status=?, result_json=?, error=?, "
            "progress_assessment=COALESCE(?, progress_assessment), "
            "next_action=COALESCE(?, next_action), updated_at=? "
            "WHERE idempotency_key=?",
            (
                status,
                _json_dumps(result) if result is not None else None,
                error or None,
                (
                    _json_dumps(dict(progress_assessment))
                    if progress_assessment is not None
                    else None
                ),
                next_action or None,
                _now(),
                key,
            ),
        )

    def _get_wave_count(self, key: str) -> int:
        row = self._conn.execute(
            "SELECT retrieval_wave_count FROM gap_closure_jobs "
            "WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    def _set_retrieval_wave_count(self, key: str, count: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE gap_closure_jobs SET retrieval_wave_count=?, "
                "updated_at=? WHERE idempotency_key=?",
                (int(count), _now(), key),
            )

    def process_next(self) -> dict[str, Any] | None:
        """Process one queued job through its single allowed retrieval wave."""

        claimed = self._claim_next()
        if claimed is None:
            return None
        key, task, registry, targets, wave_count, max_waves = claimed
        if self.pipeline is None:
            self._finish(
                key,
                STATUS_FAILED,
                "missing_pipeline",
                error="no pipeline injected",
            )
            return self.get_job(key)
        try:
            self._stage(key, STATUS_SUBMITTING, "submitting")
            if wave_count >= max_waves:
                return self._handle_wave_exhausted(
                    key, task, registry, targets, wave_count
                )
            self._set_retrieval_wave_count(key, wave_count + 1)
            submission = self.pipeline.generate_and_submit(task, registry)
            submission_status = str(getattr(submission, "status", "") or "")
            submission_reused = bool(getattr(submission, "reused", False))
            run = None
            if submission_reused and submission_status in {
                "committed",
                "no_progress",
            }:
                submission_key = str(
                    getattr(submission, "idempotency_key", "") or ""
                ).strip()
                if not submission_key:
                    self._finish(
                        key,
                        STATUS_FAILED,
                        "submission_identity_missing",
                        error="reused submission lacks idempotency_key",
                    )
                    return self.get_job(key)
                self._append_event(
                    self._conn,
                    key,
                    "submission_reused",
                    status=STATUS_RETRIEVING,
                    extra={"submission_status": submission_status},
                )
                run = SimpleNamespace(
                    status=submission_status,
                    reason=str(
                        getattr(submission, "reuse_reason", "")
                        or submission_status
                    ),
                    error="",
                    result=getattr(submission, "result", None),
                    idempotency_key=submission_key,
                    task_id=str(
                        getattr(submission, "task_id", "") or task.task_id
                    ),
                )
            elif submission_reused and submission_status == "failed":
                errors = tuple(getattr(submission, "errors", ()) or ())
                self._finish(
                    key,
                    STATUS_FAILED,
                    "pipeline_reuse_failed",
                    error="; ".join(errors) or "failed_requires_explicit_retry",
                )
                return self.get_job(key)
            else:
                self._stage(key, STATUS_RETRIEVING, "retrieving")
                runs = self.pipeline.run_pending(max_tasks=1)
                run = runs[0] if runs else None
                if run is not None:
                    ok, reason = _verify_run_identity(submission, run, task)
                    if not ok:
                        self._finish(
                            key,
                            STATUS_FAILED,
                            "run_identity_mismatch",
                            error=reason,
                        )
                        return self.get_job(key)
            if run is None:
                self._finish(
                    key,
                    STATUS_FAILED,
                    "missing_run_result",
                    error="run_pending returned no result",
                )
                return self.get_job(key)
            run_status = str(run.status)
            if run_status == "no_progress":
                return self._finish_no_progress(
                    key,
                    targets,
                    reason=str(run.reason or ""),
                )
            if run_status != "committed":
                self._finish(
                    key,
                    STATUS_FAILED,
                    f"unexpected_run_status:{run_status}",
                    error=str(run.error or ""),
                )
                return self.get_job(key)
            increment = self._extract_increment(run)
            if increment is None:
                self._finish(
                    key,
                    STATUS_FAILED,
                    "increment_paths_missing",
                    error="committed outcome metadata lacks valid increment paths",
                )
                return self.get_job(key)
            snapshot = self._merge_snapshot(key, increment)
            self._stage(
                key,
                STATUS_REVALIDATING,
                "revalidating",
                extra={"snapshot": str(snapshot)},
            )
            return self._assess_and_finish(key, targets, snapshot)
        except Exception as exc:
            self._finish(
                key,
                STATUS_FAILED,
                "worker_error",
                error=f"{type(exc).__name__}: {exc}",
            )
            return self.get_job(key)

    def _claim_next(
        self,
    ) -> tuple[
        str,
        SupplementaryRetrievalTask,
        ContextRegistry,
        list[tuple[str, str]],
        int,
        int,
    ] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM gap_closure_jobs WHERE status=? "
                "ORDER BY created_at LIMIT 1",
                (STATUS_QUEUED,),
            ).fetchone()
            if row is None:
                return None
            key = str(row["idempotency_key"])
            connection.execute(
                "UPDATE gap_closure_jobs SET attempt_count=attempt_count+1, "
                "status=?, updated_at=? WHERE idempotency_key=?",
                (STATUS_SUBMITTING, _now(), key),
            )
            self._append_event(
                connection, key, "started", status=STATUS_SUBMITTING
            )
            task = _task_from_dict(_json_loads(row["task_json"], {}))
            registry = _context_registry_from_dict(
                _json_loads(row["registry_json"], {})
            )
            target_rows = connection.execute(
                "SELECT target_id, target_type FROM gap_closure_affected_targets "
                "WHERE job_key=?",
                (key,),
            ).fetchall()
            targets = [
                (str(item[0]), str(item[1])) for item in target_rows
            ]
            return (
                key,
                task,
                registry,
                targets,
                int(row["retrieval_wave_count"] or 0),
                int(row["max_retrieval_waves"] or MAX_RETRIEVAL_WAVES),
            )

    def _stage(
        self,
        key: str,
        status: str,
        event: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        with self._transaction() as connection:
            self._set_status(connection, key, status, event, extra=extra)

    def _finish(
        self,
        key: str,
        status: str,
        event: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
        extra: Mapping[str, Any] | None = None,
        progress_assessment: Mapping[str, Any] | None = None,
        next_action: str = "",
    ) -> None:
        with self._transaction() as connection:
            self._set_status(
                connection,
                key,
                status,
                event,
                result=result,
                error=error,
                extra=extra,
                progress_assessment=progress_assessment,
                next_action=next_action,
            )

    def _handle_wave_exhausted(
        self,
        key: str,
        task: SupplementaryRetrievalTask,
        registry: ContextRegistry,
        targets: Sequence[tuple[str, str]],
        wave_count: int,
    ) -> dict[str, Any]:
        """Resume a recovered job without launching a second retrieval wave."""

        submission = self.pipeline.generate_and_submit(task, registry)
        status = str(getattr(submission, "status", "") or "")
        reused = bool(getattr(submission, "reused", False))
        submission_key = str(
            getattr(submission, "idempotency_key", "") or ""
        ).strip()
        if reused and status in {"committed", "no_progress"}:
            if not submission_key:
                self._finish(
                    key,
                    STATUS_FAILED,
                    "submission_identity_missing",
                    error="reused submission lacks idempotency_key",
                )
                return self.get_job(key)
            self._append_event(
                self._conn,
                key,
                "submission_reused",
                status=STATUS_RETRIEVING,
                extra={"submission_status": status},
            )
            run = SimpleNamespace(
                status=status,
                reason=str(
                    getattr(submission, "reuse_reason", "") or status
                ),
                error="",
                result=getattr(submission, "result", None),
                idempotency_key=submission_key,
                task_id=str(
                    getattr(submission, "task_id", "") or task.task_id
                ),
            )
            if status == "no_progress":
                return self._finish_no_progress(
                    key, targets, reason=str(run.reason or "")
                )
            increment = self._extract_increment(run)
            if increment is None:
                self._finish(
                    key,
                    STATUS_FAILED,
                    "increment_paths_missing",
                    error="committed replay metadata lacks valid increment paths",
                )
                return self.get_job(key)
            snapshot = self._merge_snapshot(key, increment)
            self._stage(
                key,
                STATUS_REVALIDATING,
                "revalidating",
                extra={"snapshot": str(snapshot)},
            )
            return self._assess_and_finish(key, targets, snapshot)
        if reused and status in {"queued", "running", "materializing"}:
            self._append_event(
                self._conn,
                key,
                "retrieval_wave_interrupted",
                status=STATUS_REVALIDATING,
                extra={
                    "wave_count": wave_count,
                    "pipeline_status": status,
                },
            )
            self._finish(
                key,
                STATUS_FAILED,
                "retrieval_wave_interrupted_pipeline_active",
                error=(
                    f"recovered job already spent its single retrieval wave "
                    f"and pipeline task is still {status}"
                ),
                next_action="manual_resume",
            )
            return self.get_job(key)
        self._append_event(
            self._conn,
            key,
            "retrieval_wave_interrupted",
            status=STATUS_REVALIDATING,
            extra={"wave_count": wave_count, "pipeline_status": status},
        )
        self._finish(
            key,
            STATUS_FAILED,
            "retrieval_wave_already_spent",
            error=(
                "recovered job already spent its single retrieval wave "
                "and pipeline has no replay"
            ),
            next_action="manual_resume",
        )
        return self.get_job(key)

    def _finish_no_progress(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        reason: str,
    ) -> dict[str, Any]:
        assessment = {
            "verdict": STATUS_REVISION_REQUIRED,
            "retrieval_wave_count": self._get_wave_count(key),
            "max_retrieval_waves": MAX_RETRIEVAL_WAVES,
            "reason": str(reason or "no_progress"),
        }
        return self._finish_revision_required(
            key,
            targets,
            snapshot=None,
            assessment=assessment,
            revision_reason=str(reason or "no_progress"),
        )

    def _assess_and_finish(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        snapshot: Path,
    ) -> dict[str, Any]:
        per_target = self._revalidate(key, targets, snapshot)
        verdict = _overall_progress_verdict(per_target)
        assessment = {
            "verdict": verdict,
            "retrieval_wave_count": self._get_wave_count(key),
            "max_retrieval_waves": MAX_RETRIEVAL_WAVES,
            "per_target": [dict(item) for item in per_target.values()],
        }
        if verdict == STATUS_CLOSED:
            result = {
                "snapshot_path": str(snapshot),
                "snapshot_version": snapshot.name,
                "merge_report_path": str(
                    snapshot / "LONG_TERM_CACHE_MERGE_REPORT.json"
                ),
                "progress_assessment": assessment,
                "per_target_results": [
                    dict(item) for item in per_target.values()
                ],
            }
            self._emit_notifications(key, targets, snapshot, per_target)
            self._finish(
                key,
                STATUS_CLOSED,
                STATUS_CLOSED,
                result=result,
                progress_assessment=assessment,
                next_action="closed",
            )
            return self.get_job(key)
        if verdict == STATUS_IMPROVED_STOP:
            revision_warning = ""
            revision_results: dict[tuple[str, str], dict[str, Any]] = {}
            try:
                if self.revision_callback is None:
                    revision_warning = (
                        "missing_revision_callback_improved_stop"
                    )
                else:
                    revision_results = self._revise(
                        key, targets, snapshot, per_target
                    )
            except Exception as exc:  # noqa: BLE001 - never destroy progress
                revision_warning = (
                    f"improved_stop_revision_failed:{exc}"
                )
            combined = _merge_per_target_revision(
                per_target, revision_results
            )
            result = {
                "snapshot_path": str(snapshot),
                "snapshot_version": snapshot.name,
                "merge_report_path": str(
                    snapshot / "LONG_TERM_CACHE_MERGE_REPORT.json"
                ),
                "progress_assessment": assessment,
                "per_target_results": [
                    dict(item) for item in combined.values()
                ],
                "revision": {
                    "reason": "improved_stop_local_revision",
                    "results": [
                        dict(item) for item in revision_results.values()
                    ],
                },
            }
            self._emit_notifications(key, targets, snapshot, combined)
            self._finish(
                key,
                STATUS_IMPROVED_STOP,
                STATUS_IMPROVED_STOP,
                result=result,
                progress_assessment=assessment,
                next_action="stop_improved",
                error=revision_warning,
            )
            return self.get_job(key)
        revision_targets = [
            (target_id, target_type)
            for target_id, target_type in targets
            if per_target[(target_id, target_type)].get("progress")
            == PROGRESS_NO_PROGRESS
        ]
        if not revision_targets:
            revision_targets = list(targets)
        revision_warning = ""
        try:
            revision_results = self._revise(
                key, revision_targets, snapshot, per_target
            )
        except Exception as exc:  # noqa: BLE001 - malformed/failed revision
            revision_warning = (
                f"no_progress_revision_failed:{type(exc).__name__}"
            )
            revision_results = {}
        combined = _merge_per_target_revision(
            per_target, revision_results
        )
        result = {
            "snapshot_path": str(snapshot),
            "snapshot_version": snapshot.name,
            "merge_report_path": str(
                snapshot / "LONG_TERM_CACHE_MERGE_REPORT.json"
            ),
            "progress_assessment": assessment,
            "per_target_results": [
                dict(item) for item in combined.values()
            ],
            "revision": {
                "reason": "no_outcome_level_progress",
                "results": [
                    dict(item) for item in revision_results.values()
                ],
            },
        }
        status = _claim_revision_status_from_results(
            revision_targets, revision_results
        )
        next_action = (
            "stop_improved"
            if status == STATUS_IMPROVED_STOP
            else _next_action_from_revision(revision_results)
        )
        self._emit_notifications(key, targets, snapshot, combined)
        self._finish(
            key,
            status,
            status,
            result=result,
            progress_assessment=assessment,
            next_action=next_action,
            error=revision_warning,
        )
        return self.get_job(key)

    def _finish_revision_required(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        *,
        snapshot: Path | None,
        assessment: Mapping[str, Any],
        revision_reason: str,
    ) -> dict[str, Any]:
        if self.revision_callback is None:
            self._finish(
                key,
                STATUS_REVISION_REQUIRED,
                "missing_revision_callback",
                error=(
                    "missing_revision_callback_revision_required"
                ),
                progress_assessment=dict(assessment),
                next_action="revision_required",
            )
            return self.get_job(key)
        assessment_per_target: dict[
            tuple[str, str], dict[str, Any]
        ] = {}
        for item in assessment.get("per_target") or []:
            if not isinstance(item, Mapping):
                continue
            target_id = str(item.get("target_id") or "")
            target_type = str(item.get("target_type") or "")
            if target_id and target_type:
                assessment_per_target[(target_id, target_type)] = dict(item)
        revision_warning = ""
        try:
            revision_results = self._revise(
                key,
                targets,
                snapshot,
                assessment_per_target or None,
            )
        except Exception as exc:  # noqa: BLE001 - malformed/failed revision
            revision_warning = (
                f"no_progress_revision_failed:{type(exc).__name__}"
            )
            revision_results = {}
        combined = _merge_per_target_revision(
            assessment_per_target, revision_results
        )
        result = {
            "snapshot_path": str(snapshot) if snapshot is not None else None,
            "snapshot_version": snapshot.name if snapshot is not None else None,
            "progress_assessment": dict(assessment),
            "per_target_results": [
                dict(item) for item in combined.values()
            ],
            "revision": {
                "reason": revision_reason,
                "results": [
                    dict(item) for item in revision_results.values()
                ],
            },
        }
        status = _claim_revision_status_from_results(
            targets, revision_results
        )
        next_action = (
            "stop_improved"
            if status == STATUS_IMPROVED_STOP
            else _next_action_from_revision(revision_results)
        )
        self._emit_notifications(key, targets, snapshot, combined)
        self._finish(
            key,
            status,
            status,
            result=result,
            progress_assessment=dict(assessment),
            next_action=next_action,
            error=revision_warning,
        )
        return self.get_job(key)

    def _extract_increment(
        self,
        run: Any,
    ) -> MaterialCacheIncrement | None:
        result = run.result if isinstance(run.result, Mapping) else {}
        materialization = result.get("materialization")
        metadata = (
            materialization.get("metadata")
            if isinstance(materialization, Mapping)
            else {}
        )
        metadata = metadata if isinstance(metadata, Mapping) else {}
        final_units_path = Path(str(metadata.get("final_units_path") or ""))
        vector_dir = Path(str(metadata.get("vector_dir") or ""))
        vectors_path = vector_dir / "material_vectors.sqlite"
        if not final_units_path.is_file() or not vectors_path.is_file():
            return None
        return MaterialCacheIncrement(
            units_path=final_units_path,
            vectors_path=vectors_path,
        )

    def _current_snapshot_path(self) -> Path:
        row = self._conn.execute(
            "SELECT value FROM gap_closure_meta WHERE key='current_snapshot'"
        ).fetchone()
        if row is not None:
            return Path(str(row[0]))
        return self.base_units_path.parent

    def _next_snapshot_dir(self) -> Path:
        highest = 0
        for path in self.snapshot_root.glob("snapshot-*"):
            if not path.is_dir():
                continue
            try:
                index = int(path.name.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                continue
            highest = max(highest, index)
        return self.snapshot_root / f"snapshot-{highest + 1:04d}"

    def _merge_snapshot(
        self,
        key: str,
        increment: MaterialCacheIncrement,
    ) -> Path:
        self._stage(
            key,
            STATUS_MERGING,
            "merging",
            extra={"increment": str(increment.units_path)},
        )
        snapshot = self._next_snapshot_dir()
        units_row = self._conn.execute(
            "SELECT value FROM gap_closure_meta WHERE key='current_units_path'"
        ).fetchone()
        vectors_row = self._conn.execute(
            "SELECT value FROM gap_closure_meta WHERE key='current_vectors_path'"
        ).fetchone()
        if units_row is not None and vectors_row is not None:
            base_units = Path(str(units_row[0]))
            base_vectors = Path(str(vectors_row[0]))
        else:
            base_units = self.base_units_path
            base_vectors = self.base_vectors_path
        merge_material_cache(
            base_units_path=base_units,
            base_vectors_path=base_vectors,
            increments=[increment],
            output_root=snapshot,
            supplementary_conflict_policy=True,
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO gap_closure_meta(key, value) VALUES (?, ?)",
            ("current_snapshot", str(snapshot)),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO gap_closure_meta(key, value) VALUES (?, ?)",
            (
                "current_units_path",
                str(snapshot / "MATERIAL_UNITS_FINAL.json"),
            ),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO gap_closure_meta(key, value) VALUES (?, ?)",
            (
                "current_vectors_path",
                str(snapshot / "material_vectors.sqlite"),
            ),
        )
        self._conn.commit()
        return snapshot

    @staticmethod
    def _normalize_per_target_results(
        result: Mapping[str, Any],
        targets: Sequence[tuple[str, str]],
        *,
        kind: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        raw_items = None
        if isinstance(result.get("results"), list):
            raw_items = result["results"]
        elif isinstance(result.get("targets"), Mapping):
            raw_items = [dict(item) for item in result["targets"].values()]
        if raw_items is None:
            raise GapClosureError(
                f"recheck result for {kind} must contain 'results' list "
                "or 'targets' mapping"
            )
        by_target: dict[tuple[str, str], dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            target_id = str(item.get("target_id") or "").strip()
            target_type = str(item.get("target_type") or "").strip()
            if target_id and target_type:
                by_target[(target_id, target_type)] = dict(item)
        if kind == "progress":
            allowed = {PROGRESS_CLOSED, PROGRESS_IMPROVED, PROGRESS_NO_PROGRESS}
            for item in by_target.values():
                if str(item.get("progress") or "") not in allowed:
                    raise GapClosureError(
                        "invalid per-target progress: "
                        f"{item.get('progress')!r}"
                    )
        elif kind == "revision":
            for item in by_target.values():
                if not str(item.get("next_action") or "").strip():
                    raise GapClosureError(
                        "per-target revision result missing next_action"
                    )
        missing = [
            f"{target_id}:{target_type}"
            for target_id, target_type in targets
            if (target_id, target_type) not in by_target
        ]
        if missing:
            raise GapClosureError(
                "missing per-target " + kind + " results: " + ",".join(missing)
            )
        return {
            target: by_target[target]
            for target in targets
        }

    def _revalidate(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        snapshot: Path,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if self.revalidator is None:
            raise GapClosureError(
                "no revalidator injected; cannot assess progress"
            )
        result = self.revalidator(
            job_key=key,
            affected_targets=list(targets),
            snapshot_path=str(snapshot),
            retrieval_wave_count=self._get_wave_count(key),
        )
        if not isinstance(result, Mapping):
            raise GapClosureError("revalidator must return a mapping")
        return self._normalize_per_target_results(
            result, targets, kind="progress"
        )

    def _revise(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        snapshot: Path | None,
        per_target: (
            Mapping[tuple[str, str], Mapping[str, Any]] | None
        ) = None,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if self.revision_callback is None:
            raise GapClosureError(
                "revision path required but no revision_callback injected"
            )
        callback_kwargs = dict(
            job_key=key,
            affected_targets=list(targets),
            snapshot_path=str(snapshot) if snapshot is not None else None,
        )
        if per_target is not None:
            callback_kwargs["per_target_results"] = per_target
        result = self.revision_callback(**callback_kwargs)
        if not isinstance(result, Mapping):
            raise GapClosureError("revision_callback must return a mapping")
        return self._normalize_per_target_results(
            result, targets, kind="revision"
        )

    def _emit_notifications(
        self,
        key: str,
        targets: Sequence[tuple[str, str]],
        snapshot: Path | None,
        per_target_results: Mapping[
            tuple[str, str], Mapping[str, Any]
        ],
    ) -> None:
        now = _now()
        if snapshot is not None:
            cache_version = snapshot.name
            cache_path = str(snapshot)
        else:
            current = self._current_snapshot_path()
            cache_version = current.name
            cache_path = str(current)
        with self._transaction() as connection:
            for target_id, target_type in targets:
                closure = per_target_results.get((target_id, target_type)) or {}
                notification_id = "notify:" + uuid.uuid4().hex[:20]
                connection.execute(
                    "INSERT INTO gap_closure_notifications("
                    "notification_id, job_key, target_id, target_type, status, "
                    "cache_version, cache_path, closure_result_json, "
                    "created_at, acked_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        notification_id,
                        key,
                        target_id,
                        target_type,
                        NOTIFICATION_PENDING,
                        cache_version,
                        cache_path,
                        _json_dumps(dict(closure)),
                        now,
                        None,
                    ),
                )

    def list_notifications(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if target_id is not None:
            clauses.append("target_id=?")
            params.append(target_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            "SELECT * FROM gap_closure_notifications" + where
            + " ORDER BY created_at",
            params,
        ).fetchall()
        return [
            {
                "notification_id": str(row["notification_id"]),
                "job_key": str(row["job_key"]),
                "target_id": str(row["target_id"]),
                "target_type": str(row["target_type"]),
                "status": str(row["status"]),
                "cache_version": str(row["cache_version"]),
                "cache_path": str(row["cache_path"]),
                "closure_result": _json_loads(row["closure_result_json"], {}),
                "created_at": str(row["created_at"]),
                "acked_at": row["acked_at"],
            }
            for row in rows
        ]

    def ack_notification(self, notification_id: str) -> bool:
        now = _now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE gap_closure_notifications SET status=?, acked_at=? "
                "WHERE notification_id=? AND status=?",
                (
                    NOTIFICATION_ACKED,
                    now,
                    notification_id,
                    NOTIFICATION_PENDING,
                ),
            )
            return cursor.rowcount > 0

    def recover(self) -> int:
        now = _now()
        recovered = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT idempotency_key, attempts_json FROM gap_closure_jobs "
                "WHERE status IN ("
                + ",".join("?" for _ in NONTERMINAL_STATUSES)
                + ")",
                tuple(sorted(NONTERMINAL_STATUSES)),
            ).fetchall()
            for row in rows:
                attempts = _json_loads(row["attempts_json"], [])
                attempts.append(
                    {
                        "event": "crash_recovery",
                        "status": STATUS_QUEUED,
                        "at": now,
                    }
                )
                connection.execute(
                    "UPDATE gap_closure_jobs SET status=?, attempts_json=?, "
                    "updated_at=? WHERE idempotency_key=?",
                    (
                        STATUS_QUEUED,
                        _json_dumps(attempts),
                        now,
                        row["idempotency_key"],
                    ),
                )
                recovered += 1
        return recovered


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_CLOSED",
    "STATUS_FAILED",
    "STATUS_IMPROVED_STOP",
    "STATUS_MERGING",
    "STATUS_NO_PROGRESS",
    "STATUS_QUEUED",
    "STATUS_RETRIEVING",
    "STATUS_REVALIDATING",
    "STATUS_REVISION_REQUIRED",
    "STATUS_STILL_OPEN",
    "STATUS_SUBMITTING",
    "MAX_RETRIEVAL_WAVES",
    "GapClosureError",
    "SupplementaryGapClosureCoordinator",
    "claim_evidence_gap_job_spec",
    "build_claim_evidence_gap_specs_from_probe",
    "typed_gap_job_spec",
    "v19_claim_evidence_gap_job_specs",
]
