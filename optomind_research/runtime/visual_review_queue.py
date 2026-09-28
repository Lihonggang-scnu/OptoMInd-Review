"""Truthful human-in-the-loop state for review visual assets.

The batch harness never blocks for interactive input. It writes a queue with a
30-second deadline that a Web UI can update. Test runs are system-approved;
headless production runs use the declared timeout policy without pretending a
human clicked Approve.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List


REVIEW_ACTIONS = {
    "approve",
    "reject",
    "edit_caption",
    "replace_image",
    "regenerate",
}


def build_visual_review_queue(
    figures: Iterable[Dict[str, Any]],
    *,
    test_mode: bool,
    timeout_seconds: int = 30,
    human_decisions: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Create a review queue and apply decisions already supplied by a UI."""

    created = datetime.now(timezone.utc)
    deadline = created + timedelta(seconds=max(0, int(timeout_seconds)))
    decisions = human_decisions or {}
    items: List[Dict[str, Any]] = []
    for figure in figures:
        figure_id = str(figure.get("figure_id") or "")
        supplied = decisions.get(figure_id, {})
        action = str(supplied.get("action") or "").strip().lower()
        if action not in REVIEW_ACTIONS:
            action = ""
        if action == "approve":
            decision = "human_approved"
        elif action == "reject":
            decision = "human_rejected"
        elif test_mode:
            decision = (
                "system_approved_test_mode_with_warnings"
                if list(figure.get("review_flags") or [])
                else "system_approved_test_mode"
            )
        else:
            decision = "timeout_accepted_for_draft"
        items.append(
            {
                "figure_id": figure_id,
                "section_id": str(figure.get("section_id") or ""),
                "local_path": str(figure.get("local_path") or ""),
                "caption_en": str(figure.get("caption_en") or ""),
                "source_route": str(figure.get("source_route") or ""),
                "data_provenance_level": str(
                    figure.get("data_provenance_level") or ""
                ),
                "model_audit_flags": list(
                    figure.get("review_flags") or []
                ),
                "available_actions": sorted(REVIEW_ACTIONS),
                "human_action": action,
                "review_decision": decision,
                "review_note": str(supplied.get("note") or ""),
            }
        )
    return {
        "schema_version": "research_harness.visual_review_queue.v1",
        "created_at": created.isoformat(),
        "deadline_at": deadline.isoformat(),
        "waiting_seconds": max(0, int(timeout_seconds)),
        "test_mode": bool(test_mode),
        "headless_policy": (
            "system_approve_without_wait"
            if test_mode
            else "record_timeout_acceptance_for_draft_without_claiming_human_review"
        ),
        "items": items,
    }


def apply_visual_review_queue(
    figures: List[Dict[str, Any]],
    queue: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply queue decisions and separate explicit human rejections."""

    by_id = {
        str(row.get("figure_id") or ""): row
        for row in queue.get("items", []) or []
        if isinstance(row, dict)
    }
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for source in figures:
        figure = dict(source)
        review = by_id.get(str(figure.get("figure_id") or ""), {})
        decision = str(
            review.get("review_decision")
            or figure.get("review_decision")
            or ""
        )
        figure["review_decision"] = decision
        figure["human_review_action"] = str(
            review.get("human_action") or ""
        )
        figure["human_review_note"] = str(
            review.get("review_note") or ""
        )
        if decision == "human_rejected":
            figure["render_status"] = "rejected"
            rejected.append(figure)
        else:
            accepted.append(figure)
    return accepted, rejected
