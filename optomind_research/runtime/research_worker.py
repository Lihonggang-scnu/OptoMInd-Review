"""ResearchWorker — wraps AgentScope Agent with the full Phase 1.1 runtime kernel."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentscope.agent import Agent, ReActConfig, ContextConfig, ModelConfig
from agentscope.state import AgentState
from agentscope.message import UserMsg
from agentscope.tool import Toolkit, TaskCreate, TaskList, TaskGet, TaskUpdate
from agentscope.permission import (
    PermissionContext,
    PermissionMode,
    PermissionBehavior,
    PermissionRule,
)
from agentscope.event import (
    ReplyEndEvent,
    ExceedMaxItersEvent,
    ToolCallStartEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolResultEndEvent,
    ModelCallStartEvent,
    ModelCallEndEvent,
    ThinkingBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    RequireUserConfirmEvent,
)

from .task_contract import TaskContract, TaskStatus, HumanInterventionPolicy, ResultManifest
from .agent_model_factory import AgentScopeModelFactory
from .tool_registry import build_research_toolkit
from .skill_loader import get_skill_loader, FilteredSkillLoader
from .artifact_store import (
    ensure_work_dir,
    atomic_write_json,
    atomic_write_text,
)
from .event_logger import EventLogger
from .cost_ledger import CostLedger, estimate_call_cost_cny
from .recovery_policy import RecoveryPolicy, classify_error, ErrorCategory
from .stop_controller import StopController
from .tool_provider import ToolProvider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[3] / "prompts" / "roles" / "Scientific Research Worker.txt"
)

# Canonical tool-name aliases: friendly names users write in allowed_tools
# map to AgentScope's actual registered names.
_TOOL_ALIAS_MAP: Dict[str, str] = {
    # Research tools
    "list_task_artifacts": "list_task_artifacts",
    "read_task_artifact": "read_task_artifact",
    "write_task_note": "write_task_note",
    "validate_task_result": "validate_task_result",
    # AgentScope built-in task tools — canonical names are CamelCase
    "TaskCreate": "TaskCreate",
    "TaskList": "TaskList",
    "TaskGet": "TaskGet",
    "TaskUpdate": "TaskUpdate",
    # Legacy lower-case aliases for backward compat
    "create_task": "TaskCreate",
    "list_tasks": "TaskList",
    "get_task": "TaskGet",
    "update_task": "TaskUpdate",
}

_MAX_OUTER_ITERATIONS_BASE = 2  # extra iterations beyond candidate count


def _load_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "You are a scientific research worker. "
        "Complete the given task step by step using the available tools. "
        "Inspect the task, create a plan, read required artifacts, "
        "use tools rather than guessing, record findings, "
        "validate results before declaring completion."
    )


def _r5_lifetime_admission_reason(
    *,
    metadata: Dict[str, Any],
    ledger_input_tokens: int,
    ledger_cost_cny: float,
    predicted_next_input: int,
    output_reserve: int,
    model_name: str,
) -> Optional[str]:
    """Check one predicted call against the immutable R5 lifetime envelope."""

    envelope = metadata.get("r5_lifetime_budget")
    if not isinstance(envelope, dict):
        return None
    try:
        root_baseline_input = int(envelope["baseline_input_tokens"])
        root_baseline_cost = float(envelope["baseline_cost_cny"])
        ceiling_input = int(envelope["ceiling_input_tokens"])
        ceiling_cost = float(envelope["ceiling_cost_cny"])
        # The root baseline is immutable, but it is not necessarily the
        # beginning of this phase.  In the recursive discovery -> plan-only
        # handoff, discovery has already spent part of the envelope.  The
        # runner records that cumulative amount as the invocation start.
        phase_start_input = int(
            envelope.get(
                "lifetime_before_invocation_input_tokens",
                envelope.get("phase_start_lifetime_input_tokens", root_baseline_input),
            )
        )
        phase_start_cost = float(
            envelope.get(
                "lifetime_before_invocation_cost_cny",
                envelope.get("phase_start_lifetime_cost_cny", root_baseline_cost),
            )
        )
        worker_baseline_input = int(
            envelope.get("worker_ledger_baseline_input_tokens", 0)
        )
        worker_baseline_cost = float(
            envelope.get("worker_ledger_baseline_cost_cny", 0.0)
        )
    except (TypeError, ValueError, KeyError):
        return "r5_lifetime_budget_invalid_metadata"

    invocation_input = max(0, int(ledger_input_tokens) - worker_baseline_input)
    lifetime_input_after_reservation = (
        phase_start_input + invocation_input + max(0, int(predicted_next_input))
        + max(0, int(output_reserve))
    )
    if lifetime_input_after_reservation > ceiling_input:
        return (
            "r5_lifetime_token_admission: phase_start="
            f"{phase_start_input} invocation_used={invocation_input} "
            f"predicted_next_input={int(predicted_next_input)} "
            f"output_reserve={int(output_reserve)} "
            f"ceiling={ceiling_input}"
        )

    invocation_cost = max(
        0.0, float(ledger_cost_cny) - worker_baseline_cost
    )
    predicted_cost = estimate_call_cost_cny(
        model_name,
        max(0, int(predicted_next_input)),
        max(0, int(output_reserve)),
    )
    lifetime_cost_after_reservation = (
        phase_start_cost + invocation_cost + predicted_cost
    )
    if lifetime_cost_after_reservation > ceiling_cost:
        return (
            "r5_lifetime_cost_admission: phase_start="
            f"{phase_start_cost:.6f} invocation_used={invocation_cost:.6f} "
            f"predicted_next_cost={predicted_cost:.6f} "
            f"ceiling={ceiling_cost:.6f}"
        )
    return None


def _resolve_tool_names(allowed_tools: List[str]) -> List[str]:
    """Map friendly tool aliases to canonical AgentScope names (deduped)."""
    seen: set[str] = set()
    resolved: List[str] = []
    for name in allowed_tools:
        canonical = _TOOL_ALIAS_MAP.get(name, name)
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    return resolved


def _build_permission_context(canonical_tools: List[str]) -> PermissionContext:
    """Build a DONT_ASK PermissionContext with explicit ALLOW rules for whitelisted tools.

    DONT_ASK mode ensures:
    - Tools with an allow_rule → ALLOW (execute automatically)
    - Tools without any allow_rule → DENY (not ASK — never blocks the loop)
    """
    allow_rules: Dict[str, List[PermissionRule]] = {}
    for tool_name in canonical_tools:
        allow_rules[tool_name] = [
            PermissionRule(
                tool_name=tool_name,
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="task_contract",
            )
        ]
    return PermissionContext(mode=PermissionMode.DONT_ASK, allow_rules=allow_rules)


def _state_after_recovery(
    restored_state: Optional[AgentState],
    canonical_tools: List[str],
) -> AgentState:
    """Return a recovered state with the current contract permissions.

    Recovery before the first checkpoint has no serialized state.  Even when a
    checkpoint exists, the contract remains the authority for the allowlist.
    """

    state = restored_state or AgentState()
    state.permission_context = _build_permission_context(canonical_tools)
    return state


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from AgentScope block-based content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _validation_failure_signature(result_text: str) -> str:
    """Normalize a validation failure for repeated-error circuit breaking."""

    text = str(result_text or "")
    if "VALIDATION_FAILED" not in text:
        return ""
    text = text.split("VALIDATION_FAILED", 1)[1].lower()
    text = re.sub(r"[a-z]:[/\\][^\s|;]+", "<path>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def _estimate_next_input_tokens(
    event: Any,
    agent_state: Optional[AgentState],
    user_message: Any,
) -> int:
    """Estimate the complete next model input before admitting the call.

    AgentScope versions differ in whether ModelCallStartEvent exposes usage.
    When it does not, the serialized state plus the current task message is a
    conservative deterministic proxy; it is deliberately not zero.
    """

    for name in ("input_tokens", "prompt_tokens", "estimated_input_tokens"):
        value = getattr(event, name, None)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                pass
    try:
        state_payload = agent_state.model_dump(mode="json") if agent_state else {}
        message_payload = getattr(user_message, "content", user_message)
        raw = json.dumps(
            {"state": state_payload, "message": message_payload},
            ensure_ascii=False,
            default=str,
        )
        # Tool schemas and system instructions are not represented in state;
        # reserve a bounded fixed allowance for them.
        return max(1200, len(raw) // 4 + 2500)
    except Exception:
        return 4000


class ResearchWorker:
    """Wraps an AgentScope Agent with the full Phase 1.1 research runtime.

    Usage:
        worker = ResearchWorker()
        result = worker.run(contract)
    """

    def __init__(
        self,
        runs_root: Optional[Path] = None,
        skills_dir: Optional[Path] = None,
        _model_override: Any = None,  # for testing only
        tool_provider: Optional[ToolProvider] = None,  # Phase 2+ extension
        _system_prompt_override: Optional[str] = None,  # for testing / role specialisation
        _work_dir_override: Optional[Path] = None,  # canonical task dir (bypasses ensure_work_dir)
    ) -> None:
        self._runs_root = runs_root
        self._skills_dir = skills_dir
        self._model_override = _model_override
        self._tool_provider = tool_provider
        self._system_prompt_override = _system_prompt_override
        self._work_dir_override = _work_dir_override

    def run(self, contract: TaskContract) -> ResultManifest:
        """Synchronous entry point — runs the async kernel via asyncio.run()."""
        try:
            return asyncio.run(self._run_async(contract))
        except RuntimeError as exc:
            if "cannot be called from a running event loop" in str(exc):
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(asyncio.run, self._run_async(contract))
                    return fut.result(timeout=contract.wall_time_budget_seconds + 30)
            raise

    async def _run_async(self, contract: TaskContract) -> ResultManifest:
        """Full async task execution with AgentScope Agent.reply_stream()."""
        run_id = contract.run_id
        task_id = contract.task_id
        # Use canonical override if provided; otherwise compute from runs_root
        if self._work_dir_override is not None:
            work_dir = self._work_dir_override
            work_dir.mkdir(parents=True, exist_ok=True)
        else:
            work_dir = ensure_work_dir(run_id, task_id, self._runs_root)

        event_logger = EventLogger(work_dir)
        ledger = CostLedger(work_dir, run_id, task_id)
        t_start = time.monotonic()
        stop_ctrl = StopController(contract, work_dir)

        event_logger.log_task_start(run_id, task_id, contract.goal)

        # Check for already-completed task (idempotent resume)
        result_path = work_dir / "RESULT.json"
        if result_path.exists():
            try:
                prev = json.loads(result_path.read_text(encoding="utf-8"))
                reusable, reuse_reason = stop_ctrl.preexisting_result_is_reusable(
                    prev
                )
                if reusable:
                    logger.info("Task %s/%s already completed. Returning cached result.", run_id, task_id)
                    return ResultManifest.model_validate(prev)
                logger.info(
                    "Ignoring pre-existing RESULT.json for %s/%s: %s",
                    run_id,
                    task_id,
                    reuse_reason,
                )
            except Exception:
                pass  # corrupt file — re-run

        # Persist task contract (immutable; only write if not already present)
        task_json_path = work_dir / "TASK.json"
        if not task_json_path.exists():
            atomic_write_json(task_json_path, contract.model_dump())

        # Build model
        factory: Optional[AgentScopeModelFactory] = None
        if self._model_override is None:
            factory = AgentScopeModelFactory(model_tier=contract.model_tier)
            if factory.mock_mode:
                return self._mock_result(contract, work_dir, event_logger, ledger, t_start)
            model = factory.current_model
        else:
            model = self._model_override

        # Resolve tool names (alias → canonical)
        canonical_tools = _resolve_tool_names(contract.allowed_tools)

        # Build tools — only expose whitelisted research tools
        input_paths = [
            work_dir / aid for aid in contract.input_artifact_ids
            if (work_dir / aid).exists()
        ]
        all_research_tools, _ = build_research_toolkit(work_dir, input_paths)

        # Filter to only whitelist-approved research tools
        _whitelisted_research = {
            "list_task_artifacts", "read_task_artifact",
            "write_task_note", "validate_task_result",
        }
        research_tools = [
            t for t in all_research_tools
            if t.name in _whitelisted_research and t.name in canonical_tools
        ]

        # AgentScope built-in task tools (only those in whitelist)
        _task_tool_map = {
            "TaskCreate": TaskCreate,
            "TaskList": TaskList,
            "TaskGet": TaskGet,
            "TaskUpdate": TaskUpdate,
        }
        task_tools = [
            cls() for name, cls in _task_tool_map.items()
            if name in canonical_tools
        ]

        # Filtered skill loader — skill_ids=[] means no skills; ["*"] means all
        skill_loader = FilteredSkillLoader(
            get_skill_loader(self._skills_dir),
            skill_ids=contract.skill_ids,
        )

        # Phase 2+ tool provider extension (None → no extra tools, Phase 1 behavior unchanged)
        # IMPORTANT: TaskContract.allowed_tools is the authority. Provider tools that are NOT
        # explicitly listed in the contract are silently dropped — the provider cannot
        # self-authorise by returning them from get_allowed_tool_names().
        provider_tools = []
        if self._tool_provider is not None:
            contract_tool_set = set(canonical_tools)
            all_provider_tools = self._tool_provider.get_tools(work_dir)
            provider_tools = [t for t in all_provider_tools if t.name in contract_tool_set]

        # Keep the runtime role-agnostic: every explicitly allowlisted provider
        # validator may complete its own task. Prefix matching is safe because
        # the contract/provider intersection already removed unapproved tools.
        validation_tool_names = {"validate_task_result"}
        validation_tool_names.update(
            tool.name
            for tool in provider_tools
            if tool.name.startswith("validate_")
        )
            # canonical_tools is NOT expanded here — the contract is immutable

        toolkit = Toolkit(
            tools=research_tools + task_tools + provider_tools,
            skills_or_loaders=[skill_loader],
        )

        # Restore or create AgentState with permission context pre-configured
        state_path = work_dir / "AGENT_STATE.json"
        agent_state: Optional[AgentState] = None
        if state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                agent_state = AgentState.model_validate(raw)
                # Always refresh permission context to current contract
                agent_state.permission_context = _build_permission_context(canonical_tools)
                logger.info("Restored AgentState from %s", state_path)
            except Exception as exc:
                logger.warning("AgentState restore failed: %s. Starting fresh.", exc)
                agent_state = None

        if agent_state is None:
            agent_state = AgentState(
                permission_context=_build_permission_context(canonical_tools)
            )

        recovery = RecoveryPolicy(
            factory, event_logger,
            max_recovery_attempts=factory.candidate_count,
        ) if factory else None
        # Write initial plan placeholder only if not already present
        plan_path = work_dir / "PLAN.md"
        if not plan_path.exists():
            atomic_write_text(
                plan_path,
                f"# Task Plan\n\nGoal: {contract.goal}\n\nStatus: pending\n",
            )

        # Build initial user message
        constraints_text = (
            "\n".join(f"- {c}" for c in contract.constraints)
            if contract.constraints else "None"
        )
        criteria_text = (
            "\n".join(f"- {c}" for c in contract.success_criteria)
            if contract.success_criteria else "Not specified"
        )
        expected_text = (
            ", ".join(contract.expected_outputs)
            if contract.expected_outputs else "Not specified"
        )

        goal_message = (
            f"Task ID: {task_id}\n\n"
            f"Goal: {contract.goal}\n\n"
            f"Constraints:\n{constraints_text}\n\n"
            f"Success criteria:\n{criteria_text}\n\n"
            f"Expected output files: {expected_text}\n\n"
            f"Available tools: {', '.join(canonical_tools)}\n\n"
            "Please proceed: inspect the task; create a task list only when the "
            "work has multiple dependent steps; read required artifacts, use tools "
            "instead of guessing, record findings, and call an approved validation "
            "tool before declaring completion. Approved validators: "
            f"{', '.join(sorted(validation_tool_names))}."
        )

        user_msg = UserMsg(name="user", content=goal_message)

        # Tracking state across reply_stream events
        tool_call_id_to_name: Dict[str, str] = {}       # call_id → tool_name
        tool_result_texts: Dict[str, str] = {}           # call_id → accumulated text
        last_validation_result: Optional[str] = None
        validation_failure_counts: Dict[str, int] = {}
        final_status = TaskStatus.running
        final_stop_reason = "in_progress"
        model_call_t0: Optional[float] = None
        model_call_input_tokens_before = 0
        last_model_input_tokens_observed = 0
        model_call_cost_cny_before = 0.0
        tool_call_t0s: Dict[str, float] = {}
        pending_tool_call_ids: set[str] = set()
        iter_count = 0
        react_iter_at_stop: Optional[int] = None
        last_agent: Optional[Agent] = None
        input_tokens_total = ledger.total_input_tokens
        output_tokens_total = ledger.total_output_tokens
        final_call_token_overshoot_reason: Optional[str] = None
        has_unhandled_confirm = False
        errors: List[str] = []

        outer_iter = 0
        criteria_met: List[str] = []
        criteria_failed: List[str] = []
        max_outer = (factory.candidate_count + _MAX_OUTER_ITERATIONS_BASE) if factory else _MAX_OUTER_ITERATIONS_BASE
        try:
            # Bounded outer loop: covers all candidates + 1 normal run
            while outer_iter < max_outer:
                outer_iter += 1
                logger.debug("Outer loop iteration %d/%d", outer_iter, max_outer)

                # Create agent with current model and state
                metadata = contract.metadata if isinstance(contract.metadata, dict) else {}
                context_trigger_ratio = float(
                    metadata.get("context_trigger_ratio", 0.70) or 0.70
                )
                context_reserve_ratio = float(
                    metadata.get("context_reserve_ratio", 0.15) or 0.15
                )
                context_tool_result_limit = int(
                    metadata.get("context_tool_result_limit", 1800) or 1800
                )
                # Keep the reserve below the trigger even when a caller gives
                # malformed metadata.  AgentScope performs the actual summary
                # compression; this contract only makes it happen before the
                # transcript becomes a multi-million-token replay.
                context_trigger_ratio = min(0.85, max(0.35, context_trigger_ratio))
                context_reserve_ratio = min(
                    context_trigger_ratio - 0.05,
                    max(0.05, context_reserve_ratio),
                )
                agent = Agent(
                    name="ResearchWorker",
                    system_prompt=self._system_prompt_override or _load_system_prompt(),
                    model=model,
                    toolkit=toolkit,
                    state=agent_state,
                    react_config=ReActConfig(
                        max_iters=contract.max_iters, stop_on_reject=False
                    ),
                    context_config=ContextConfig(
                        trigger_ratio=context_trigger_ratio,
                        reserve_ratio=context_reserve_ratio,
                        tool_result_limit=max(400, context_tool_result_limit),
                    ),
                    model_config=ModelConfig(max_retries=1),
                )
                last_agent = agent

                try:
                  stream_broke_for_recovery = False
                  async for event in agent.reply_stream(user_msg):
                    elapsed = time.monotonic() - t_start

                    # Skip thinking blocks entirely
                    if isinstance(
                        event,
                        (ThinkingBlockStartEvent, ThinkingBlockDeltaEvent, ThinkingBlockEndEvent),
                    ):
                        continue

                    if isinstance(event, ModelCallStartEvent):
                        # Admission control happens before allowing another model
                        # call. A previously admitted call may have crossed the
                        # token budget, but its emitted tools were allowed to finish.
                        if final_status == TaskStatus.completed:
                            break
                        model_name = getattr(event, "model_name", None) or (
                            factory.current_model_name if factory else "unknown"
                        )
                        admission_status, admission_reason = stop_ctrl.check(
                            iter_count=iter_count,
                            wall_time_seconds=elapsed,
                            input_tokens=input_tokens_total,
                            estimated_cost_cny=ledger.estimated_cost_cny(),
                        )
                        if admission_status == TaskStatus.budget_exhausted:
                            final_status = TaskStatus.budget_exhausted
                            if final_call_token_overshoot_reason:
                                final_stop_reason = (
                                    f"{final_call_token_overshoot_reason}; pending tools finished "
                                    "without successful validation; no further model call admitted"
                                )
                            else:
                                final_stop_reason = f"model_call_not_admitted: {admission_reason}"
                            break
                        event_input_observed = any(
                            getattr(event, name, None) is not None
                            for name in ("input_tokens", "prompt_tokens", "estimated_input_tokens")
                        )
                        predicted_next_input = (
                            max(1, last_model_input_tokens_observed)
                            if last_model_input_tokens_observed and not event_input_observed
                            else _estimate_next_input_tokens(event, agent_state, user_msg)
                        )
                        # Admission is cumulative: reserve the complete next
                        # prompt and a bounded output reserve before allowing
                        # the provider call.  This prevents a large final
                        # prompt from taking cumulative usage past the token
                        # budget after it has already been sent.
                        output_reserve = min(
                            4000,
                            max(1000, int(contract.token_budget * 0.05)),
                        )
                        r5_admission_reason = _r5_lifetime_admission_reason(
                            metadata=contract.metadata,
                            ledger_input_tokens=input_tokens_total,
                            ledger_cost_cny=ledger.estimated_cost_cny(),
                            predicted_next_input=predicted_next_input,
                            output_reserve=output_reserve,
                            model_name=model_name,
                        )
                        if r5_admission_reason:
                            final_status = TaskStatus.budget_exhausted
                            final_stop_reason = (
                                "model_call_not_admitted: " + r5_admission_reason
                            )
                            event_logger.log_observation({
                                "event": "r5_model_call_not_admitted",
                                "reason": r5_admission_reason,
                                "model": model_name,
                            })
                            break
                        stage_name = str(
                            contract.metadata.get("r5_discovery_stage") or ""
                        )
                        stage_reserve = int(
                            contract.metadata.get("stage_iteration_reserve", 0)
                            or 0
                        )
                        if (
                            stage_name in {"opportunity", "hypothesis", "focus"}
                            and stage_reserve > 0
                            and iter_count
                            >= max(0, contract.max_iters - stage_reserve)
                        ):
                            # Leave one deterministic termination slot.  If
                            # the stage artifact was not accepted by the
                            # preceding tool result, stop before AgentScope
                            # can emit a final unexecuted tool call.
                            final_status = TaskStatus.waiting_for_human
                            final_stop_reason = (
                                "r5_stage_terminal_reserve: "
                                f"stage={stage_name} iter_count={iter_count} "
                                f"max_iters={contract.max_iters}"
                            )
                            event_logger.log_observation({
                                "event": "r5_stage_terminal_reserve",
                                "stage": stage_name,
                                "iter_count": iter_count,
                                "max_iters": contract.max_iters,
                            })
                            break
                        if (
                            input_tokens_total
                            + predicted_next_input
                            + output_reserve
                            > contract.token_budget
                        ):
                            final_status = TaskStatus.budget_exhausted
                            final_stop_reason = (
                                "model_call_not_admitted: token_admission "
                                f"used={input_tokens_total} predicted_next_input="
                                f"{predicted_next_input} output_reserve={output_reserve} "
                                f"budget={contract.token_budget}"
                            )
                            event_logger.log_observation({
                                "event": "model_call_not_admitted",
                                "used_tokens": input_tokens_total,
                                "predicted_next_input": predicted_next_input,
                                "output_reserve": output_reserve,
                                "token_budget": contract.token_budget,
                            })
                            break
                        model_call_input_tokens_before = input_tokens_total
                        model_call_cost_cny_before = ledger.estimated_cost_cny()
                        model_call_t0 = time.monotonic()
                        event_logger.log_model_call_start(
                            model_name,
                            key_fingerprint=(
                                factory.current_key_fingerprint
                                if factory else ""
                            ),
                            key_source=(
                                factory.current_key_source
                                if factory else ""
                            ),
                        )

                    elif isinstance(event, ModelCallEndEvent):
                        duration_ms = (
                            time.monotonic() - (model_call_t0 or time.monotonic())
                        ) * 1000
                        model_name = getattr(event, "model_name", None) or (
                            factory.current_model_name if factory else "unknown"
                        )
                        # Read real token counts from event
                        ev_in = getattr(event, "input_tokens", None)
                        ev_out = getattr(event, "output_tokens", None)
                        in_tok = int(ev_in) if ev_in is not None else 500
                        out_tok = int(ev_out) if ev_out is not None else 200
                        last_model_input_tokens_observed = in_tok
                        ledger.record_call(model_name, in_tok, out_tok)
                        if factory is not None:
                            factory.mark_current_candidate_successful()
                        input_tokens_total += in_tok
                        output_tokens_total += out_tok
                        event_logger.log_model_call_end(model_name, in_tok, out_tok, duration_ms)
                        # Persist after every paid call.  A killed process must
                        # not erase spend or admit the same full budget again.
                        ledger.save("running", "checkpoint_after_model_call")
                        iter_count += 1

                        # A call admitted below the token budget is allowed to
                        # finish. If that final call crosses the token budget, keep
                        # consuming its already-emitted tool events, then prohibit
                        # another model call.
                        elapsed = time.monotonic() - t_start
                        budget_status, budget_reason = stop_ctrl.check(
                            iter_count=iter_count,
                            wall_time_seconds=elapsed,
                            input_tokens=input_tokens_total,
                            estimated_cost_cny=ledger.estimated_cost_cny(),
                            # The current call has already been paid for.  Its
                            # emitted tool call must be allowed to persist and
                            # validate artifacts.  The reserve is checked only
                            # before admitting the next model call.
                            include_next_call_reserve=False,
                        )
                        if budget_status == TaskStatus.budget_exhausted:
                            token_overshoot = (
                                model_call_input_tokens_before < contract.token_budget
                                and input_tokens_total >= contract.token_budget
                            )
                            wall_time_overrun = "wall_time" in budget_reason
                            cost_overshoot = (
                                contract.cost_budget_cny is not None
                                and model_call_cost_cny_before
                                < contract.cost_budget_cny
                                and ledger.estimated_cost_cny()
                                >= contract.cost_budget_cny
                            )
                            if token_overshoot or cost_overshoot or wall_time_overrun:
                                # The model call was already paid for; let its tool calls
                                # execute and persist artifacts before stopping.
                                overshoot_detail = (
                                    f"input_tokens={input_tokens_total}, budget={contract.token_budget}, "
                                    f"overshoot={input_tokens_total - contract.token_budget}"
                                    if token_overshoot
                                    else (
                                        f"estimated_cost_cny={ledger.estimated_cost_cny():.4f}, "
                                        f"budget={contract.cost_budget_cny:.4f}"
                                        if cost_overshoot
                                        else budget_reason
                                    )
                                )
                                overshoot_kind = (
                                    "token_overshoot"
                                    if token_overshoot
                                    else (
                                        "cost_overshoot"
                                        if cost_overshoot
                                        else "wall_time_overrun"
                                    )
                                )
                                final_call_token_overshoot_reason = (
                                    f"final_admitted_model_call_{overshoot_kind}: {overshoot_detail}"
                                )
                            else:
                                final_status = budget_status
                                final_stop_reason = budget_reason
                                break

                    elif isinstance(event, ToolCallStartEvent):
                        # Use tool_call_name (not name) — confirmed from AgentScope 2.0.2 source
                        tool_name = getattr(event, "tool_call_name", "unknown")
                        call_id = getattr(event, "tool_call_id", "?")
                        tool_call_id_to_name[call_id] = tool_name
                        tool_result_texts[call_id] = ""
                        tool_call_t0s[call_id] = time.monotonic()
                        pending_tool_call_ids.add(call_id)
                        ledger.record_tool_call()
                        event_logger.log_tool_call(tool_name, call_id)

                    elif isinstance(event, ToolResultStartEvent):
                        # ToolResultStartEvent also carries tool_call_name
                        call_id = getattr(event, "tool_call_id", "?")
                        t_name = getattr(event, "tool_call_name", None)
                        if t_name and call_id not in tool_call_id_to_name:
                            tool_call_id_to_name[call_id] = t_name

                    elif isinstance(event, ToolResultTextDeltaEvent):
                        call_id = getattr(event, "tool_call_id", "?")
                        delta = getattr(event, "delta", "")
                        if call_id in tool_result_texts:
                            tool_result_texts[call_id] += delta

                    elif isinstance(event, ToolResultEndEvent):
                        call_id = getattr(event, "tool_call_id", "?")
                        state_val = str(getattr(event, "state", "unknown"))
                        duration_ms = (
                            time.monotonic() - tool_call_t0s.pop(call_id, time.monotonic())
                        ) * 1000
                        tool_name = tool_call_id_to_name.get(call_id, "unknown")
                        pending_tool_call_ids.discard(call_id)

                        # Capture full tool result text
                        result_text = tool_result_texts.get(call_id, "")
                        summary = result_text[:300] if result_text else f"state={state_val}"
                        event_logger.log_tool_result(tool_name, call_id, state_val, summary, duration_ms)

                        # A long ReAct pass may execute several expensive tools
                        # before AgentScope emits ReplyEnd.  Persist after every
                        # completed tool so a network/key/budget interruption
                        # resumes from the latest durable step instead of
                        # repeating acquisition, parsing, or drafting work.
                        try:
                            agent_state = agent.state
                            atomic_write_json(
                                state_path,
                                agent_state.model_dump(mode="json"),
                            )
                            _sync_plan_md(work_dir, agent_state)
                        except Exception as exc:
                            logger.warning(
                                "Post-tool AgentState checkpoint failed: %s",
                                exc,
                            )

                        # A trusted provider may deterministically recognize
                        # that its durable artifacts already satisfy every
                        # gate.  This avoids spending another long model turn
                        # solely because the model forgot its final validator.
                        if (
                            last_validation_result != "VALIDATION_PASSED"
                            and self._tool_provider is not None
                        ):
                            try:
                                auto_result = (
                                    self._tool_provider.try_auto_finalize()
                                )
                            except Exception as exc:
                                auto_result = None
                                logger.warning(
                                    "Provider auto-finalization failed: %s",
                                    exc,
                                )
                            if (
                                isinstance(auto_result, str)
                                and "VALIDATION_AWAITING_HUMAN_REVIEW" in auto_result
                            ):
                                final_status = TaskStatus.waiting_for_human
                                final_stop_reason = auto_result[:500]
                                event_logger.log_observation({
                                    "event": "authoring_convergence_stop",
                                    "status": "awaiting_human_review",
                                    "reason": final_stop_reason,
                                })
                                break
                            if (
                                isinstance(auto_result, str)
                                and "VALIDATION_PASSED" in auto_result
                            ):
                                last_validation_result = "VALIDATION_PASSED"
                                event_logger.log_tool_result(
                                    "provider_auto_finalize",
                                    f"auto_{call_id}",
                                    "success",
                                    auto_result[:300],
                                    0.0,
                                )

                        # P1-G: Only accept VALIDATION_PASSED from allowlisted validation tools.
                        # An unrelated tool returning VALIDATION_PASSED text must not complete the task.
                        if tool_name in validation_tool_names:
                            if "VALIDATION_AWAITING_HUMAN_REVIEW" in result_text:
                                last_validation_result = "VALIDATION_AWAITING_HUMAN_REVIEW"
                                final_status = TaskStatus.waiting_for_human
                                final_stop_reason = result_text[:500]
                                event_logger.log_observation({
                                    "event": "authoring_convergence_stop",
                                    "status": "awaiting_human_review",
                                    "reason": final_stop_reason,
                                })
                                break
                            elif "VALIDATION_PASSED" in result_text:
                                last_validation_result = "VALIDATION_PASSED"
                            elif "VALIDATION_FAILED" in result_text:
                                last_validation_result = "VALIDATION_FAILED"
                                signature = _validation_failure_signature(
                                    result_text
                                )
                                if signature:
                                    validation_failure_counts[signature] = (
                                        validation_failure_counts.get(
                                            signature,
                                            0,
                                        )
                                        + 1
                                    )
                                    if (
                                        validation_failure_counts[signature]
                                        >= 3
                                    ):
                                        final_status = (
                                            TaskStatus.validation_failed
                                        )
                                        final_stop_reason = (
                                            "repeated_identical_validation_failure"
                                            "_circuit_breaker: "
                                            + signature[:240]
                                        )
                                        errors.append(final_stop_reason)
                                        event_logger.log_error(
                                            "RepeatedValidationFailure",
                                            final_stop_reason,
                                        )
                                        break

                        # A ReAct stream may continue directly into another model
                        # call after a tool result without emitting ReplyEnd.  Stop
                        # immediately after a successful allowlisted validator so
                        # completed Phase-2/3 workers cannot loop until max_iters.
                        if last_validation_result == "VALIDATION_PASSED":
                            candidate_status, candidate_reason, met, failed = (
                                stop_ctrl.check_completion(
                                    validation_tool_result=last_validation_result,
                                    iter_count=iter_count,
                                    wall_time_seconds=time.monotonic() - t_start,
                                    input_tokens=input_tokens_total,
                                    estimated_cost_cny=ledger.estimated_cost_cny(),
                                    allow_final_token_overshoot=bool(final_call_token_overshoot_reason),
                                )
                            )
                            if candidate_status == TaskStatus.completed:
                                final_status = candidate_status
                                final_stop_reason = (
                                    f"{candidate_reason}; {final_call_token_overshoot_reason}; "
                                    "validation completed; no further model call admitted"
                                    if final_call_token_overshoot_reason else candidate_reason
                                )
                                criteria_met, criteria_failed = met, failed
                                try:
                                    agent_state = agent.state
                                    atomic_write_json(
                                        state_path,
                                        agent_state.model_dump(mode="json"),
                                    )
                                    _sync_plan_md(work_dir, agent_state)
                                except Exception as exc:
                                    logger.warning(
                                        "Successful validation AgentState save failed: %s",
                                        exc,
                                    )
                                break

                        # The final admitted call may complete through its
                        # validation tool even though its exact usage crossed the
                        # token budget. Expected-output and validation gates still
                        # apply; only the final token check is waived.
                        if (
                            final_call_token_overshoot_reason
                            and last_validation_result == "VALIDATION_PASSED"
                        ):
                            candidate_status, candidate_reason, met, failed = (
                                stop_ctrl.check_completion(
                                    validation_tool_result=last_validation_result,
                                    iter_count=iter_count,
                                    wall_time_seconds=time.monotonic() - t_start,
                                    input_tokens=input_tokens_total,
                                    estimated_cost_cny=ledger.estimated_cost_cny(),
                                    allow_final_token_overshoot=True,
                                )
                            )
                            if candidate_status == TaskStatus.completed:
                                final_status = candidate_status
                                final_stop_reason = (
                                    f"{candidate_reason}; {final_call_token_overshoot_reason}; "
                                    "validation completed; no further model call admitted"
                                )
                                criteria_met, criteria_failed = met, failed
                                # No ReplyEnd is required after the final validation
                                # tool, so checkpoint the completed state here.
                                try:
                                    agent_state = agent.state
                                    atomic_write_json(
                                        state_path,
                                        agent_state.model_dump(mode="json"),
                                    )
                                    _sync_plan_md(work_dir, agent_state)
                                except Exception as exc:
                                    logger.warning(
                                        "Final validation AgentState save failed: %s",
                                        exc,
                                    )

                    elif isinstance(event, RequireUserConfirmEvent):
                        # This should not happen with DONT_ASK mode + allow_rules,
                        # but handle defensively per HumanInterventionPolicy.
                        tool_calls_asking = getattr(event, "tool_calls", [])
                        asking_names = [getattr(tc, "name", "?") for tc in tool_calls_asking]
                        event_logger.log_permission_request(asking_names)

                        policy = contract.human_intervention_policy
                        if policy == HumanInterventionPolicy.never:
                            # Deny and stop — unexpected permission request
                            final_status = TaskStatus.waiting_for_human
                            final_stop_reason = (
                                f"Unexpected permission request for tools: {asking_names}. "
                                f"Policy=never. These tools must be in allowed_tools and pre-authorized."
                            )
                            errors.append(final_stop_reason)
                            has_unhandled_confirm = True
                            break
                        elif policy == HumanInterventionPolicy.always_confirm:
                            final_status = TaskStatus.waiting_for_human
                            final_stop_reason = (
                                f"Waiting for human confirmation for tools: {asking_names}"
                            )
                            break
                        # on_tool_error / on_validation_failure: same as never here
                        else:
                            final_status = TaskStatus.waiting_for_human
                            final_stop_reason = (
                                f"Permission request for {asking_names} under policy={policy.value}"
                            )
                            break

                    elif isinstance(event, ExceedMaxItersEvent):
                        # P1-4: the framework gauge (state.cur_iter, one per
                        # reasoning-acting turn, persisted across resumes) is
                        # what actually triggers this event, while the local
                        # iter_count below counts completed model calls in
                        # this process only.  Record both so the record can
                        # never read as self-contradictory again.
                        try:
                            react_iter_at_stop = int(
                                getattr(agent.state, "cur_iter", 0) or 0
                            )
                        except Exception:
                            react_iter_at_stop = None
                        final_status = TaskStatus.budget_exhausted
                        final_stop_reason = (
                            f"max_iters={contract.max_iters} exceeded "
                            f"(react_iter={react_iter_at_stop}; "
                            "iter_count separately counts completed "
                            "model calls)"
                        )
                        event_logger.log_error("ExceedMaxIters", final_stop_reason)
                        break

                    elif isinstance(event, ReplyEndEvent):
                        # Agent finished this pass — save state before deciding
                        try:
                            agent_state = agent.state
                            state_dump = agent_state.model_dump(mode="json")
                            atomic_write_json(state_path, state_dump)
                            # Sync PLAN.md from tasks_context
                            _sync_plan_md(work_dir, agent_state)
                        except Exception as exc:
                            logger.warning("AgentState save failed: %s", exc)

                        if final_status == TaskStatus.completed:
                            break
                        if final_call_token_overshoot_reason:
                            final_status = TaskStatus.budget_exhausted
                            final_stop_reason = (
                                f"{final_call_token_overshoot_reason}; pending tools finished "
                                "without successful validation; no further model call admitted"
                            )
                        else:
                            budget_status, budget_reason = stop_ctrl.check(
                                iter_count=iter_count,
                                wall_time_seconds=elapsed,
                                input_tokens=input_tokens_total,
                                estimated_cost_cny=ledger.estimated_cost_cny(),
                            )
                            if budget_status == TaskStatus.budget_exhausted:
                                final_status = budget_status
                                final_stop_reason = budget_reason
                        break

                  # After stream: run completion gate
                  if final_status == TaskStatus.running:
                      if final_call_token_overshoot_reason:
                          final_status = TaskStatus.budget_exhausted
                          final_stop_reason = (
                              f"{final_call_token_overshoot_reason}; "
                              f"pending_tools={len(pending_tool_call_ids)}; "
                              "pending tools finished without successful validation; "
                              "no further model call admitted"
                          )
                          criteria_met = []
                          criteria_failed = []
                          break
                      final_status, final_stop_reason, criteria_met, criteria_failed = (
                          stop_ctrl.check_completion(
                              validation_tool_result=last_validation_result,
                              iter_count=iter_count,
                              wall_time_seconds=time.monotonic() - t_start,
                              input_tokens=input_tokens_total,
                              estimated_cost_cny=ledger.estimated_cost_cny(),
                          )
                      )
                      if final_status == TaskStatus.completed:
                          break
                      if outer_iter >= max_outer:
                          break
                      break
                  else:
                      if final_status == TaskStatus.completed:
                          break
                      criteria_met = []
                      criteria_failed = []
                      break

                except Exception as exc:
                    logger.exception("ResearchWorker._run_async failed: %s", exc)
                    errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
                    event_logger.log_error(type(exc).__name__, str(exc)[:500])
                    criteria_met = []
                    criteria_failed = []

                    if recovery is not None:
                        error_category = classify_error(exc)
                        saved = None
                        if state_path.exists():
                            try:
                                saved = json.loads(state_path.read_text(encoding="utf-8"))
                            except Exception:
                                pass
                        recovered, new_state, new_model = recovery.handle_failure(
                            exc, error_category, saved, current_model=model
                        )
                        if recovered and new_model is not None:
                            model = new_model
                            # A model/key error can happen before the first
                            # checkpoint exists.  In that case RecoveryPolicy
                            # correctly returns ``None`` for the restored
                            # conversation state, but passing that value into
                            # Agent would silently create its DEFAULT
                            # permission context.  Custom provider tools would
                            # then ask for confirmation even though the task
                            # contract explicitly pre-authorised them.
                            #
                            # Recreate the empty state with the same
                            # fail-closed allowlist used at initial startup.
                            agent_state = _state_after_recovery(
                                new_state,
                                canonical_tools,
                            )
                            continue  # retry outer loop with recovered model/key

                    final_status = TaskStatus.failed
                    final_stop_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
                    break

        finally:
            # Guard: running is never a valid terminal state
            if final_status == TaskStatus.running:
                final_status = TaskStatus.failed
                final_stop_reason = final_stop_reason or "exhausted all recovery iterations without completion"
            # The local admission gate can stop immediately before
            # AgentScope emits its deprecated ExceedMaxItersEvent (for
            # example, after the final admitted tool result).  Preserve the
            # framework iteration gauge in that path as well; otherwise a
            # truthful ``budget_exhausted`` result carries a null
            # ``react_iter_count`` and cannot be audited or resumed cleanly.
            if (
                final_status == TaskStatus.budget_exhausted
                and react_iter_at_stop is None
                and re.search(r"\bmax_iters=", str(final_stop_reason or ""))
            ):
                observed_react_iter = 0
                if last_agent is not None:
                    try:
                        observed_react_iter = int(
                            getattr(last_agent.state, "cur_iter", 0) or 0
                        )
                    except Exception:
                        observed_react_iter = 0
                react_iter_at_stop = min(
                    contract.max_iters,
                    max(observed_react_iter, iter_count),
                )
                if react_iter_at_stop <= 0:
                    react_iter_at_stop = contract.max_iters
                if "react_iter=" not in str(final_stop_reason or ""):
                    final_stop_reason = (
                        f"{final_stop_reason} "
                        f"(react_iter={react_iter_at_stop}; "
                        "iter_count separately counts completed model calls)"
                    )
            event_logger.log_task_end(final_status.value, final_stop_reason)
            ledger.save(final_status.value, final_stop_reason)

        # Build result
        wall_time = round(time.monotonic() - t_start, 2)
        result = ResultManifest(
            run_id=run_id,
            task_id=task_id,
            status=final_status,
            stop_reason=final_stop_reason,
            success_criteria_met=criteria_met,
            success_criteria_failed=criteria_failed,
            validation_passed=(final_status == TaskStatus.completed),
            tool_call_count=ledger.tool_call_count,
            iter_count=iter_count,
            react_iter_count=react_iter_at_stop,
            wall_time_seconds=wall_time,
            total_input_tokens=ledger.total_input_tokens,
            total_output_tokens=ledger.total_output_tokens,
            estimated_cost_cny=ledger.estimated_cost_cny(),
            estimated_cost_usd=ledger.estimated_cost_usd(),
            output_paths={
                "work_dir": str(work_dir),
                "events": str(work_dir / "EVENTS.jsonl"),
                "agent_state": str(state_path),
                "cost": str(work_dir / "COST.json"),
                "task": str(work_dir / "TASK.json"),
            },
            errors=errors,
        )
        atomic_write_json(work_dir / "RESULT.json", result.model_dump())
        atomic_write_text(
            work_dir / "RESULT.md",
            f"# Result\n\n"
            f"Status: **{final_status.value}**  \n"
            f"Stop reason: {final_stop_reason}  \n"
            f"Tool calls: {ledger.tool_call_count}  \n"
            f"Iterations: {iter_count}  \n"
            f"Wall time: {wall_time}s  \n"
            f"Estimated cost: CNY {ledger.estimated_cost_cny():.6f} "
            f"(approximately ${ledger.estimated_cost_usd():.6f} USD)\n",
        )

        return result

    def _mock_result(
        self,
        contract: TaskContract,
        work_dir: Path,
        event_logger: EventLogger,
        ledger: CostLedger,
        t_start: float,
    ) -> ResultManifest:
        """Return a mock result when no API key is available."""
        event_logger.log_task_end("failed", "mock_mode_no_api_key")
        ledger.save("failed", "mock_mode_no_api_key")
        result = ResultManifest(
            run_id=contract.run_id,
            task_id=contract.task_id,
            status=TaskStatus.failed,
            stop_reason="mock_mode_no_api_key",
            wall_time_seconds=round(time.monotonic() - t_start, 2),
        )
        atomic_write_json(work_dir / "RESULT.json", result.model_dump())
        return result


def _sync_plan_md(work_dir: Path, state: AgentState) -> None:
    """Sync PLAN.md from AgentState.tasks_context if tasks exist."""
    try:
        tasks_ctx = state.tasks_context
        if tasks_ctx is None:
            return
        tasks = getattr(tasks_ctx, "tasks", [])
        if not tasks:
            return
        lines = ["# Task Plan\n"]
        for task in tasks:
            t_id = getattr(task, "id", "?")
            subject = getattr(task, "subject", "?")
            t_state = getattr(task, "state", "?")
            lines.append(f"- [{t_state}] {t_id}: {subject}")
        atomic_write_text(work_dir / "PLAN.md", "\n".join(lines) + "\n")
    except Exception as exc:
        logger.debug("PLAN.md sync failed: %s", exc)
