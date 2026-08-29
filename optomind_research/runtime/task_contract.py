"""Task contract protocol — the formal interface between callers and ResearchWorker."""

from __future__ import annotations

import re
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

_SAFE_SLUG = re.compile(r'^[a-zA-Z0-9_\-]{1,128}$')


def _validate_slug(v: str, field_name: str) -> str:
    """Reject IDs that could be used for path traversal."""
    if not v:
        raise ValueError(f"{field_name} must not be empty")
    if not _SAFE_SLUG.match(v):
        raise ValueError(
            f"{field_name}={v!r} is not a safe slug "
            f"(only [a-zA-Z0-9_-], max 128 chars)"
        )
    return v


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    waiting_for_human = "waiting_for_human"
    completed = "completed"
    failed = "failed"
    budget_exhausted = "budget_exhausted"
    validation_failed = "validation_failed"


class HumanInterventionPolicy(str, Enum):
    never = "never"
    on_validation_failure = "on_validation_failure"
    on_tool_error = "on_tool_error"
    always_confirm = "always_confirm"


_SAFE_FILENAME = re.compile(r'^[a-zA-Z0-9_\-\.]{1,128}$')


class TaskContract(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str
    input_artifact_ids: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(
        default_factory=lambda: [
            "list_task_artifacts",
            "read_task_artifact",
            "write_task_note",
            "validate_task_result",
            "TaskCreate",
            "TaskList",
            "TaskGet",
            "TaskUpdate",
        ]
    )
    skill_ids: List[str] = Field(default_factory=list)
    model_tier: str = "standard_model"
    max_iters: int = 10
    wall_time_budget_seconds: float = 300.0
    token_budget: int = 50000
    cost_budget_cny: Optional[float] = None
    # Optional reserve checked before admitting another model call.
    next_call_cost_reserve_cny: float = 0.0
    human_intervention_policy: HumanInterventionPolicy = HumanInterventionPolicy.never
    expected_outputs: List[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.pending
    stop_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, v: str) -> str:
        return _validate_slug(v, "run_id")

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        return _validate_slug(v, "task_id")

    @field_validator("expected_outputs")
    @classmethod
    def validate_expected_outputs(cls, v: List[str]) -> List[str]:
        for name in v:
            if ".." in name or "/" in name or "\\" in name:
                raise ValueError(
                    f"expected_outputs entry {name!r} contains path separators or '..'"
                )
            if not _SAFE_FILENAME.match(name):
                raise ValueError(
                    f"expected_outputs entry {name!r} is not a safe filename"
                )
        return v

    @field_validator("input_artifact_ids")
    @classmethod
    def validate_input_artifact_ids(cls, v: List[str]) -> List[str]:
        for name in v:
            if ".." in name or "/" in name or "\\" in name:
                raise ValueError(
                    f"input_artifact_ids entry {name!r} contains path separators or '..'"
                )
        return v

    @field_validator("human_intervention_policy")
    @classmethod
    def validate_human_intervention_policy(cls, v: HumanInterventionPolicy) -> HumanInterventionPolicy:
        _UNIMPLEMENTED = {
            HumanInterventionPolicy.on_validation_failure,
            HumanInterventionPolicy.on_tool_error,
        }
        if v in _UNIMPLEMENTED:
            raise ValueError(
                f"HumanInterventionPolicy.{v.value!r} is not implemented in Phase 1.x. "
                "Use 'never' or 'always_confirm'."
            )
        return v

    @field_validator("cost_budget_cny")
    @classmethod
    def validate_cost_budget_cny(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("cost_budget_cny must be positive when provided")
        return v

    @field_validator("next_call_cost_reserve_cny")
    @classmethod
    def validate_cost_reserve_cny(cls, v: float) -> float:
        if v < 0:
            raise ValueError("next_call_cost_reserve_cny must be non-negative")
        return v


class ResultManifest(BaseModel):
    run_id: str
    task_id: str
    status: TaskStatus
    stop_reason: Optional[str] = None
    success_criteria_met: List[str] = Field(default_factory=list)
    success_criteria_failed: List[str] = Field(default_factory=list)
    validation_passed: bool = False
    tool_call_count: int = 0
    iter_count: int = 0
    # P1-4: framework ReAct-turn gauge at stop time (agentscope
    # state.cur_iter).  None whenever the run did not end on an
    # ExceedMaxItersEvent.  Distinct from iter_count, which counts
    # completed model calls in the current process.
    react_iter_count: Optional[int] = None
    wall_time_seconds: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    estimated_cost_usd: float = 0.0
    output_paths: Dict[str, str] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
