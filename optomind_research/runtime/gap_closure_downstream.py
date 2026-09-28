"""Local downstream adapter from S04 gap-closure reports to writing context.

This adapter is deliberately pure and local.  It turns one or more S04
supplementary gap-closure reports into a deterministic writing/blueprint
context so the one-wave policy is reflected in downstream inputs.  It never
calls a model, never touches the network, and never starts a retrieval wave.

Only ``claim_evidence_gap`` tasks are applied as claim closures.
``section_argument_gap`` tasks are collected into a separate
``section_gap_summary`` and never change claim dispositions.
``review_structure_gap`` and ``whole_review_gap`` tasks (and any unknown or
untyped task) are ignored and recorded in ``ignored_non_claim_records``.

Status policy:

- ``closed`` -> ``write_ready`` (eligible downstream).
- ``improved_stop`` -> ``write_with_limits`` (eligible downstream; residual
  reviewer comments and revision/author records become compact
  ``writing_limits``).
- An explicit final action ``delete`` (task-level or per-target
  ``next_action``) overrides the outer status and forces
  ``revise_before_write``; a claim that must be deleted is never
  write-eligible.
- ``revision_required``, ``no_progress``, ``still_open``, ``failed``, and any
  nonterminal/unknown status -> ``revise_before_write`` (not eligible until a
  later local revision/recheck).

Reports may be supplied as paths or already-loaded mappings.  Later reports
override earlier records for the same claim/component id, which supports the
v3 report plus the c4.2/c14.2 recovery reports.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "optomind.gap_closure_downstream.v1"

STATUS_CLOSED = "closed"
STATUS_IMPROVED_STOP = "improved_stop"
STATUS_REVISION_REQUIRED = "revision_required"
STATUS_NO_PROGRESS = "no_progress"
STATUS_STILL_OPEN = "still_open"
STATUS_FAILED = "failed"

_DELETE_ACTION = "delete"
_WEAK_SUPPLEMENTARY_MARKERS = ("abstract", "tldr", "summary", "metadata")

DISPOSITION_WRITE_READY = "write_ready"
DISPOSITION_WRITE_WITH_LIMITS = "write_with_limits"
DISPOSITION_REVISE_BEFORE_WRITE = "revise_before_write"

CLAIM_GAP_TYPE = "claim_evidence_gap"
SECTION_GAP_TYPE = "section_argument_gap"
IGNORED_GAP_TYPES = frozenset({"review_structure_gap", "whole_review_gap"})

MAX_RETRIEVAL_WAVES = 1


class GapClosureDownstreamError(ValueError):
    """Raised when a gap-closure report cannot be normalized safely."""


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dedupe_texts(values: Sequence[Any]) -> list[str]:
    """Deduplicate texts by normalized (whitespace-folded, casefolded) form."""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "")
        key = _normalized_text(text)
        if not _text(text) or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _dedupe_mappings(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        item = dict(value)
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _flatten_report_inputs(reports: Any) -> list[Any]:
    if reports is None:
        return []
    if isinstance(reports, (str, Path)) or isinstance(reports, Mapping):
        return [reports]
    return list(reports)


def _load_one_report(raw: Any, label: str, index: int) -> dict[str, Any]:
    """Load one report path/mapping, or reuse an already loaded wrapper."""

    if isinstance(raw, Mapping):
        if "payload" in raw and "source_report" in raw and isinstance(raw.get("payload"), Mapping):
            payload = raw["payload"]
            try:
                source_index = int(raw.get("source_index") if raw.get("source_index") is not None else index)
            except (TypeError, ValueError):
                source_index = index
            return {
                "source_report": _text(raw.get("source_report")) or label,
                "source_index": source_index,
                "payload": copy.deepcopy(dict(payload)),
            }
        return {
            "source_report": label,
            "source_index": index,
            "payload": copy.deepcopy(dict(raw)),
        }
    if isinstance(raw, (str, Path)):
        path = Path(raw)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise GapClosureDownstreamError(
                f"cannot read gap-closure report {path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise GapClosureDownstreamError(
                f"gap-closure report {path} must contain a JSON object"
            )
        return {
            "source_report": str(path),
            "source_index": index,
            "payload": dict(payload),
        }
    raise GapClosureDownstreamError(
        f"unsupported report input: {type(raw).__name__}"
    )


def load_gap_closure_reports(
    reports: (
        str
        | Path
        | Mapping[str, Any]
        | Sequence[str | Path | Mapping[str, Any]]
        | None
    ) = None,
) -> list[dict[str, Any]]:
    """Load one or more supplementary gap-closure reports without mutating inputs.

    Accepts a JSON path, an already-loaded mapping, or a sequence of either.
    ``None`` means no reports.  Mappings are deep-copied; files are read as
    UTF-8 JSON.  The returned wrappers are JSON-serializable:

    ``{"source_report": ..., "source_index": ..., "payload": {...}}``
    """

    loaded: list[dict[str, Any]] = []
    for index, raw in enumerate(_flatten_report_inputs(reports)):
        label = (
            str(raw)
            if isinstance(raw, (str, Path))
            else f"<mapping:{index + 1}>"
        )
        loaded.append(_load_one_report(raw, label, index))
    return loaded


def _disposition_for(status: str) -> tuple[str, bool]:
    normalized = _text(status).casefold()
    if normalized == STATUS_CLOSED:
        return DISPOSITION_WRITE_READY, True
    if normalized == STATUS_IMPROVED_STOP:
        return DISPOSITION_WRITE_WITH_LIMITS, True
    return DISPOSITION_REVISE_BEFORE_WRITE, False


def _resolved_final_action(
    task: Mapping[str, Any],
    per_target: Sequence[Mapping[str, Any]],
) -> str:
    """Resolve the final claim action from per-target and task-level output.

    Per-target ``next_action`` values and task-level ``revision.results`` are
    the final, claim-specific signals and take precedence over the task-level
    summary.  An explicit ``delete`` is a veto: it wins regardless of where it
    appears, so an optimistic outer status cannot make a destructive
    disposition write-eligible.
    """

    actions: list[str] = []
    for item in per_target:
        action = _text(item.get("next_action"))
        if action:
            actions.append(action)
    revision = task.get("revision")
    if isinstance(revision, Mapping):
        for item in _list_of_mappings(revision.get("results")):
            action = _text(item.get("next_action"))
            if action:
                actions.append(action)
    task_action = _text(task.get("next_action"))
    if task_action:
        actions.append(task_action)
    if any(_normalized_text(action) == _DELETE_ACTION for action in actions):
        return _DELETE_ACTION
    return actions[0] if actions else ""


def _collect_reviewer_comments(
    task: Mapping[str, Any],
    per_target: Sequence[Mapping[str, Any]],
) -> list[str]:
    candidates: list[Any] = []
    for item in per_target:
        candidates.extend(item.get("residual_reviewer_comments") or [])
        candidates.extend(item.get("reviewer_comments") or [])
        for key in ("failure_reason", "why_current_evidence_fails", "comment"):
            candidates.append(item.get(key))
    assessment = task.get("progress_assessment")
    if isinstance(assessment, Mapping):
        for item in _list_of_mappings(assessment.get("per_target")):
            candidates.extend(item.get("residual_reviewer_comments") or [])
            candidates.extend(item.get("reviewer_comments") or [])
    feedback = task.get("reviewer_feedback")
    if isinstance(feedback, Mapping):
        candidates.extend(feedback.get("residual_reviewer_comments") or [])
        for key in ("failure_reason", "why_current_evidence_fails"):
            candidates.append(feedback.get(key))
    record = task.get("record")
    if isinstance(record, Mapping):
        record_feedback = record.get("reviewer_feedback")
        if isinstance(record_feedback, Mapping):
            candidates.extend(record_feedback.get("residual_reviewer_comments") or [])
            for key in ("failure_reason", "why_current_evidence_fails"):
                candidates.append(record_feedback.get(key))
        candidates.extend(record.get("reviewer_comments") or [])
    candidates.extend(task.get("reviewer_comments") or [])
    return _dedupe_texts(candidates)


def _collect_revision_records(
    task: Mapping[str, Any],
    per_target: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in per_target:
        if any(key in item for key in ("next_action", "revised_claim", "recommendation")):
            records.append(copy.deepcopy(dict(item)))
    revision = task.get("revision")
    if isinstance(revision, Mapping):
        for item in _list_of_mappings(revision.get("results")):
            if any(key in item for key in ("next_action", "revised_claim", "recommendation")):
                records.append(copy.deepcopy(dict(item)))
    return _dedupe_mappings(records)


def _collect_author_revision_records(
    task: Mapping[str, Any],
) -> list[dict[str, Any]]:
    sources = [task]
    if isinstance(task.get("record"), Mapping):
        sources.append(task["record"])
    records: list[dict[str, Any]] = []
    for source in sources:
        item: dict[str, Any] = {}
        for key in (
            "author_revision_suggestion",
            "required_revision_or_qualification",
            "author_revision_history",
            "recommendation",
        ):
            if key in source:
                item[key] = copy.deepcopy(source[key])
        if item:
            records.append(item)
    return _dedupe_mappings(records)


def _writing_limits(
    comments: Sequence[str],
    revision_records: Sequence[Mapping[str, Any]],
    author_records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Build compact, non-fabricated writing limits for improved_stop claims."""

    limits: list[str] = list(comments)
    for record in revision_records:
        action = _text(record.get("next_action"))
        if action:
            limits.append(f"revision:{action}")
        revised = _text(record.get("revised_claim"))
        if revised:
            limits.append(f"revised_claim:{revised}")
    for record in author_records:
        suggestion = _text(
            record.get("author_revision_suggestion")
            or record.get("required_revision_or_qualification")
        )
        if suggestion:
            limits.append(f"author_revision_suggestion:{suggestion}")
    return _dedupe_texts(limits)


def _notification_target_map(
    payload: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("notifications") or []:
        if not isinstance(item, Mapping):
            continue
        target_id = _text(item.get("target_id"))
        if not target_id:
            continue
        entry: dict[str, Any] = {}
        for key in ("cache_path", "cache_version"):
            if item.get(key) is not None:
                entry[key] = str(item[key])
        if entry:
            result[target_id] = entry
    return result


def _normalize_claim_record(
    task: Mapping[str, Any],
    *,
    source: str,
    index: int,
    task_index: int,
    output_snapshot: str,
    notification_map: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    per_target = _list_of_mappings(task.get("per_target_results"))
    component_id = _text(task.get("component_id") or task.get("claim_id"))
    if not component_id:
        for item in per_target:
            candidate = _text(item.get("target_id"))
            if candidate:
                component_id = candidate
                break
    comments = _collect_reviewer_comments(task, per_target)
    revision_records = _collect_revision_records(task, per_target)
    author_records = _collect_author_revision_records(task)
    status = _text(task.get("status"))
    disposition, eligible = _disposition_for(status)
    final_action = _resolved_final_action(task, per_target)
    if final_action == _DELETE_ACTION:
        disposition = DISPOSITION_REVISE_BEFORE_WRITE
        eligible = False
    next_action = (
        final_action
        if final_action == _DELETE_ACTION
        else _text(task.get("next_action")) or None
    )
    snapshot_path = _text(task.get("snapshot_path")) or output_snapshot
    snapshot_version = _text(task.get("snapshot_version"))
    notification = notification_map.get(component_id) or {}
    cache_path = _text(task.get("cache_path")) or _text(notification.get("cache_path"))
    if not snapshot_path and cache_path:
        snapshot_path = cache_path
    cache_version = (
        _text(task.get("cache_version"))
        or _text(notification.get("cache_version"))
        or snapshot_version
    )
    if not cache_version and snapshot_path:
        cache_version = Path(snapshot_path).name
    return {
        "schema_version": SCHEMA_VERSION,
        "claim_id": component_id,
        "component_id": component_id,
        "gap_type": CLAIM_GAP_TYPE,
        "status": status,
        "disposition": disposition,
        "eligible": eligible,
        "writing_limits": (
            _writing_limits(comments, revision_records, author_records)
            if disposition == DISPOSITION_WRITE_WITH_LIMITS
            else []
        ),
        "source_report": source,
        "source_index": index,
        "task_index": task_index,
        "task_id": _text(task.get("task_id")),
        "idempotency_key": _text(task.get("idempotency_key")),
        "retrieval_wave_count": _int_or_zero(task.get("retrieval_wave_count")),
        "max_retrieval_waves": _int_or_zero(task.get("max_retrieval_waves")),
        "next_action": next_action,
        "error": copy.deepcopy(task.get("error")),
        "snapshot_path": snapshot_path or None,
        "snapshot_version": snapshot_version or None,
        "cache_version": cache_version or None,
        "reviewer_comments": comments,
        "revision_records": revision_records,
        "author_revision_records": author_records,
        "progress_assessment": copy.deepcopy(
            task.get("progress_assessment")
            if isinstance(task.get("progress_assessment"), (Mapping, list))
            else None
        ),
        "per_target_results": copy.deepcopy(per_target),
        "record": (
            copy.deepcopy(task["record"])
            if isinstance(task.get("record"), Mapping)
            else None
        ),
    }


def _normalize_section_task_record(
    task: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    source: str,
    index: int,
    task_index: int,
) -> dict[str, Any]:
    per_target = _list_of_mappings(task.get("per_target_results"))
    candidate_claims: list[Any] = []
    candidate_audit: dict[str, Any] = {}
    for item in per_target:
        candidate_claims.extend(item.get("candidate_claims") or [])
        audit = item.get("candidate_claim_audit")
        if isinstance(audit, Mapping):
            for key, value in audit.items():
                candidate_audit.setdefault(key, value)
    missing_roles = payload.get("missing_roles")
    return {
        "schema_version": SCHEMA_VERSION,
        "gap_type": SECTION_GAP_TYPE,
        "status": _text(task.get("status")) or None,
        "outcome": _text(payload.get("outcome")) or None,
        "actionable_section_gap": bool(payload.get("actionable_section_gap")),
        "missing_roles": copy.deepcopy(
            missing_roles if isinstance(missing_roles, list) else []
        ),
        "source_report": source,
        "source_index": index,
        "task_index": task_index,
        "task_id": _text(task.get("task_id")),
        "next_action": _text(task.get("next_action")) or None,
        "snapshot_path": (
            _text(task.get("snapshot_path"))
            or _text(payload.get("output_snapshot"))
            or None
        ),
        "candidate_claims": copy.deepcopy(candidate_claims),
        "candidate_claim_audit": copy.deepcopy(candidate_audit),
        "task": copy.deepcopy(dict(task)),
        "never_invoked": copy.deepcopy(
            payload.get("never_invoked")
            if isinstance(payload.get("never_invoked"), list)
            else []
        ),
    }


def _normalize_section_report_level(
    payload: Mapping[str, Any],
    *,
    source: str,
    index: int,
) -> dict[str, Any]:
    missing_roles = payload.get("missing_roles")
    candidate_claims = payload.get("candidate_claims")
    candidate_audit = payload.get("candidate_claim_audit")
    return {
        "schema_version": SCHEMA_VERSION,
        "gap_type": SECTION_GAP_TYPE,
        "status": None,
        "outcome": _text(payload.get("outcome")) or None,
        "actionable_section_gap": bool(payload.get("actionable_section_gap")),
        "missing_roles": copy.deepcopy(
            missing_roles if isinstance(missing_roles, list) else []
        ),
        "source_report": source,
        "source_index": index,
        "task_index": None,
        "task_id": None,
        "next_action": None,
        "snapshot_path": _text(payload.get("output_snapshot")) or None,
        "candidate_claims": copy.deepcopy(
            candidate_claims if isinstance(candidate_claims, list) else []
        ),
        "candidate_claim_audit": copy.deepcopy(
            candidate_audit if isinstance(candidate_audit, Mapping) else {}
        ),
        "task": None,
        "never_invoked": copy.deepcopy(
            payload.get("never_invoked")
            if isinstance(payload.get("never_invoked"), list)
            else []
        ),
    }


def _audit_entry(
    *,
    source: str,
    index: int,
    task_index: int | None,
    task: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_report": source,
        "source_index": index,
        "task_index": task_index,
        "task_id": _text(task.get("task_id")) if task else None,
        "component_id": (
            _text(task.get("component_id") or task.get("claim_id"))
            if task
            else None
        ),
        "gap_type": _text(task.get("gap_type")) if task else None,
        "status": _text(task.get("status")) if task else None,
        "reason": reason,
    }


def merge_gap_closure_reports(
    reports: (
        str
        | Path
        | Mapping[str, Any]
        | Sequence[str | Path | Mapping[str, Any]]
        | None
    ) = None,
) -> dict[str, Any]:
    """Merge supplementary gap-closure reports into one deterministic index.

    Later reports override earlier records for the same claim/component id.
    Returns a JSON-serializable mapping with ``claim_closures``,
    ``section_gap_summaries``, ``ignored_non_claim_records``, and ``counts``.
    """

    if (
        isinstance(reports, Mapping)
        and reports.get("schema_version") == SCHEMA_VERSION
        and "claim_closures" in reports
    ):
        return copy.deepcopy(dict(reports))
    loaded = load_gap_closure_reports(reports)
    claim_closures: dict[str, dict[str, Any]] = {}
    section_summaries: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for wrapper in loaded:
        payload = wrapper["payload"]
        source = wrapper["source_report"]
        index = wrapper["source_index"]
        notification_map = _notification_target_map(payload)
        output_snapshot = _text(payload.get("output_snapshot"))
        tasks = payload.get("tasks")
        if tasks is not None and not isinstance(tasks, list):
            raise GapClosureDownstreamError(
                f"report {source} has non-list 'tasks'"
            )
        task_items = tasks if isinstance(tasks, list) else []
        has_section_task = False
        for task_index, task in enumerate(task_items):
            if not isinstance(task, Mapping):
                ignored.append(
                    _audit_entry(
                        source=source,
                        index=index,
                        task_index=task_index,
                        task={},
                        reason="non_mapping_task",
                    )
                )
                continue
            gap_type = _text(task.get("gap_type"))
            gap_key = gap_type.casefold()
            if gap_key == CLAIM_GAP_TYPE:
                record = _normalize_claim_record(
                    task,
                    source=source,
                    index=index,
                    task_index=task_index,
                    output_snapshot=output_snapshot,
                    notification_map=notification_map,
                )
                if not record["claim_id"]:
                    ignored.append(
                        _audit_entry(
                            source=source,
                            index=index,
                            task_index=task_index,
                            task=task,
                            reason="claim_record_missing_target_id",
                        )
                    )
                    continue
                claim_closures[record["claim_id"]] = record
            elif gap_key == SECTION_GAP_TYPE:
                has_section_task = True
                section_summaries.append(
                    _normalize_section_task_record(
                        task,
                        payload=payload,
                        source=source,
                        index=index,
                        task_index=task_index,
                    )
                )
            elif gap_key in IGNORED_GAP_TYPES:
                ignored.append(
                    _audit_entry(
                        source=source,
                        index=index,
                        task_index=task_index,
                        task=task,
                        reason=f"{gap_type}_not_applied",
                    )
                )
            else:
                ignored.append(
                    _audit_entry(
                        source=source,
                        index=index,
                        task_index=task_index,
                        task=task,
                        reason="missing_gap_type" if not gap_type else "unsupported_gap_type",
                    )
                )
        top_gap_type = _text(payload.get("gap_type"))
        top_gap_key = top_gap_type.casefold()
        if top_gap_key in IGNORED_GAP_TYPES and not task_items:
            ignored.append(
                _audit_entry(
                    source=source,
                    index=index,
                    task_index=None,
                    task={"gap_type": top_gap_type, "status": _text(payload.get("status"))},
                    reason=f"{top_gap_type}_not_applied",
                )
            )
        if (payload.get("outcome") is not None or top_gap_key == SECTION_GAP_TYPE) and not has_section_task:
            section_summaries.append(
                _normalize_section_report_level(
                    payload,
                    source=source,
                    index=index,
                )
            )
    section_summaries.sort(
        key=lambda record: (
            record["source_index"],
            -1 if record["task_index"] is None else record["task_index"],
            record["gap_type"],
            record["outcome"] or "",
        )
    )
    ignored.sort(
        key=lambda record: (
            record["source_index"],
            -1 if record["task_index"] is None else record["task_index"],
            record["reason"],
            record["component_id"] or "",
            record["gap_type"] or "",
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "claim_closures": dict(sorted(claim_closures.items())),
        "section_gap_summaries": section_summaries,
        "ignored_non_claim_records": ignored,
        "counts": {
            "report_count": len(loaded),
            "claim_closure_count": len(claim_closures),
            "section_gap_count": len(section_summaries),
            "ignored_non_claim_count": len(ignored),
        },
    }


def _coerce_merged(reports: Any) -> dict[str, Any] | None:
    if (
        isinstance(reports, Mapping)
        and reports.get("schema_version") == SCHEMA_VERSION
        and ("claim_closures" in reports or "claim_dispositions" in reports)
    ):
        if "claim_dispositions" in reports:
            dispositions = reports.get("claim_dispositions")
            closures = {
                key: copy.deepcopy(dict(value))
                for key, value in (dispositions or {}).items()
                if isinstance(value, Mapping)
            }
            return {
                "schema_version": SCHEMA_VERSION,
                "claim_closures": closures,
                "section_gap_summaries": copy.deepcopy(
                    reports.get("section_gap_summary") or []
                ),
                "ignored_non_claim_records": copy.deepcopy(
                    reports.get("ignored_non_claim_records") or []
                ),
                "counts": copy.deepcopy(reports.get("counts") or {}),
            }
        return copy.deepcopy(dict(reports))
    return None


def _context_source_reports(reports: Any, merged: Mapping[str, Any]) -> list[str]:
    if isinstance(reports, Mapping) and reports.get("schema_version") == SCHEMA_VERSION:
        labels: set[str] = set()
        for record in (reports.get("claim_closures") or {}).values():
            if isinstance(record, Mapping):
                label = _text(record.get("source_report"))
                if label:
                    labels.add(label)
        for record in (
            reports.get("section_gap_summaries")
            or reports.get("section_gap_summary")
            or []
        ):
            if isinstance(record, Mapping):
                label = _text(record.get("source_report"))
                if label:
                    labels.add(label)
        for record in reports.get("ignored_non_claim_records") or []:
            if isinstance(record, Mapping):
                label = _text(record.get("source_report"))
                if label:
                    labels.add(label)
        return sorted(labels)
    return [wrapper["source_report"] for wrapper in load_gap_closure_reports(reports)]


def build_gap_closure_writing_context(
    reports: (
        str
        | Path
        | Mapping[str, Any]
        | Sequence[str | Path | Mapping[str, Any]]
        | None
    ) = None,
) -> dict[str, Any]:
    """Build the downstream writing/blueprint context from closure reports.

    The context contains claim dispositions, eligible/revise-before-write ids,
    retained reviewer comments, revision and author records, cache snapshot
    paths, counts, the separate section-gap summary, and the audit trail of
    ignored non-claim records.  Output is deterministic and JSON-serializable.
    """

    merged = _coerce_merged(reports)
    if merged is None:
        merged = merge_gap_closure_reports(reports)
    closures = merged.get("claim_closures") or {}
    section_summaries = merged.get("section_gap_summaries") or []
    ignored = merged.get("ignored_non_claim_records") or []
    claim_dispositions = {
        claim_id: copy.deepcopy(dict(record))
        for claim_id, record in sorted(closures.items())
    }
    eligible_ids = sorted(
        claim_id for claim_id, record in closures.items() if record.get("eligible")
    )
    write_ready_ids = sorted(
        claim_id
        for claim_id, record in closures.items()
        if record.get("disposition") == DISPOSITION_WRITE_READY
    )
    write_with_limits_ids = sorted(
        claim_id
        for claim_id, record in closures.items()
        if record.get("disposition") == DISPOSITION_WRITE_WITH_LIMITS
    )
    revise_ids = sorted(
        claim_id for claim_id, record in closures.items() if not record.get("eligible")
    )
    retained_comments = {
        claim_id: list(record.get("reviewer_comments") or [])
        for claim_id, record in sorted(closures.items())
    }
    revision_records = {
        claim_id: copy.deepcopy(record.get("revision_records") or [])
        for claim_id, record in sorted(closures.items())
    }
    author_revision_records = {
        claim_id: copy.deepcopy(record.get("author_revision_records") or [])
        for claim_id, record in sorted(closures.items())
    }
    snapshot_paths = {
        claim_id: record.get("snapshot_path")
        for claim_id, record in sorted(closures.items())
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_scope": "writing_blueprint_context",
        "one_wave_policy": {
            "max_retrieval_waves": MAX_RETRIEVAL_WAVES,
            "policy": (
                "Each S04 claim gap is allowed at most one supplementary "
                "retrieval wave.  improved_stop claims carry residual "
                "reviewer limits into writing; revision_required, "
                "no_progress, still_open, and failed claims are not eligible "
                "until a later local revision/recheck."
            ),
        },
        "execution_prohibitions": [
            "No network calls.",
            "No model calls.",
            "No new retrieval waves.",
        ],
        "report_count": int((merged.get("counts") or {}).get("report_count") or 0),
        "source_reports": _context_source_reports(reports, merged),
        "claim_dispositions": claim_dispositions,
        "eligible_claim_ids": eligible_ids,
        "write_ready_claim_ids": write_ready_ids,
        "write_with_limits_claim_ids": write_with_limits_ids,
        "revise_before_write_claim_ids": revise_ids,
        "counts": {
            "report_count": int((merged.get("counts") or {}).get("report_count") or 0),
            "claim_closure_count": len(closures),
            "eligible_count": len(eligible_ids),
            "write_ready_count": len(write_ready_ids),
            "write_with_limits_count": len(write_with_limits_ids),
            "revise_before_write_count": len(revise_ids),
            "section_gap_count": len(section_summaries),
            "ignored_non_claim_count": len(ignored),
            "retained_reviewer_comment_count": sum(
                len(comments) for comments in retained_comments.values()
            ),
            "revision_record_count": sum(
                len(records) for records in revision_records.values()
            ),
        },
        "retained_reviewer_comments": retained_comments,
        "revision_records": revision_records,
        "author_revision_records": author_revision_records,
        "cache_snapshot_paths": snapshot_paths,
        "section_gap_summary": copy.deepcopy(section_summaries),
        "ignored_non_claim_records": copy.deepcopy(ignored),
    }


def _claim_records_from_closures(closures: Any) -> dict[str, dict[str, Any]]:
    if closures is None:
        return {}
    if isinstance(closures, list):
        records: dict[str, dict[str, Any]] = {}
        for item in closures:
            if not isinstance(item, Mapping):
                continue
            claim_id = _text(item.get("claim_id") or item.get("component_id"))
            if claim_id:
                records[claim_id] = dict(item)
        return records
    if isinstance(closures, Mapping):
        if "claim_closures" in closures and isinstance(closures.get("claim_closures"), Mapping):
            source = closures["claim_closures"]
        elif "claim_dispositions" in closures and isinstance(
            closures.get("claim_dispositions"), Mapping
        ):
            source = closures["claim_dispositions"]
        else:
            source = closures
        records = {}
        for key, value in source.items():
            if not isinstance(value, Mapping):
                continue
            claim_id = _text(value.get("claim_id") or value.get("component_id") or key)
            if claim_id:
                records[claim_id] = copy.deepcopy(dict(value))
        return records
    raise GapClosureDownstreamError(
        "closures must be a merged mapping, a writing context, a list of "
        "records, or None"
    )


def apply_gap_closure_to_report(
    report: Mapping[str, Any],
    closures: (
        Mapping[str, Any]
        | list[Mapping[str, Any]]
        | None
    ) = None,
) -> dict[str, Any]:
    """Deep-copy a report and annotate its ``final_claims`` with closures.

    Matching final claims (by top-level ``claim_id`` or ``component_id``)
    receive a ``gap_closure`` object.  ``ready_for_write`` is set true only
    for ``closed`` or ``improved_stop`` dispositions; claims that must be
    revised first are explicitly set false.  Unmatched claims are untouched,
    and the input report is never mutated.
    """

    result = copy.deepcopy(dict(report))
    records = _claim_records_from_closures(closures)
    claims = result.get("final_claims")
    if not isinstance(claims, list):
        return result
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = _text(claim.get("claim_id") or claim.get("component_id"))
        if not claim_id or claim_id not in records:
            continue
        record = records[claim_id]
        claim["gap_closure"] = {
            "schema_version": SCHEMA_VERSION,
            "claim_id": record.get("claim_id") or claim_id,
            "component_id": record.get("component_id") or claim_id,
            "gap_type": record.get("gap_type") or CLAIM_GAP_TYPE,
            "status": record.get("status"),
            "disposition": record.get("disposition"),
            "eligible": bool(record.get("eligible")),
            "writing_limits": list(record.get("writing_limits") or []),
            "reviewer_comments": list(record.get("reviewer_comments") or []),
            "revision_records": copy.deepcopy(record.get("revision_records") or []),
            "author_revision_records": copy.deepcopy(
                record.get("author_revision_records") or []
            ),
            "snapshot_path": record.get("snapshot_path"),
            "cache_version": record.get("cache_version"),
            "source_report": record.get("source_report"),
            "source_index": record.get("source_index"),
            "task_id": record.get("task_id"),
            "next_action": record.get("next_action"),
            "retrieval_wave_count": record.get("retrieval_wave_count"),
            "max_retrieval_waves": record.get("max_retrieval_waves"),
            "per_target_results": copy.deepcopy(
                record.get("per_target_results") or []
            ),
        }
        eligible = bool(record.get("eligible"))
        claim["ready_for_write"] = eligible
        if eligible:
            revised_claim = ""
            for revision_record in record.get("revision_records") or []:
                if not isinstance(revision_record, Mapping):
                    continue
                candidate = str(
                    revision_record.get("revised_claim") or ""
                ).strip()
                if candidate:
                    revised_claim = candidate
                    break
            if revised_claim:
                claim["original_statement"] = str(
                    claim.get("statement") or ""
                )
                claim["statement"] = revised_claim
                claim["statement_revision_source"] = {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": record.get("task_id"),
                    "source_report": record.get("source_report"),
                    "reason": "supplementary_closure_revised_claim",
                }
    return result


def _normalized_contiguous(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _unit_references_task(unit: Mapping[str, Any], task_id: str) -> bool:
    for annotation in unit.get("query_annotations") or []:
        if not isinstance(annotation, Mapping):
            continue
        for reference in annotation.get(
            "supplementary_task_references"
        ) or []:
            if not isinstance(reference, Mapping):
                continue
            if str(reference.get("task_id") or "") == task_id:
                return True
    return False


def _is_weak_supplementary_content(
    content_depth: str,
    source_kind: str,
) -> bool:
    """Return True for abstract/summary/metadata material, never full text.

    S2 full-text snippets and OA full text are equally strong; only
    abstract, TLDR/summary, and metadata material is background-only.
    """

    depth = str(content_depth or "").casefold()
    kind = str(source_kind or "").casefold()
    return any(
        marker in depth or marker in kind
        for marker in _WEAK_SUPPLEMENTARY_MARKERS
    )


def build_supplementary_evidence_packets(
    closure: Mapping[str, Any],
    snapshot_units: Sequence[Mapping[str, Any]],
    *,
    claim_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build claim-local evidence rows from an eligible closure's snapshot.

    Only exact, task-referenced supplementary evidence for the affected claim
    is admitted.  Quotes must be exact substrings (raw or whitespace-normalized
    contiguous) of ``durable_content.raw_text``, and the unit must reference
    the closure's ``task_id``.  Abstract/summary/metadata material is never
    elevated to strong evidence.  Missing/corrupt units are audited and
    skipped.
    """
    if not bool(closure.get("eligible")):
        return [], {
            "claim_id": claim_id,
            "status": "not_eligible",
            "admitted_packet_count": 0,
            "rejected": [],
        }
    task_id = str(
        closure.get("task_id")
        or closure.get("idempotency_key")
        or ""
    )
    by_unit = {
        str(unit.get("unit_id") or ""): unit
        for unit in snapshot_units
        if isinstance(unit, Mapping) and unit.get("unit_id")
    }
    candidates: list[tuple[str, str, str]] = []
    seen_candidates: set[tuple[str, str]] = set()
    for item in closure.get("per_target_results") or []:
        if not isinstance(item, Mapping):
            continue
        target_id = str(item.get("target_id") or "")
        if target_id and target_id not in {claim_id, str(closure.get("component_id") or "")}:
            continue
        for entry in item.get("locally_validated_quotes") or []:
            if not isinstance(entry, Mapping):
                continue
            unit_id = str(entry.get("unit_id") or "")
            quote = str(entry.get("quote") or "").strip()
            key = (unit_id, quote)
            if unit_id and quote and key not in seen_candidates:
                seen_candidates.add(key)
                candidates.append((unit_id, quote, "locally_validated_quote"))
        for phrase, unit_ids in (item.get("exact_quote_matches") or {}).items():
            phrase = str(phrase or "").strip()
            if not phrase:
                continue
            for unit_id in unit_ids or []:
                unit_id = str(unit_id or "").strip()
                key = (unit_id, phrase)
                if unit_id and key not in seen_candidates:
                    seen_candidates.add(key)
                    candidates.append((unit_id, phrase, "exact_quote_match"))

    packets: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for unit_id, quote, method in candidates:
        unit = by_unit.get(unit_id)
        if unit is None:
            rejected.append({
                "unit_id": unit_id,
                "reason": "missing_unit",
            })
            continue
        if task_id and not _unit_references_task(unit, task_id):
            rejected.append({
                "unit_id": unit_id,
                "reason": "task_reference_mismatch",
            })
            continue
        raw_text = str(
            (unit.get("durable_content") or {}).get("raw_text") or ""
        )
        if quote in raw_text:
            validation = "exact_substring"
        elif (
            _normalized_contiguous(quote)
            and _normalized_contiguous(quote)
            in _normalized_contiguous(raw_text)
        ):
            validation = "normalized_substring"
        else:
            rejected.append({
                "unit_id": unit_id,
                "reason": "quote_not_found",
            })
            continue
        content = unit.get("durable_content") or {}
        content_depth = str(content.get("content_depth") or "").casefold()
        quality = (unit.get("durable_content_card") or {}).get(
            "content_quality"
        ) or {}
        source_kind = str(
            quality.get("source_kind") or unit.get("source_kind") or "fulltext"
        )
        abstract_only = _is_weak_supplementary_content(
            content_depth, source_kind
        )
        identity = unit.get("identity") or {}
        packets.append({
            "claim_id": claim_id,
            "paper_id": str(
                identity.get("paper_id") or identity.get("doi") or ""
            ),
            "chunk_id": str(identity.get("chunk_id") or ""),
            "exact_spans": [quote],
            "visual_refs": [],
            "support_relation": (
                "background_support" if abstract_only else "component_support"
            ),
            "limitations": (
                ["Abstract-only background material; not strong evidence."]
                if abstract_only
                else []
            ),
            "evidence_level": "abstract" if abstract_only else "fulltext",
            "source_kind": source_kind,
            "scope_fit": "in_domain",
            "retrieval_role": (
                "supplementary_background_only"
                if abstract_only
                else "supplementary_evidence_candidate"
            ),
            "source_title": str(identity.get("title") or ""),
            "validation": validation,
        })
    audit = {
        "claim_id": claim_id,
        "task_id": task_id,
        "status": "completed" if packets or rejected else "no_candidates",
        "candidate_count": len(candidates),
        "admitted_packet_count": len(packets),
        "snapshot_unit_count": len(snapshot_units),
        "validation": [packet["validation"] for packet in packets],
        "rejected": rejected,
    }
    return packets, audit


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_CLOSED",
    "STATUS_IMPROVED_STOP",
    "STATUS_REVISION_REQUIRED",
    "STATUS_NO_PROGRESS",
    "STATUS_STILL_OPEN",
    "STATUS_FAILED",
    "DISPOSITION_WRITE_READY",
    "DISPOSITION_WRITE_WITH_LIMITS",
    "DISPOSITION_REVISE_BEFORE_WRITE",
    "CLAIM_GAP_TYPE",
    "SECTION_GAP_TYPE",
    "IGNORED_GAP_TYPES",
    "MAX_RETRIEVAL_WAVES",
    "GapClosureDownstreamError",
    "load_gap_closure_reports",
    "merge_gap_closure_reports",
    "build_gap_closure_writing_context",
    "apply_gap_closure_to_report",
    "build_supplementary_evidence_packets",
]
