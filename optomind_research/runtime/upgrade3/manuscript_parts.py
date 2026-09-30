"""Small, dependency-free validation for the manuscript responsibility contract.

This is not a planner. Keep validation equivalent to the checked-in v1 schema;
semantic quality belongs to the material-informed planner and editorial review.
Publication headings never determine whether a task is substantive BODY work.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

PARTS_CONTRACT_VERSION = "optomind.manuscript_parts_plan.v1"
PART_NAMES = ("abstract", "introduction", "conclusion")
PART_FIELDS = {"purpose", "focus", "boundary", "placement", "finalize_from"}


class ManuscriptPartsError(ValueError):
    """Invalid responsibility contract or explicit BODY/part category mixing."""


def _object(value: Any, keys: set[str], location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManuscriptPartsError(f"{location}:expected_object")
    missing, extra = keys - set(value), set(value) - keys
    if missing or extra:
        raise ManuscriptPartsError(
            f"{location}:missing={sorted(missing)}:unexpected={sorted(extra, key=str)}")
    return value


def _string(value: Any, location: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str) or (nonempty and len(value) == 0):
        raise ManuscriptPartsError(f"{location}:expected_{'nonempty_' if nonempty else ''}string")


def validate_manuscript_parts_plan(value: Any) -> dict[str, Any]:
    """Validate exact v1 shape and return a detached copy, never a default card.

    Empty arrays/semantic anchors remain schema-valid: no paragraph quotas or
    mandatory outline template are imposed by the software validator.
    """
    plan = _object(value, {"context", *PART_NAMES}, "manuscript_parts_plan")
    _string(plan["context"], "context", nonempty=True)
    for name in PART_NAMES:
        part = _object(plan[name], PART_FIELDS, name)
        _string(part["purpose"], f"{name}.purpose", nonempty=True)
        for field in ("focus", "boundary", "finalize_from"):
            rows = part[field]
            if not isinstance(rows, list):
                raise ManuscriptPartsError(f"{name}.{field}:expected_array")
            for index, item in enumerate(rows):
                _string(item, f"{name}.{field}[{index}]")
        placement = _object(part["placement"], {"mode", "anchor"}, f"{name}.placement")
        if placement["mode"] not in ("standalone", "embedded", "distributed"):
            raise ManuscriptPartsError(f"{name}.placement.mode:unsupported_value")
        _string(placement["anchor"], f"{name}.placement.anchor")
    return deepcopy(dict(plan))


def boundary_projection(plan: Any) -> dict[str, Any]:
    """Only reader task and BODY boundaries, never a second chapter assignment."""
    validated = validate_manuscript_parts_plan(plan)
    return {name: {key: validated[name][key] for key in ("purpose", "focus", "boundary")}
            for name in ("introduction", "conclusion")}


def validate_body_tasks(rows: Any) -> None:
    """Reject explicit responsibility-card leakage without guessing from titles.

    Caller supplies normalized chapter rows. A BODY task titled Introduction,
    Outlook or Conclusion is legitimate. This checks category/shape, not prose.
    """
    if not isinstance(rows, (list, tuple)):
        raise ManuscriptPartsError("body_tasks:expected_array")
    light_roles = {"manuscript_part", "manuscript-level", "manuscript_level",
                   "lightweight_part", "abstract", "introduction", "conclusion"}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ManuscriptPartsError(f"body_tasks[{index}]:expected_object")
        # Nested packet shape is accepted for final-assembly verification.
        chapter = row.get("chapter", row)
        if not isinstance(chapter, Mapping):
            raise ManuscriptPartsError(f"body_tasks[{index}].chapter:expected_object")
        if PART_FIELDS.issubset(chapter) or "manuscript_parts_plan" in chapter:
            raise ManuscriptPartsError(f"body_tasks[{index}]:part_plan_is_not_body_task")
        for field in ("knowledge_role", "task_type", "planning_role"):
            role = chapter.get(field)
            if isinstance(role, str) and role.casefold() in light_roles:
                raise ManuscriptPartsError(f"body_tasks[{index}]:explicit_manuscript_role:{field}")
