"""Revision planner — pure Python, no LLM calls.

Reads a GLOBAL_AUDIT_REPORT.json and produces a minimal REVISION_PLAN.json
that classifies root causes and specifies concrete revision actions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Maps audit flag type → (root_cause, revision_action, requires_rerun)
_FLAG_CLASSIFICATION: Dict[str, tuple] = {
    "duplicate_content":           ("structural",   "remove_duplicate_paragraph",         False),
    "missing_transition":          ("transition",   "rerun_section_with_transition_fix",  True),
    "terminology_inconsistency":   ("terminology",  "standardize_term_usage",             False),
    "orphaned_conclusion":         ("transition",   "rerun_section_with_transition",      True),
    "visual_gap":                  ("visual",       "append_conceptual_figure_request",   False),
    "classification_confusion":    ("structural",   "escalate_to_human_review",           False),
    # Layer-2 editors are intentionally free to name the same root cause in
    # different scholarly language.  These canonical families keep that
    # variation from degrading into a silent no-op.
    "source_diversity":             ("literature",   "rerun_section_with_source_synthesis", True),
    "citation_concentration":       ("literature",   "rerun_section_with_source_synthesis", True),
    "source_concentration":         ("literature",   "rerun_section_with_source_synthesis", True),
    "literature_breadth":           ("literature",   "rerun_section_with_source_synthesis", True),
    "insufficient_source_diversity": ("literature",  "rerun_section_with_source_synthesis", True),
    "evidence_synthesis":           ("literature",   "rerun_section_with_source_synthesis", True),
    "missing_pivotal_evidence":     ("literature",   "request_literature_then_rerun",       True),
    "argumentative_structure":      ("structure",    "rerun_section_with_role_boundary",    True),
    "scope_bleed":                  ("structure",    "rerun_section_with_role_boundary",    True),
    "section_role_fulfillment":     ("structure",    "rerun_section_with_role_boundary",    True),
    "thesis_leakage":               ("structure",    "rerun_section_with_role_boundary",    True),
    "narrative_progression":        ("structure",    "rerun_section_with_role_boundary",    True),
}

# Flag types that cannot be auto-resolved
_HUMAN_REVIEW_TYPES = {"classification_confusion"}


class RevisionPlan:
    """Structured revision plan output."""

    def __init__(self, round_num: int, flags: List[Dict[str, Any]]) -> None:
        self.round_num = round_num
        self.auto_resolvable: List[Dict[str, Any]] = []
        self.human_review: List[Dict[str, Any]] = []
        self.revisions: List[Dict[str, Any]] = []
        self.sections_to_revise: List[str] = []
        self.sections_to_patch_inline: List[str] = []
        self._classify(flags)

    def _classify(self, flags: List[Dict[str, Any]]) -> None:
        rerun_sections = set()
        inline_sections = set()

        for flag in flags:
            flag_type = flag.get("type", "")
            flag_id = flag.get("flag_id", "")
            section_ids = flag.get("section_ids", [])

            classification = _FLAG_CLASSIFICATION.get(flag_type)
            if classification is None:
                root_cause, action, requires_rerun = (
                    "unknown",
                    "escalate_to_human_review",
                    False,
                )
            else:
                root_cause, action, requires_rerun = classification

            revision = {
                "flag_id": flag_id,
                "flag_type": flag_type,
                "root_cause": root_cause,
                "action": action,
                "requires_rerun": requires_rerun,
                "target_sections": section_ids,
                "description": flag.get("description", ""),
                "editor_root_cause": flag.get("root_cause", ""),
                "recommended_action": flag.get("recommended_action", ""),
            }

            if flag_type in _HUMAN_REVIEW_TYPES or classification is None:
                self.human_review.append(revision)
            else:
                self.auto_resolvable.append(revision)
                self.revisions.append(revision)

                if requires_rerun:
                    # For reruns, pick the "source" section (first in list)
                    for sid in section_ids[:1]:
                        rerun_sections.add(sid)
                else:
                    # Inline patch: pick lower-priority section (second if available)
                    target = section_ids[-1] if section_ids else None
                    if target:
                        inline_sections.add(target)

        self.sections_to_revise = sorted(rerun_sections)
        self.sections_to_patch_inline = sorted(inline_sections - rerun_sections)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "phase4.revision_plan.v1",
            "round": self.round_num,
            "auto_resolvable_flags": self.auto_resolvable,
            "human_review_flags": self.human_review,
            "sections_to_revise": self.sections_to_revise,
            "sections_to_patch_inline": self.sections_to_patch_inline,
            "revisions": self.revisions,
            "created_at": _now(),
        }


class RevisionPlanner:
    """Pure Python revision planner. No LLM calls."""

    def plan(
        self,
        audit_report: Dict[str, Any],
        section_registry: Optional[Dict[str, Any]] = None,
        work_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Classify audit flags into a revision plan.

        Args:
            audit_report: GLOBAL_AUDIT_REPORT.json content.
            section_registry: Optional SECTION_REGISTRY.json for section ordering.
            work_dir: If given, writes REVISION_PLAN.json into revision_round_{N}/ subdir.
        """
        round_num = audit_report.get("round", 1)
        flags = audit_report.get("flags", [])

        plan = RevisionPlan(round_num=round_num, flags=flags)
        plan_dict = plan.to_dict()

        if work_dir is not None:
            plan_path = work_dir / f"revision_round_{round_num}" / "REVISION_PLAN.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan_dict, indent=2), encoding="utf-8")

        return plan_dict
