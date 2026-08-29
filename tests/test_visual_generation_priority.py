"""P1-5 regression tests: declared priority now drives generation order.

The planner writes high/medium/low on every conceptual figure request;
the factory sorter used to ignore that field entirely (kind markers were
the only signal), so a medium taxonomy diagram outranked two high
requests in the reference run.  These tests pin the new contract:
priority is the primary key, the historical mechanism/workflow pairing
survives as the secondary key inside one priority level, unknown or
missing priorities behave like medium, early exits are unchanged, and
the max_generated_images defaults stay at 2 everywhere.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from optomind_research.runtime.visual_evidence_factory import (
    _prioritized_generation_order,
)

REFERENCE_REQUESTS = [
    {"visual_plan_id": "V1", "section_id": "S01",
     "figure_kind": "mechanism_schematic", "priority": "high"},
    {"visual_plan_id": "V4", "section_id": "S04",
     "figure_kind": "comparison_diagram", "priority": "high"},
    {"visual_plan_id": "V7", "section_id": "S07",
     "figure_kind": "concept_map", "priority": "high"},
    {"visual_plan_id": "V8", "section_id": "S08",
     "figure_kind": "taxonomy_diagram", "priority": "medium"},
]


def test_reference_requests_select_only_high_at_cap_two() -> None:
    ordered = _prioritized_generation_order(
        REFERENCE_REQUESTS, 2,
    )
    selected = ordered[:2]
    assert all(
        row[1].get("priority") == "high" for row in selected
    ), f"medium must not outrank high: {ordered!r}"
    ranks = ["high", "medium", "low"]
    observed = [
        ranks.index(row[1].get("priority")) for row in ordered
    ]
    assert observed == sorted(observed)


def test_missing_priority_behaves_like_all_medium() -> None:
    without_priority = [
        {k: v for k, v in row.items() if k != "priority"}
        for row in REFERENCE_REQUESTS
    ]
    explicit_medium = [
        {**row, "priority": "medium"} for row in REFERENCE_REQUESTS
    ]
    assert [row[0] for row in _prioritized_generation_order(without_priority, 2)] == (
        [row[0] for row in _prioritized_generation_order(explicit_medium, 2)]
    )


def test_cap_one_keeps_original_order() -> None:
    shuffled_input = list(reversed(REFERENCE_REQUESTS))
    ordered = _prioritized_generation_order(shuffled_input, 1)
    assert [row[0] for row in ordered] == [0, 1, 2, 3]


def test_unknown_priority_treated_as_medium_not_last() -> None:
    requests = [
        {"visual_plan_id": "A", "section_id": "S01",
         "figure_kind": "mechanism_schematic", "priority": "weird"},
        {"visual_plan_id": "B", "section_id": "S04",
         "figure_kind": "comparison_diagram", "priority": "low"},
    ]
    ordered = _prioritized_generation_order(requests, 2)
    assert ordered[0][1]["visual_plan_id"] == "A"


def test_max_generated_images_defaults_match_editor_request_cap() -> None:
    """The three default sites must agree with the editor's request ceiling.

    The invariant being guarded is agreement, not a particular number.  A
    factory cap below ``MAX_CONCEPTUAL_FIGURE_REQUESTS`` silently discards
    the editor's trailing requests as
    ``generation_task_budget_or_lower_priority`` -- no reviewer ever sees
    them (be780761: S04 and S05 lost this way).
    """
    from optomind_research.runtime.visual_editor_tool_provider import (
        MAX_CONCEPTUAL_FIGURE_REQUESTS as CAP,
    )

    factory_source = (
        PROJECT_ROOT / "optomind_research" / "runtime"
        / "visual_evidence_factory.py"
    ).read_text(encoding="utf-8")
    orchestrator_source = (
        PROJECT_ROOT / "optomind_research" / "runtime"
        / "review_harness_orchestrator.py"
    ).read_text(encoding="utf-8")
    assert factory_source.count(
        f"max_generated_images: int = {CAP}"
    ) == 2
    assert (
        f"visual_max_generated_images: int = {CAP}" in orchestrator_source
    )
