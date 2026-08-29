"""Bounded revision workflow for visual transformation tasks.

Additive sidecar module that prepares tasks for the existing Qwen conceptual
visual generator/factory without changing those components.  The actual Qwen
and image processing adapters are deliberately injected as callables so this
module stays deterministic, offline, and testable.  The workflow enforces:

* at most three total attempts (initial attempt plus at most two revisions);
* reviewer feedback -> prompt revision -> re-audit on every rejection;
* cost placeholders for every attempt until real adapters report usage;
* nonblocking unfilled needs when attempts are exhausted;
* mandatory disclosure and explanatory-not-evidence status on generated
  output;
* complete lineage with source hash and permission for enhanced/redrawn
  output;
* durable-cache-ready records with article information placeholders.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .visual_generation_policy import (
    AI_GENERATED_EXPLANATORY_VISUAL,
    AUTHOR_REDRAW,
    DETERMINISTIC_DATA_PLOT,
    ENHANCED_SOURCE,
    SOURCE_VISUAL,
    classify_visual_task,
)


SCHEMA_VERSION = "visual_transformation_workflow.v1"
DURABLE_RECORD_SCHEMA_VERSION = "durable_visual_cache_record.v1"
COST_PLACEHOLDER_SCHEMA_VERSION = "visual_cost_placeholder.v1"
MAX_TOTAL_ATTEMPTS = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any, limit: int = 2400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: str) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_file():
        return ""
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_ref(task: dict[str, Any]) -> str:
    return _compact(
        task.get("source_ref")
        or task.get("source_path")
        or task.get("local_image_path")
        or task.get("original_path")
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _not_configured_adapter(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "not_configured",
        "error": "adapter_not_injected",
        "payload_keys": sorted(payload.keys()),
    }


def _review_not_configured(payload: dict[str, Any]) -> dict[str, Any]:
    del payload
    return {
        "verdict": "needs_human_review",
        "approved": False,
        "feedback": [
            "Review adapter not injected; visual remains unresolved."
        ],
    }


def build_prompt(task: dict[str, Any]) -> str:
    """Build the deterministic base prompt for an attempt."""

    parts = [
        "Purpose: "
        + _compact(
            task.get("purpose")
            or task.get("argumentative_purpose")
            or task.get("argument_role")
        ),
        "Generation brief: " + _compact(task.get("generation_brief")),
    ]
    if task.get("figure_kind"):
        parts.append("Figure kind: " + _compact(task.get("figure_kind")))
    if task.get("input_data"):
        parts.append(
            "Input data: "
            + _canonical_json(task.get("input_data"))
        )
    parts.append(
        "Disclosure: "
        + (
            "non-semantic enhancement only; preserve the original image "
            "and its provenance."
            if str(task.get("category") or "") == ENHANCED_SOURCE
            else (
                "AI-generated explanatory visual; not empirical evidence."
            )
        )
    )
    return "\n".join(parts)


def apply_revision_feedback(
    base_prompt: str,
    feedback: list[str],
    attempt_number: int,
) -> str:
    """Append reviewer feedback to the prompt for the next attempt."""

    if attempt_number <= 1 or not feedback:
        return base_prompt
    unique_feedback = list(dict.fromkeys(feedback))
    lines = "\n".join(f"- {line}" for line in unique_feedback)
    return (
        base_prompt
        + "\n\nREVISION FEEDBACK FROM REVIEWER (round "
        + str(attempt_number)
        + "):\n"
        + lines
    )


def _review_feedback(review: Any) -> list[str]:
    if not isinstance(review, dict):
        return []
    raw = (
        review.get("feedback")
        or review.get("required_revisions")
        or review.get("reviewer_feedback")
        or review.get("misleading_elements")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [
        _compact(line)
        for line in raw
        if _compact(line)
    ]


def _review_approved(review: Any) -> bool:
    if not isinstance(review, dict):
        return False
    if review.get("rejected") is True:
        return False
    if review.get("approved") is True:
        return True
    verdict = str(review.get("verdict") or "").strip().lower()
    return verdict in {"approve", "approved", "accept", "accepted", "pass"}


def _produced_success(produced: Any) -> bool:
    if not isinstance(produced, dict):
        return False
    status = str(
        produced.get("status")
        or produced.get("generation_status")
        or ""
    ).strip().lower()
    if status in {"ok", "ready", "success", "approved", "generated", "completed"}:
        return True
    return bool(status) and status not in {
        "failed",
        "error",
        "not_configured",
    }


def _cost_placeholder(produced: Any) -> dict[str, Any]:
    produced = produced if isinstance(produced, dict) else {}
    reported = produced.get("cost") or produced.get("usage") or {}
    if not isinstance(reported, dict):
        reported = {}
    return {
        "schema_version": COST_PLACEHOLDER_SCHEMA_VERSION,
        "model": _compact(reported.get("model") or produced.get("model")),
        "input_tokens": _safe_int(reported.get("input_tokens")),
        "output_tokens": _safe_int(reported.get("output_tokens")),
        "estimated_cost_cny": _safe_float(
            reported.get("estimated_cost_cny")
        ),
        "currency": "CNY",
        "note": (
            "Placeholder until the real Qwen/image processing adapter "
            "reports usage."
        ),
    }


@dataclass
class VisualTransformationWorkflowConfig:
    """Injected adapter callables and workflow limits."""

    max_attempts: int = MAX_TOTAL_ATTEMPTS
    generation_adapter: Callable[[dict[str, Any]], dict[str, Any]] = (
        _not_configured_adapter
    )
    enhancement_adapter: Callable[[dict[str, Any]], dict[str, Any]] = (
        _not_configured_adapter
    )
    render_adapter: Callable[[dict[str, Any]], dict[str, Any]] = (
        _not_configured_adapter
    )
    review_adapter: Callable[[dict[str, Any]], dict[str, Any]] = (
        _review_not_configured
    )
    clock: Callable[[], str] = _utc_now

    def __post_init__(self) -> None:
        try:
            requested = int(self.max_attempts)
        except Exception:
            requested = MAX_TOTAL_ATTEMPTS
        self.max_attempts = max(
            1,
            min(MAX_TOTAL_ATTEMPTS, requested),
        )


class VisualTransformationWorkflow:
    """Run one visual transformation task through the bounded state machine."""

    def __init__(
        self,
        config: VisualTransformationWorkflowConfig | None = None,
    ) -> None:
        self.config = config or VisualTransformationWorkflowConfig()

    def _now(self) -> str:
        try:
            return str(self.config.clock())
        except Exception:
            return _utc_now()

    def submit(self, task: dict[str, Any]) -> dict[str, Any]:
        """Classify and prepare a task record without executing adapters."""

        invalid_input = not isinstance(task, dict)
        task = dict(task) if isinstance(task, dict) else {}
        classification = classify_visual_task(task)
        if invalid_input:
            classification["policy_decision"] = "denied"
            classification["denied_reason"] = "invalid_task_input"
        record_id = str(
            task.get("task_id")
            or task.get("visual_plan_id")
            or ""
        )
        if not record_id:
            record_id = "visual-transformation-" + _sha256_text(
                _canonical_json(task)
            )[:12]
        denied = classification["policy_decision"] == "denied"
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record_id": record_id,
            "task": task,
            "classification": classification,
            "status": "policy_denied" if denied else "created",
            "attempts": [],
            "result": {},
            "unfilled_need": {},
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        if denied:
            record["result"] = {
                "status": "policy_denied",
                "denied_reason": classification["denied_reason"],
                "category": classification["category"],
                "policy_decision": "denied",
            }
            record["unfilled_need"] = self._unfilled_need(
                record,
                [],
                reason=classification["denied_reason"],
            )
        return record

    def run(self, record: dict[str, Any]) -> dict[str, Any]:
        """Execute the task through attempts, review, and approval/exhaustion."""

        if not isinstance(record, dict):
            record = {}
        if not isinstance(record.get("task"), dict) or not isinstance(
            record.get("classification"),
            dict,
        ):
            task = (
                record.get("task")
                if isinstance(record.get("task"), dict)
                else {}
            )
            record = self.submit(task)
        if record.get("status") in {
            "policy_denied",
            "approved",
            "exhausted_unfilled",
        }:
            return record
        classification = dict(record.get("classification") or {})
        category = str(classification.get("category") or "")
        record["status"] = "running"
        record["updated_at"] = self._now()
        if category == SOURCE_VISUAL:
            return self._passthrough_source(record)
        if category == DETERMINISTIC_DATA_PLOT:
            return self._deterministic_render(record)
        return self._revision_loop(record)

    def process(self, task: dict[str, Any]) -> dict[str, Any]:
        """Convenience wrapper: submit then run."""

        return self.run(self.submit(task))

    # ------------------------------------------------------------------
    # Route implementations
    # ------------------------------------------------------------------

    def _new_attempt(
        self,
        *,
        attempt_number: int,
        prompt: str,
        feedback: list[str],
    ) -> dict[str, Any]:
        return {
            "attempt_number": attempt_number,
            "status": "attempting",
            "prompt": prompt,
            "revised_prompt": "" if attempt_number == 1 else prompt,
            "reviewer_feedback": list(feedback),
            "result": {},
            "review": {},
            "cost_placeholder": {
                "schema_version": COST_PLACEHOLDER_SCHEMA_VERSION,
                "model": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_cny": 0.0,
                "currency": "CNY",
                "note": (
                    "Placeholder until the real Qwen/image processing "
                    "adapter reports usage."
                ),
            },
            "started_at": self._now(),
            "finished_at": "",
        }

    def _passthrough_source(self, record: dict[str, Any]) -> dict[str, Any]:
        task = dict(record["task"])
        classification = dict(record["classification"])
        source_ref = _source_ref(task)
        source_sha256 = str(
            task.get("source_sha256")
            or _sha256_file(source_ref)
        )
        attempt = self._new_attempt(
            attempt_number=1,
            prompt="",
            feedback=[],
        )
        attempt["status"] = "approved"
        attempt["result"] = {
            "status": "ready",
            "local_path": source_ref,
            "sha256": source_sha256,
        }
        attempt["review"] = {
            "verdict": "source_accepted",
            "approved": True,
            "reviewer": "deterministic_passthrough",
        }
        attempt["finished_at"] = self._now()
        lineage = [
            {
                "action": "source",
                "ref": source_ref,
                "sha256": source_sha256,
                "permission": task.get("permission") or {},
            }
        ]
        record["attempts"] = [attempt]
        record["result"] = self._approved_result(
            record=record,
            attempts=[attempt],
            produced=attempt["result"],
            review=attempt["review"],
            lineage=lineage,
            local_path=source_ref,
            sha256=source_sha256,
            generated_or_source="source_visual",
        )
        record["status"] = "approved"
        record["updated_at"] = self._now()
        return record

    def _deterministic_render(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        task = dict(record["task"])
        classification = dict(record["classification"])
        attempt = self._new_attempt(
            attempt_number=1,
            prompt="Deterministic data plot render.",
            feedback=[],
        )
        try:
            produced = self.config.render_adapter(
                {
                    "task": task,
                    "category": DETERMINISTIC_DATA_PLOT,
                    "verified_structured_data": classification[
                        "verified_structured_data"
                    ],
                    "input_data": task.get("input_data") or {},
                }
            )
        except Exception as exc:
            produced = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        produced = produced if isinstance(produced, dict) else {}
        attempt["result"] = produced
        attempt["cost_placeholder"] = _cost_placeholder(produced)
        if not _produced_success(produced):
            attempt["status"] = "adapter_failed"
            attempt["finished_at"] = self._now()
            record["attempts"] = [attempt]
            record["status"] = "exhausted_unfilled"
            record["unfilled_need"] = self._unfilled_need(
                record,
                [attempt],
                reason="deterministic_render_failed",
            )
            record["updated_at"] = self._now()
            return record

        attempt["status"] = "approved"
        attempt["finished_at"] = self._now()
        attempt["review"] = {
            "verdict": "deterministic_verified",
            "approved": True,
            "reviewer": "verified_structured_data",
        }
        output_ref = str(
            produced.get("local_path") or produced.get("output_path") or ""
        )
        output_sha256 = str(
            produced.get("sha256")
            or produced.get("artifact_sha256")
            or _sha256_file(output_ref)
        )
        lineage = [
            {
                "action": "deterministic_render",
                "verified_structured_data": True,
                "input_data_fingerprint": _sha256_text(
                    _canonical_json(task.get("input_data") or {})
                ),
                "output_ref": output_ref,
                "sha256": output_sha256,
            }
        ]
        record["attempts"] = [attempt]
        record["result"] = self._approved_result(
            record=record,
            attempts=[attempt],
            produced=produced,
            review=attempt["review"],
            lineage=lineage,
            local_path=output_ref,
            sha256=output_sha256,
            generated_or_source="deterministic_data_plot",
        )
        record["status"] = "approved"
        record["updated_at"] = self._now()
        return record

    def _revision_loop(self, record: dict[str, Any]) -> dict[str, Any]:
        task = dict(record["task"])
        classification = dict(record["classification"])
        category = str(classification["category"])
        attempts: list[dict[str, Any]] = []
        feedback: list[str] = []
        base_prompt = build_prompt(task)
        max_attempts = self.config.max_attempts

        for attempt_number in range(1, max_attempts + 1):
            prompt = apply_revision_feedback(
                base_prompt,
                feedback,
                attempt_number,
            )
            attempt = self._new_attempt(
                attempt_number=attempt_number,
                prompt=prompt,
                feedback=feedback,
            )
            if category == ENHANCED_SOURCE:
                adapter = self.config.enhancement_adapter
                payload = {
                    "task": task,
                    "prompt": prompt,
                    "category": category,
                    "attempt_number": attempt_number,
                    "operations": list(
                        classification["enhancement_operations"]
                    ),
                }
            else:
                adapter = self.config.generation_adapter
                payload = {
                    "task": task,
                    "prompt": prompt,
                    "category": category,
                    "attempt_number": attempt_number,
                }
            try:
                produced = adapter(payload)
            except Exception as exc:
                produced = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            produced = produced if isinstance(produced, dict) else {}
            attempt["result"] = produced
            attempt["cost_placeholder"] = _cost_placeholder(produced)
            if self._adapter_would_overwrite_source(task, produced):
                produced = {
                    **produced,
                    "status": "failed",
                    "error": "adapter_would_overwrite_source",
                }
                attempt["result"] = produced
            if not _produced_success(produced):
                attempt["status"] = "adapter_failed"
                attempt["finished_at"] = self._now()
                feedback.append(
                    "Adapter failed: "
                    + str(
                        produced.get("error")
                        or produced.get("status")
                        or "unknown"
                    )
                )
                attempts.append(attempt)
                continue

            try:
                review = self.config.review_adapter(
                    {
                        "task": task,
                        "attempt_number": attempt_number,
                        "prompt": prompt,
                        "produced": produced,
                    }
                )
            except Exception as exc:
                review = {
                    "verdict": "needs_human_review",
                    "approved": False,
                    "feedback": [
                        f"Review adapter raised {type(exc).__name__}: {exc}"
                    ],
                }
            review = review if isinstance(review, dict) else {}
            attempt["review"] = review
            if _review_approved(review):
                attempt["status"] = "approved"
                attempt["finished_at"] = self._now()
                attempts.append(attempt)
                record["attempts"] = attempts
                output_ref = str(
                    produced.get("local_path")
                    or produced.get("output_path")
                    or ""
                )
                output_sha256 = str(
                    produced.get("sha256")
                    or produced.get("artifact_sha256")
                    or _sha256_file(output_ref)
                )
                lineage = self._build_lineage(
                    record,
                    produced,
                    output_ref,
                    output_sha256,
                )
                record["result"] = self._approved_result(
                    record=record,
                    attempts=attempts,
                    produced=produced,
                    review=review,
                    lineage=lineage,
                    local_path=output_ref,
                    sha256=output_sha256,
                    generated_or_source=(
                        "author_redraw"
                        if category == AUTHOR_REDRAW
                        else "ai_generated_explanatory_visual"
                        if category == AI_GENERATED_EXPLANATORY_VISUAL
                        else "enhanced_source"
                    ),
                )
                record["status"] = "approved"
                record["updated_at"] = self._now()
                return record

            feedback = list(
                dict.fromkeys(
                    [*feedback, *_review_feedback(review)]
                )
            )
            attempt["status"] = "rejected"
            attempt["reviewer_feedback"] = list(feedback)
            attempt["finished_at"] = self._now()
            attempts.append(attempt)

        record["attempts"] = attempts
        record["status"] = "exhausted_unfilled"
        record["unfilled_need"] = self._unfilled_need(
            record,
            attempts,
            reason="attempts_exhausted",
        )
        record["updated_at"] = self._now()
        return record

    # ------------------------------------------------------------------
    # Approval, lineage, and durable records
    # ------------------------------------------------------------------

    def _build_lineage(
        self,
        record: dict[str, Any],
        produced: dict[str, Any],
        output_ref: str,
        output_sha256: str,
    ) -> list[dict[str, Any]]:
        task = dict(record["task"])
        classification = dict(record["classification"])
        category = str(classification["category"])
        source_ref = _source_ref(task)
        source_sha256 = str(
            task.get("source_sha256")
            or _sha256_file(source_ref)
        )
        lineage: list[dict[str, Any]] = []
        if source_ref:
            lineage.append(
                {
                    "action": "source",
                    "ref": source_ref,
                    "sha256": source_sha256,
                    "permission": task.get("permission") or {},
                }
            )
        if category == ENHANCED_SOURCE:
            lineage.append(
                {
                    "action": "enhance",
                    "operation": list(
                        classification["enhancement_operations"]
                    ),
                    "non_semantic": True,
                    "original_preserved": True,
                    "output_ref": output_ref,
                    "sha256": output_sha256,
                }
            )
        elif category == AUTHOR_REDRAW:
            lineage.append(
                {
                    "action": "redraw",
                    "derivative_of": source_ref,
                    "is_enhancement": False,
                    "output_ref": output_ref,
                    "sha256": output_sha256,
                }
            )
        elif category == AI_GENERATED_EXPLANATORY_VISUAL:
            last_prompt = str(
                (record.get("attempts") or [{}])[-1].get("prompt")
                or ""
            )
            lineage.append(
                {
                    "action": "generate",
                    "prompt_fingerprint": _sha256_text(last_prompt),
                    "output_ref": output_ref,
                    "sha256": output_sha256,
                }
            )
        elif category == DETERMINISTIC_DATA_PLOT:
            lineage.append(
                {
                    "action": "deterministic_render",
                    "verified_structured_data": True,
                    "input_data_fingerprint": _sha256_text(
                        _canonical_json(task.get("input_data") or {})
                    ),
                    "output_ref": output_ref,
                    "sha256": output_sha256,
                }
            )
        return lineage

    @staticmethod
    def _adapter_would_overwrite_source(
        task: dict[str, Any],
        produced: Any,
    ) -> bool:
        """Reject adapter output that points back at the source image."""

        source = _source_ref(task)
        if not isinstance(produced, dict):
            return False
        output = str(
            produced.get("local_path") or produced.get("output_path") or ""
        )
        if not source or not output:
            return False
        try:
            return os.path.normcase(
                os.path.abspath(source)
            ) == os.path.normcase(os.path.abspath(output))
        except Exception:
            return False

    def _approved_result(
        self,
        *,
        record: dict[str, Any],
        attempts: list[dict[str, Any]],
        produced: dict[str, Any],
        review: dict[str, Any],
        lineage: list[dict[str, Any]],
        local_path: str,
        sha256: str,
        generated_or_source: str,
    ) -> dict[str, Any]:
        classification = dict(record["classification"])
        costs = [dict(a.get("cost_placeholder") or {}) for a in attempts]
        return {
            "status": "approved",
            "category": classification["category"],
            "purpose": classification["purpose"],
            "route": classification["route"],
            "local_path": local_path,
            "sha256": sha256,
            "mime_type": str(
                produced.get("mime_type") or "image/png"
            ),
            "required_disclosure": classification["required_disclosure"],
            "evidence_status": classification["evidence_status"],
            "explanation_status": (
                "explanatory_not_evidence"
                if classification["category"]
                in {
                    AI_GENERATED_EXPLANATORY_VISUAL,
                    AUTHOR_REDRAW,
                }
                else classification["evidence_status"]
            ),
            "generated_or_source": generated_or_source,
            "review_decision": str(
                review.get("verdict") or "approved"
            ),
            "review": review,
            "lineage": lineage,
            "permission": record["task"].get("permission") or {},
            "attempts": attempts,
            "cost_summary": {
                "attempt_count": len(attempts),
                "cost_placeholders": costs,
                "estimated_cost_cny": 0.0,
            },
            "durable_cache_ready": True,
        }

    def _unfilled_need(
        self,
        record: dict[str, Any],
        attempts: list[dict[str, Any]],
        *,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "visual_need_id": record["record_id"],
            "blocking": False,
            "reason": reason,
            "category": record["classification"]["category"],
            "attempt_count": len(attempts),
            "last_status": (
                attempts[-1].get("status", "") if attempts else ""
            ),
            "suggested_followup": (
                "Human review or deterministic fallback; article completion "
                "must not block on this optional visual."
            ),
        }

    def to_durable_cache_record(
        self,
        record: dict[str, Any],
        article_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a durable-cache-ready record for an approved result.

        Article fields are placeholders unless caller supplies ``article_info``.
        """

        record = dict(record or {})
        result = dict(record.get("result") or {})
        if record.get("status") != "approved" or not result.get(
            "durable_cache_ready"
        ):
            return {
                "status": "not_durable_cache_ready",
                "record_id": record.get("record_id", ""),
                "reason": record.get("status", ""),
                "blocking": False,
            }
        article = (
            dict(article_info)
            if isinstance(article_info, dict)
            else {}
        )
        article_record = {
            "article_id": str(article.get("article_id") or ""),
            "article_title": str(article.get("article_title") or ""),
            "section_id": str(article.get("section_id") or ""),
            "section_title": str(article.get("section_title") or ""),
            "figure_id": str(article.get("figure_id") or ""),
            "figure_number": str(article.get("figure_number") or "TBD"),
            "caption_placeholder": str(
                article.get("caption")
                or "Caption to be finalized by the human reviewer."
            ),
        }
        attempts = list(record.get("attempts") or [])
        lineage = list(result.get("lineage") or [])
        cache_key_payload = {
            "record_id": record["record_id"],
            "category": result.get("category", ""),
            "purpose": result.get("purpose", ""),
            "lineage": lineage,
            "attempt_statuses": [
                {
                    "attempt_number": a.get("attempt_number"),
                    "status": a.get("status"),
                }
                for a in attempts
            ],
        }
        return {
            "schema_version": DURABLE_RECORD_SCHEMA_VERSION,
            "cache_kind": "visual_transformation_durable_cache",
            "cache_key": _sha256_text(_canonical_json(cache_key_payload)),
            "record_id": record["record_id"],
            "status": "approved",
            "category": result.get("category", ""),
            "purpose": result.get("purpose", ""),
            "generated_or_source": result.get("generated_or_source", ""),
            "local_path": result.get("local_path", ""),
            "sha256": result.get("sha256", ""),
            "mime_type": result.get("mime_type", "image/png"),
            "required_disclosure": result.get("required_disclosure", ""),
            "evidence_status": result.get("evidence_status", ""),
            "explanation_status": result.get("explanation_status", ""),
            "review_decision": result.get("review_decision", ""),
            "article_info": article_record,
            "lineage": lineage,
            "permission": result.get("permission", {}),
            "attempts": attempts,
            "costs": [
                dict(a.get("cost_placeholder") or {})
                for a in attempts
            ],
            "durable_cache_ready": True,
        }
