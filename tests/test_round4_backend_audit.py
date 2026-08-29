# -*- coding: utf-8 -*-
"""Round-4 audit regressions: the defects a green unit suite did not catch.

Every test here reproduces a failure that survived the existing 3125-test
suite.  They have one thing in common: each lived in the *interaction* between
two components that were individually correct.  The gate rewrote a stage row
correctly; the delivery contract read stage rows correctly; together they
turned a correct human stop into a failed delivery.  So these tests exercise
pairs, not units.

  1. delivery gate x human gate -- a rewritten stage must stay exempt
  2. human gate -- a human's "accept" must not be worse than walking away
  3. human gate -- reject and timeout stay distinguishable, and answering
     early must not pay the whole window
  4. style pipeline x opener queue -- measured candidates must reach the
     rewriter even when the critic finds nothing
  5. style convergence -- a wave that makes the text worse must not ship
  6. stage visibility -- declared-but-unwired and not-taken stages leave a
     trace instead of an empty row
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from optomind_research.runtime.delivery_contract import build_delivery_gate
from optomind_research.runtime.human_decision_gate import (
    list_pending,
    resolve_decision,
)
from optomind_research.runtime.llm_style_pipeline import (
    run_style_convergence,
    run_style_pipeline,
    style_opener_metrics,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)


# ----------------------------------------------------------------- helpers

class _StubObservability:
    def start_stage(self, *args, **kwargs):
        return None

    def finish_stage(self, *args, **kwargs):
        return 0.0

    def snapshot(self, *args, **kwargs):
        return None


def _bare_orchestrator(tmp_path: Path, *, gate_seconds: float) -> ReviewHarnessOrchestrator:
    """An orchestrator with just enough state for the gate helpers.

    __new__ rather than __init__ on purpose: __init__ builds a real run
    directory and a real query plan, none of which the gate path reads.
    """

    orch = ReviewHarnessOrchestrator.__new__(ReviewHarnessOrchestrator)
    orch.config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "qp.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
        human_gate_auto_accept_seconds=gate_seconds,
    )
    orch.work_dir = tmp_path
    orch.run_id = "r4_test"
    orch.state = {"stages": {}}
    orch.stage_costs = {}
    orch._writeback_threads = []
    orch.observability = _StubObservability()
    orch.state_path = tmp_path / "HARNESS_STATE.json"
    orch.cost_path = tmp_path / "HARNESS_COST.json"
    return orch


def _plan_package(stage_row: dict) -> dict:
    """A delivered review whose research plan never produced its artifacts."""

    return {
        "latex_pdf_path": None,
        "stage_status": {"research_plan": stage_row},
    }


# --------------------------------------------------------------- 1. gate x contract

def test_timed_out_plan_gate_stays_degraded_not_failed(tmp_path):
    """The regression: gate rewrites the stage, contract loses the exemption.

    Before the fix the contract read only the live status.  The gate had just
    rewritten it from waiting_for_human to completed, so the four research-plan
    checks lost their awaiting_human flag and became blocking -- a run that
    correctly stopped for a human was reported as a failed delivery.
    """

    gate = build_delivery_gate(
        work_dir=tmp_path,
        package=_plan_package(
            {
                "status": "completed",
                "original_status": "waiting_for_human",
                "human_gate": "auto_accepted_after_30s",
            }
        ),
        require_review=False,
        require_chinese_review=False,
        require_research_plan=True,
    )
    assert gate["status"] == "degraded", gate.get("blocking_checks")
    assert not gate["blocking_checks"]
    assert gate["awaiting_human_checks"]
    reasons = {
        str(gate["checks"][key].get("awaiting_human_reason") or "")
        for key in gate["awaiting_human_checks"]
    }
    assert reasons == {"research_plan_waiting_for_human"}


def test_plain_completed_plan_without_artifacts_still_fails(tmp_path):
    """Negative control: the exemption must stay narrow.

    A research_plan that claims completed with no human stop behind it and no
    artifacts on disk is a genuine failure, and must keep failing.  Without
    this assertion the fix above could be "mark everything awaiting".
    """

    gate = build_delivery_gate(
        work_dir=tmp_path,
        package=_plan_package({"status": "completed"}),
        require_review=False,
        require_chinese_review=False,
        require_research_plan=True,
    )
    assert gate["status"] == "failed"
    assert gate["blocking_checks"]


def test_awaiting_exemption_survives_either_status_field(tmp_path):
    """Pre-gate and post-gate rows must reach the same verdict.

    Whether the contract sees the run before the gate fired (live status is
    the awaiting one) or after (awaiting moved to original_status), the
    delivery verdict is the same.  A gate settles a decision; it does not
    create a research plan.
    """

    pre = build_delivery_gate(
        work_dir=tmp_path,
        package=_plan_package({"status": "waiting_for_human"}),
        require_review=False,
        require_chinese_review=False,
        require_research_plan=True,
    )
    post = build_delivery_gate(
        work_dir=tmp_path,
        package=_plan_package(
            {"status": "completed", "original_status": "waiting_for_human"}
        ),
        require_review=False,
        require_chinese_review=False,
        require_research_plan=True,
    )
    assert pre["status"] == post["status"] == "degraded"
    assert sorted(pre["awaiting_human_checks"]) == sorted(
        post["awaiting_human_checks"]
    )


# ------------------------------------------------------------ 2. human accept

def test_human_accept_completes_the_stage(tmp_path):
    """The inversion: accepting was strictly worse than walking away.

    The gate used to judge acceptance by the ledger's ``auto`` flag.  A
    timeout sets auto=True and completed the stage; resolve_decision records
    auto=False for a real person, so a human clicking accept left the stage
    awaiting and degraded the run.  Membership in the accept options -- not
    who answered -- decides.
    """

    orch = _bare_orchestrator(tmp_path, gate_seconds=30.0)
    accepted: list = []

    def _resolve_as_human(decision_id, seconds):
        pending = list_pending(tmp_path)
        assert pending, "gate did not register a decision"
        resolve_decision(
            run_dir=tmp_path,
            decision_id=decision_id,
            chosen="accept",
            actor="operator",
            note="reviewed by hand",
        )
        accepted.append(decision_id)
        return ReviewHarnessOrchestrator._await_gate_decision(
            orch, decision_id, seconds
        )

    orch._await_gate_decision = _resolve_as_human
    ok = orch._resolve_human_gate(
        stage="quality_review_gate",
        kind="quality_attention_acceptance",
        subject_id="r4_test:quality",
        context={},
        original_status="needs_attention",
    )
    assert ok is True
    row = orch.state["stages"]["quality_review_gate"]
    assert row["status"] == "completed"
    assert row["human_gate"] == "human_accepted:operator"
    assert row["human_gate_chosen"] == "accept"
    assert row["original_status"] == "needs_attention"
    assert accepted


def test_human_reject_keeps_the_original_status(tmp_path):
    orch = _bare_orchestrator(tmp_path, gate_seconds=30.0)

    def _reject(decision_id, seconds):
        resolve_decision(
            run_dir=tmp_path,
            decision_id=decision_id,
            chosen="reject",
            actor="operator",
        )
        return ReviewHarnessOrchestrator._await_gate_decision(
            orch, decision_id, seconds
        )

    orch._await_gate_decision = _reject
    ok = orch._resolve_human_gate(
        stage="authoring_revision",
        kind="authoring_revision",
        subject_id="r4_test:authoring",
        context={},
        original_status="awaiting_human_review",
    )
    assert ok is False
    row = orch.state["stages"]["authoring_revision"]
    assert row["status"] == "awaiting_human_review"
    assert row["human_gate"] == "rejected"
    assert row["human_gate_chosen"] == "reject"


def test_timeout_and_human_accept_are_distinguishable(tmp_path):
    """Both ship, but the record must say which one happened."""

    orch = _bare_orchestrator(tmp_path, gate_seconds=0.05)
    assert orch._resolve_human_gate(
        stage="staged_completion",
        kind="staged_completion",
        subject_id="r4_test:staged",
        context={},
        original_status="awaiting_approval",
    )
    row = orch.state["stages"]["staged_completion"]
    assert row["status"] == "completed"
    assert str(row["human_gate"]).startswith("auto_accepted_after_")
    assert "human_accepted" not in str(row["human_gate"])


# ------------------------------------------------------------- 3. gate timing

def test_early_answer_does_not_pay_the_whole_window(tmp_path):
    """A 30s window is an upper bound, not a fixed cost.

    The first implementation slept the full window before reading the ledger,
    so a human who answered in two seconds still waited out the other
    twenty-eight -- and every gate in a run paid it.
    """

    orch = _bare_orchestrator(tmp_path, gate_seconds=20.0)

    def _answer_at_once(decision_id, seconds):
        resolve_decision(
            run_dir=tmp_path,
            decision_id=decision_id,
            chosen="accept",
            actor="operator",
        )
        return ReviewHarnessOrchestrator._await_gate_decision(
            orch, decision_id, seconds
        )

    orch._await_gate_decision = _answer_at_once
    started = time.monotonic()
    assert orch._resolve_human_gate(
        stage="quality_review_gate",
        kind="quality_attention_acceptance",
        subject_id="r4_test:early",
        context={},
        original_status="needs_attention",
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"gate waited {elapsed:.1f}s on an answered decision"


def test_gate_registration_failure_does_not_ship(tmp_path):
    """Fail closed: if no decision can be registered, nothing was decided."""

    orch = _bare_orchestrator(tmp_path, gate_seconds=1.0)

    def _explode(*args, **kwargs):
        raise OSError("ledger unwritable")

    orch._await_gate_decision = _explode
    ok = orch._resolve_human_gate(
        stage="authoring_revision",
        kind="authoring_revision",
        subject_id="r4_test:boom",
        context={},
        original_status="awaiting_human_review",
    )
    assert ok is False
    row = orch.state["stages"]["authoring_revision"]
    assert row["status"] == "awaiting_human_review"
    assert row["human_gate"] == "registration_failed"
    assert "OSError" in row["human_gate_error"]


# -------------------------------------------------- 4/5. style queue + ratchet

_OPENER = "While the results are promising, the mechanism remains unclear."
_FILLER = " ".join(f"Sentence {i} carries measured detail." for i in range(28))


def _templated_text(count: int = 12) -> str:
    """Paragraphs long enough to be eligible, sharing one opener."""

    return "\n\n".join(f"{_OPENER} {_FILLER}" for _ in range(count))


def _mixed_text() -> str:
    """A realistic distribution: two crowded openers among many singletons.

    max_share starts at 0.333 -- low enough that a bad rewrite can measurably
    make it worse.  A text where every paragraph already shares one opener
    sits at 1.0 and cannot degrade, which is why an earlier version of the
    ratchet test passed against the unratcheted code.
    """

    paragraphs = [f"However, the mechanism remains unclear. {_FILLER}" for _ in range(10)]
    paragraphs += [f"{_OPENER} {_FILLER}" for _ in range(8)]
    paragraphs += [
        f"{word} analysis anchors this passage in evidence. {_FILLER}"
        for word in (
            "Spectral", "Measured", "Detector", "Coupling", "Phase", "Noise",
            "Lens", "Sensor", "Aperture", "Photon", "Grating", "Cavity",
        )
    ]
    return "\n\n".join(paragraphs)


def _silent_critic_llm(rewrite):
    """A critic that reports nothing, plus a caller-supplied rewriter.

    The one llm_call fan-in dispatches on agent_name, so a stub has to do the
    same.  ``rewrite`` takes the paragraph text and returns the replacement.
    """

    def _call(agent_name, system, payload, *, json_mode=False):
        if agent_name == "StyleCritic":
            return {
                "content": '{"issues": []}',
                "prompt_tokens": 8,
                "completion_tokens": 4,
            }
        return {
            "content": rewrite(str(payload.get("paragraph") or "")),
            "prompt_tokens": 20,
            "completion_tokens": 15,
        }

    return _call


def test_convergence_feeds_its_measured_queue_to_the_pipeline():
    """The queue used to be computed and thrown away.

    run_style_convergence measured the over-represented openers every wave,
    then called run_style_pipeline without them -- so the rewriter only ever
    saw what the critic happened to report.  On the real 15k-word manuscript
    the critic reported zero and the measured queue held 64, and none of them
    were ever rewritten.  Assert at the convergence level: a silent critic
    must still produce work.
    """

    touched: list = []

    def _rewrite(paragraph):
        touched.append(paragraph)
        return "Measured evidence opens this passage. " + paragraph.partition(". ")[2]

    report = run_style_convergence(
        _mixed_text(),
        cost_budget_cny=0.50,
        max_rewrites=80,
        protected_terms=[],
        llm_call=_silent_critic_llm(_rewrite),
    )
    first_wave = report["waves"][0]
    assert first_wave["candidates_supplied"] > 0, (
        "measured queue never reached the pipeline"
    )
    assert first_wave["issues_found"] >= first_wave["candidates_supplied"]
    assert first_wave["attempted"] > 0
    assert touched, "a silent critic left the rewriter with nothing to do"


def test_candidate_issues_are_admitted_but_still_validated():
    """Supplied candidates are admitted, not trusted.

    Out-of-range indices, unknown issue types and non-dict rows must be
    dropped exactly as critic output is, or the queue becomes a way to
    smuggle an unvalidated edit past the schema check.
    """

    report = run_style_pipeline(
        _templated_text(count=4),
        cost_budget_cny=0.50,
        protected_terms=[],
        candidate_issues=[
            {"paragraph_index": 999, "issue_type": "template_opener"},
            {"paragraph_index": 0, "issue_type": "not_a_real_issue"},
            {"paragraph_index": -1, "issue_type": "template_opener"},
            "not even a dict",
            {"paragraph_index": 0, "issue_type": "template_opener"},
        ],
        llm_call=_silent_critic_llm(lambda paragraph: paragraph),
    )
    assert report["candidate_issues_supplied"] == 5
    assert report["issues_found"] == 1


def test_convergence_never_ships_text_worse_than_its_input():
    """The ratchet.

    A rewriter that collapses flagged paragraphs onto one opener *raises* the
    concentration it was called to lower.  Without a ratchet the wave's output
    shipped regardless: measured here, wave 1 moves max_share 0.333 -> 0.600
    and the run would have returned it as its result.
    """

    text = _mixed_text()
    before = style_opener_metrics(text)

    def _collapse(paragraph):
        # Every flagged paragraph gets the same -- template -- opener.
        return (
            "However, this passage collapses onto one opener. "
            + paragraph.partition(". ")[2]
        )

    report = run_style_convergence(
        text,
        cost_budget_cny=0.50,
        max_rewrites=80,
        protected_terms=[],
        llm_call=_silent_critic_llm(_collapse),
    )
    # The wave really did make it worse -- otherwise this asserts nothing.
    assert report["waves"][0]["max_share_after"] > before[
        "paragraph_opener_max_share"
    ], "scenario no longer degrades; the ratchet is not being exercised"

    after = style_opener_metrics(report["review_text"])
    assert after["paragraph_opener_max_share"] <= before[
        "paragraph_opener_max_share"
    ] + 1e-9, "convergence shipped a more concentrated text than it received"
    assert report["review_text"] == text
    assert report["best_wave"] == 0
    assert 1 in report["regressed_waves_discarded"]
    assert report["converged"] is False
    assert report["metrics_after"]["paragraph_opener_max_share"] == before[
        "paragraph_opener_max_share"
    ], "metrics_after must describe the text actually returned"


# ------------------------------------------------------- 6. stage visibility

def test_unwired_closure_stage_records_why(tmp_path):
    """A declared stage that nothing runs must say so, not stay blank.

    section_supplementary_closure is in STAGES and has a UI row, but its
    entry point needs a blueprint_quality_probe.v1 report that no component
    emits.  An absent row reads as "did not apply"; this reads as "not built".
    """

    orch = _bare_orchestrator(tmp_path, gate_seconds=1.0)
    orch.state["current_stage"] = "authoring_revision"
    orch._record_supplementary_closure_gap(["s03", "s07"])
    row = orch.state["stages"]["section_supplementary_closure"]
    assert row["status"] == "not_integrated"
    assert row["blocked_on"] == "blueprint_quality_probe.v1"
    assert "run_section_supplementary_closure" in row["entry_point"]
    assert row["coverage_feedback_sections"] == ["s03", "s07"]
    assert "section_supplementary_closure" in ReviewHarnessOrchestrator.STAGES
    # A note about the build must not claim the run is standing on that stage.
    assert orch.state["current_stage"] == "authoring_revision"
    assert (tmp_path / "HARNESS_STATE.json").exists(), "note was never persisted"


def test_annotate_stage_keeps_what_record_stage_wrote(tmp_path):
    """_set_stage replaces the row; a late annotation must not erase it.

    This is how the gate silently dropped work_dir -- which _prior_stage_work_dir
    needs on resume -- and the measured wall_time_seconds.
    """

    orch = _bare_orchestrator(tmp_path, gate_seconds=1.0)
    orch._set_stage(
        "visual_materialization",
        "completed",
        work_dir=str(tmp_path / "factory"),
        wall_time_seconds=812.4,
        validation="VALIDATION_PASSED",
    )
    orch._annotate_stage(
        "visual_materialization",
        "degraded",
        unfilled_needs_submitted=16,
    )
    row = orch.state["stages"]["visual_materialization"]
    assert row["status"] == "degraded"
    assert row["wall_time_seconds"] == 812.4
    assert row["validation"] == "VALIDATION_PASSED"
    assert row["work_dir"] == str(tmp_path / "factory")
    assert row["unfilled_needs_submitted"] == 16


def test_annotation_does_not_finish_a_stage_twice(tmp_path):
    """A gate annotation is not a second lifecycle transition or timer sample."""
    orch = _bare_orchestrator(tmp_path, gate_seconds=1.0)
    finished = []
    orch.observability.finish_stage = lambda *args, **kwargs: finished.append(args)
    orch._set_stage("visual_materialization", "completed", wall_time_seconds=2.0)
    assert len(finished) == 1
    orch._annotate_stage("visual_materialization", "degraded", reason="unfilled")
    assert len(finished) == 1
    assert orch.state["stages"]["visual_materialization"]["wall_time_seconds"] == 2.0


def test_unfilled_visual_needs_merge_both_field_names():
    """The queue must see every unmet need, not one field's worth.

    Measured on rhr_be780761: FINAL_VISUAL_PACKAGE carried
    unfilled_visual_needs=[] and seven rows under
    unfilled_visual_opportunities, while the editorial plan carried four under
    unfilled_visual_needs.  Reading one name queued exactly the four rows a
    retry cannot help ("no traceable source figures found") and dropped the
    three it exists for -- S01 generation_attempts_exhausted, S04/S05
    generation_task_budget_or_lower_priority.  The LaTeX build report had been
    reporting 7 the whole time, which is how the discrepancy showed up.
    """

    package = {
        "unfilled_visual_needs": [],
        "unfilled_visual_opportunities": [
            {"section_id": "S03", "reason": "No traceable source figures found"},
            {"section_id": "S01", "reason": "generation_attempts_exhausted"},
            {"section_id": "S04",
             "reason": "generation_task_budget_or_lower_priority"},
            {"section_id": "S05",
             "reason": "generation_task_budget_or_lower_priority"},
        ],
    }
    plan = {
        "unfilled_visual_needs": [
            # Same row as the package's S03: must be queued once, not twice.
            {"section_id": "S03", "reason": "No traceable source figures found"},
            {"section_id": "S06", "reason": "No traceable source figures found"},
        ],
    }
    merged = ReviewHarnessOrchestrator._collect_unfilled_visual_needs(
        package, plan
    )
    sections = sorted(row["section_id"] for row in merged)
    assert sections == ["S01", "S03", "S04", "S05", "S06"]
    retryable = {
        row["section_id"]
        for row in merged
        if "generation" in row["reason"]
    }
    assert retryable == {"S01", "S04", "S05"}, (
        "the rows a retry is for must reach the queue"
    )


def test_unfilled_needs_merge_tolerates_bare_strings_and_absence():
    assert ReviewHarnessOrchestrator._collect_unfilled_visual_needs({}, {}) == []
    merged = ReviewHarnessOrchestrator._collect_unfilled_visual_needs(
        {"unfilled_visual_needs": ["S09 had no candidate figure", None]},
        {},
    )
    assert [row["reason"] for row in merged] == [
        "S09 had no candidate figure",
        "None",
    ]


def test_every_status_written_has_a_ui_label():
    """The registry must be able to render what the orchestrator writes.

    Unknown codes fall back to the raw code, which shows an operator
    ``blocked_hard_quality`` in a Chinese UI.
    """

    from optomind_ui.stage_registry import _STATUS_LABELS, status_label

    for code in (
        "not_integrated",
        "not_required",
        "blocked_hard_quality",
        "skipped_cost_budget",
        "skipped_no_visual_plan",
    ):
        assert code in _STATUS_LABELS, f"{code} has no Chinese label"
        assert status_label(code) != code
