from __future__ import annotations

from optomind_research.runtime.research_program_tool_provider import (
    _build_plan_only_traceability_matrix,
    _normalize_plan_work_package_readiness,
    _rehydrate_main_hypothesis_statements,
)
from optomind_research.runtime.program_focus_gate import (
    _allows_textual_evaluation_metrics,
)


def _focus() -> dict:
    return {
        "main_problem": {"problem_id": "P01"},
        "selected_opportunity_ids": ["OP01"],
        "main_hypothesis_ids": ["H01"],
    }


def _hypotheses() -> dict[str, dict]:
    return {
        "H01": {
            "hypothesis_id": "H01",
            "title": "Accepted hypothesis",
            "statement": "The accepted hypothesis statement.",
            "readiness": "needs_more_literature",
            "falsification_conditions": ["The measured effect is absent."],
        }
    }


def test_cleanup_readiness_migration_is_conservative():
    plan = {
        "work_packages": [
            {
                "work_package_id": "WP01",
                "hypothesis_ids": ["H01"],
                "readiness": "ready",
            },
            {
                "work_package_id": "WP02",
                "hypothesis_ids": ["H01"],
                "readiness": "verification_deferred",
            },
        ]
    }

    normalized, corrections = _normalize_plan_work_package_readiness(
        plan, _focus(), _hypotheses()
    )

    assert [item["readiness"] for item in normalized["work_packages"]] == [
        "needs_more_literature",
        "needs_more_literature",
    ]
    assert {item["action"] for item in corrections} == {
        "downgrade_work_package_readiness_to_hypothesis",
        "normalize_work_package_readiness",
    }
    assert plan["work_packages"][0]["readiness"] == "ready"


def test_cleanup_rehydrates_exact_selected_hypothesis_statement():
    plan = {
        "main_hypothesis_statements": [
            {
                "hypothesis_id": "H01",
                "title": "Stale title",
                "statement": "Stale statement.",
            }
        ]
    }

    normalized, correction, errors = _rehydrate_main_hypothesis_statements(
        plan, _focus(), _hypotheses()
    )

    assert not errors
    assert correction is not None
    assert normalized["main_hypothesis_statements"] == [
        {
            "hypothesis_id": "H01",
            "title": "Accepted hypothesis",
            "statement": "The accepted hypothesis statement.",
        }
    ]
    assert plan["main_hypothesis_statements"][0]["statement"] == (
        "Stale statement."
    )


def test_cleanup_rebuilds_trace_rows_from_current_work_package_fields():
    plan = {
        "work_packages": [
            {
                "work_package_id": "WP01",
                "hypothesis_ids": ["H01"],
                "opportunity_ids": ["OP01"],
                "methods": ["Review the deposition evidence."],
                "evaluation_metrics": ["Completeness of parameter coverage."],
                "controls_or_baselines": ["Published reference design."],
                "stop_or_pivot_criteria": ["Pivot if evidence remains incomplete."],
            }
        ],
        "traceability_matrix": [
            {
                "work_package_id": "WP01",
                "problem_id": "",
                "opportunity_id": "",
                "hypothesis_id": "",
                "proposed_tests": [],
                "metrics": [],
                "baselines": [],
                "falsification_conditions": [],
                "stop_or_pivot_decisions": [],
            }
        ],
    }

    matrix, audit = _build_plan_only_traceability_matrix(
        plan,
        _focus(),
        _hypotheses(),
        force_rebuild=True,
    )

    assert len(matrix) == 1
    row = matrix[0]
    assert row["problem_id"] == "P01"
    assert row["opportunity_id"] == "OP01"
    assert row["hypothesis_id"] == "H01"
    assert row["proposed_tests"] == ["Review the deposition evidence."]
    assert row["metrics"] == ["Completeness of parameter coverage."]
    assert row["baselines"] == ["Published reference design."]
    assert row["falsification_conditions"] == ["The measured effect is absent."]
    assert row["stop_or_pivot_decisions"] == [
        "Pivot if evidence remains incomplete."
    ]
    assert audit["forced_rebuild"] is True
    assert any(
        item["source_field"] == "evaluation_metrics"
        for item in audit["fallback_field_sources"]
    )


def test_only_foundational_package_may_use_textual_evaluation_metrics():
    assert _allows_textual_evaluation_metrics(
        {
            "title": "Literature review and deposition characterization",
            "methods": ["Review published deposition data."],
            "evaluation_metrics": ["Completeness of parameter coverage."],
            "metric_ids": [],
        }
    )
    assert not _allows_textual_evaluation_metrics(
        {
            "title": "Optimization experiments",
            "methods": ["Run the optimization experiment."],
            "evaluation_metrics": ["Performance improvement."],
            "metric_ids": [],
        }
    )
