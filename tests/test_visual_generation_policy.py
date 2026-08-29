"""Deterministic policy tests for the visual transformation sidecar."""

from __future__ import annotations

import pytest

from optomind_research.runtime.visual_generation_policy import (
    AI_GENERATED_EXPLANATORY_VISUAL,
    AUTHOR_REDRAW,
    DETERMINISTIC_DATA_PLOT,
    ENHANCED_SOURCE,
    SOURCE_VISUAL,
    classify_visual_task,
    validate_enhancement_operations,
)


def test_classifies_all_five_categories() -> None:
    source = classify_visual_task(
        {
            "purpose": "representative source figure",
            "source_ref": "src.png",
        }
    )
    enhanced = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "enhancement_operations": ["scale", "contrast"],
        }
    )
    redraw = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "generative_restyle": True,
        }
    )
    generated = classify_visual_task(
        {"purpose": "conceptual mechanism schematic"}
    )
    plot = classify_visual_task(
        {
            "purpose": "quantitative comparison",
            "render_mode": "deterministic_data_plot",
            "verified_structured_data": True,
            "input_data": {
                "series": [
                    {"label": "a", "x": [1, 2], "y": [1, 2]}
                ]
            },
        }
    )

    assert source["category"] == SOURCE_VISUAL
    assert source["policy_decision"] == "allowed"
    assert enhanced["category"] == ENHANCED_SOURCE
    assert enhanced["route"] == "non_semantic_enhancement"
    assert redraw["category"] == AUTHOR_REDRAW
    assert redraw["route"] == "generative_redraw"
    assert generated["category"] == AI_GENERATED_EXPLANATORY_VISUAL
    assert generated["policy_decision"] == "allowed"
    assert plot["category"] == DETERMINISTIC_DATA_PLOT
    assert plot["route"] == "deterministic_render"


@pytest.mark.parametrize(
    ("text", "purpose"),
    [
        (
            "A conceptual mechanism schematic of phonon-polariton resonance.",
            "conceptual_mechanism",
        ),
        (
            "A method and workflow diagram for the fabrication process.",
            "method_process_workflow",
        ),
        (
            "A taxonomy and field map of radiative cooling material classes.",
            "taxonomy_field_map",
        ),
        (
            "A qualitative timeline and roadmap of research milestones.",
            "qualitative_timeline_roadmap",
        ),
    ],
)
def test_ai_generation_allowed_purposes(text: str, purpose: str) -> None:
    classification = classify_visual_task({"purpose": text})
    assert classification["purpose"] == purpose
    assert classification["policy_decision"] == "allowed"
    assert classification["generation_allowed"] is True


@pytest.mark.parametrize(
    ("text", "purpose"),
    [
        (
            "A synthetic empirical curve of measured cooling power.",
            "synthetic_empirical_curve",
        ),
        (
            "A measured FTIR emissivity spectrum.",
            "spectrum",
        ),
        (
            "A scanning electron microscopy micrograph.",
            "microscopy",
        ),
        (
            "A simulated temperature field map.",
            "measured_or_simulated_field",
        ),
        (
            "A photograph of the experimental apparatus.",
            "apparatus_evidence",
        ),
        (
            "A quantitative comparison bar chart of three samples.",
            "quantitative_comparison",
        ),
    ],
)
def test_ai_generation_prohibited_purposes(
    text: str,
    purpose: str,
) -> None:
    classification = classify_visual_task({"purpose": text})
    assert classification["purpose"] == purpose
    assert classification["policy_decision"] == "denied"
    assert (
        classification["denied_reason"]
        == f"prohibited_generation_purpose:{purpose}"
    )


def test_quantitative_comparison_never_routes_to_ai_generation() -> None:
    classification = classify_visual_task(
        {"purpose": "quantitative comparison of measured values"}
    )
    assert classification["category"] == AI_GENERATED_EXPLANATORY_VISUAL
    assert classification["route"] == "ai_generation"
    assert classification["policy_decision"] == "denied"


def test_generative_restyle_is_derivative_redraw_not_enhancement() -> None:
    classification = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "enhancement_operations": ["scale", "restyle"],
        }
    )
    assert classification["category"] == AUTHOR_REDRAW
    assert classification["route"] == "generative_redraw"
    assert classification["semantic_change"] is True
    assert classification["enhancement_operations"] == ["scale"]
    assert classification["rejected_enhancement_operations"] == ["restyle"]
    assert classification["policy_decision"] == "allowed"


def test_deterministic_plot_requires_verified_structured_data() -> None:
    denied = classify_visual_task(
        {
            "purpose": "quantitative comparison",
            "render_mode": "deterministic_data_plot",
            "input_data": {
                "series": [
                    {"label": "a", "x": [1, 2], "y": [1, 2]}
                ]
            },
        }
    )
    assert denied["category"] == DETERMINISTIC_DATA_PLOT
    assert denied["policy_decision"] == "denied"
    assert denied["denied_reason"] == (
        "quantitative_or_data_visual_requires_verified_structured_"
        "data_deterministic_render"
    )

    allowed = classify_visual_task(
        {
            "purpose": "quantitative comparison",
            "render_mode": "deterministic_data_plot",
            "verified_structured_data": True,
            "input_data": {
                "series": [
                    {"label": "a", "x": [1, 2], "y": [1, 2]}
                ]
            },
        }
    )
    assert allowed["category"] == DETERMINISTIC_DATA_PLOT
    assert allowed["policy_decision"] == "allowed"


def test_verified_data_plot_detected_without_render_mode() -> None:
    classification = classify_visual_task(
        {
            "purpose": "quantitative comparison of performance",
            "verified_structured_data": True,
            "input_data": {
                "categories": ["a", "b"],
                "values": [1, 2],
            },
        }
    )
    assert classification["category"] == DETERMINISTIC_DATA_PLOT
    assert classification["policy_decision"] == "allowed"


def test_permission_denial_blocks_enhancement_and_redraw() -> None:
    enhanced = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "enhancement_operations": ["scale"],
            "permission": {"transform_allowed": False},
        }
    )
    assert enhanced["category"] == ENHANCED_SOURCE
    assert enhanced["policy_decision"] == "denied"
    assert enhanced["denied_reason"] == "permission_denies_transform"
    assert enhanced["permission_status"] == "denied"

    redraw = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "generative_restyle": True,
            "permission": {"status": "display_only"},
        }
    )
    assert redraw["category"] == AUTHOR_REDRAW
    assert redraw["policy_decision"] == "denied"


def test_permission_is_preserved_when_allowed() -> None:
    classification = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "enhancement_operations": ["scale"],
            "permission": {
                "transform_allowed": True,
                "license": "CC-BY-4.0",
            },
        }
    )
    assert classification["policy_decision"] == "allowed"
    assert classification["permission_status"] == "preserved"


def test_unclassified_generation_purpose_is_denied() -> None:
    classification = classify_visual_task(
        {"purpose": "decorative banner for the article"}
    )
    assert classification["category"] == AI_GENERATED_EXPLANATORY_VISUAL
    assert classification["policy_decision"] == "denied"
    assert classification["denied_reason"] == (
        "unclassified_generation_purpose"
    )


def test_enhancement_operation_validation() -> None:
    report = validate_enhancement_operations(
        ["SCALE", "denoise", "restyle", "blur"]
    )
    assert report["allowed_operations"] == ["scale", "denoise"]
    assert report["rejected_operations"] == ["restyle", "blur"]
    assert report["valid"] is False


def test_explicit_enhancement_category_with_restyle_is_sanitized() -> None:
    classification = classify_visual_task(
        {
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "category": ENHANCED_SOURCE,
            "enhancement_operations": ["contrast", "restyle"],
        }
    )
    assert classification["category"] == AUTHOR_REDRAW
    assert classification["route"] == "generative_redraw"


def test_invalid_input_never_raises() -> None:
    classification = classify_visual_task(None)
    assert classification["policy_decision"] == "denied"
    assert classification["denied_reason"] == (
        "unclassified_generation_purpose"
    )

    classification = classify_visual_task("not a dict")
    assert classification["policy_decision"] == "denied"
    assert classification["category"] == AI_GENERATED_EXPLANATORY_VISUAL
