"""Canonical visual-argument protocol adapters.

The upstream visual profiler is deliberately richer and more open-ended than
M4's eight argument roles. This module converts either a freshly profiled
visual chunk or a canonical-KB record into the stable M4 protocol. The
conversion does not pretend to re-read the image: it canonicalises labels that
were already produced from image, caption, and nearby text by the vision
model. Claim-specific multimodal acceptance remains the reranker's job.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


VALID_VISUAL_ARGUMENT_TYPES: frozenset[str] = frozenset({
    "mechanism_anchor",
    "taxonomy_or_roadmap",
    "method_or_workflow",
    "quantitative_comparison",
    "trend_or_parameter_map",
    "representative_example",
    "anomaly_or_limitation",
    "synthesis_overview",
})


def _mapping_text(record: dict[str, Any]) -> str:
    profile = record.get("visual_profile") if isinstance(record.get("visual_profile"), dict) else {}
    intrinsic = profile.get("intrinsic_visual_labels") if isinstance(profile.get("intrinsic_visual_labels"), dict) else {}
    task = profile.get("review_task_labels") if isinstance(profile.get("review_task_labels"), dict) else {}
    values: list[Any] = [
        record.get("visual_role"), record.get("visual_content_type"),
        intrinsic.get("visual_role"), intrinsic.get("functional_visual_type"),
        intrinsic.get("visual_content_type"), intrinsic.get("concise_label"),
        task.get("argument_function"), task.get("visual_evidence_use"),
        task.get("best_section_roles"), record.get("caption"),
        record.get("subfigure_caption_focus"),
    ]
    return " ".join(str(value) for value in values if value).lower().replace("-", "_")


def infer_visual_argument_type(record: dict[str, Any]) -> str:
    """Map existing multimodal labels to one stable argument-function role."""
    explicit = str(record.get("visual_argument_type") or "").strip()
    if explicit in VALID_VISUAL_ARGUMENT_TYPES:
        return explicit
    text = _mapping_text(record)
    if any(token in text for token in (
        "anomaly", "limitation", "failure", "degradation", "error map",
        "uncertainty", "artifact", "side_lobe", "sidelobe",
    )):
        return "anomaly_or_limitation"
    if any(token in text for token in (
        "workflow", "fabrication_process", "experimental_setup", "architecture",
        "pipeline", "procedure", "process flow", "training framework",
    )):
        return "method_or_workflow"
    if any(token in text for token in (
        "taxonomy", "roadmap", "timeline", "classification", "design space",
        "landscape", "decision tree",
    )):
        return "taxonomy_or_roadmap"
    if any(token in text for token in (
        "benchmark", "performance_comparison", "comparison_plot", "comparative",
        "quantitative comparison", "baseline_comparison", "bar_chart",
        "table_like", "box_plot",
    )):
        return "quantitative_comparison"
    if any(token in text for token in (
        "spectrum", "spectral", "line_plot", "line_chart", "heatmap", "contour",
        "polar_plot", "parameter map", "response map", "learning_curve", "trend",
        "dispersion", "transmittance", "reflectance", "absorptance",
    )):
        return "trend_or_parameter_map"
    if any(token in text for token in (
        "mechanism", "field_distribution", "mode profile", "phase distribution",
        "causal", "physical principle",
    )):
        return "mechanism_anchor"
    if any(token in text for token in (
        "overview", "summary", "synthesis", "conceptual_diagram", "mixed",
    )):
        return "synthesis_overview"
    if any(token in text for token in (
        "photograph", "micrograph", "device_structure", "material_structure",
        "demonstration", "representative", "sample image", "fabricated device",
    )):
        return "representative_example"
    if "schematic" in text or "illustrat" in text:
        return "mechanism_anchor"
    return "representative_example"


def derive_visual_argument_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Return canonical M4 fields without fabricating claim-specific support."""
    profile = record.get("visual_profile") if isinstance(record.get("visual_profile"), dict) else {}
    intrinsic = profile.get("intrinsic_visual_labels") if isinstance(profile.get("intrinsic_visual_labels"), dict) else {}
    task = profile.get("review_task_labels") if isinstance(profile.get("review_task_labels"), dict) else {}
    qa = profile.get("qa") if isinstance(profile.get("qa"), dict) else {}
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}

    image_path = str(record.get("local_image_path") or "").strip()
    fatal = bool(quality.get("failure_reason")) or not image_path or not (intrinsic or task)
    if image_path and not Path(image_path).exists():
        fatal = True
    if str(task.get("review_utility") or record.get("review_utility") or "").lower() == "exclude":
        fatal = True
    if str(intrinsic.get("visual_role") or record.get("visual_role") or "").lower() == "unclear":
        fatal = True

    confidence = str(record.get("visual_argument_confidence") or qa.get("confidence") or "medium").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    claims = task.get("candidate_claims_supported_by_caption_or_text") or task.get(
        "candidate_claims_supported_by_caption"
    ) or []
    if not isinstance(claims, list):
        claims = [str(claims)] if claims else []

    return {
        "visual_argument_type": infer_visual_argument_type(record),
        "visual_argument_status": "failed" if fatal else "ok",
        "visual_argument_confidence": confidence,
        "visual_argument_claim": "; ".join(str(item).strip() for item in claims[:3] if str(item).strip()),
        "visual_argument_needs_human_review": bool(
            record.get("visual_argument_needs_human_review", qa.get("needs_human_review", False))
        ),
        "visual_argument_schema_version": "visual_argument_protocol.v1",
    }
