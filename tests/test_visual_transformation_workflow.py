"""Workflow state machine tests for the visual transformation sidecar."""

from __future__ import annotations

from typing import Any

from optomind_research.runtime.visual_transformation_workflow import (
    MAX_TOTAL_ATTEMPTS,
    VisualTransformationWorkflow,
    VisualTransformationWorkflowConfig,
)


def _ready_adapter(**overrides: Any) -> Any:
    def adapter(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "status": "ready",
            "local_path": "out.png",
            "sha256": "output-hash-1",
            "mime_type": "image/png",
            **overrides,
        }

    return adapter


def _reviewer(decisions: list[str]) -> Any:
    state = {"calls": 0}

    def reviewer(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        verdict = decisions[min(state["calls"], len(decisions) - 1)]
        state["calls"] += 1
        if verdict == "approve":
            return {
                "verdict": "approve",
                "approved": True,
                "feedback": [],
            }
        return {
            "verdict": "revise",
            "approved": False,
            "feedback": [f"feedback-{state['calls']}"],
        }

    return reviewer


def test_max_three_attempts_then_nonblocking_exhaustion() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            max_attempts=9,
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(
                ["revise", "revise", "revise", "approve"]
            ),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-EXHAUST",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert len(record["attempts"]) == MAX_TOTAL_ATTEMPTS == 3
    assert [attempt["status"] for attempt in record["attempts"]] == [
        "rejected",
        "rejected",
        "rejected",
    ]
    need = record["unfilled_need"]
    assert need["blocking"] is False
    assert need["reason"] == "attempts_exhausted"
    assert need["attempt_count"] == 3
    assert need["category"] == "ai_generated_explanatory_visual"


def test_reviewer_feedback_propagates_to_revised_prompt_and_reaudit() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["revise", "approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-FEEDBACK",
            "purpose": "method process workflow schematic",
        }
    )

    assert record["status"] == "approved"
    assert len(record["attempts"]) == 2
    first, second = record["attempts"]
    assert first["status"] == "rejected"
    assert "feedback-1" in first["reviewer_feedback"]
    assert second["status"] == "approved"
    assert "feedback-1" in second["prompt"]
    assert "REVISION FEEDBACK FROM REVIEWER" in second["prompt"]
    assert second["review"]["verdict"] == "approve"


def test_generated_output_carries_mandatory_disclosure() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-DISCLOSURE",
            "purpose": "taxonomy and field map of material classes",
        }
    )

    result = record["result"]
    assert record["status"] == "approved"
    assert (
        result["required_disclosure"]
        == "AI-generated explanatory visual; not empirical evidence."
    )
    assert result["evidence_status"] == (
        "explanatory_not_empirical_evidence"
    )
    assert result["explanation_status"] == "explanatory_not_evidence"
    assert result["durable_cache_ready"] is True


def test_enhancement_preserves_original_lineage_hash_and_permission() -> None:
    source_hash = "source-hash-1"
    output_hash = "enhanced-hash-1"

    def enhancement_adapter(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert payload["operations"] == ["scale", "contrast"]
        return {
            "status": "ready",
            "local_path": "enhanced.png",
            "sha256": output_hash,
            "mime_type": "image/png",
        }

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            enhancement_adapter=enhancement_adapter,
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-ENHANCED",
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "source_sha256": source_hash,
            "enhancement_operations": ["scale", "contrast"],
            "permission": {
                "transform_allowed": True,
                "license": "CC-BY-4.0",
            },
        }
    )

    assert record["status"] == "approved"
    result = record["result"]
    assert result["category"] == "enhanced_source"
    lineage = result["lineage"]
    assert lineage[0] == {
        "action": "source",
        "ref": "src.png",
        "sha256": source_hash,
        "permission": {
            "transform_allowed": True,
            "license": "CC-BY-4.0",
        },
    }
    assert lineage[1]["action"] == "enhance"
    assert lineage[1]["operation"] == ["scale", "contrast"]
    assert lineage[1]["non_semantic"] is True
    assert lineage[1]["original_preserved"] is True
    assert lineage[1]["sha256"] == output_hash
    assert result["permission"] == {
        "transform_allowed": True,
        "license": "CC-BY-4.0",
    }


def test_redraw_is_derivative_never_enhancement() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(local_path="redraw.png"),
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-REDRAW",
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "source_sha256": "source-hash-1",
            "generative_restyle": True,
        }
    )

    result = record["result"]
    assert result["category"] == "author_redraw"
    assert [entry["action"] for entry in result["lineage"]] == [
        "source",
        "redraw",
    ]
    assert result["lineage"][1]["is_enhancement"] is False
    assert result["lineage"][1]["derivative_of"] == "src.png"
    assert "derived from the source visual" in result["required_disclosure"]
    assert result["evidence_status"] == "explanatory_not_empirical_evidence"


def test_policy_denied_task_never_attempts() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-DENIED",
            "purpose": "measured spectrum",
        }
    )

    assert record["status"] == "policy_denied"
    assert record["attempts"] == []
    assert (
        record["result"]["denied_reason"]
        == "prohibited_generation_purpose:spectrum"
    )
    assert record["unfilled_need"]["blocking"] is False
    assert record["unfilled_need"]["reason"] == (
        "prohibited_generation_purpose:spectrum"
    )


def test_deterministic_plot_durable_record() -> None:
    def render_adapter(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["verified_structured_data"] is True
        return {
            "status": "ready",
            "local_path": "plot.png",
            "sha256": "plot-hash",
            "mime_type": "image/png",
        }

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(render_adapter=render_adapter)
    )
    record = workflow.process(
        {
            "task_id": "T-PLOT",
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

    assert record["status"] == "approved"
    assert len(record["attempts"]) == 1
    assert (
        record["result"]["review"]["verdict"]
        == "deterministic_verified"
    )
    durable = workflow.to_durable_cache_record(record)
    assert durable["durable_cache_ready"] is True
    assert durable["category"] == "deterministic_data_plot"
    assert durable["evidence_status"] == "deterministic_data_plot"
    assert durable["cache_key"]


def test_durable_cache_record_has_article_placeholders() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-ARTICLE",
            "purpose": "qualitative timeline and roadmap of research phases",
        }
    )

    durable = workflow.to_durable_cache_record(record)
    article = durable["article_info"]
    assert article["article_id"] == ""
    assert article["section_id"] == ""
    assert article["figure_number"] == "TBD"
    assert (
        article["caption_placeholder"]
        == "Caption to be finalized by the human reviewer."
    )

    filled = workflow.to_durable_cache_record(
        record,
        {
            "article_id": "A-1",
            "article_title": "Radiative cooling review",
            "section_id": "S02",
            "section_title": "Methods",
            "figure_id": "FIG-2",
            "figure_number": "2",
            "caption": "Final caption approved by the editor.",
        },
    )
    assert filled["article_info"]["article_id"] == "A-1"
    assert filled["article_info"]["section_title"] == "Methods"
    assert filled["article_info"]["figure_number"] == "2"
    assert (
        filled["article_info"]["caption_placeholder"]
        == "Final caption approved by the editor."
    )
    bad_article = workflow.to_durable_cache_record(record, "not-a-dict")
    assert bad_article["article_info"]["article_id"] == ""
    assert bad_article["durable_cache_ready"] is True


def test_exhausted_record_is_not_durable_cache_ready() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["revise"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-EXHAUSTED-NOT-READY",
            "purpose": "conceptual mechanism schematic",
        }
    )

    durable = workflow.to_durable_cache_record(record)
    assert durable["status"] == "not_durable_cache_ready"
    assert durable["blocking"] is False


def test_every_attempt_records_cost_placeholder() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=_reviewer(["revise", "approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-COST",
            "purpose": "conceptual mechanism schematic",
        }
    )

    for attempt in record["attempts"]:
        placeholder = attempt["cost_placeholder"]
        assert (
            placeholder["schema_version"]
            == "visual_cost_placeholder.v1"
        )
        assert placeholder["estimated_cost_cny"] == 0.0
    assert record["result"]["cost_summary"]["attempt_count"] == 2
    assert (
        len(record["result"]["cost_summary"]["cost_placeholders"]) == 2
    )


def test_adapter_failure_counts_toward_attempt_budget() -> None:
    def failing_adapter(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {"status": "failed", "error": "adapter_boom"}

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=failing_adapter,
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-ADAPTER-FAIL",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert len(record["attempts"]) == 3
    assert all(
        attempt["status"] == "adapter_failed"
        for attempt in record["attempts"]
    )
    assert record["unfilled_need"]["blocking"] is False


def test_source_passthrough_produces_durable_record() -> None:
    workflow = VisualTransformationWorkflow()
    record = workflow.process(
        {
            "task_id": "T-SOURCE",
            "purpose": "representative source figure",
            "source_ref": "src.png",
            "source_sha256": "source-hash-1",
        }
    )

    assert record["status"] == "approved"
    assert record["result"]["category"] == "source_visual"
    assert record["result"]["lineage"][0]["action"] == "source"
    assert record["result"]["lineage"][0]["sha256"] == "source-hash-1"
    durable = workflow.to_durable_cache_record(record)
    assert durable["durable_cache_ready"] is True
    assert durable["required_disclosure"] == (
        "Original source visual; no transformation applied."
    )


def test_invalid_task_input_is_nonblocking_gap() -> None:
    workflow = VisualTransformationWorkflow()

    record = workflow.process(["not", "a", "dict"])
    assert record["status"] == "policy_denied"
    assert record["result"]["denied_reason"] == "invalid_task_input"
    assert record["unfilled_need"]["blocking"] is False
    assert record["unfilled_need"]["reason"] == "invalid_task_input"

    malformed = workflow.run({})
    assert malformed["status"] == "policy_denied"
    assert malformed["unfilled_need"]["blocking"] is False

    malformed = workflow.run("not a record")
    assert malformed["status"] == "policy_denied"
    assert malformed["unfilled_need"]["blocking"] is False


def test_adapter_exception_is_recorded_as_nonblocking_gap() -> None:
    def raising_adapter(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise RuntimeError("adapter_boom")

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=raising_adapter,
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-ADAPTER-RAISE",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert len(record["attempts"]) == 3
    assert all(
        attempt["status"] == "adapter_failed"
        for attempt in record["attempts"]
    )
    assert "RuntimeError" in record["attempts"][0]["result"]["error"]
    assert record["unfilled_need"]["blocking"] is False


def test_review_adapter_exception_does_not_crash() -> None:
    def bad_reviewer(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise ValueError("reviewer_down")

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=_ready_adapter(),
            review_adapter=bad_reviewer,
        )
    )
    record = workflow.process(
        {
            "task_id": "T-REVIEW-RAISE",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert len(record["attempts"]) == 3
    assert record["attempts"][0]["status"] == "rejected"
    assert "Review adapter raised" in (
        record["attempts"][0]["reviewer_feedback"][0]
    )
    assert record["unfilled_need"]["blocking"] is False


def test_non_dict_adapter_output_does_not_crash() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=lambda payload: None,
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-NONE-OUTPUT",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert len(record["attempts"]) == 3
    assert all(
        attempt["status"] == "adapter_failed"
        for attempt in record["attempts"]
    )
    assert record["unfilled_need"]["blocking"] is False


def test_adapter_cannot_overwrite_source() -> None:
    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            enhancement_adapter=_ready_adapter(local_path="src.png"),
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-OVERWRITE",
            "purpose": "conceptual mechanism schematic",
            "source_ref": "src.png",
            "source_sha256": "source-hash-1",
            "enhancement_operations": ["scale"],
        }
    )

    assert record["status"] == "exhausted_unfilled"
    assert all(
        attempt["status"] == "adapter_failed"
        for attempt in record["attempts"]
    )
    assert (
        record["attempts"][0]["result"]["error"]
        == "adapter_would_overwrite_source"
    )
    assert record["unfilled_need"]["blocking"] is False


def test_cost_placeholder_tolerates_non_numeric_usage() -> None:
    def noisy_adapter(payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        return {
            "status": "ready",
            "local_path": "out.png",
            "sha256": "output-hash-1",
            "usage": {
                "input_tokens": "not-a-number",
                "estimated_cost_cny": "also-not-a-number",
            },
        }

    workflow = VisualTransformationWorkflow(
        VisualTransformationWorkflowConfig(
            generation_adapter=noisy_adapter,
            review_adapter=_reviewer(["approve"]),
        )
    )
    record = workflow.process(
        {
            "task_id": "T-NOISY-COST",
            "purpose": "conceptual mechanism schematic",
        }
    )

    assert record["status"] == "approved"
    placeholder = record["attempts"][0]["cost_placeholder"]
    assert placeholder["input_tokens"] == 0
    assert placeholder["estimated_cost_cny"] == 0.0
