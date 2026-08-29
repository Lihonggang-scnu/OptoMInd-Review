"""Offline tests for the generic multi-source expansion trigger."""

from __future__ import annotations

from typing import Any

from optomind_research.dominant_review_expansion import (
    classify_source_expansion_type,
    plan_source_expansion_triggers,
    run_dominant_review_expansion,
    source_expansion_policy,
    source_expansion_to_expansion_input,
)
from optomind_research.review_source_unpacking import (
    parse_numbered_bibliography,
)


def _bibliography_text(count: int = 255) -> str:
    lines = ["References"]
    for number in range(1, count + 1):
        lines.append(
            f"[{number}] Synthetic Author {number}, \"Synthetic Title {number},"
            f"\" Journal of Studies, 2024."
        )
    return "\n".join(lines)


def _bibliography(count: int = 255):
    return parse_numbered_bibliography(
        _bibliography_text(count), mode="whole_document"
    )


def test_exact_threshold_one_of_ten_no_trigger_two_of_ten_triggers() -> None:
    claims = [
        f"c{index}" for index in range(1, 11)
    ] + ["c1"]  # duplicate entry proves the denominator is distinct
    index = {
        "c1": "source_a",
        "c2": "source_a",
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_a": {
                "title": "Review A",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    assert plan["denominator_count"] == 10
    rows = {row["source_id"]: row for row in plan["source_rows"]}
    assert rows["source_a"]["claim_count"] == 2
    assert rows["source_a"]["claim_share"] == 0.2
    assert rows["source_a"]["triggered"] is True
    assert len(plan["triggered_tasks"]) == 1

    index_one = {"c1": "source_a"}
    plan_one = plan_source_expansion_triggers(
        claims,
        index_one,
        source_metadata={
            "source_a": {
                "title": "Review A",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    row = plan_one["source_rows"][0]
    assert row["claim_share"] == 0.1
    assert row["triggered"] is False
    assert row["skip_reason"] == "below_threshold"
    assert plan_one["triggered_tasks"] == []


def test_duplicate_chunks_do_not_double_count() -> None:
    claims = [f"c{index}" for index in range(1, 11)]
    index = {
        "c1": ["source_x", "source_x", "source_x"],  # same source, 3 chunks
        "c2": ["source_x", "source_y"],
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_x": {
                "title": "Review X",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            },
            "source_y": {
                "title": "Review Y",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            },
        },
    )
    rows = {row["source_id"]: row for row in plan["source_rows"]}
    assert rows["source_x"]["claim_count"] == 2  # c1 and c2, not 4
    assert rows["source_x"]["triggered"] is True
    assert rows["source_y"]["claim_count"] == 1
    assert rows["source_y"]["triggered"] is False


def test_two_sources_at_eleven_and_twelve_percent_both_trigger() -> None:
    claims = [f"c{index}" for index in range(1, 101)]
    index = {}
    for index_num in range(1, 12):
        index[f"c{index_num}"] = "source_11"
    for index_num in range(1, 13):
        index[f"c{index_num}"] = [
            "source_12" if index_num > 11 else "source_11",
            "source_12",
        ]
    metadata = {
        "source_11": {
            "title": "Review 11",
            "publication_types": ["Review"],
            "bibliography": _bibliography(5),
        },
        "source_12": {
            "title": "Review 12",
            "publication_types": ["Review"],
            "bibliography": _bibliography(5),
        },
    }
    plan = plan_source_expansion_triggers(
        claims, index, source_metadata=metadata
    )
    rows = {row["source_id"]: row for row in plan["source_rows"]}
    assert rows["source_11"]["claim_share"] == 0.11
    assert rows["source_12"]["claim_share"] == 0.12
    assert rows["source_11"]["triggered"] is True
    assert rows["source_12"]["triggered"] is True
    assert len(plan["triggered_tasks"]) == 2
    assert {task["source_id"] for task in plan["triggered_tasks"]} == {
        "source_11",
        "source_12",
    }


def test_missing_unparseable_and_empty_references_skip_nonblocking() -> None:
    claims = [f"c{index}" for index in range(1, 11)]
    index = {
        "c1": "source_missing",
        "c2": "source_missing",
        "c3": "source_unparseable",
        "c4": "source_unparseable",
        "c5": "source_empty",
        "c6": "source_empty",
    }
    metadata = {
        "source_missing": {"title": "Missing"},
        "source_unparseable": {
            "title": "Unparseable",
            "bibliography": {"error": "pdf text extraction failed"},
        },
        "source_empty": {
            "title": "Empty",
            "bibliography": {},
        },
    }
    plan = plan_source_expansion_triggers(
        claims, index, source_metadata=metadata
    )
    assert plan["triggered_tasks"] == []
    assert plan["audit"]["tasks_created"] == 0
    rows = {row["source_id"]: row for row in plan["source_rows"]}
    assert rows["source_missing"]["reference_status"] == "missing"
    assert rows["source_missing"]["skip_reason"] == "missing_bibliography"
    assert rows["source_unparseable"]["reference_status"] == "unparseable"
    assert rows["source_unparseable"]["skip_reason"] == (
        "pdf text extraction failed"
    )
    assert rows["source_empty"]["skip_reason"] == "empty_bibliography"
    assert plan["audit"]["skipped_sources"] == 3


def test_source_type_policy() -> None:
    review_type = classify_source_expansion_type(
        "p_review",
        {"title": "A Roadmap", "publication_types": ["Review"]},
    )
    assert review_type == "review_unbundling"
    assert source_expansion_policy(review_type)["role"] == (
        "review_secondary_for_synthesis_history"
    )
    empirical_type = classify_source_expansion_type(
        "p_emp",
        {"title": "An Experiment", "source_type": "empirical"},
    )
    assert empirical_type == "empirical_antecedent_expansion"
    empirical_policy = source_expansion_policy(empirical_type)
    assert empirical_policy["role"] == "empirical_primary_for_own_findings"
    assert "never overwrite the empirical paper's own findings" in (
        empirical_policy["reference_role"]
    )
    unknown_type = classify_source_expansion_type("p_unknown", {})
    assert unknown_type == "unknown_source_expansion"
    assert source_expansion_policy(unknown_type)["never_overwrite"] is True


def test_triggered_task_feeds_full_reference_screener() -> None:
    claims = [f"c{index}" for index in range(1, 11)]
    index = {
        f"c{index}": "source_full" for index in range(1, 3)
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_full": {
                "title": "Full Review",
                "publication_types": ["Review"],
                "body_text": "The roadmap summarizes the field.\n",
                "bibliography": _bibliography(255),
            }
        },
    )
    assert plan["audit"]["tasks_created"] == 1
    task = plan["triggered_tasks"][0]
    assert task["can_feed_full_reference_screener"] is True
    assert task["acquisition_contract"]["no_top_n_quota"] is True
    assert len(task["reference_bibliography"]) == 255
    expansion_input = source_expansion_to_expansion_input(
        task, user_question="Which originals support the review?"
    )

    def screen_all(batch):
        return [
            {
                "reference_number": number,
                "relevance_score": 80.0,
                "keep": True,
                "useful_axes": ["mechanism"],
                "useful_sections": ["S02"],
                "likely_evidence_roles": ["central_fact"],
                "acquisition_priority": "high",
                "reason": "Directly relevant.",
            }
            for number in batch["reference_numbers"]
        ]

    result = run_dominant_review_expansion(
        expansion_input, screen_decisions_call=screen_all
    )
    assert result["coverage_audit"]["complete"] is True
    assert result["coverage_audit"]["expected_count"] == 255
    assert result["screening_audit"]["status_counts"]["kept"] == 255
    assert result["acquisition"]["audit"]["kept_request_count"] == 255


def test_mapping_claims_denominator_excludes_unverified_rows() -> None:
    claims = [
        {"claim_id": f"c{index}", "ready_for_write": True}
        for index in range(1, 9)
    ] + [
        {"claim_id": "c9", "ready_for_write": False},
        {"claim_id": "c10", "status": "failed"},
    ]
    index = {
        f"c{index}": "source_verified" for index in range(1, 11)
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_verified": {
                "title": "Verified Review",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    assert plan["denominator_count"] == 8
    row = plan["source_rows"][0]
    assert row["claim_count"] == 8
    assert row["claim_share"] == 1.0
    assert row["triggered"] is True

    verified_state_claims = [
        {"claim_id": "c1", "verified": True},
        {"claim_id": "c2", "status": "pass"},
        {"claim_id": "c3", "status": "pending"},
    ]
    plan_state = plan_source_expansion_triggers(
        verified_state_claims,
        {"c1": "s1", "c2": "s1", "c3": "s1"},
        source_metadata={
            "s1": {
                "title": "State Review",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    assert plan_state["denominator_count"] == 2
    assert plan_state["source_rows"][0]["claim_count"] == 2


def test_stale_index_claims_do_not_inflate_numerator_or_share() -> None:
    claims = [f"c{index}" for index in range(1, 11)]
    index = {
        "c1": "source_clean",
        "c2": "source_clean",
        "c99": "source_clean",
        "c_other_section": "source_clean",
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_clean": {
                "title": "Clean Review",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    row = plan["source_rows"][0]
    assert row["claim_count"] == 2
    assert row["claim_share"] == 0.2
    assert row["triggered"] is True


def test_raw_share_above_threshold_triggers_despite_rounded_display() -> None:
    claims = [f"c{index}" for index in range(1, 2010)]
    index = {
        f"c{index}": "source_tiny" for index in range(1, 202)
    }
    plan = plan_source_expansion_triggers(
        claims,
        index,
        source_metadata={
            "source_tiny": {
                "title": "Tiny Review",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    row = plan["source_rows"][0]
    assert row["claim_count"] == 201
    assert row["claim_share"] == 0.1  # displayed rounded value only
    assert row["triggered"] is True
    assert len(plan["triggered_tasks"]) == 1

    exact = plan_source_expansion_triggers(
        [f"c{index}" for index in range(1, 11)],
        {"c1": "source_exact"},
        source_metadata={
            "source_exact": {
                "title": "Exact Review",
                "publication_types": ["Review"],
                "bibliography": _bibliography(5),
            }
        },
    )
    exact_row = exact["source_rows"][0]
    assert exact_row["claim_share"] == 0.1
    assert exact_row["triggered"] is False
