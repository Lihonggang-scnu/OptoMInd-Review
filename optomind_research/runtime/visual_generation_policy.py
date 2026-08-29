"""Deterministic policy for visual transformation and generation tasks.

This is an additive sidecar module for the existing Qwen conceptual visual
generator/factory.  It never calls a model and never edits the existing
generator, factory, editor, prompts, or configuration.  The module owns the
boundary decisions that must hold regardless of which model or image
processing adapter is injected later:

* classify a task as source visual, enhanced source, author redraw,
  AI-generated explanatory visual, or deterministic data plot;
* allow AI generation only for conceptual mechanism, method/process/workflow,
  taxonomy/field map, and qualitative timeline/roadmap requests;
* treat any generative restyle of a source image as a derivative redraw,
  never as simple enhancement;
* require deterministic rendering from verified structured data before any
  quantitative comparison may proceed.
"""

from __future__ import annotations

import re
from typing import Any


SCHEMA_VERSION = "visual_generation_policy.v1"

# Visual task categories.
SOURCE_VISUAL = "source_visual"
ENHANCED_SOURCE = "enhanced_source"
AUTHOR_REDRAW = "author_redraw"
AI_GENERATED_EXPLANATORY_VISUAL = "ai_generated_explanatory_visual"
DETERMINISTIC_DATA_PLOT = "deterministic_data_plot"

VALID_CATEGORIES = frozenset(
    {
        SOURCE_VISUAL,
        ENHANCED_SOURCE,
        AUTHOR_REDRAW,
        AI_GENERATED_EXPLANATORY_VISUAL,
        DETERMINISTIC_DATA_PLOT,
    }
)

# Generation purposes.
CONCEPTUAL_MECHANISM = "conceptual_mechanism"
METHOD_PROCESS_WORKFLOW = "method_process_workflow"
TAXONOMY_FIELD_MAP = "taxonomy_field_map"
QUALITATIVE_TIMELINE_ROADMAP = "qualitative_timeline_roadmap"

SYNTHETIC_EMPIRICAL_CURVE = "synthetic_empirical_curve"
SPECTRUM = "spectrum"
MICROSCOPY = "microscopy"
MEASURED_OR_SIMULATED_FIELD = "measured_or_simulated_field"
APPARATUS_EVIDENCE = "apparatus_evidence"
QUANTITATIVE_COMPARISON = "quantitative_comparison"

UNCLASSIFIED_PURPOSE = "unclassified"

ALLOWED_GENERATION_PURPOSES = frozenset(
    {
        CONCEPTUAL_MECHANISM,
        METHOD_PROCESS_WORKFLOW,
        TAXONOMY_FIELD_MAP,
        QUALITATIVE_TIMELINE_ROADMAP,
    }
)

PROHIBITED_GENERATION_PURPOSES = frozenset(
    {
        SYNTHETIC_EMPIRICAL_CURVE,
        SPECTRUM,
        MICROSCOPY,
        MEASURED_OR_SIMULATED_FIELD,
        APPARATUS_EVIDENCE,
        QUANTITATIVE_COMPARISON,
    }
)

# Non-semantic enhancement operations only.  Anything else is a semantic
# transformation and must be treated as a derivative redraw.
VALID_ENHANCEMENT_OPERATIONS = frozenset(
    {
        "scale",
        "denoise",
        "contrast",
        "sharpness",
    }
)

_PROHIBITED_PURPOSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        SYNTHETIC_EMPIRICAL_CURVE,
        (
            "synthetic empirical",
            "empirical curve",
            "synthetic curve",
            "curve fit",
            "fitted curve",
            "measured curve",
        ),
    ),
    (
        SPECTRUM,
        (
            "spectrum",
            "spectra",
            "spectral",
        ),
    ),
    (
        MICROSCOPY,
        (
            "microscopy",
            "micrograph",
            "sem image",
            "tem image",
            "afm image",
            "electron microscope",
        ),
    ),
    (
        MEASURED_OR_SIMULATED_FIELD,
        (
            "measured field",
            "simulated field",
            "simulation field",
            "temperature field",
            "electric field",
            "magnetic field",
            "field distribution",
            "field profile",
        ),
    ),
    (
        APPARATUS_EVIDENCE,
        (
            "apparatus",
            "experimental setup",
            "measurement setup",
            "test setup",
            "setup photo",
            "equipment photo",
        ),
    ),
    (
        QUANTITATIVE_COMPARISON,
        (
            "quantitative comparison",
            "comparison",
            "compare",
            "benchmark",
            "performance matrix",
            "scatter plot",
            "bar chart",
            "bar plot",
            "versus",
            " vs ",
            "quantitative",
        ),
    ),
)

_ALLOWED_PURPOSE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        QUALITATIVE_TIMELINE_ROADMAP,
        (
            "timeline",
            "roadmap",
            "historical evolution",
            "historical",
            "evolution",
            "phases",
            "milestones",
        ),
    ),
    (
        CONCEPTUAL_MECHANISM,
        (
            "mechanism",
            "working principle",
            "conceptual schematic",
            "conceptual diagram",
            "pathway",
        ),
    ),
    (
        METHOD_PROCESS_WORKFLOW,
        (
            "method",
            "process",
            "workflow",
            "methodology",
            "flow",
            "procedure",
            "pipeline",
        ),
    ),
    (
        TAXONOMY_FIELD_MAP,
        (
            "taxonomy",
            "classification",
            "field map",
            "landscape",
            "map",
            "categories",
            "categor",
        ),
    ),
)

_DENIED_PERMISSION_STATUSES = frozenset(
    {
        "display_only",
        "no_derivatives",
        "review_only",
    }
)


def _compact(value: Any, limit: int = 1600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def classify_purpose(value: Any) -> str:
    """Map free-text purpose to a deterministic purpose key.

    Prohibited patterns win over allowed patterns so ambiguous phrases such as
    "simulated field map" can never be admitted as a qualitative field map.
    """

    text = _compact(value).lower()
    if not text:
        return UNCLASSIFIED_PURPOSE
    for purpose, phrases in _PROHIBITED_PURPOSE_PATTERNS:
        if any(phrase in text for phrase in phrases):
            return purpose
    for purpose, phrases in _ALLOWED_PURPOSE_PATTERNS:
        if any(phrase in text for phrase in phrases):
            return purpose
    return UNCLASSIFIED_PURPOSE


def normalize_enhancement_operations(value: Any) -> tuple[list[str], list[str]]:
    """Return ``(allowed_operations, rejected_operations)``.

    Unknown operations are not silently dropped: they force the task to the
    derivative redraw category.
    """

    if not isinstance(value, (list, tuple, set)):
        return [], []
    allowed: list[str] = []
    rejected: list[str] = []
    for raw in value:
        operation = str(raw or "").strip().lower()
        if not operation:
            continue
        if operation in VALID_ENHANCEMENT_OPERATIONS:
            if operation not in allowed:
                allowed.append(operation)
        elif operation not in rejected:
            rejected.append(operation)
    return allowed, rejected


def _source_ref(task: dict[str, Any]) -> str:
    return _compact(
        task.get("source_ref")
        or task.get("source_path")
        or task.get("local_image_path")
        or task.get("original_path")
    )


def _has_structured_data(task: dict[str, Any]) -> bool:
    data = task.get("input_data") or task.get("structured_data") or {}
    if not isinstance(data, dict) or not data:
        return False
    if isinstance(data.get("series"), list) and data["series"]:
        return True
    return bool(
        isinstance(data.get("values"), list)
        and data["values"]
        and isinstance(data.get("categories"), list)
        and data["categories"]
    )


def _verified_structured_data(task: dict[str, Any]) -> bool:
    if task.get("verified_structured_data") is True:
        return True
    verification = task.get("data_verification") or {}
    if isinstance(verification, dict) and (
        verification.get("status") == "verified"
        or verification.get("verified") is True
    ):
        return True
    provenance = _compact(task.get("data_provenance_level")).lower()
    return "verified" in provenance or provenance == "exact"


def _redraw_requested(
    task: dict[str, Any],
    rejected_operations: list[str],
) -> bool:
    if task.get("generative_restyle") is True or task.get("redraw") is True:
        return True
    if task.get("derivative") is True:
        return True
    transformation = _compact(task.get("transformation")).lower()
    if transformation in {"redraw", "restyle", "derivative"}:
        return True
    return bool(rejected_operations)


def _explicit_transform_denied(permission: Any) -> bool:
    if isinstance(permission, dict):
        if (
            permission.get("transform_allowed") is False
            or permission.get("enhancement_allowed") is False
            or permission.get("redraw_allowed") is False
        ):
            return True
        status = _compact(permission.get("status")).lower()
        return status in _DENIED_PERMISSION_STATUSES
    if isinstance(permission, str):
        return _compact(permission).lower() in _DENIED_PERMISSION_STATUSES
    return False


def _permission_status(task: dict[str, Any]) -> str:
    permission = task.get("permission")
    if permission is None or permission == "":
        return "unstated"
    return "denied" if _explicit_transform_denied(permission) else "preserved"


def _disclosure_for(category: str) -> tuple[bool, str, str]:
    if category == AI_GENERATED_EXPLANATORY_VISUAL:
        return (
            True,
            "AI-generated explanatory visual; not empirical evidence.",
            "explanatory_not_empirical_evidence",
        )
    if category == AUTHOR_REDRAW:
        return (
            True,
            (
                "Author redraw derived from the source visual; explanatory, "
                "not empirical evidence."
            ),
            "explanatory_not_empirical_evidence",
        )
    if category == ENHANCED_SOURCE:
        return (
            True,
            (
                "Enhanced source visual; the original image, hash, and "
                "permission are preserved."
            ),
            "enhanced_source_visual",
        )
    if category == DETERMINISTIC_DATA_PLOT:
        return (
            True,
            (
                "Deterministic data plot rendered from verified structured "
                "data; not AI-generated and not paper evidence."
            ),
            "deterministic_data_plot",
        )
    return (
        False,
        "Original source visual; no transformation applied.",
        "source_visual",
    )


def classify_visual_task(task: dict[str, Any]) -> dict[str, Any]:
    """Classify a visual transformation task and adjudicate policy.

    The returned record is the deterministic contract consumed by the
    workflow state machine.  No model or image processing adapter is invoked
    here.  Invalid or missing input is never allowed to raise: it is treated
    as an unclassified generation request and denied by policy, so a caller
    can record it as a nonblocking gap instead of blocking the manuscript
    workflow.
    """

    task = dict(task) if isinstance(task, dict) else {}
    purpose_text = _compact(
        task.get("purpose")
        or task.get("argument_role")
        or task.get("generation_brief")
        or task.get("argumentative_purpose")
    )
    purpose = classify_purpose(purpose_text)
    allowed_operations, rejected_operations = normalize_enhancement_operations(
        task.get("enhancement_operations")
        or task.get("operations")
        or []
    )
    source_ref = _source_ref(task)
    verified = _verified_structured_data(task)
    has_data = _has_structured_data(task)
    explicit_category = str(task.get("category") or "").strip()

    if explicit_category in VALID_CATEGORIES:
        category = explicit_category
        reason = "explicit_category"
        if category == ENHANCED_SOURCE and rejected_operations:
            category = AUTHOR_REDRAW
            reason = "explicit_enhancement_has_rejected_operations"
    elif (
        task.get("deterministic_render") is True
        or str(task.get("render_mode") or "").lower()
        == "deterministic_data_plot"
        or (purpose == QUANTITATIVE_COMPARISON and verified and has_data)
    ):
        category = DETERMINISTIC_DATA_PLOT
        reason = "deterministic_data_plot_route"
    elif source_ref:
        if _redraw_requested(task, rejected_operations):
            category = AUTHOR_REDRAW
            reason = "generative_restyle_is_derivative_redraw"
        elif allowed_operations:
            category = ENHANCED_SOURCE
            reason = "non_semantic_enhancement_route"
        else:
            category = SOURCE_VISUAL
            reason = "untouched_source_visual"
    else:
        category = AI_GENERATED_EXPLANATORY_VISUAL
        reason = "no_source_visual_generation_route"

    if category == DETERMINISTIC_DATA_PLOT:
        route = "deterministic_render"
    elif category == ENHANCED_SOURCE:
        route = "non_semantic_enhancement"
    elif category == AUTHOR_REDRAW:
        route = "generative_redraw"
    elif category == AI_GENERATED_EXPLANATORY_VISUAL:
        route = "ai_generation"
    else:
        route = "passthrough_source"

    denied_reason = ""
    generation_allowed = True
    enhancement_allowed = True
    if category == DETERMINISTIC_DATA_PLOT:
        generation_allowed = bool(verified and has_data)
        if not generation_allowed:
            denied_reason = (
                "quantitative_or_data_visual_requires_verified_structured_"
                "data_deterministic_render"
            )
    elif category in {AI_GENERATED_EXPLANATORY_VISUAL, AUTHOR_REDRAW}:
        if purpose in PROHIBITED_GENERATION_PURPOSES:
            generation_allowed = False
            denied_reason = f"prohibited_generation_purpose:{purpose}"
        elif purpose == UNCLASSIFIED_PURPOSE:
            generation_allowed = False
            denied_reason = "unclassified_generation_purpose"

    permission_denied = _explicit_transform_denied(task.get("permission"))
    if (
        category in {ENHANCED_SOURCE, AUTHOR_REDRAW}
        and permission_denied
    ):
        enhancement_allowed = False
        denied_reason = "permission_denies_transform"

    policy_decision = (
        "denied"
        if denied_reason or not generation_allowed or not enhancement_allowed
        else "allowed"
    )
    disclosure_required, required_disclosure, evidence_status = (
        _disclosure_for(category)
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": str(
            task.get("task_id")
            or task.get("visual_plan_id")
            or ""
        ),
        "category": category,
        "category_reason": reason,
        "purpose": purpose,
        "purpose_text": purpose_text,
        "route": route,
        "policy_decision": policy_decision,
        "denied_reason": denied_reason,
        "generation_allowed": generation_allowed,
        "enhancement_allowed": enhancement_allowed,
        "enhancement_operations": list(allowed_operations),
        "rejected_enhancement_operations": list(rejected_operations),
        "semantic_change": category == AUTHOR_REDRAW,
        "verified_structured_data": verified,
        "has_input_data": has_data,
        "disclosure_required": disclosure_required,
        "required_disclosure": required_disclosure,
        "evidence_status": evidence_status,
        "permission_status": _permission_status(task),
    }


def validate_enhancement_operations(
    operations: Any,
) -> dict[str, Any]:
    """Public helper for callers that only need operation validation."""

    allowed, rejected = normalize_enhancement_operations(operations)
    return {
        "valid": not rejected,
        "allowed_operations": allowed,
        "rejected_operations": rejected,
    }
