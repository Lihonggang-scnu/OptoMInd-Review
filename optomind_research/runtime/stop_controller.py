"""Stop controller — deterministic gate that decides if a task is truly complete."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .task_contract import TaskContract, TaskStatus

logger = logging.getLogger(__name__)


class StopController:
    """Checks completion conditions before accepting an agent's self-declared finish.

    The agent saying 'I am done' is NOT sufficient. All gates must pass.
    """

    def __init__(self, contract: TaskContract, work_dir: Path) -> None:
        self._contract = contract
        self._work_dir = work_dir

    @staticmethod
    def _phase_identity(payload: Dict[str, Any]) -> str:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return str(
            metadata.get("phase_identity")
            or metadata.get("phase")
            or ""
        ).strip()

    def preexisting_result_is_reusable(
        self,
        result_payload: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Accept a cached result only for the same phase and complete outputs.

        A shared R5 work directory can contain a completed discovery result
        while the plan-only phase is still pending.  ``run_id`` and
        ``task_id`` alone therefore do not identify a reusable result.  The
        persisted task contract supplies the phase identity, and the current
        contract's expected outputs are checked against the filesystem.
        """

        if not isinstance(result_payload, dict):
            return False, "result_payload_not_object"
        if result_payload.get("status") != TaskStatus.completed.value:
            return False, "result_not_completed"
        if result_payload.get("run_id") != self._contract.run_id:
            return False, "result_run_id_mismatch"
        if result_payload.get("task_id") != self._contract.task_id:
            return False, "result_task_id_mismatch"

        task_path = self._work_dir / "TASK.json"
        if not task_path.exists():
            return False, "task_contract_missing"
        try:
            persisted_contract = json.loads(
                task_path.read_text(encoding="utf-8")
            )
        except Exception:
            return False, "task_contract_invalid"
        if not isinstance(persisted_contract, dict):
            return False, "task_contract_not_object"
        if persisted_contract.get("run_id") != self._contract.run_id:
            return False, "task_contract_run_id_mismatch"
        if persisted_contract.get("task_id") != self._contract.task_id:
            return False, "task_contract_task_id_mismatch"

        current_identity = self._phase_identity(
            self._contract.model_dump(mode="json")
        )
        persisted_identity = self._phase_identity(persisted_contract)
        if current_identity and persisted_identity != current_identity:
            return False, "task_contract_phase_identity_mismatch"

        missing = [
            name
            for name in self._contract.expected_outputs
            if not (self._work_dir / name).exists()
        ]
        if missing:
            return False, "expected_outputs_missing:" + ",".join(missing)
        return True, "same_phase_contract_and_outputs_present"

    def check(
        self,
        validation_tool_result: Optional[str] = None,
        iter_count: int = 0,
        wall_time_seconds: float = 0.0,
        input_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        has_unhandled_error: bool = False,
        include_next_call_reserve: bool = True,
    ) -> Tuple[TaskStatus, str]:
        """Run all completion gates.

        Returns:
            (status, reason) where status is one of the terminal TaskStatus values.
        """
        # Budget checks first
        if iter_count >= self._contract.max_iters:
            return TaskStatus.budget_exhausted, f"max_iters={self._contract.max_iters} exceeded"
        if wall_time_seconds >= self._contract.wall_time_budget_seconds:
            return (
                TaskStatus.budget_exhausted,
                f"wall_time {wall_time_seconds:.1f}s >= budget {self._contract.wall_time_budget_seconds}s",
            )
        if input_tokens >= self._contract.token_budget:
            return (
                TaskStatus.budget_exhausted,
                f"input_tokens {input_tokens} >= budget {self._contract.token_budget}",
            )
        if (
            self._contract.cost_budget_cny is not None
            and estimated_cost_cny
            + (
                self._contract.next_call_cost_reserve_cny
                if include_next_call_reserve
                else 0.0
            )
            >= self._contract.cost_budget_cny
        ):
            return (
                TaskStatus.budget_exhausted,
                "estimated_cost_cny "
                f"{estimated_cost_cny:.4f} + reserve "
                f"{(self._contract.next_call_cost_reserve_cny if include_next_call_reserve else 0.0):.4f} "
                ">= budget "
                f"{self._contract.cost_budget_cny:.4f}",
            )

        # Unhandled error
        if has_unhandled_error:
            return TaskStatus.failed, "unhandled_error"

        # Not yet finished — caller should continue
        return TaskStatus.running, "still_running"

    def check_completion(
        self,
        validation_tool_result: Optional[str],
        iter_count: int = 0,
        wall_time_seconds: float = 0.0,
        input_tokens: int = 0,
        estimated_cost_cny: float = 0.0,
        allow_final_token_overshoot: bool = False,
    ) -> Tuple[TaskStatus, str, List[str], List[str]]:
        """Full completion gate — called when agent signals it is done.

        Returns:
            (status, reason, criteria_met, criteria_failed)
        """
        contract = self._contract
        criteria_met: List[str] = []
        criteria_failed: List[str] = []
        errors: List[str] = []

        # 1. Check expected_outputs exist
        for fname in contract.expected_outputs:
            target = self._work_dir / fname
            if target.exists():
                criteria_met.append(f"output_exists:{fname}")
            else:
                criteria_failed.append(f"output_missing:{fname}")
                errors.append(f"Expected output not found: {fname}")

        # 2. Check validate_task_result was called and passed
        validation_passed = (
            validation_tool_result is not None
            and "VALIDATION_PASSED" in str(validation_tool_result)
        )
        if not validation_passed:
            criteria_failed.append("validate_task_result:not_passed")
            errors.append(
                "validate_task_result was not called or did not return VALIDATION_PASSED."
            )
        else:
            criteria_met.append("validate_task_result:passed")

        # 3. Budget check
        # A model call admitted while under budget may finish and validate the
        # task.  In that narrow case, its bounded token overshoot prevents any
        # further model call but does not retroactively invalidate completion.
        budget_status, budget_reason = self.check(
            iter_count=iter_count,
            wall_time_seconds=wall_time_seconds,
            input_tokens=(0 if allow_final_token_overshoot else input_tokens),
            estimated_cost_cny=(
                0.0 if allow_final_token_overshoot else estimated_cost_cny
            ),
            # Completion does not admit another model call.  The reserve is an
            # admission-control margin, not a reason to reject already-written
            # and successfully validated artifacts.
            include_next_call_reserve=False,
        )
        if budget_status == TaskStatus.budget_exhausted:
            return budget_status, budget_reason, criteria_met, criteria_failed

        # 4. Final verdict
        if errors:
            return (
                TaskStatus.validation_failed,
                "; ".join(errors),
                criteria_met,
                criteria_failed,
            )

        return TaskStatus.completed, "all_gates_passed", criteria_met, criteria_failed
