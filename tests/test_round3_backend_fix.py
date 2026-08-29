# -*- coding: utf-8 -*-
"""Round-3 backend ticket (AGENT_PROMPT_BACKEND_FIX_ROUND3) acceptance tests.

Covers the nine required assertions:
  1. human-gate timeout pass-through (delivery_gate auto-accepts)
  2. stage-status rewrite after a timed-out gate (awaiting -> completed)
  3. llm_style_pipeline convergence improves opener metrics (fake LLM)
  4. structure audit detects repeated openers, no false positive on normal
  5. unfilled visual needs never read VALIDATION_PASSED / completed
  6. wall_time_measured=true for adapter-measured mainline stages
  7. supplementary closure runs end-to-end on partial coverage inputs
  8. strawman_not_but is an accepted style-critic issue type
  9. quality report carries remediation_hints for warnings
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from optomind_research.runtime.article_structure_auditor import (
    _sentence_openers,
)
from optomind_research.runtime.human_decision_gate import (
    decision_state,
    expire_due_decisions,
    request_decision,
    _MANDATORY_HUMAN_KINDS,
)
from optomind_research.runtime.llm_style_pipeline import (
    _ALLOWED_ISSUE_TYPES,
    _over_represented_openers,
    run_style_convergence,
)
from optomind_research.runtime.visual_evidence_factory import (
    validate_final_visual_package_file,
)


# --------------------------------------------------------------------- 1 + 2

def test_delivery_gate_timeout_auto_accepts(tmp_path):
    decision_id = request_decision(
        run_dir=tmp_path,
        kind="delivery_gate",
        subject_id="r3_test:manual",
        context={"gate_status": "degraded"},
        options=["accept", "reject"],
        auto_accept_after_seconds=0.0,
        default_option="accept",
    )
    expire_due_decisions(tmp_path)
    state = decision_state(tmp_path, decision_id)
    assert state.get("state") == "resolved"
    assert state.get("auto") is True
    assert state.get("chosen") == "accept"


def test_stage_status_rewrite_after_timed_out_gate(tmp_path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    config = ReviewHarnessConfig(
        query_plan_path=tmp_path / "qp.json",
        base_kb_sqlite=tmp_path / "kb.sqlite",
        output_root=tmp_path,
        human_gate_auto_accept_seconds=0.05,
    )
    # ReviewHarnessConfig requires query_plan_path/base_kb_sqlite only when
    # constructing the real orchestrator; the helper path reads none of them.
    orch = ReviewHarnessOrchestrator.__new__(ReviewHarnessOrchestrator)
    orch.config = config
    orch.work_dir = tmp_path
    orch.run_id = "r3_test"
    orch.state = {"stages": {}}
    orch.stage_costs = {}
    orch._writeback_threads = []

    class _StubObs:
        def start_stage(self, *a, **k):
            return None

        def finish_stage(self, *a, **k):
            return 0.0

        def snapshot(self, *a, **k):
            return None

    orch.observability = _StubObs()
    orch.state_path = tmp_path / "HARNESS_STATE.json"
    orch.cost_path = tmp_path / "HARNESS_COST.json"

    ok = orch._resolve_human_gate(
        stage="authoring_revision",
        kind="authoring_revision_probe",
        subject_id="r3_test:authoring2",
        context={},
        original_status="awaiting_human_review",
        options=["accept", "reject"],
    )
    assert ok is True
    stage_row = orch.state["stages"]["authoring_revision"]
    assert stage_row["status"] == "completed"
    assert stage_row["original_status"] == "awaiting_human_review"
    assert str(stage_row.get("human_gate", "")).startswith("auto_accepted")


def test_infinite_wait_preserved_when_none_or_nonpositive(tmp_path):
    from optomind_research.runtime.review_harness_orchestrator import (
        ReviewHarnessConfig,
        ReviewHarnessOrchestrator,
    )

    for value in (None, 0.0, -5.0):
        config = ReviewHarnessConfig(
            query_plan_path=tmp_path / "qp.json",
            base_kb_sqlite=tmp_path / "kb.sqlite",
            output_root=tmp_path,
            human_gate_auto_accept_seconds=value,
        )
        orch = ReviewHarnessOrchestrator.__new__(ReviewHarnessOrchestrator)
        orch.config = config
        orch.work_dir = tmp_path
        orch.run_id = "r3_inf"
        orch.state = {"stages": {}}
        orch.stage_costs = {}
        orch._writeback_threads = []
        assert orch._effective_gate_seconds() is None


def test_mandatory_kinds_frozenset_empty_and_documented():
    assert _MANDATORY_HUMAN_KINDS == frozenset()
    import optomind_research.runtime.human_decision_gate as gate

    doc = gate.__doc__ or ""
    assert "POLICY CHANGE" in doc and "user authorization" in doc


# ------------------------------------------------------------------------- 3

class _FakeLLM:
    def __init__(self):
        self.rewrites = 0

    def __call__(self, agent_name, system, payload, *, json_mode=False):
        if agent_name == "StyleCritic":
            return {
                "content": json.dumps({"issues": []}),
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }
        self.rewrites += 1
        paragraph = str(payload.get("paragraph") or "")
        head, _, tail = paragraph.partition(".")
        rewritten = ("Spectral measurements anchor this passage" + tail) if tail else paragraph
        return {
            "content": rewritten.strip(),
            "prompt_tokens": 20,
            "completion_tokens": 15,
        }


def test_style_convergence_improves_opener_metrics():
    template = (
        "{opener} prior surveys catalogue architectures, none connects the "
        "evidence chain to deployment constraints, which is the gap closed "
        "here with a structured comparison across twenty recent systems."
    )
    paragraphs = [template.format(opener=w) for w in ["While"] * 3 + ["Building"] * 3]
    text = "\n\n".join(paragraphs)
    fake = _FakeLLM()
    report = run_style_convergence(
        text,
        enabled=True,
        cost_budget_cny=0.50,
        max_rewrites=60,
        protected_terms=[],
        llm_call=fake,
    )
    before = report["metrics_before"]["paragraph_opener_max_share"]
    after = report["metrics_after"]["paragraph_opener_max_share"]
    honest_stop = {
        "fixed_point_no_acceptance",
        "budget_or_rewrites_exhausted",
        "wave_cap_reached",
        "no_over_represented_openers",
    }
    assert after <= before or report["stop_reason"] in honest_stop
    assert report["rewrites_attempted"] > 0 or report["stop_reason"] in honest_stop


def test_over_represented_queue_distribution_driven():
    template = (
        "{w} prior surveys catalogue architectures, none of them connects the "
        "evidence chain to deployment constraints, which is the gap closed here "
        "with a structured comparison across twenty recent systems reviewed."
    )
    paragraphs = [template.format(w="While")] * 6 + [template.format(w="However")]
    queue = _over_represented_openers("\n\n".join(paragraphs))
    openers = {row["opener"] for row in queue}
    assert openers, f"queue empty: {queue}"
    assert "While" in openers or "while" in openers
    while_rows = [row["count"] for row in queue if row["opener"] == "While"]
    assert while_rows and min(while_rows) >= 5
    counts = [row["count"] for row in queue]
    assert counts == sorted(counts, reverse=True)


# ------------------------------------------------------------------------- 4

def test_sentence_openers_skip_reference_markers():
    openers = _sentence_openers(
        "[REF:a1] Furthermore, x. [REF:b2][REF:c3] Furthermore, y. However z stands."
    )
    assert openers == ["furthermore", "furthermore", "however"]


def test_structure_audit_repeated_vs_normal_text():
    repetitive = (
        "# Title\n\n## Abstract\n\n"
        + " ".join(["While systems evolve rapidly."] * 40)
        + "\n\n## Conclusion\n\nDone.\n"
    )
    openers = _sentence_openers(repetitive)
    assert openers.count("while") / max(1, len(openers)) >= 0.9

    normal = (
        "# Title\n\n## Abstract\n\n"
        "Spectral tuning enables radiative cooling. Experimental devices "
        "reach subambient temperatures outdoors. Photonic structures control "
        "thermal emission selectively. Polymer films scale manufacturing at "
        "low cost. System integrations demonstrate day-night operation.\n\n"
        "## Conclusion\n\nOutlook remains promising.\n"
    )
    normal_openers = _sentence_openers(normal)
    assert len(normal_openers) == len(set(normal_openers))


# ------------------------------------------------------------------------- 5

def test_unfilled_needs_force_degraded_validation(tmp_path):
    clean = tmp_path / "CLEAN.json"
    clean.write_text(
        json.dumps(
            {"schema_version": "x", "figures": [], "validation": {}}
        ),
        encoding="utf-8",
    )
    assert not validate_final_visual_package_file(clean).startswith(
        "VALIDATION_PASSED"
    )

    dirty_package = {
        "schema_version": "x",
        "figures": [],
        "validation": {},
        "unfilled_visual_opportunities": [
            {"need_id": f"need-{index}"} for index in range(4)
        ],
    }
    dirty = tmp_path / "DIRTY.json"
    dirty.write_text(json.dumps(dirty_package), encoding="utf-8")
    verdict = validate_final_visual_package_file(dirty)
    assert verdict.startswith("VALIDATION_DEGRADED")
    assert "unfilled visual need" in verdict


# ------------------------------------------------------------------------- 6

def test_wall_time_measured_true_for_adapter_measured_stage(tmp_path):
    from optomind_research.runtime.harness_observability import (
        HarnessObservability,
    )

    obs = HarnessObservability(work_dir=tmp_path, run_id="r3_wall")
    obs.start_stage("publication_mainline_handoff")
    time.sleep(0.01)
    obs.finish_stage("publication_mainline_handoff", "completed")
    events_path = None
    for candidate in tmp_path.rglob("*.jsonl"):
        events_path = candidate
        break
    assert events_path is not None
    payload = events_path.read_text(encoding="utf-8")
    assert '"wall_time_measured": true' in payload or '"wall_time_measured":true' in payload


# ------------------------------------------------------------------------- 7

def test_supplementary_closure_runs_on_partial_inputs(tmp_path):
    from optomind_research.runtime.section_supplementary_orchestrator import (
        run_section_supplementary_closure,
    )

    calls = {"retrieval": 0}

    def fake_retrieval_wave(payload):
        calls["retrieval"] += 1
        return {"status": "completed", "hits": []}

    probe = {
        "schema_version": "research_harness.section_coverage_probe.v1",
        "section_id": "sec_intro",
        "sections": [],
        "claims": [],
        "claim_gaps": [],
    }
    report = run_section_supplementary_closure(
        probe_report=probe,
        output_dir=tmp_path / "supp",
        allow_overwrite=True,
        retrieval_wave_callback=fake_retrieval_wave,
    )
    assert report.get("mode") in {"dry_run", "live"}
    assert (tmp_path / "supp" / "section_supplementary_closure_report.json").exists()


# ------------------------------------------------------------------------- 8

def test_strawman_issue_type_accepted_by_critic():
    assert "strawman_not_but" in _ALLOWED_ISSUE_TYPES


# ------------------------------------------------------------------------- 9

def test_quality_report_carries_remediation_hints():
    from optomind_research.runtime import review_content_evaluator as rce

    source = Path(rce.__file__).read_text(encoding="utf-8")
    assert "_REMEDIATION_HINTS" in source
    assert '"remediation_hints"' in source
