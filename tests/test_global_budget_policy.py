"""Regression tests for the single-pool CLI budget policy."""

from __future__ import annotations

from pathlib import Path

import run_review_harness as harness_cli
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)
from optomind_research.runtime.visual_evidence_factory import (
    VisualEvidenceFactory,
    VisualEvidenceFactoryConfig,
)


def test_cli_without_stage_overrides_selects_global_only() -> None:
    args = harness_cli.build_parser().parse_args(
        ["--question", "a new optical question", "--global-budget-cny", "15"]
    )

    assert all(
        getattr(args, name) is None
        for name in harness_cli._LEGACY_STAGE_BUDGET_DEFAULTS
    )
    harness_cli._normalize_budget_arguments(args)

    assert args.global_budget_only is True
    assert args.visual_budget_cny == 5.0


def test_global_only_preflight_does_not_sum_stage_caps(tmp_path: Path) -> None:
    query_plan = tmp_path / "query.json"
    query_plan.write_text("{}", encoding="utf-8")
    base_kb = tmp_path / "kb.sqlite"
    base_kb.touch()
    orchestrator = ReviewHarnessOrchestrator(
        ReviewHarnessConfig(
            query_plan_path=query_plan,
            base_kb_sqlite=base_kb,
            output_root=tmp_path,
            global_cost_budget_cny=15.0,
            global_budget_only=True,
            publication_mainline_enabled=True,
        ),
        run_dir=tmp_path / "run",
    )

    report = orchestrator.preflight()

    assert report["budget_policy"] == "global_only"
    assert report["stage_hard_caps_cny"] == {}
    assert report["allocated_max_cny"] == 15.0
    assert report["within_budget"] is True
    assert orchestrator._admission_budget(0.5) == 15.0


def test_explicit_legacy_stage_override_keeps_hard_cap_mode(
    tmp_path: Path,
) -> None:
    args = harness_cli.build_parser().parse_args(
        [
            "--question",
            "a new optical question",
            "--global-budget-cny",
            "15",
            "--review-lead-budget-cny",
            "1",
        ]
    )
    harness_cli._normalize_budget_arguments(args)

    assert args.global_budget_only is False
    assert args.review_lead_budget_cny == 1.0


def test_visual_factory_global_balance_is_not_recorded_as_stage_budget(
    tmp_path: Path,
) -> None:
    factory = VisualEvidenceFactory(
        VisualEvidenceFactoryConfig(
            output_dir=tmp_path,
            global_budget_remaining_cny=8.88,
        )
    )

    assert factory.cost["budget_policy"] == "global_remaining_snapshot"
    assert factory.cost["global_remaining_cny"] == 8.88
    assert "budget_cny" not in factory.cost
    assert factory._remaining_budget() == 8.88
