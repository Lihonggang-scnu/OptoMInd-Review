"""Run the Research Program Architect on the shared AgentScope workbench."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .research_program_tool_provider import (
    ResearchProgramContext,
    ResearchProgramToolProvider,
    _unsupported_narrative_quantitative_claims,
)
from .research_program_schemas import (
    ResearchHypothesisPortfolio,
    ResearchOpportunityMap,
)
from .artifact_store import atomic_write_json, atomic_write_text
from .research_worker import ResearchWorker
from .task_contract import ResultManifest, TaskContract, TaskStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]


_PROGRAM_REBUILD_ARTIFACTS = (
    "RESULT.json",
    "RESULT.md",
    "AGENT_STATE.json",
    "TASK.json",
    "PLAN.md",
    "EVENTS.jsonl",
    "RESEARCH_PROBLEM_FRAME.json",
    "RESEARCH_GAP_MAP.json",
    "PROGRAM_SHARED_CONTEXT.json",
    "RESEARCH_OPPORTUNITY_MAP.json",
    "HYPOTHESIS_PORTFOLIO.json",
    "HYPOTHESIS_READINESS_AUDIT.json",
    "PROGRAM_FOCUS_GATE.json",
    "R5_RECONCILIATION.json",
    "R5_BUDGET.json",
    "R5_PHASE_ACCOUNTING.json",
    "RESEARCH_PLAN.json",
    "RESEARCH_PLAN.md",
    "RESEARCH_PLAN_AUDIT.json",
)
_R5_PHASE_HANDOFF_ARTIFACTS = (
    "RESULT.json",
    "RESULT.md",
    "TASK.json",
    "PLAN.md",
    "AGENT_STATE.json",
    "R5_DISCOVERY_STATUS.json",
)
_QUANTITATIVE_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?|[<>]=?|[\u00b1\u2248\u221d]|"
    r"\^[+-]?\d+|lambda\s*/\s*\d+)",
    re.I,
)


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _cost_snapshot(value: dict[str, Any]) -> dict[str, float | int]:
    """Extract comparable cumulative accounting fields from COST.json."""

    return {
        "model_calls": int(
            value.get("model_call_count", value.get("model_calls", 0)) or 0
        ),
        "tool_calls": int(
            value.get("tool_call_count", value.get("tool_calls", 0)) or 0
        ),
        "input_tokens": int(
            value.get("total_input_tokens", value.get("input_tokens", 0)) or 0
        ),
        "output_tokens": int(
            value.get("total_output_tokens", value.get("output_tokens", 0)) or 0
        ),
        "estimated_cost_cny": float(
            value.get("estimated_cost_cny", 0.0) or 0.0
        ),
        "wall_time_seconds": float(
            value.get("wall_time_seconds", 0.0) or 0.0
        ),
    }


def _zero_cost() -> dict[str, float | int]:
    return {
        "model_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "wall_time_seconds": 0.0,
    }


def _add_cost(
    left: dict[str, float | int], right: dict[str, float | int]
) -> dict[str, float | int]:
    """Add two non-overlapping usage snapshots."""

    return {
        "model_calls": int(left["model_calls"]) + int(right["model_calls"]),
        "tool_calls": int(left["tool_calls"]) + int(right["tool_calls"]),
        "input_tokens": int(left["input_tokens"]) + int(right["input_tokens"]),
        "output_tokens": int(left["output_tokens"]) + int(right["output_tokens"]),
        "estimated_cost_cny": round(
            float(left["estimated_cost_cny"])
            + float(right["estimated_cost_cny"]),
            6,
        ),
        "wall_time_seconds": round(
            float(left["wall_time_seconds"])
            + float(right["wall_time_seconds"]),
            2,
        ),
    }


def _max_cost(
    left: dict[str, float | int], right: dict[str, float | int]
) -> dict[str, float | int]:
    """Merge monotonic observations without summing possibly-overlapping data."""

    return {
        "model_calls": max(int(left["model_calls"]), int(right["model_calls"])),
        "tool_calls": max(int(left["tool_calls"]), int(right["tool_calls"])),
        "input_tokens": max(int(left["input_tokens"]), int(right["input_tokens"])),
        "output_tokens": max(int(left["output_tokens"]), int(right["output_tokens"])),
        "estimated_cost_cny": round(
            max(
                float(left["estimated_cost_cny"]),
                float(right["estimated_cost_cny"]),
            ),
            6,
        ),
        "wall_time_seconds": round(
            max(
                float(left["wall_time_seconds"]),
                float(right["wall_time_seconds"]),
            ),
            2,
        ),
    }


def _same_r5_ledger(
    before_raw: dict[str, Any], after_raw: dict[str, Any], run_id: str
) -> bool:
    return (
        str(before_raw.get("run_id") or "") == str(run_id)
        and str(after_raw.get("run_id") or "") == str(run_id)
        and str(after_raw.get("task_id") or "research_program")
        == "research_program"
    )


def _load_r5_lifetime_state(
    context: ResearchProgramContext,
    current_cost: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile lifetime usage without pretending ambiguous legacy totals agree.

    R5 phase-accounting v1 mixed a phase-local ``COST.json`` with a cumulative
    baseline.  For that legacy shape, the only defensible reconstruction is the
    observed baseline plus the observed current ledger; the raw observations
    and the ambiguity flag are retained for audit.  v2 writes a dedicated
    lifetime total and never needs this inference.
    """

    accounting = _read_mapping(context.work_dir / "R5_PHASE_ACCOUNTING.json")
    budget = _read_mapping(context.work_dir / "R5_BUDGET.json")
    cost_snapshot = _cost_snapshot(current_cost)
    top_total = accounting.get("lifetime_total")
    if not isinstance(top_total, dict):
        top_total = accounting.get("total")
    top_snapshot = _cost_snapshot(top_total) if isinstance(top_total, dict) else None
    baseline_raw = budget.get("baseline")
    baseline_snapshot = (
        _cost_snapshot(baseline_raw)
        if isinstance(baseline_raw, dict)
        else None
    )
    budget_current_raw = budget.get("current")
    budget_current_snapshot = (
        _cost_snapshot(budget_current_raw)
        if isinstance(budget_current_raw, dict)
        else None
    )

    schema = str(accounting.get("schema_version") or "")
    historical_ambiguity = False
    if schema == "research_harness.r5_phase_accounting.v2" and isinstance(
        top_total, dict
    ):
        lifetime = _max_cost(_cost_snapshot(top_total), cost_snapshot)
        source = "r5_phase_accounting_v2_lifetime_total"
    elif baseline_snapshot is not None and (
        accounting or budget_current_snapshot is not None
    ):
        # Legacy R5_BUDGET recorded the baseline and the latest COST ledger in
        # separate coordinate systems.  Keep both observations; do not sum
        # old phase rows whose overlap cannot be proven.
        lifetime = _add_cost(baseline_snapshot, cost_snapshot)
        if top_snapshot is not None:
            lifetime = _max_cost(lifetime, top_snapshot)
        source = "legacy_baseline_plus_current_ledger"
        historical_ambiguity = True
    else:
        candidates = [cost_snapshot]
        if top_snapshot is not None:
            candidates.append(top_snapshot)
        lifetime = candidates[0]
        for candidate in candidates[1:]:
            lifetime = _max_cost(lifetime, candidate)
        source = "cost_json_or_observed_legacy_max"
        historical_ambiguity = bool(accounting or budget)

    return {
        "lifetime_total": lifetime,
        "source": source,
        "historical_ambiguity": historical_ambiguity,
        "raw_observations": {
            "cost_json": cost_snapshot,
            "phase_accounting_total": top_snapshot,
            "budget_baseline": baseline_snapshot,
            "budget_current": budget_current_snapshot,
        },
    }


def _subtract_cost(after: dict[str, float | int], before: dict[str, float | int]) -> dict[str, float | int]:
    return {
        "model_calls": max(0, int(after["model_calls"]) - int(before["model_calls"])),
        "tool_calls": max(0, int(after["tool_calls"]) - int(before["tool_calls"])),
        "input_tokens": max(0, int(after["input_tokens"]) - int(before["input_tokens"])),
        "output_tokens": max(0, int(after["output_tokens"]) - int(before["output_tokens"])),
        "estimated_cost_cny": round(
            max(0.0, float(after["estimated_cost_cny"]) - float(before["estimated_cost_cny"])),
            6,
        ),
        "wall_time_seconds": round(
            max(0.0, float(after["wall_time_seconds"]) - float(before["wall_time_seconds"])),
            2,
        ),
    }


def _build_r5_budget_state(
    *,
    prior_cost: dict[str, Any],
    requested_cost_cny: float,
    requested_token_budget: int,
    root_cost_ceiling: Optional[float] = None,
    root_token_ceiling: Optional[int] = None,
    root_baseline_cost_cny: Optional[float] = None,
    root_baseline_input_tokens: Optional[int] = None,
    root_requested_cost_cny: Optional[float] = None,
    root_requested_token_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Build one absolute budget shared by the R5 discovery/plan phases.

    ``TaskContract`` budgets are cumulative because ``CostLedger`` restores the
    prior spend.  The public CLI budget is an *increment for the whole R5 run*,
    not an allowance to be re-added when the runner crosses from discovery to
    plan-only.  Recursive calls pass the root ceiling explicitly; this helper
    therefore makes the distinction auditable and prevents phase-local budget
    doubling.
    """

    current = _cost_snapshot(prior_cost)
    requested_cost = max(
        0.01,
        float(
            root_requested_cost_cny
            if root_requested_cost_cny is not None
            else requested_cost_cny
        ),
    )
    requested_tokens = max(
        1,
        int(
            root_requested_token_budget
            if root_requested_token_budget is not None
            else requested_token_budget
        ),
    )
    if root_cost_ceiling is None or root_token_ceiling is None:
        baseline_cost = float(current["estimated_cost_cny"])
        baseline_tokens = int(current["input_tokens"])
        ceiling_cost = baseline_cost + requested_cost
        ceiling_tokens = baseline_tokens + requested_tokens
    else:
        ceiling_cost = max(0.0, float(root_cost_ceiling))
        ceiling_tokens = max(0, int(root_token_ceiling))
        baseline_cost = (
            float(root_baseline_cost_cny)
            if root_baseline_cost_cny is not None
            else max(0.0, ceiling_cost - requested_cost)
        )
        baseline_tokens = (
            int(root_baseline_input_tokens)
            if root_baseline_input_tokens is not None
            else max(0, ceiling_tokens - requested_tokens)
        )
    return {
        "scope": "entire_r5_run",
        "baseline": {
            "input_tokens": baseline_tokens,
            "estimated_cost_cny": round(baseline_cost, 6),
        },
        "requested_increment": {
            "input_tokens": requested_tokens,
            "estimated_cost_cny": round(requested_cost, 6),
        },
        "ceiling": {
            "input_tokens": ceiling_tokens,
            "estimated_cost_cny": round(ceiling_cost, 6),
        },
        "current": current,
        "remaining": {
            "input_tokens": max(0, ceiling_tokens - int(current["input_tokens"])),
            "estimated_cost_cny": round(
                max(0.0, ceiling_cost - float(current["estimated_cost_cny"])),
                6,
            ),
        },
    }


def _write_r5_budget_state(
    context: ResearchProgramContext,
    state: dict[str, Any],
    *,
    phase: str,
    run_id: Optional[str] = None,
) -> None:
    """Persist the immutable root envelope and lifetime usage before a phase."""

    current = _cost_snapshot(state.get("current") or {})
    ceiling = dict(state.get("ceiling") or {})
    state = dict(state)
    state["phase"] = phase
    state["lifetime_cumulative"] = current
    if run_id:
        invocation = dict(state.get("invocation") or {})
        invocation.setdefault("run_id", run_id)
        invocation.setdefault("phase", phase)
        invocation.setdefault("start_lifetime", current)
        invocation.setdefault("usage", _zero_cost())
        state["invocation"] = invocation
    state["current"] = current
    state["remaining"] = {
        "input_tokens": max(
            0,
            int(ceiling.get("input_tokens", 0) or 0)
            - int(current["input_tokens"]),
        ),
        "estimated_cost_cny": round(
            max(
                0.0,
                float(ceiling.get("estimated_cost_cny", 0.0) or 0.0)
                - float(current["estimated_cost_cny"]),
            ),
            6,
        ),
    }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(context.work_dir / "R5_BUDGET.json", state)


def _record_r5_phase_accounting(
    context: ResearchProgramContext,
    *,
    phase: str,
    run_id: str,
    before_cost: dict[str, Any],
    result: ResultManifest,
    budget_state: Optional[dict[str, Any]] = None,
) -> None:
    """Persist per-phase deltas plus the cumulative R5 ledger."""

    path = context.work_dir / "R5_PHASE_ACCOUNTING.json"
    existing = _read_mapping(path)
    after_raw = _read_mapping(context.work_dir / "COST.json")
    before = _cost_snapshot(before_cost)
    after = _cost_snapshot(after_raw)
    budget = dict(budget_state or {})
    lifetime_before = _cost_snapshot(
        budget.get("current")
        or budget.get("lifetime_cumulative")
        or before
    )
    same_ledger = _same_r5_ledger(before_cost, after_raw, run_id)
    delta = _subtract_cost(after, before) if same_ledger else after
    lifetime_after = _add_cost(lifetime_before, delta)
    prior_lifetime = existing.get("lifetime_total")
    if isinstance(prior_lifetime, dict):
        lifetime_after = _max_cost(_cost_snapshot(prior_lifetime), lifetime_after)
    phases = dict(existing.get("phases") or {})
    phase_data = dict(phases.get(phase) or {})
    runs = list(phase_data.get("runs") or [])
    recorded_status = result.status.value
    run_record = {
        "run_id": run_id,
        "status": recorded_status,
        "worker_status": result.status.value,
        "stop_reason": result.stop_reason or "",
        **delta,
    }
    if phase == "plan_only" and result.status == TaskStatus.completed:
        if not _r5_plan_artifacts_present_and_audit_passed(context.work_dir):
            # A completed discovery result must never turn an empty plan-only
            # phase into a completed accounting row, especially when the
            # phase-local worker was accidentally allowed to reuse RESULT.json.
            run_record["status"] = TaskStatus.validation_failed.value
            run_record["completion_gate"] = (
                "plan_artifacts_and_passed_audit_required"
            )
    runs.append(run_record)
    totals = {
        key: round(
            sum(float(item.get(key, 0) or 0) for item in runs),
            6 if key == "estimated_cost_cny" else 2,
        )
        for key in delta
    }
    phases[phase] = {"runs": runs, "totals": totals}
    if budget:
        budget["current"] = lifetime_after
        budget["lifetime_cumulative"] = lifetime_after
        ceiling = dict(budget.get("ceiling") or {})
        budget["remaining"] = {
            "input_tokens": max(
                0,
                int(ceiling.get("input_tokens", 0) or 0)
                - int(lifetime_after["input_tokens"]),
            ),
            "estimated_cost_cny": round(
                max(
                    0.0,
                    float(ceiling.get("estimated_cost_cny", 0.0) or 0.0)
                    - float(lifetime_after["estimated_cost_cny"]),
                ),
                6,
            ),
        }
        budget["last_phase"] = phase
        budget["invocation"] = {
            "run_id": run_id,
            "phase": phase,
            "ledger_mode": "continued" if same_ledger else "fresh_run_id",
            "start_lifetime": lifetime_before,
            "usage": delta,
            "end_lifetime": lifetime_after,
        }
    atomic_write_json(
        path,
        {
            "schema_version": "research_harness.r5_phase_accounting.v2",
            "phases": phases,
            "total": lifetime_after,
            "lifetime_total": lifetime_after,
            "last_phase": phase,
            "last_run_id": run_id,
            "budget": budget,
            "legacy_reconciliation": (
                dict(budget.get("reconciliation") or {}) if budget else {}
            ),
            "policy": (
                "initial_discovery and plan_only are separately accounted; "
                "the root ceiling is shared; total/lifetime_total is monotonic "
                "lifetime usage, while each run stores its own delta."
            ),
        },
    )


def _focus_gate_is_passed(work_dir: Path) -> bool:
    focus = _read_mapping(work_dir / "PROGRAM_FOCUS_GATE.json")
    return str(focus.get("status") or "").strip().lower() == "passed"


def _has_accepted_discovery_artifacts(work_dir: Path) -> bool:
    """Return true when an interrupted discovery run has a usable spine."""

    opportunities = _read_mapping(work_dir / "RESEARCH_OPPORTUNITY_MAP.json")
    hypotheses = _read_mapping(work_dir / "HYPOTHESIS_PORTFOLIO.json")
    return bool(
        isinstance(opportunities.get("opportunities"), list)
        and opportunities.get("opportunities")
        and isinstance(hypotheses.get("hypotheses"), list)
        and hypotheses.get("hypotheses")
    )


_DISCOVERY_EVIDENCE_TOOLS = (
    # Explicit allowlist — never guess by substring ("research" contains
    # "search"; "read_" matches every reader tool).  Keep in sync with the
    # opportunity-stage evidence tools registered at
    # research_program_runner.py generation_tools ("opportunity" branch,
    # lines ~1476-1481).
    "read_review_sections_batch",
    "inspect_research_evidence_batch",
)


def _discovery_evidence_batches(work_dir: Path, result: ResultManifest) -> dict:
    """Summarize which evidence tools the discovery worker actually ran.

    P1-4: the awaiting-human record must say what was already tried.  The
    per-call query ledger does not exist at this layer, so this stays at
    the granularity EVENTS.jsonl actually records: tool-name call counts.

    P1-5 caliber fixes (verified against the reference-run EVENTS.jsonl):
    - count only ``event == "tool_call"`` rows — the logger writes one
      start and one result row per call, so counting every row doubled
      each number;
    - match tool names against the explicit allowlist above instead of
      substrings, so context loaders are not mistaken for evidence reads;
    - the real logger key is ``tool`` (event_logger.py log_tool_call /
      log_tool_result); ``tool_name`` stays as a defensive fallback only.
    """

    counts: dict[str, int] = {}
    events_path = (result.output_paths or {}).get("events")
    if not events_path:
        return {"tracked": False, "tool_call_counts": counts}
    path = Path(events_path)
    if not path.is_file():
        return {"tracked": False, "tool_call_counts": counts}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("event") or "") != "tool_call":
                # One call emits a start row and a result row; count starts
                # only (a crashed call may never produce its result row,
                # and "what was attempted" is exactly what humans need).
                continue
            name = str(
                row.get("tool") or row.get("tool_name") or ""
            )
            if name in _DISCOVERY_EVIDENCE_TOOLS:
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        return {"tracked": False, "tool_call_counts": counts}
    return {"tracked": True, "tool_call_counts": counts}


def _discovery_gap_description(work_dir: Path) -> str:
    """State where the focus-gate pipeline stopped, from artifacts alone."""

    opportunities = _read_mapping(work_dir / "RESEARCH_OPPORTUNITY_MAP.json")
    hypotheses = _read_mapping(work_dir / "HYPOTHESIS_PORTFOLIO.json")
    opp_list = opportunities.get("opportunities")
    hyp_list = hypotheses.get("hypotheses")
    opp_n = len(opp_list) if isinstance(opp_list, list) else 0
    hyp_n = len(hyp_list) if isinstance(hyp_list, list) else 0
    if _focus_gate_is_passed(work_dir):
        return "focus_gate_passed"
    if opp_n and hyp_n:
        return (
            "focus_gate_missing: opportunity map and hypothesis portfolio "
            f"accepted ({opp_n} opportunities, {hyp_n} hypotheses); "
            "next required artifact is a passed PROGRAM_FOCUS_GATE.json"
        )
    if opp_n:
        return (
            "focus_gate_missing: opportunity map accepted "
            f"({opp_n} opportunities) but no accepted hypothesis portfolio; "
            "hypothesis stage has not completed"
        )
    return (
        "focus_gate_missing: no accepted opportunity map yet; "
        "initial discovery never produced its first artifact"
    )


def _r5_opportunity_map_is_accepted(
    provider: ResearchProgramToolProvider,
) -> bool:
    payload = _read_mapping(
        provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
    )
    try:
        model = ResearchOpportunityMap.model_validate(payload)
        errors = provider._validate_opportunities(
            [item.model_dump() for item in model.opportunities]
        )
        return not errors
    except Exception:
        return False


def _r5_hypothesis_portfolio_is_accepted(
    provider: ResearchProgramToolProvider,
) -> bool:
    if not _r5_opportunity_map_is_accepted(provider):
        return False
    opportunities = _read_mapping(
        provider.ctx.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
    )
    valid_opportunity_ids = {
        str(item.get("opportunity_id"))
        for item in opportunities.get("opportunities", [])
        if isinstance(item, dict)
    }
    payload = _read_mapping(
        provider.ctx.work_dir / "HYPOTHESIS_PORTFOLIO.json"
    )
    try:
        model = ResearchHypothesisPortfolio.model_validate(payload)
        errors = provider._validate_hypotheses(
            [item.model_dump() for item in model.hypotheses],
            valid_opportunity_ids,
        )
        return not errors
    except Exception:
        return False


def _determine_r5_discovery_stage(
    provider: ResearchProgramToolProvider,
) -> str:
    """Choose the smallest durable discovery protocol from disk artifacts."""

    if not _r5_opportunity_map_is_accepted(provider):
        return "opportunity"
    if not _r5_hypothesis_portfolio_is_accepted(provider):
        return "hypothesis"
    if _focus_gate_is_passed(provider.ctx.work_dir):
        return "plan_only_handoff"
    return "focus"


def _archive_r5_agent_state_for_resume(
    work_dir: Path,
    *,
    stage: str,
    run_id: str,
) -> Optional[Path]:
    """Move the previous ReAct state aside before a narrowed resume."""

    state_path = work_dir / "AGENT_STATE.json"
    if not state_path.exists():
        return None
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_id))[:80] or "run"
    archive_dir = (
        work_dir
        / "_runtime_archive"
        / (
            "r5_discovery_resume_"
            + re.sub(r"[^A-Za-z0-9_.-]+", "_", str(stage))
            + "_"
            + safe_run_id
            + "_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "_"
            + uuid.uuid4().hex[:6]
        )
    )
    archive_dir.mkdir(parents=True, exist_ok=False)
    shutil.move(str(state_path), str(archive_dir / state_path.name))
    atomic_write_json(
        archive_dir / "RESUME_ARCHIVE_AUDIT.json",
        {
            "schema_version": "research_harness.r5_resume_archive.v1",
            "stage": stage,
            "run_id": run_id,
            "archived": ["AGENT_STATE.json"],
            "reason": (
                "A narrowed stage resume must start a fresh AgentState; the prior "
                "ReAct history is retained only for audit."
            ),
        },
    )
    atomic_write_json(
        work_dir / "R5_STAGE_RESUME_AUDIT.json",
        {
            "schema_version": "research_harness.r5_stage_resume.v1",
            "stage": stage,
            "run_id": run_id,
            "fresh_agent_state": True,
            "archived_state": str(archive_dir / state_path.name),
        },
    )
    return archive_dir


def _finalize_discovery_stage_transition(
    context: ResearchProgramContext,
    result: ResultManifest,
    *,
    stage: str,
    next_stage: str,
) -> ResultManifest:
    """Pause after one durable discovery stage without claiming final success."""

    payload = result.model_dump(mode="json")
    status_path = context.work_dir / "R5_DISCOVERY_STATUS.json"
    status_payload = {
        "schema_version": "research_harness.r5_discovery_status.v2",
        "status": "stage_complete",
        "completed_stage": stage,
        "next_stage": next_stage,
        "focus_gate_ready": False,
        "worker_status": result.status.value,
        "stop_reason": result.stop_reason or "",
        "policy": (
            "Discovery resumes are stage-aware. A later invocation receives a "
            "fresh AgentState and the next narrower tool protocol."
        ),
    }
    atomic_write_json(status_path, status_payload)
    output_paths = dict(payload.get("output_paths") or {})
    output_paths.update(
        {
            "work_dir": str(context.work_dir),
            "r5_discovery_status": str(status_path),
            "r5_stage_resume_audit": str(
                context.work_dir / "R5_STAGE_RESUME_AUDIT.json"
            ),
        }
    )
    payload.update(
        {
            "status": TaskStatus.waiting_for_human.value,
            "stop_reason": f"r5_{stage}_stage_complete_next_{next_stage}",
            "success_criteria_met": [f"Durable {stage} artifacts were accepted."],
            "success_criteria_failed": [
                "The complete research-program focus gate has not been reached."
            ],
            "validation_passed": False,
            "output_paths": output_paths,
            "errors": list(
                dict.fromkeys(
                    [*(payload.get("errors") or []), f"next_stage:{next_stage}"]
                )
            ),
        }
    )
    final = ResultManifest.model_validate(payload)
    atomic_write_json(context.work_dir / "RESULT.json", final.model_dump())
    atomic_write_text(
        context.work_dir / "RESULT.md",
        "# Research program result\n\n"
        "- Status: awaiting_human_review\n"
        f"- Completed discovery stage: {stage}\n"
        f"- Next stage: {next_stage}\n"
        "- No complete research plan is claimed before the focus gate.\n",
    )
    return final


def _requires_quantitative_provenance_migration(work_dir: Path) -> bool:
    hypotheses = _read_mapping(work_dir / "HYPOTHESIS_PORTFOLIO.json")
    plan = _read_mapping(work_dir / "RESEARCH_PLAN.json")
    if plan and (
        plan.get("schema_version") != "research_harness.research_plan.v2"
        or plan.get("results_status") != "verification_deferred"
    ):
        return True
    if _unsupported_narrative_quantitative_claims(
        str(plan.get("narrative_markdown") or "")
    ):
        return True
    for item in hypotheses.get("hypotheses", []):
        if (
            isinstance(item, dict)
            and _QUANTITATIVE_PATTERN.search(str(item.get("statement") or ""))
            and not item.get("quantitative_commitment_status")
        ):
            return True
    for item in plan.get("work_packages", []):
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(value)
            for key in (
                "objective",
                "methods",
                "expected_outputs",
                "evaluation_metrics",
                "stop_or_pivot_criteria",
            )
            for value in (
                item.get(key, [])
                if isinstance(item.get(key), list)
                else [item.get(key, "")]
            )
        )
        if (
            _QUANTITATIVE_PATTERN.search(text)
            and not item.get("quantitative_target_status")
        ):
            return True
    return False


def _archive_program_for_schema_migration(work_dir: Path) -> None:
    if not _requires_quantitative_provenance_migration(work_dir):
        return
    archive = (
        work_dir
        / "_runtime_archive"
        / (
            "quantitative_provenance_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "_"
            + uuid.uuid4().hex[:6]
        )
    )
    archive.mkdir(parents=True, exist_ok=False)
    for name in _PROGRAM_REBUILD_ARTIFACTS:
        path = work_dir / name
        if path.exists():
            shutil.move(str(path), str(archive / name))
    cost = work_dir / "COST.json"
    if cost.exists():
        shutil.copy2(cost, archive / "COST.snapshot.json")
    (archive / "MIGRATION.json").write_text(
        json.dumps(
            {
                "reason": "quantitative target provenance fields required",
                "cost_preserved_in_work_dir": cost.exists(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_deterministic_program_validation(
    provider: ResearchProgramToolProvider,
) -> str:
    """Run the canonical package validator without another model turn."""
    tool = next(
        (
            item
            for item in provider.get_tools(provider.ctx.work_dir)
            if item.name == "validate_research_program_package"
        ),
        None,
    )
    if tool is None:
        return "VALIDATION_FAILED: validator tool unavailable"
    value = tool()
    if inspect.isawaitable(value):
        value = asyncio.run(value)
    return " ".join(
        str(block.text)
        for block in getattr(value, "content", [])
        if hasattr(block, "text")
    )


def _r5_plan_artifacts_present_and_audit_passed(work_dir: Path) -> bool:
    """Return true only when the plan-only publication artifacts exist."""

    required = (
        "RESEARCH_PLAN.json",
        "RESEARCH_PLAN.md",
        "RESEARCH_PLAN_AUDIT.json",
    )
    if not all((work_dir / name).exists() for name in required):
        return False
    audit = _read_mapping(work_dir / "RESEARCH_PLAN_AUDIT.json")
    return str(audit.get("status") or "").strip().lower() == "passed"


def _run_r5_plan_completion_validation(
    provider: ResearchProgramToolProvider,
) -> str:
    """Validate the plan package only after all phase-local artifacts exist."""

    missing = [
        name
        for name in (
            "RESEARCH_PLAN.json",
            "RESEARCH_PLAN.md",
            "RESEARCH_PLAN_AUDIT.json",
        )
        if not (provider.ctx.work_dir / name).exists()
    ]
    if missing:
        return (
            "VALIDATION_FAILED: plan_completion_missing: "
            + ",".join(missing)
        )
    validation = _run_deterministic_program_validation(provider)
    audit = _read_mapping(provider.ctx.work_dir / "RESEARCH_PLAN_AUDIT.json")
    audit_status = str(audit.get("status") or "").strip().lower()
    if audit_status != "passed":
        return (
            "VALIDATION_FAILED: RESEARCH_PLAN_AUDIT.status must be passed; "
            f"got {audit_status or 'missing'}"
        )
    return validation


def _archive_r5_phase_runtime_for_handoff(
    work_dir: Path,
    *,
    run_id: str,
    from_phase: str,
    to_phase: str,
) -> Optional[Path]:
    """Archive phase-local runtime files before a same-directory handoff.

    Scientific artifacts, COST.json and EVENTS.jsonl stay in place.  Only
    mutable worker identity/state files move, so the next phase cannot reuse
    the previous phase's RESULT or AgentState while lifetime accounting remains
    continuous.
    """

    present = [
        name
        for name in _R5_PHASE_HANDOFF_ARTIFACTS
        if (work_dir / name).exists()
    ]
    if not present:
        return None
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_id))[:80] or "run"
    archive_dir = (
        work_dir
        / "_runtime_archive"
        / (
            "r5_phase_handoff_"
            + re.sub(r"[^A-Za-z0-9_.-]+", "_", str(from_phase))
            + "_to_"
            + re.sub(r"[^A-Za-z0-9_.-]+", "_", str(to_phase))
            + "_"
            + safe_run_id
            + "_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "_"
            + uuid.uuid4().hex[:6]
        )
    )
    archive_dir.mkdir(parents=True, exist_ok=False)
    for name in present:
        shutil.move(str(work_dir / name), str(archive_dir / name))
    audit = {
        "schema_version": "research_harness.r5_phase_handoff.v1",
        "run_id": run_id,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "archived_runtime_artifacts": present,
        "preserved_lifetime_artifacts": [
            "COST.json",
            "EVENTS.jsonl",
            "R5_BUDGET.json",
            "R5_PHASE_ACCOUNTING.json",
            "RESEARCH_OPPORTUNITY_MAP.json",
            "HYPOTHESIS_PORTFOLIO.json",
            "PROGRAM_FOCUS_GATE.json",
        ],
        "policy": (
            "A phase result and AgentState are not reusable by a different "
            "phase, even when run_id/task_id are unchanged."
        ),
    }
    atomic_write_json(archive_dir / "PHASE_HANDOFF_AUDIT.json", audit)
    atomic_write_json(work_dir / "R5_PHASE_HANDOFF_AUDIT.json", audit)
    return archive_dir


def _finalize_deterministically_valid_program(
    context: ResearchProgramContext,
    *,
    run_id: str,
    validation: str,
    prior_result: Optional[ResultManifest] = None,
) -> ResultManifest:
    """Promote durable validated artifacts after a worker budget boundary."""
    prior = (
        prior_result.model_dump()
        if prior_result is not None
        else _read_mapping(context.work_dir / "RESULT.json")
    )
    cost = _read_mapping(context.work_dir / "COST.json")
    output_paths = dict(prior.get("output_paths") or {})
    output_paths.update({
        "work_dir": str(context.work_dir),
        "opportunity_map": str(
            context.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
        ),
        "hypothesis_portfolio": str(
            context.work_dir / "HYPOTHESIS_PORTFOLIO.json"
        ),
        "hypothesis_readiness_audit": str(
            context.work_dir / "HYPOTHESIS_READINESS_AUDIT.json"
        ),
        "research_plan_json": str(context.work_dir / "RESEARCH_PLAN.json"),
        "research_plan_markdown": str(context.work_dir / "RESEARCH_PLAN.md"),
        "research_plan_audit": str(
            context.work_dir / "RESEARCH_PLAN_AUDIT.json"
        ),
        "research_problem_frame": str(
            context.work_dir / "RESEARCH_PROBLEM_FRAME.json"
        ),
        "research_gap_map": str(
            context.work_dir / "RESEARCH_GAP_MAP.json"
        ),
        "program_shared_context": str(
            context.work_dir / "PROGRAM_SHARED_CONTEXT.json"
        ),
        "program_focus_gate": str(
            context.work_dir / "PROGRAM_FOCUS_GATE.json"
        ),
        "r5_reconciliation": str(
            context.work_dir / "R5_RECONCILIATION.json"
        ),
        "r5_budget": str(context.work_dir / "R5_BUDGET.json"),
        "r5_phase_accounting": str(
            context.work_dir / "R5_PHASE_ACCOUNTING.json"
        ),
    })
    result = ResultManifest(
        run_id=run_id,
        task_id="research_program",
        status=TaskStatus.completed,
        stop_reason="deterministic_post_validation_passed",
        success_criteria_met=[
            "The deterministic research-program validator passes."
        ],
        success_criteria_failed=[],
        validation_passed=True,
        tool_call_count=int(
            prior.get("tool_call_count")
            or cost.get("tool_call_count")
            or 0
        ),
        iter_count=int(prior.get("iter_count") or 0),
        wall_time_seconds=float(
            prior.get("wall_time_seconds")
            or cost.get("wall_time_seconds")
            or 0.0
        ),
        total_input_tokens=int(
            prior.get("total_input_tokens")
            or cost.get("total_input_tokens")
            or 0
        ),
        total_output_tokens=int(
            prior.get("total_output_tokens")
            or cost.get("total_output_tokens")
            or 0
        ),
        estimated_cost_cny=float(
            prior.get("estimated_cost_cny")
            or cost.get("estimated_cost_cny")
            or 0.0
        ),
        estimated_cost_usd=float(
            prior.get("estimated_cost_usd")
            or cost.get("estimated_cost_usd")
            or 0.0
        ),
        output_paths=output_paths,
        errors=[],
    )
    atomic_write_json(context.work_dir / "RESULT.json", result.model_dump())
    atomic_write_text(
        context.work_dir / "RESULT.md",
        "# Research program result\n\n"
        "- Status: completed\n"
        "- Stop reason: deterministic post-validation passed\n"
        f"- Validation: {validation.strip()}\n",
    )
    return result


def _finalize_plan_only_human_review(
    context: ResearchProgramContext,
    result: ResultManifest,
    *,
    reason: str,
    validation: str = "",
) -> ResultManifest:
    """Stop a failed plan-only repair without relaunching the model loop."""

    payload = result.model_dump(mode="json")
    output_paths = dict(payload.get("output_paths") or {})
    output_paths.update(
        {
            "work_dir": str(context.work_dir),
            "plan_scaffold": str(
                context.work_dir / "RESEARCH_PLAN_SCAFFOLD.json"
            ),
            "plan_normalization_audit": str(
                context.work_dir / "RESEARCH_PLAN_NORMALIZATION_AUDIT.json"
            ),
            "research_plan_json": str(
                context.work_dir / "RESEARCH_PLAN.json"
            ),
            "research_plan_audit": str(
                context.work_dir / "RESEARCH_PLAN_AUDIT.json"
            ),
            "r5_reconciliation": str(
                context.work_dir / "R5_RECONCILIATION.json"
            ),
            "opportunity_map": str(
                context.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
            ),
            "hypothesis_portfolio": str(
                context.work_dir / "HYPOTHESIS_PORTFOLIO.json"
            ),
            "program_focus_gate": str(
                context.work_dir / "PROGRAM_FOCUS_GATE.json"
            ),
            "r5_budget": str(context.work_dir / "R5_BUDGET.json"),
            "r5_phase_accounting": str(
                context.work_dir / "R5_PHASE_ACCOUNTING.json"
            ),
        }
    )
    payload.update(
        {
            "status": TaskStatus.waiting_for_human.value,
            "stop_reason": reason,
            "success_criteria_met": [],
            "success_criteria_failed": [
                "The plan-only path did not reach an independently validated plan within one repair attempt."
            ],
            "validation_passed": False,
            "output_paths": output_paths,
            "errors": list(
                dict.fromkeys(
                    [
                        *(payload.get("errors") or []),
                        reason,
                        validation[:800] if validation else "",
                    ]
                )
            ),
        }
    )
    payload["errors"] = [item for item in payload["errors"] if item]
    final = ResultManifest.model_validate(payload)
    atomic_write_json(context.work_dir / "RESULT.json", final.model_dump())
    atomic_write_text(
        context.work_dir / "RESULT.md",
        "# Research program result\n\n"
        "- Status: awaiting_human_review\n"
        f"- Stop reason: {reason}\n"
        "- Policy: plan-only resume stops after one semantic repair; no further model calls are admitted.\n"
        + (f"- Validation: {validation.strip()}\n" if validation else ""),
    )
    return final


def _finalize_discovery_needs_more_literature(
    context: ResearchProgramContext,
    result: ResultManifest,
    *,
    discovery_stage: str = "",
    effective_max_iters: Optional[int] = None,
) -> ResultManifest:
    """Stop initial discovery honestly when the focus gate was not reached."""

    payload = result.model_dump(mode="json")
    output_paths = dict(payload.get("output_paths") or {})
    status_path = context.work_dir / "R5_DISCOVERY_STATUS.json"
    opportunities = _read_mapping(
        context.work_dir / "RESEARCH_OPPORTUNITY_MAP.json"
    )
    hypotheses = _read_mapping(
        context.work_dir / "HYPOTHESIS_PORTFOLIO.json"
    )
    opp_list = opportunities.get("opportunities")
    hyp_list = hypotheses.get("hypotheses")
    # P1-4: a human can only decide "what next" if the record says how far
    # the run got and what it already tried.  These fields are additive;
    # existing keys are untouched.
    status_payload = {
        "schema_version": "research_harness.r5_discovery_status.v1",
        "status": "needs_more_literature",
        "focus_gate_ready": False,
        "worker_status": result.status.value,
        "stop_reason": result.stop_reason or "",
        "next_step": "Provide more bounded evidence or human direction before retrying focus.",
        "policy": "No research plan may be generated before a validated focus gate exists.",
        "iter_model_calls": int(result.iter_count or 0),
        "react_iter_count": result.react_iter_count,
        "effective_max_iters": effective_max_iters,
        "discovery_stage": discovery_stage or "opportunity",
        "accepted_artifacts": {
            "opportunity_map_accepted": bool(
                isinstance(opp_list, list) and opp_list
            ),
            "opportunity_count": (
                len(opp_list) if isinstance(opp_list, list) else 0
            ),
            "hypothesis_portfolio_accepted": bool(
                isinstance(hyp_list, list) and hyp_list
            ),
            "hypothesis_count": (
                len(hyp_list) if isinstance(hyp_list, list) else 0
            ),
        },
        "evidence_batches_tried": _discovery_evidence_batches(
            context.work_dir, result
        ),
        "gap_description": _discovery_gap_description(context.work_dir),
        # TODO(P2-1, unwired kind): when the discovery awaiting-human flow
        # gets its own decision surface, call
        # optomind_research.runtime.human_decision_gate.request_decision
        # with kind="discovery_needs_more_literature" (a HUMAN kind — no
        # auto-accept), subject_id=run_id, and mirror the fields above as
        # context.  Do NOT hand-roll a local decision-wait mechanism here.
        # The module now exists; only this call site is deliberately left
        # unwired pending an architect ticket (P1-4 scope boundary).
    }
    atomic_write_json(status_path, status_payload)
    output_paths.update(
        {
            "work_dir": str(context.work_dir),
            "r5_discovery_status": str(status_path),
            "r5_phase_accounting": str(context.work_dir / "R5_PHASE_ACCOUNTING.json"),
            "r5_budget": str(context.work_dir / "R5_BUDGET.json"),
        }
    )
    payload.update(
        {
            "status": TaskStatus.waiting_for_human.value,
            "stop_reason": "initial_discovery_focus_not_completed_needs_more_literature",
            "success_criteria_met": [],
            "success_criteria_failed": [
                "The initial discovery phase did not produce a validated focus gate."
            ],
            "validation_passed": False,
            "output_paths": output_paths,
            "errors": list(
                dict.fromkeys(
                    [
                        *(payload.get("errors") or []),
                        "needs_more_literature",
                    ]
                )
            ),
        }
    )
    final = ResultManifest.model_validate(payload)
    atomic_write_json(context.work_dir / "RESULT.json", final.model_dump())
    atomic_write_text(
        context.work_dir / "RESULT.md",
        "# Research program result\n\n"
        "- Status: awaiting_human_review\n"
        "- Scientific status: needs_more_literature\n"
        "- Stop reason: initial discovery focus gate was not completed.\n",
    )
    return final


def run_research_program(
    context: ResearchProgramContext,
    *,
    run_id: str,
    model_tier: str = "premium_model",
    model_override: Optional[Any] = None,
    cost_budget_cny: float = 4.0,
    max_iters: int = 24,
    token_budget: int = 240_000,
    resume_plan_only: bool = False,
    auto_continue_discovery: bool = False,
    _r5_budget_ceiling_cny: Optional[float] = None,
    _r5_budget_ceiling_tokens: Optional[int] = None,
    _r5_budget_baseline_cny: Optional[float] = None,
    _r5_budget_baseline_tokens: Optional[int] = None,
    _r5_budget_requested_cny: Optional[float] = None,
    _r5_budget_requested_tokens: Optional[int] = None,
) -> ResultManifest:
    """Build or resume a validated research-program package."""

    context.work_dir.mkdir(parents=True, exist_ok=True)
    # Make the mode explicit inside the provider as well as the worker
    # contract.  This prevents a plan-only resume from accidentally exposing
    # the general discovery tool set.
    context.plan_only_resume = bool(resume_plan_only)
    _archive_program_for_schema_migration(context.work_dir)
    # The public budget is one increment for the whole R5 run.  Reconcile the
    # lifetime baseline before any worker is constructed; a fresh resume
    # run_id must not turn a phase-local COST.json into a new baseline.
    prior_cost = _read_mapping(context.work_dir / "COST.json")
    lifetime_state = _load_r5_lifetime_state(context, prior_cost)
    # Only the recursive discovery -> plan-only handoff may provide the
    # private root-envelope arguments below.  A later explicit CLI resume is a
    # new invocation: its immutable baseline is the reconciled lifetime total,
    # and its requested CLI allowance is added exactly once.  Never reuse an
    # old R5_BUDGET ceiling merely because the six private arguments are absent.
    budget_state = _build_r5_budget_state(
        prior_cost=lifetime_state["lifetime_total"],
        requested_cost_cny=cost_budget_cny,
        requested_token_budget=token_budget,
        root_cost_ceiling=_r5_budget_ceiling_cny,
        root_token_ceiling=_r5_budget_ceiling_tokens,
        root_baseline_cost_cny=_r5_budget_baseline_cny,
        root_baseline_input_tokens=_r5_budget_baseline_tokens,
        root_requested_cost_cny=_r5_budget_requested_cny,
        root_requested_token_budget=_r5_budget_requested_tokens,
    )
    budget_state["reconciliation"] = lifetime_state
    budget_state["lifetime_cumulative"] = budget_state["current"]
    root_token_ceiling = int(budget_state["ceiling"]["input_tokens"])
    root_cost_ceiling = float(budget_state["ceiling"]["estimated_cost_cny"])
    remaining_token_budget = int(budget_state["remaining"]["input_tokens"])
    remaining_cost_budget = float(
        budget_state["remaining"]["estimated_cost_cny"]
    )
    phase_name = "plan_only" if resume_plan_only else "initial_discovery"
    budget_state["invocation"] = {
        "run_id": run_id,
        "phase": phase_name,
        "start_lifetime": budget_state["current"],
        "usage": _zero_cost(),
    }
    _write_r5_budget_state(
        context,
        budget_state,
        phase=phase_name,
        run_id=run_id,
    )
    provider = ResearchProgramToolProvider(context)
    discovery_stage = (
        "plan_only"
        if resume_plan_only
        else _determine_r5_discovery_stage(provider)
    )
    # A passed focus is handled by the existing plan-only handoff below; no
    # discovery worker is constructed for the transitional marker.
    context.discovery_stage = (
        "focus" if discovery_stage == "plan_only_handoff" else discovery_stage
    )
    existing_focus_artifacts = all(
        (context.work_dir / name).exists()
        for name in (
            "RESEARCH_OPPORTUNITY_MAP.json",
            "HYPOTHESIS_PORTFOLIO.json",
            "PROGRAM_FOCUS_GATE.json",
        )
    )
    if resume_plan_only or existing_focus_artifacts:
        reconciliation = provider.reconcile_existing_r5_artifacts()
        if resume_plan_only and reconciliation.get("status") != "ready_for_plan_resume":
            return ResultManifest(
                run_id=run_id,
                task_id="research_program",
                status=TaskStatus.validation_failed,
                stop_reason="r5_plan_only_resume_blocked_by_reconciliation",
                success_criteria_met=[],
                success_criteria_failed=["Existing focus artifacts require deterministic repair before plan-only resume."],
                validation_passed=False,
                output_paths={
                    "work_dir": str(context.work_dir),
                    "r5_reconciliation": str(context.work_dir / "R5_RECONCILIATION.json"),
                },
                errors=list(reconciliation.get("errors") or []),
            )
        # A terminal ReAct history contains the old loop.  Every explicit
        # resume, including a narrowed discovery-stage resume, starts fresh;
        # the old state remains available only in the runtime archive.
        if resume_plan_only:
            _archive_r5_agent_state_for_resume(
                context.work_dir,
                stage="plan_only",
                run_id=run_id,
            )
    elif discovery_stage in {"opportunity", "hypothesis", "focus"}:
        _archive_r5_agent_state_for_resume(
            context.work_dir,
            stage=discovery_stage,
            run_id=run_id,
        )
    durable_outputs = (
        "RESEARCH_PROBLEM_FRAME.json",
        "RESEARCH_GAP_MAP.json",
        "RESEARCH_OPPORTUNITY_MAP.json",
        "HYPOTHESIS_PORTFOLIO.json",
        "RESEARCH_PLAN.json",
        "RESEARCH_PLAN.md",
        "RESEARCH_PLAN_AUDIT.json",
    )
    # A plan-only resume must not construct a model worker merely because the
    # broader R5 handoff set is incomplete.  The plan itself is a durable
    # publication boundary: reconciliation has already revalidated its
    # current content against the focus/opportunity/hypothesis artifacts, and
    # this validator independently rechecks the complete package.  The old
    # all-artifacts condition remains for ordinary runs, while this narrower
    # condition makes a valid plan-only resume deterministic and zero-model.
    plan_only_resume_ready = bool(
        resume_plan_only
        and _r5_plan_artifacts_present_and_audit_passed(context.work_dir)
    )
    if plan_only_resume_ready or all(
        (context.work_dir / name).exists() for name in durable_outputs
    ):
        validation = _run_r5_plan_completion_validation(provider)
        if "VALIDATION_PASSED" in validation:
            return _finalize_deterministically_valid_program(
                context,
                run_id=run_id,
                validation=validation,
            )

    # A validated focus gate is the durable hand-off boundary.  Start a fresh
    # plan-only worker instead of allowing a discovery worker to carry its
    # long review context into plan writing.
    if not resume_plan_only and _focus_gate_is_passed(context.work_dir):
        _archive_r5_phase_runtime_for_handoff(
            context.work_dir,
            run_id=run_id,
            from_phase="initial_discovery",
            to_phase="plan_only",
        )
        return run_research_program(
            context,
            run_id=run_id,
            model_tier=model_tier,
            model_override=model_override,
            cost_budget_cny=max(0.01, remaining_cost_budget),
            token_budget=max(1, remaining_token_budget),
            max_iters=6,
            resume_plan_only=True,
            auto_continue_discovery=auto_continue_discovery,
            _r5_budget_ceiling_cny=root_cost_ceiling,
            _r5_budget_ceiling_tokens=root_token_ceiling,
            _r5_budget_baseline_cny=float(
                budget_state["baseline"]["estimated_cost_cny"]
            ),
            _r5_budget_baseline_tokens=int(
                budget_state["baseline"]["input_tokens"]
            ),
            _r5_budget_requested_cny=float(
                budget_state["requested_increment"]["estimated_cost_cny"]
            ),
            _r5_budget_requested_tokens=int(
                budget_state["requested_increment"]["input_tokens"]
            ),
        )

    # Do not teach a fresh repair pass to repeat the same malformed root
    # container from a terminal ReAct history.  Preserve the history for
    # audit while retaining COST.json as the cumulative accounting floor.
    prior_result = _read_mapping(context.work_dir / "RESULT.json")
    if (
        str(prior_result.get("status") or "")
        in {"budget_exhausted", "validation_failed"}
        and not (context.work_dir / "RESEARCH_OPPORTUNITY_MAP.json").exists()
    ):
        state_path = context.work_dir / "AGENT_STATE.json"
        if state_path.exists():
            archive_dir = (
                context.work_dir
                / "_runtime_archive"
                / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            )
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(state_path), str(archive_dir / state_path.name))

    prompt = (
        PROJECT_ROOT
        / "prompts"
        / "roles"
        / "Research Program Architect.txt"
    ).read_text(encoding="utf-8")
    worker = ResearchWorker(
        tool_provider=provider,
        _model_override=model_override,
        _system_prompt_override=prompt,
        _work_dir_override=context.work_dir,
    )
    if resume_plan_only:
        generation_tools = [
            "load_research_program_context",
            "submit_research_plan",
            "validate_research_program_package",
        ]
    elif discovery_stage == "opportunity":
        generation_tools = [
            "load_research_program_context",
            "read_review_sections_batch",
            "inspect_research_evidence_batch",
            "submit_research_opportunity_map",
        ]
    elif discovery_stage == "hypothesis":
        generation_tools = [
            "load_research_program_context",
            "submit_hypothesis_portfolio",
            "submit_program_focus_gate",
        ]
    else:
        generation_tools = [
            "load_research_program_context",
            "submit_program_focus_gate",
        ]
    if resume_plan_only:
        expected_outputs = [
            "RESEARCH_OPPORTUNITY_MAP.json",
            "HYPOTHESIS_PORTFOLIO.json",
            "RESEARCH_PLAN.json",
            "RESEARCH_PLAN.md",
            "RESEARCH_PLAN_AUDIT.json",
            "RESEARCH_PROBLEM_FRAME.json",
            "RESEARCH_GAP_MAP.json",
            "PROGRAM_SHARED_CONTEXT.json",
            "PROGRAM_FOCUS_GATE.json",
            "HYPOTHESIS_READINESS_AUDIT.json",
        ]
    elif discovery_stage == "opportunity":
        expected_outputs = ["RESEARCH_OPPORTUNITY_MAP.json"]
    elif discovery_stage == "hypothesis":
        expected_outputs = [
            "RESEARCH_OPPORTUNITY_MAP.json",
            "HYPOTHESIS_PORTFOLIO.json",
            "HYPOTHESIS_READINESS_AUDIT.json",
        ]
    else:
        expected_outputs = [
            "RESEARCH_OPPORTUNITY_MAP.json",
            "HYPOTHESIS_PORTFOLIO.json",
            "RESEARCH_PROBLEM_FRAME.json",
            "RESEARCH_GAP_MAP.json",
            "PROGRAM_SHARED_CONTEXT.json",
            "PROGRAM_FOCUS_GATE.json",
            "HYPOTHESIS_READINESS_AUDIT.json",
        ]
    if resume_plan_only:
        expected_outputs.append("R5_RECONCILIATION.json")
        expected_outputs.extend(
            [
                "RESEARCH_PLAN_SCAFFOLD.json",
                "RESEARCH_PLAN_NORMALIZATION_AUDIT.json",
            ]
        )
    focus_repair_extension = bool(
        not resume_plan_only
        and not _focus_gate_is_passed(context.work_dir)
        and _has_accepted_discovery_artifacts(context.work_dir)
    )
    discovery_max_iters = 10 if focus_repair_extension else 8
    stage_max_iters = {
        # Four useful turns (load, two optional batches, submit) plus one
        # terminal reserve so a final tool call is not stranded at the hard
        # ReAct boundary.
        "opportunity": 5,
        # Load, hypothesis, focus, plus one bounded repair/termination turn.
        "hypothesis": 4,
        # Load, focus, one semantic repair, plus one terminal reserve.
        "focus": 4,
    }
    effective_max_iters = (
        min(6, max(1, int(max_iters)))
        if resume_plan_only
        else min(
            stage_max_iters.get(discovery_stage, discovery_max_iters),
            max(1, int(max_iters)),
        )
    )
    if resume_plan_only:
        success_criteria = [
            "The deterministic research-program validator passes.",
        ]
    elif discovery_stage == "opportunity":
        success_criteria = [
            "Three to eight high-value research opportunities are traceable.",
            "Stop after the accepted opportunity map; do not generate hypotheses or focus in this stage.",
        ]
    elif discovery_stage == "hypothesis":
        success_criteria = [
            "Two to six hypotheses state assumptions, alternatives, and falsification conditions.",
            "Use the persisted opportunity map; do not regenerate or resubmit opportunities.",
            "If hypotheses are accepted, submit the focus gate within the bounded stage protocol.",
        ]
    else:
        success_criteria = [
            "The focus gate selects exactly one problem, one compatible platform, and one to three dependent main hypotheses.",
            "Nonselected opportunities are isolated as future branches and never enter current work packages.",
            "Stop at the accepted focus gate and hand off to the existing plan-only stage.",
        ]
    ledger_continues = (
        str(prior_cost.get("run_id") or "") == str(run_id)
        and str(prior_cost.get("task_id") or "research_program")
        == "research_program"
    )
    worker_ledger_baseline = (
        _cost_snapshot(prior_cost) if ledger_continues else _zero_cost()
    )
    worker_token_budget = (
        int(worker_ledger_baseline["input_tokens"])
        + remaining_token_budget
    )
    worker_cost_budget = (
        float(worker_ledger_baseline["estimated_cost_cny"])
        + remaining_cost_budget
    )
    r5_lifetime_budget = {
        "schema_version": "optomind.r5_lifetime_budget.v1",
        "baseline_input_tokens": int(budget_state["baseline"]["input_tokens"]),
        "baseline_cost_cny": float(budget_state["baseline"]["estimated_cost_cny"]),
        "ceiling_input_tokens": root_token_ceiling,
        "ceiling_cost_cny": root_cost_ceiling,
        "requested_increment_input_tokens": int(
            budget_state["requested_increment"]["input_tokens"]
        ),
        "requested_increment_cost_cny": float(
            budget_state["requested_increment"]["estimated_cost_cny"]
        ),
        "lifetime_before_invocation_input_tokens": int(
            budget_state["current"]["input_tokens"]
        ),
        "lifetime_before_invocation_cost_cny": float(
            budget_state["current"]["estimated_cost_cny"]
        ),
        "worker_ledger_baseline_input_tokens": int(
            worker_ledger_baseline["input_tokens"]
        ),
        "worker_ledger_baseline_cost_cny": float(
            worker_ledger_baseline["estimated_cost_cny"]
        ),
        "ledger_mode": "continued" if ledger_continues else "fresh_run_id",
        "historical_ambiguity": bool(
            (budget_state.get("reconciliation") or {}).get(
                "historical_ambiguity", False
            )
        ),
    }
    if resume_plan_only:
        stage_goal = (
            "Resume the research-program plan-writing stage from the existing "
            "validated opportunity, hypothesis, and focus artifacts. Do not "
            "recompute discovery, hypotheses, or focus; write and validate only "
            "the research plan."
        )
        stage_constraints = [
            "Do not reopen discovery, evidence archaeology, or completed-stage tools."
        ]
    elif discovery_stage == "opportunity":
        stage_goal = (
            "Build and persist the validated opportunity map from the review. "
            "Stop before hypothesis generation and focus selection."
        )
        stage_constraints = [
            "Use at most one section batch and one evidence batch; repeated batches must reuse the cached result.",
            "Do not generate hypotheses or a program focus in this stage.",
        ]
    elif discovery_stage == "hypothesis":
        stage_goal = (
            "Using the persisted opportunity map and compact bounded context, "
            "build the falsifiable hypothesis portfolio and, if accepted, submit "
            "the program focus gate."
        )
        stage_constraints = [
            "Do not regenerate or resubmit the persisted opportunity map.",
            "Do not read full review sections or call evidence archaeology tools.",
        ]
    else:
        stage_goal = (
            "Using the persisted opportunity map and hypothesis portfolio, "
            "submit and validate one converged program focus gate."
        )
        stage_constraints = [
            "Do not regenerate opportunities or hypotheses.",
            "Use only the compact focus-stage context and the focus submission tool.",
        ]
    contract = TaskContract(
        run_id=run_id,
        task_id="research_program",
        goal=stage_goal,
        constraints=[
            "Use English only.",
            "Use only allowlisted paper and chunk identifiers.",
            "Separate established evidence from author inference and hypothesis.",
            (
                "Do not present unverified results, exact values, DOI values, "
                "or novelty as established literature facts."
            ),
            "Do not execute experiments or simulations.",
            (
            "Every planned experiment, simulation, dataset collection, and "
            "data-analysis step must be labelled verification_deferred."
            ),
            *stage_constraints,
        ],
        success_criteria=success_criteria,
        allowed_tools=generation_tools,
        skill_ids=[],
        model_tier=model_tier,
        max_iters=effective_max_iters,
        wall_time_budget_seconds=900.0,
        # These are worker-ledger ceilings.  The nested R5 envelope below is
        # the authoritative lifetime admission rule and remains correct when a
        # fresh resume run_id starts its local CostLedger at zero.
        token_budget=max(0, worker_token_budget),
        cost_budget_cny=max(0.000001, worker_cost_budget),
        next_call_cost_reserve_cny=min(
            0.35, max(0.0, remaining_cost_budget) * 0.15
        ),
        expected_outputs=expected_outputs,
        metadata={
            "budget_scope": "entire_r5_run",
            "budget_baseline_input_tokens": int(
                budget_state["baseline"]["input_tokens"]
            ),
            "budget_baseline_cost_cny": float(
                budget_state["baseline"]["estimated_cost_cny"]
            ),
            "budget_ceiling_input_tokens": root_token_ceiling,
            "budget_ceiling_cost_cny": root_cost_ceiling,
            "budget_used_before_phase_input_tokens": int(
                budget_state["current"]["input_tokens"]
            ),
            "budget_used_before_phase_cost_cny": float(
                budget_state["current"]["estimated_cost_cny"]
            ),
            "budget_remaining_before_phase_input_tokens": remaining_token_budget,
            "budget_remaining_before_phase_cost_cny": remaining_cost_budget,
            "r5_lifetime_budget": r5_lifetime_budget,
            "budget_reconciliation": budget_state.get("reconciliation") or {},
            "phase": phase_name,
            "phase_identity": (
                f"research_program:{phase_name}:{discovery_stage}"
            ),
            "r5_discovery_stage": discovery_stage,
            "stage_iteration_reserve": 1,
            "stage_tool_protocol": list(generation_tools),
            "focus_repair_extension_applied": focus_repair_extension,
            "focus_repair_max_iters": discovery_max_iters,
        },
    )
    before_worker_cost = _read_mapping(context.work_dir / "COST.json")
    result = worker.run(contract)
    phase_name = "plan_only" if resume_plan_only else "initial_discovery"
    _record_r5_phase_accounting(
        context,
        phase=phase_name,
        run_id=run_id,
        before_cost=before_worker_cost,
        result=result,
        budget_state=budget_state,
    )
    _write_r5_budget_state(
        context,
        budget_state,
        phase=phase_name,
        run_id=run_id,
    )
    if not resume_plan_only and _focus_gate_is_passed(context.work_dir):
        # The focus artifact is the only hand-off signal.  A new invocation
        # archives the discovery AgentState and exposes only the plan-only
        # tools/context; it cannot inherit the long discovery conversation.
        _archive_r5_phase_runtime_for_handoff(
            context.work_dir,
            run_id=run_id,
            from_phase="initial_discovery",
            to_phase="plan_only",
        )
        return run_research_program(
            context,
            run_id=run_id,
            model_tier=model_tier,
            model_override=model_override,
            cost_budget_cny=max(0.01, remaining_cost_budget),
            token_budget=max(1, remaining_token_budget),
            max_iters=6,
            resume_plan_only=True,
            auto_continue_discovery=auto_continue_discovery,
            _r5_budget_ceiling_cny=root_cost_ceiling,
            _r5_budget_ceiling_tokens=root_token_ceiling,
            _r5_budget_baseline_cny=float(
                budget_state["baseline"]["estimated_cost_cny"]
            ),
            _r5_budget_baseline_tokens=int(
                budget_state["baseline"]["input_tokens"]
            ),
            _r5_budget_requested_cny=float(
                budget_state["requested_increment"]["estimated_cost_cny"]
            ),
            _r5_budget_requested_tokens=int(
                budget_state["requested_increment"]["input_tokens"]
            ),
        )
    if not resume_plan_only and not _focus_gate_is_passed(context.work_dir):
        post_stage = _determine_r5_discovery_stage(provider)
        if discovery_stage == "opportunity" and post_stage == "hypothesis":
            transition = _finalize_discovery_stage_transition(
                context,
                result,
                stage="opportunity",
                next_stage="hypothesis",
            )
            if (
                auto_continue_discovery
                and remaining_cost_budget > 0.05
                and remaining_token_budget > 1000
            ):
                return run_research_program(
                    context,
                    run_id=run_id,
                    model_tier=model_tier,
                    model_override=model_override,
                    cost_budget_cny=max(0.01, remaining_cost_budget),
                    token_budget=max(1, remaining_token_budget),
                    max_iters=max_iters,
                    auto_continue_discovery=True,
                    _r5_budget_ceiling_cny=root_cost_ceiling,
                    _r5_budget_ceiling_tokens=root_token_ceiling,
                    _r5_budget_baseline_cny=float(budget_state["baseline"]["estimated_cost_cny"]),
                    _r5_budget_baseline_tokens=int(budget_state["baseline"]["input_tokens"]),
                    _r5_budget_requested_cny=float(budget_state["requested_increment"]["estimated_cost_cny"]),
                    _r5_budget_requested_tokens=int(budget_state["requested_increment"]["input_tokens"]),
                )
            return transition
        if discovery_stage == "hypothesis" and post_stage == "focus":
            transition = _finalize_discovery_stage_transition(
                context,
                result,
                stage="hypothesis",
                next_stage="focus",
            )
            if (
                auto_continue_discovery
                and remaining_cost_budget > 0.05
                and remaining_token_budget > 1000
            ):
                return run_research_program(
                    context,
                    run_id=run_id,
                    model_tier=model_tier,
                    model_override=model_override,
                    cost_budget_cny=max(0.01, remaining_cost_budget),
                    token_budget=max(1, remaining_token_budget),
                    max_iters=max_iters,
                    auto_continue_discovery=True,
                    _r5_budget_ceiling_cny=root_cost_ceiling,
                    _r5_budget_ceiling_tokens=root_token_ceiling,
                    _r5_budget_baseline_cny=float(budget_state["baseline"]["estimated_cost_cny"]),
                    _r5_budget_baseline_tokens=int(budget_state["baseline"]["input_tokens"]),
                    _r5_budget_requested_cny=float(budget_state["requested_increment"]["estimated_cost_cny"]),
                    _r5_budget_requested_tokens=int(budget_state["requested_increment"]["input_tokens"]),
                )
            return transition
        return _finalize_discovery_needs_more_literature(
            context,
            result,
            discovery_stage=discovery_stage,
            effective_max_iters=effective_max_iters,
        )
    if result.status == TaskStatus.completed:
        completion_validation = _run_r5_plan_completion_validation(provider)
        if "VALIDATION_PASSED" in completion_validation:
            return result
        if resume_plan_only:
            return _finalize_plan_only_human_review(
                context,
                result,
                reason=(
                    "plan_only_completion_gate_failed; a completed worker result "
                    "is not reusable without a complete validated research plan"
                ),
                validation=completion_validation,
            )
        return _finalize_plan_only_human_review(
            context,
            result,
            reason=(
                "research_program_completion_gate_failed; plan artifacts and "
                "passed audit are required before completed"
            ),
            validation=completion_validation,
        )
    if all((context.work_dir / name).exists() for name in durable_outputs):
        validation = _run_r5_plan_completion_validation(provider)
        if "VALIDATION_PASSED" in validation:
            return _finalize_deterministically_valid_program(
                context,
                run_id=run_id,
                validation=validation,
                prior_result=result,
            )
        if resume_plan_only and result.status in {
            TaskStatus.budget_exhausted,
            TaskStatus.validation_failed,
        }:
            return _finalize_plan_only_human_review(
                context,
                result,
                reason=(
                    "plan_only_semantic_repair_limit_reached; "
                    "the durable plan still fails independent validation"
                ),
                validation=validation,
            )
    elif resume_plan_only and result.status in {
        TaskStatus.budget_exhausted,
        TaskStatus.validation_failed,
    }:
        return _finalize_plan_only_human_review(
            context,
            result,
            reason=(
                "plan_only_plan_not_submitted_within_bounded_protocol; "
                "human review is required"
            ),
        )
    return result
