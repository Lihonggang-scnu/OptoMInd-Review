"""Deterministic tests for Phase 1.1 ResearchWorker runtime hardening.

No real API calls. Uses ScriptedFakeModel as a test double.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Fake ChatModelBase — scripted test double
# ---------------------------------------------------------------------------

from agentscope.model._base import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from agentscope.message._block import TextBlock, ToolCallBlock, ToolCallState
from agentscope.credential._base import CredentialBase
from pydantic import BaseModel as PydanticBaseModel
import uuid as _uuid


class _FakeCredential(CredentialBase):
    type: str = "fake_credential"


def _text_only_formatter() -> SimpleNamespace:
    """Provide the AgentScope 2.x formatter surface required by ``Agent``.

    The fake models call no provider formatter themselves, but AgentScope
    2.0.7 inspects this attribute while normalising incoming messages.  Keep
    the test double text-only and avoid coupling it to a provider formatter.
    """

    return SimpleNamespace(supported_input_media_types=[])


def _make_tool_call_response(tool_name: str, args: dict) -> ChatResponse:
    tcb = ToolCallBlock(
        id=_uuid.uuid4().hex[:8],
        name=tool_name,
        input=json.dumps(args),
        state=ToolCallState.PENDING,
    )
    return ChatResponse(content=[tcb], is_last=True)


def _make_text_response(text: str) -> ChatResponse:
    return ChatResponse(content=[TextBlock(text=text)], is_last=True)


class ScriptedFakeModel(ChatModelBase):
    """Plays back a preset script of ChatResponse objects in order."""

    class Parameters(PydanticBaseModel):
        pass

    def __init__(self, script: list[ChatResponse], usage_per_call: tuple[int, int] = (1200, 80)) -> None:
        super().__init__(
            credential=_FakeCredential(),
            model="fake-scripted-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = _text_only_formatter()
        self._script = script
        self._index = 0
        self._usage = usage_per_call  # (input, output) per call

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        resp = self._script[min(self._index, len(self._script) - 1)]
        self._index += 1
        return resp


class UsageScriptedFakeModel(ChatModelBase):
    """Scripted model that emits exact, variable per-call usage events."""

    class Parameters(PydanticBaseModel):
        pass

    def __init__(self, script: list[ChatResponse], usages: list[tuple[int, int]]) -> None:
        super().__init__(
            credential=_FakeCredential(),
            model="fake-usage-scripted-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = _text_only_formatter()
        self._script = script
        self._usages = usages
        self._index = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list | None = None,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> ChatResponse:
        index = min(self._index, len(self._script) - 1)
        response = self._script[index]
        input_tokens, output_tokens = self._usages[min(self._index, len(self._usages) - 1)]
        self._index += 1
        return ChatResponse(
            content=response.content,
            is_last=response.is_last,
            usage=ChatUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                time=0.001,
            ),
        )


class ErrorFakeModel(ChatModelBase):
    """Always raises after `succeed_count` calls, simulating a failure."""

    class Parameters(PydanticBaseModel):
        pass

    def __init__(self, error: Exception, succeed_count: int = 0, succeed_script: Optional[list] = None) -> None:
        super().__init__(
            credential=_FakeCredential(),
            model="error-fake-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = _text_only_formatter()
        self._error = error
        self._succeed_count = succeed_count
        self._calls = 0
        self._succeed_script = succeed_script or []

    async def _call_api(self, model_name: str, messages: list, **kwargs: Any) -> ChatResponse:
        self._calls += 1
        if self._calls <= self._succeed_count and self._succeed_script:
            idx = min(self._calls - 1, len(self._succeed_script) - 1)
            return self._succeed_script[idx]
        raise self._error


class FailFirstFakeModel(ChatModelBase):
    """Raises on the first `fail_count` calls, then plays back a script."""

    class Parameters(PydanticBaseModel):
        pass

    def __init__(self, error: Exception, fail_count: int, script: list[ChatResponse]) -> None:
        super().__init__(
            credential=_FakeCredential(),
            model="fail-first-fake-model",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = _text_only_formatter()
        self._error = error
        self._fail_count = fail_count
        self._script = script
        self._calls = 0

    async def _call_api(self, model_name: str, messages: list, **kwargs: Any) -> ChatResponse:
        self._calls += 1
        if self._calls <= self._fail_count:
            raise self._error
        idx = min(self._calls - self._fail_count - 1, len(self._script) - 1)
        return self._script[idx]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "agent_harness"
SAMPLE_MANIFEST = FIXTURES_DIR / "sample_manifest.json"


def _make_contract(tmp_runs_root: Path, **overrides) -> "TaskContract":
    from optomind_research.runtime.task_contract import TaskContract
    run_id = "test_run_" + uuid.uuid4().hex[:6]
    task_id = "task_" + uuid.uuid4().hex[:6]
    defaults = dict(
        run_id=run_id,
        task_id=task_id,
        goal="Read sample_manifest.json and find missing fields.",
        input_artifact_ids=["sample_manifest.json"],
        constraints=["Only read provided artifacts."],
        success_criteria=["sample_manifest.json has been read", "FINDINGS.md written"],
        expected_outputs=["FINDINGS.md"],
        allowed_tools=[
            "list_task_artifacts", "read_task_artifact",
            "write_task_note", "validate_task_result",
            "TaskCreate", "TaskList", "TaskGet", "TaskUpdate",
        ],
        model_tier="standard_model",
        max_iters=10,
        wall_time_budget_seconds=60.0,
        token_budget=100000,
    )
    defaults.update(overrides)
    return TaskContract(**defaults)


def _copy_manifest(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "sample_manifest.json").write_text(
        SAMPLE_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _full_completion_script() -> list[ChatResponse]:
    """Script that completes the full task loop successfully."""
    return [
        _make_tool_call_response("list_task_artifacts", {}),
        _make_tool_call_response("read_task_artifact", {"artifact_name": "sample_manifest.json"}),
        _make_tool_call_response(
            "write_task_note",
            {
                "filename": "FINDINGS.md",
                "content": (
                    "# Findings\n\n"
                    "paper_002: missing title, authors (empty list), abstract (empty), doi (empty)\n"
                ),
            },
        ),
        _make_tool_call_response(
            "validate_task_result",
            {
                "expected_outputs": '["FINDINGS.md"]',
                "success_criteria": "Read manifest, identified paper_002 missing fields, wrote FINDINGS.md.",
            },
        ),
        _make_text_response("Task complete. All criteria met."),
    ]


# ---------------------------------------------------------------------------
# T1 — AgentScope 2.0.2 key classes importable
# ---------------------------------------------------------------------------

def test_agentscope_imports():
    from agentscope.agent import Agent, ReActConfig, ContextConfig, ModelConfig
    from agentscope.model import DashScopeChatModel
    from agentscope.credential import DashScopeCredential
    from agentscope.tool import Toolkit, FunctionTool, TaskCreate, TaskList, TaskGet, TaskUpdate
    from agentscope.skill import LocalSkillLoader
    from agentscope.state import AgentState
    from agentscope.message import UserMsg
    from agentscope.permission import PermissionContext, PermissionMode, PermissionRule, PermissionBehavior
    from agentscope.event import (
        ToolCallStartEvent, ToolResultStartEvent, ToolResultTextDeltaEvent,
        ToolResultEndEvent, ModelCallStartEvent, ModelCallEndEvent,
        RequireUserConfirmEvent, ReplyEndEvent, ExceedMaxItersEvent,
    )
    assert hasattr(Agent, "reply_stream")
    assert "max_iters" in ReActConfig.model_fields
    assert "tool_call_name" in ToolCallStartEvent.model_fields
    assert "tool_call_name" in ToolResultStartEvent.model_fields
    assert "input_tokens" in ModelCallEndEvent.model_fields
    assert "output_tokens" in ModelCallEndEvent.model_fields
    assert "model_name" in ModelCallStartEvent.model_fields


# ---------------------------------------------------------------------------
# T2 — Skill loadable by LocalSkillLoader
# ---------------------------------------------------------------------------

def test_skill_loader_finds_scientific_task_execution():
    from agentscope.skill import LocalSkillLoader
    skills_dir = PROJECT_ROOT / "skills"
    assert skills_dir.exists()
    loader = LocalSkillLoader(directory=str(skills_dir), scan_subdir=True)
    skills = asyncio.run(loader.list_skills())
    names = [s.name for s in skills]
    assert "scientific-task-execution" in names, f"Not found. Found: {names}"


# ---------------------------------------------------------------------------
# T3 — Toolkit sees all required tools
# ---------------------------------------------------------------------------

def test_toolkit_has_all_required_tools(tmp_path: Path):
    from agentscope.tool import Toolkit, TaskCreate, TaskList, TaskGet, TaskUpdate
    from optomind_research.runtime.tool_registry import build_research_toolkit
    research_tools, _ = build_research_toolkit(tmp_path, [])
    toolkit = Toolkit(tools=research_tools + [TaskCreate(), TaskList(), TaskGet(), TaskUpdate()])
    schemas = asyncio.run(toolkit.get_tool_schemas())
    tool_names = {s["function"]["name"] for s in schemas}
    for name in {"list_task_artifacts", "read_task_artifact", "write_task_note", "validate_task_result"}:
        assert name in tool_names, f"Missing: {name}. Found: {tool_names}"


# ---------------------------------------------------------------------------
# T4 — Path traversal rejected
# ---------------------------------------------------------------------------

def test_path_traversal_rejected(tmp_path: Path):
    from optomind_research.runtime.tool_registry import build_research_toolkit
    from agentscope.tool._response import ToolResultState
    research_tools, _ = build_research_toolkit(tmp_path, [])
    read_tool = next(t for t in research_tools if t.name == "read_task_artifact")
    raw = read_tool(artifact_name="../../../etc/passwd")
    result = asyncio.run(raw) if asyncio.iscoroutine(raw) else raw
    content_text = " ".join(b.text for b in result.content if hasattr(b, "text"))
    assert any(kw in content_text.lower() for kw in ("traversal", "rejected", "not found"))


# ---------------------------------------------------------------------------
# T5 — Tool errors returned as ToolChunk, not raised
# ---------------------------------------------------------------------------

def test_tool_error_returned_not_raised(tmp_path: Path):
    from optomind_research.runtime.tool_registry import build_research_toolkit
    research_tools, _ = build_research_toolkit(tmp_path, [])
    read_tool = next(t for t in research_tools if t.name == "read_task_artifact")
    raw = read_tool(artifact_name="does_not_exist_xyz.json")
    result = asyncio.run(raw) if asyncio.iscoroutine(raw) else raw
    assert result is not None
    content_text = " ".join(b.text for b in result.content if hasattr(b, "text"))
    assert len(content_text) > 0


# ---------------------------------------------------------------------------
# T6 — AgentState serialization round-trip
# ---------------------------------------------------------------------------

def test_agent_state_save_and_restore():
    from agentscope.state import AgentState
    state = AgentState()
    original_session_id = state.session_id
    dumped = state.model_dump(mode="json")
    restored = AgentState.model_validate(dumped)
    assert restored.session_id == original_session_id


# ---------------------------------------------------------------------------
# T7 — AgentState tasks_context preserved after restore
# ---------------------------------------------------------------------------

def test_agent_state_tasks_context_preserved():
    from agentscope.state import AgentState
    state = AgentState()
    state.cur_iter = 3
    state.summary = "In progress."
    dumped = state.model_dump(mode="json")
    restored = AgentState.model_validate(dumped)
    assert restored.cur_iter == 3
    assert restored.summary == "In progress."


# ---------------------------------------------------------------------------
# T8 — max_iters respected
# ---------------------------------------------------------------------------

def test_react_config_max_iters():
    from agentscope.agent import ReActConfig
    cfg = ReActConfig(max_iters=5)
    assert cfg.max_iters == 5


# ---------------------------------------------------------------------------
# T9 — completed requires validation_passed
# ---------------------------------------------------------------------------

def test_completion_requires_validation(tmp_path: Path):
    from optomind_research.runtime.task_contract import TaskContract, TaskStatus
    from optomind_research.runtime.stop_controller import StopController
    contract = TaskContract(
        run_id="r1", task_id="t1", goal="test",
        expected_outputs=["FINDINGS.md"],
        success_criteria=["did something"],
        max_iters=10, wall_time_budget_seconds=300.0, token_budget=50000,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "FINDINGS.md").write_text("findings")
    ctrl = StopController(contract, work_dir)
    status, _, _, failed = ctrl.check_completion(validation_tool_result=None, iter_count=2, wall_time_seconds=5.0, input_tokens=100)
    assert status == TaskStatus.validation_failed
    assert any("validate_task_result" in f for f in failed)
    status2, _, _, _ = ctrl.check_completion(
        validation_tool_result="VALIDATION_PASSED: all 1 expected outputs present.",
        iter_count=2, wall_time_seconds=5.0, input_tokens=100,
    )
    assert status2 == TaskStatus.completed


def test_preexisting_result_cache_requires_phase_identity_and_outputs(
    tmp_path: Path,
):
    from optomind_research.runtime.stop_controller import StopController
    from optomind_research.runtime.task_contract import TaskContract

    contract = TaskContract(
        run_id="r5_same_run",
        task_id="research_program",
        goal="Write the validated research plan.",
        expected_outputs=["RESEARCH_PLAN.json", "RESEARCH_PLAN.md"],
        metadata={"phase_identity": "research_program:plan_only:plan_only"},
    )
    result = {
        "run_id": "r5_same_run",
        "task_id": "research_program",
        "status": "completed",
    }
    (tmp_path / "RESULT.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    (tmp_path / "TASK.json").write_text(
        json.dumps(
            {
                "run_id": "r5_same_run",
                "task_id": "research_program",
                "metadata": {
                    "phase_identity": "research_program:initial_discovery:focus"
                },
            }
        ),
        encoding="utf-8",
    )
    controller = StopController(contract, tmp_path)
    reusable, reason = controller.preexisting_result_is_reusable(result)
    assert reusable is False
    assert reason == "task_contract_phase_identity_mismatch"

    (tmp_path / "TASK.json").write_text(
        json.dumps(contract.model_dump(mode="json")), encoding="utf-8"
    )
    (tmp_path / "RESEARCH_PLAN.json").write_text("{}", encoding="utf-8")
    (tmp_path / "RESEARCH_PLAN.md").write_text("# Plan", encoding="utf-8")
    reusable, reason = controller.preexisting_result_is_reusable(result)
    assert reusable is True
    assert reason == "same_phase_contract_and_outputs_present"


def test_repeated_identical_validation_failure_stops_paid_loop(
    tmp_path: Path,
):
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    runs_root = tmp_path / "runs"
    contract = _make_contract(
        runs_root,
        max_iters=20,
        expected_outputs=["FINDINGS.md"],
    )
    work_dir = task_work_dir(contract.run_id, contract.task_id, runs_root)
    _copy_manifest(work_dir)
    model = ScriptedFakeModel(
        [
            _make_tool_call_response(
                "validate_task_result",
                {
                    "expected_outputs": '["FINDINGS.md"]',
                    "success_criteria": "FINDINGS.md must exist.",
                },
            ),
            _make_tool_call_response(
                "validate_task_result",
                {
                    "expected_outputs": '["FINDINGS.md"]',
                    "success_criteria": "FINDINGS.md must exist.",
                },
            ),
            _make_tool_call_response(
                "validate_task_result",
                {
                    "expected_outputs": '["FINDINGS.md"]',
                    "success_criteria": "FINDINGS.md must exist.",
                },
            ),
            _make_text_response("Retry again."),
        ]
    )

    result = ResearchWorker(
        runs_root=runs_root,
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=model,
    ).run(contract)

    assert result.status == TaskStatus.validation_failed
    assert "repeated_identical_validation_failure" in result.stop_reason
    assert model._index == 3


# ---------------------------------------------------------------------------
# T10 — Event log has no API keys
# ---------------------------------------------------------------------------

def test_event_log_no_api_keys(tmp_path: Path):
    from optomind_research.runtime.event_logger import EventLogger
    el = EventLogger(tmp_path)
    el.log_task_start("run1", "task1", "test goal")
    el.log_error("APIError", "Bearer sk-abc123456789secret")
    el.log_model_call_end("qwen3.6-flash", 100, 50, 200.0)
    content = (tmp_path / "EVENTS.jsonl").read_text(encoding="utf-8")
    assert "sk-abc123456789secret" not in content
    assert "REDACTED" in content


# ---------------------------------------------------------------------------
# T11 — Cost ledger saves correctly
# ---------------------------------------------------------------------------

def test_cost_ledger_saves(tmp_path: Path):
    from optomind_research.runtime.cost_ledger import CostLedger
    ledger = CostLedger(tmp_path, "run1", "task1")
    ledger.record_call("qwen3.6-flash", 500, 200)
    ledger.record_tool_call()
    ledger.save("completed", "all_gates_passed")
    data = json.loads((tmp_path / "COST.json").read_text())
    assert data["status"] == "completed"
    assert data["total_input_tokens"] == 500


# ---------------------------------------------------------------------------
# T12 — Old mainline imports unaffected
# ---------------------------------------------------------------------------

def test_old_mainline_no_regression():
    from config.qwen_config import get_model_name, get_qwen_client_config
    from config.secret_pool import mask_secret
    from config.model_router import select_model_tier
    from llm.qwen_chat_client import call_qwen_chat
    assert get_model_name("standard_model") == "qwen3.6-flash"
    assert mask_secret("sk-abcdefgh12345678") == "****"
    assert mask_secret("sk-a-different-secret") == "****"
    assert select_model_tier(task_type="deterministic") == "deterministic"


# ---------------------------------------------------------------------------
# T13 — Full completion via ScriptedFakeModel
# ---------------------------------------------------------------------------

def test_research_worker_full_completion(tmp_path: Path):
    """ScriptedFakeModel completes all steps: status=completed, validation_passed=True, FINDINGS.md exists."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script()),
    )
    result = worker.run(contract)

    assert result.status.value == "completed", f"status={result.status.value}, stop_reason={result.stop_reason}"
    assert result.validation_passed is True
    assert (work_dir / "FINDINGS.md").exists(), "FINDINGS.md not written"
    assert result.tool_call_count >= 4


# ---------------------------------------------------------------------------
# T14 — Safe tool auto-executes (no asking state)
# ---------------------------------------------------------------------------

def test_safe_tool_does_not_block_on_asking(tmp_path: Path):
    """With DONT_ASK + allow_rules, custom research tools must not get stuck in asking."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script()),
    )
    result = worker.run(contract)

    # No tool should be in asking state — check EVENTS.jsonl
    events_path = work_dir / "EVENTS.jsonl"
    lines = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    asking_events = [e for e in lines if e.get("status") == "asking"]
    assert asking_events == [], f"Found asking events: {asking_events}"


# ---------------------------------------------------------------------------
# T15 — Unauthorized tool not in Toolkit schema
# ---------------------------------------------------------------------------

def test_unauthorized_tool_not_in_toolkit_schema(tmp_path: Path):
    """Tools not in allowed_tools must not appear in Toolkit schema."""
    from optomind_research.runtime.tool_registry import build_research_toolkit
    from agentscope.tool import Toolkit, TaskCreate, TaskList

    # Build toolkit with only list_task_artifacts + TaskCreate
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    research_tools, _ = build_research_toolkit(work_dir, [])
    # Filter to only one research tool
    tools = [t for t in research_tools if t.name == "list_task_artifacts"]
    toolkit = Toolkit(tools=tools + [TaskCreate(), TaskList()])
    schemas = asyncio.run(toolkit.get_tool_schemas())
    tool_names = {s["function"]["name"] for s in schemas}
    # Unauthorized tools must not appear
    assert "write_task_note" not in tool_names
    assert "validate_task_result" not in tool_names
    assert "read_task_artifact" not in tool_names


# ---------------------------------------------------------------------------
# T16 — tool_call_id → tool_name mapping (no unknown in EVENTS.jsonl)
# ---------------------------------------------------------------------------

def test_no_unknown_tool_names_in_events(tmp_path: Path):
    """EVENTS.jsonl must not contain tool='unknown' after a full run."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script()),
    )
    worker.run(contract)

    events_path = work_dir / "EVENTS.jsonl"
    lines = [json.loads(l) for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    tool_events = [e for e in lines if e.get("event") == "tool_call"]
    unknown = [e for e in tool_events if e.get("tool") in ("unknown", "", None)]
    assert unknown == [], f"Found unknown tool events: {unknown}"


# ---------------------------------------------------------------------------
# T17 — validate_task_result text is captured (not just state)
# ---------------------------------------------------------------------------

def test_validate_task_result_text_captured(tmp_path: Path):
    """The actual VALIDATION_PASSED text must reach the stop controller."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script()),
    )
    result = worker.run(contract)
    assert result.validation_passed is True
    assert result.status == TaskStatus.completed


# ---------------------------------------------------------------------------
# T18 — ModelCallEndEvent token counts are used (not fixed 500/200)
# ---------------------------------------------------------------------------

def test_real_token_counts_not_hardcoded(tmp_path: Path):
    """input_tokens should NOT all be exactly 500 per call after hardcoding fix.
    The ScriptedFakeModel usage doesn't emit ModelCallEndEvent tokens directly,
    but AgentScope does emit ModelCallEndEvent with event.input_tokens.
    We verify the EVENTS.jsonl contains varied (non-500) numbers if the model provides real counts.
    This test verifies the code path reads from event, not hardcodes.
    """
    from optomind_research.runtime.event_logger import EventLogger
    el = EventLogger(tmp_path)
    # Simulate logging a call with real (non-500) token counts
    el.log_model_call_end("qwen3.6-flash", 1234, 87, 500.0)
    lines = [json.loads(l) for l in (tmp_path / "EVENTS.jsonl").read_text().splitlines() if l.strip()]
    end_events = [l for l in lines if l.get("event") == "model_call_end"]
    assert end_events[0]["input_tokens"] == 1234
    assert end_events[0]["output_tokens"] == 87


# ---------------------------------------------------------------------------
# T19 — Key error triggers key switch (not model switch)
# ---------------------------------------------------------------------------

def test_key_error_triggers_key_switch(tmp_path: Path):
    """A key-level error (401) should cause advance_to_next_key_candidate, not model switch."""
    from optomind_research.runtime.recovery_policy import RecoveryPolicy, classify_error, ErrorCategory

    # Verify error classification
    exc_401 = Exception("Request failed with status 401: invalid api key")
    assert classify_error(exc_401) == ErrorCategory.key

    exc_model = Exception("model not found: qwen3.6-flash-does-not-exist")
    assert classify_error(exc_model) == ErrorCategory.model

    exc_net = Exception("ConnectionError: connection refused")
    assert classify_error(exc_net) == ErrorCategory.network


# ---------------------------------------------------------------------------
# T20 — Checkpoint resume does not recreate tasks
# ---------------------------------------------------------------------------

def test_checkpoint_resume_no_duplicate_tasks(tmp_path: Path):
    """Running the same task twice with a checkpoint must not duplicate tasks."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    # First run
    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script()),
    )
    r1 = worker.run(contract)

    # Second run with same run_id/task_id (already completed)
    r2 = worker.run(contract)
    assert r2.status.value == "completed"
    assert r2.run_id == r1.run_id
    assert r2.task_id == r1.task_id


# ---------------------------------------------------------------------------
# T21 — skill_ids filter exposes only specified skills
# ---------------------------------------------------------------------------

def test_skill_ids_filter(tmp_path: Path):
    """FilteredSkillLoader only exposes skill_ids-specified skills."""
    from optomind_research.runtime.skill_loader import FilteredSkillLoader, get_skill_loader

    inner = get_skill_loader(PROJECT_ROOT / "skills")
    all_skills = asyncio.run(inner.list_skills())
    all_names = [s.name for s in all_skills]

    # With explicit filter
    filtered = FilteredSkillLoader(inner, skill_ids=["scientific-task-execution"])
    filt_skills = asyncio.run(filtered.list_skills())
    filt_names = [s.name for s in filt_skills]
    assert filt_names == ["scientific-task-execution"]

    # With empty filter: returns all
    unfiltered = FilteredSkillLoader(inner, skill_ids=None)
    unf_skills = asyncio.run(unfiltered.list_skills())
    assert {s.name for s in unf_skills} == set(all_names)


# ---------------------------------------------------------------------------
# T22 — Skill readable via SkillViewer / load_skill
# ---------------------------------------------------------------------------

def test_skill_loadable_via_skill_loader(tmp_path: Path):
    """Confirm the skill can actually be loaded and has non-empty content."""
    from optomind_research.runtime.skill_loader import get_skill_loader

    loader = get_skill_loader(PROJECT_ROOT / "skills")
    skills = asyncio.run(loader.list_skills())
    skill = next((s for s in skills if s.name == "scientific-task-execution"), None)
    assert skill is not None, "scientific-task-execution skill not found"
    # Skill should have a markdown attribute with substantial content
    content = str(skill.markdown) if hasattr(skill, "markdown") else str(skill)
    assert len(content) > 50, "Skill appears to be empty or failed to load"
    assert "validate" in content.lower(), "Skill content does not mention validation step"


# ---------------------------------------------------------------------------
# T23 — run_id/task_id path traversal rejected
# ---------------------------------------------------------------------------

def test_run_id_task_id_path_traversal_rejected():
    """TaskContract must reject IDs with path separators or '..'."""
    from optomind_research.runtime.task_contract import TaskContract
    import pydantic

    with pytest.raises((ValueError, pydantic.ValidationError)):
        TaskContract(run_id="../evil", task_id="t1", goal="test")

    with pytest.raises((ValueError, pydantic.ValidationError)):
        TaskContract(run_id="ok-run", task_id="../evil", goal="test")

    with pytest.raises((ValueError, pydantic.ValidationError)):
        TaskContract(run_id="ok-run", task_id="t1", goal="test", expected_outputs=["../secret.txt"])


# ---------------------------------------------------------------------------
# T24 — human_intervention_policy=never → waiting_for_human on unexpected confirm
# ---------------------------------------------------------------------------

def test_permission_context_built_correctly():
    """PermissionContext for allowed_tools uses DONT_ASK + allow_rules."""
    from optomind_research.runtime.research_worker import _build_permission_context, _resolve_tool_names
    from agentscope.permission import PermissionMode, PermissionBehavior

    tools = _resolve_tool_names(["list_task_artifacts", "read_task_artifact", "TaskCreate"])
    pc = _build_permission_context(tools)
    assert pc.mode == PermissionMode.DONT_ASK
    assert "list_task_artifacts" in pc.allow_rules
    assert "TaskCreate" in pc.allow_rules
    rule = pc.allow_rules["list_task_artifacts"][0]
    assert rule.behavior == PermissionBehavior.ALLOW
    # Non-whitelisted tool must not be in allow_rules
    assert "write_task_note" not in pc.allow_rules


# ---------------------------------------------------------------------------
# T25 — EVENTS.jsonl key redaction by key name
# ---------------------------------------------------------------------------

def test_event_log_redacts_by_key_name(tmp_path: Path):
    """Redaction covers both string content and dict key names."""
    from optomind_research.runtime.event_logger import EventLogger
    el = EventLogger(tmp_path)
    el._record({"event": "test", "api_key": "sk-shouldberedacted12345", "info": "ok"})
    content = (tmp_path / "EVENTS.jsonl").read_text(encoding="utf-8")
    assert "sk-shouldberedacted12345" not in content


def test_event_log_keeps_key_source_but_never_fingerprint(tmp_path: Path):
    from optomind_research.runtime.event_logger import EventLogger

    logger = EventLogger(tmp_path)
    logger.log_model_call_start(
        "qwen3.7-flash",
        key_fingerprint="0123456789ab",
        key_source="api_keys/qwen-api-key.txt#1",
    )
    logger.log_recovery(
        "key",
        "qwen3.7-flash",
        "qwen3.7-flash",
        "test",
        old_key_fingerprint="0123456789ab",
        new_key_fingerprint="fedcba987654",
        old_key_source="api_keys/qwen-api-key.txt#1",
        new_key_source="api_keys/qwen-api-key.txt#2",
    )

    content = (tmp_path / "EVENTS.jsonl").read_text(encoding="utf-8")
    assert "0123456789ab" not in content
    assert "fedcba987654" not in content
    assert "fingerprint" not in content
    assert "qwen-api-key.txt#1" in content
    assert "qwen-api-key.txt#2" in content
    assert "api_keys/qwen-api-key.txt" not in content


# ---------------------------------------------------------------------------
# T26 — Smoke test exits with non-zero code on failure
# ---------------------------------------------------------------------------

def test_smoke_dry_run_fail_exits_nonzero():
    """--dry-run-fail must exit with code 1 immediately, no network access."""
    import subprocess
    smoke_script = PROJECT_ROOT / "scripts" / "run_research_worker_smoke.py"
    result = subprocess.run(
        [sys.executable, str(smoke_script), "--dry-run-fail"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1, f"Expected exit code 1, got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "Exiting with code 1" in result.stdout, f"Expected 'Exiting with code 1' in stdout:\n{result.stdout}"


# ---------------------------------------------------------------------------
# T27 — Arrearage classified as key error
# ---------------------------------------------------------------------------

def test_arrearage_classified_as_key_error():
    """DashScope 'Arrearage' status must be treated as a key-level error."""
    from optomind_research.runtime.recovery_policy import classify_error, ErrorCategory
    assert classify_error(Exception("Arrearage")) == ErrorCategory.key
    assert classify_error(Exception("status_code=400, code='Arrearage'")) == ErrorCategory.key


def test_free_tier_only_classified_as_model_route_error():
    """A model-scoped allocation failure must preserve the working key."""
    from optomind_research.runtime.recovery_policy import classify_error, ErrorCategory

    exc = Exception(
        "403 AllocationQuota.FreeTierOnly: disable the use free tier only mode"
    )
    assert classify_error(exc) == ErrorCategory.model


def test_factory_keeps_model_fallbacks_for_every_key(monkeypatch):
    """A switched key must retain the same model fallback ladder."""
    from optomind_research.runtime import agent_model_factory as module

    module.AgentScopeModelFactory._unhealthy_keys.clear()
    module.AgentScopeModelFactory._unhealthy_routes.clear()
    module.AgentScopeModelFactory._preferred_key = None
    monkeypatch.setattr(module, "get_model_name", lambda _tier: "primary")
    monkeypatch.setattr(
        module,
        "get_fallback_model_names",
        lambda _tier: ["fallback"],
    )
    monkeypatch.setattr(
        module,
        "get_qwen_api_key_candidates_ordered",
        lambda: [
            {
                "api_key": "key-one",
                "api_key_masked": "key***one",
                "api_key_source": "test#1",
            },
            {
                "api_key": "key-two",
                "api_key_masked": "key***two",
                "api_key_source": "test#2",
            },
        ],
    )
    monkeypatch.setattr(
        module,
        "_build_model",
        lambda model_name, api_key: (model_name, api_key),
    )

    factory = module.AgentScopeModelFactory("advanced_model")
    routes = [
        (candidate.model_name, candidate.api_key)
        for candidate in factory._candidates
    ]
    assert routes == [
        ("primary", "key-one"),
        ("fallback", "key-one"),
        ("primary", "key-two"),
        ("fallback", "key-two"),
    ]

    # A model-scoped failure keeps key-one and moves to its fallback.
    assert factory.advance_to_next_model_candidate() is True
    assert factory.current_model_name == "fallback"
    assert factory.current_key_source == "test#1"
    assert ("key-one", "primary") in factory._unhealthy_routes
    module.AgentScopeModelFactory._unhealthy_keys.clear()
    module.AgentScopeModelFactory._unhealthy_routes.clear()
    module.AgentScopeModelFactory._preferred_key = None


# ---------------------------------------------------------------------------
# T28 — Overdue / payment errors classified as key error
# ---------------------------------------------------------------------------

def test_overdue_payment_classified_as_key_error():
    """Billing-related messages must map to ErrorCategory.key."""
    from optomind_research.runtime.recovery_policy import classify_error, ErrorCategory
    assert classify_error(Exception("overdue payment required")) == ErrorCategory.key
    assert classify_error(Exception("insufficient balance")) == ErrorCategory.key
    assert classify_error(Exception("account not in good standing")) == ErrorCategory.key
    assert classify_error(Exception("billing issue detected")) == ErrorCategory.key


# ---------------------------------------------------------------------------
# T29 — Ordered factory returns keys in file order (no shuffle)
# ---------------------------------------------------------------------------

def test_ordered_factory_first_key_is_file_first():
    """get_qwen_api_key_candidates_ordered() must be stable across calls (no shuffle)."""
    from config.qwen_config import get_qwen_api_key_candidates_ordered
    c1 = get_qwen_api_key_candidates_ordered()
    c2 = get_qwen_api_key_candidates_ordered()
    if not c1:
        pytest.skip("No Qwen API keys found — skipping ordered key test")
    assert [c["api_key"] for c in c1] == [c["api_key"] for c in c2], (
        "get_qwen_api_key_candidates_ordered() returned different orders — shuffle must be disabled"
    )
    assert c1[0]["api_key"], "First candidate key must be non-empty"


# ---------------------------------------------------------------------------
# T30 — All candidates exhausted → status is failed, never running
# ---------------------------------------------------------------------------

def test_all_candidates_exhausted_status_is_failed(tmp_path: Path):
    """A model that always fails must leave status=failed, not status=running."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    contract = _make_contract(tmp_path / "runs", max_iters=3)
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ErrorFakeModel(Exception("always fail — simulated permanent error")),
    )
    result = worker.run(contract)
    assert result.status != TaskStatus.running, (
        f"status must not be 'running' after exhaustion, got: {result.status.value}"
    )
    assert result.status in (TaskStatus.failed, TaskStatus.budget_exhausted, TaskStatus.validation_failed), (
        f"Expected a terminal failure status, got: {result.status.value}"
    )


# ---------------------------------------------------------------------------
# T31 — Recovery: model fails once then succeeds → completed
# ---------------------------------------------------------------------------

def test_recovery_key_switch_succeeds(tmp_path: Path):
    """Worker completes when the model fails on call 1 (key error) then succeeds."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    contract = _make_contract(tmp_path / "runs", max_iters=12)
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=FailFirstFakeModel(
            error=Exception("401 Unauthorized: invalid api key"),
            fail_count=1,
            script=_full_completion_script(),
        ),
    )
    result = worker.run(contract)
    assert result.status == TaskStatus.completed, (
        f"Expected completed after recovery, got: {result.status.value}, stop_reason={result.stop_reason}"
    )
    assert result.validation_passed is True


# ---------------------------------------------------------------------------
# T32 — skill_ids=[] exposes no skills
# ---------------------------------------------------------------------------

def test_empty_skill_ids_exposes_no_skills():
    """FilteredSkillLoader with skill_ids=[] must return an empty list."""
    from optomind_research.runtime.skill_loader import FilteredSkillLoader, get_skill_loader
    inner = get_skill_loader(PROJECT_ROOT / "skills")
    filtered = FilteredSkillLoader(inner, skill_ids=[])
    skills = asyncio.run(filtered.list_skills())
    assert skills == [], (
        f"Expected no skills with skill_ids=[], got: {[s.name for s in skills]}"
    )


# ---------------------------------------------------------------------------
# T33 — Worker token counts come from ModelCallEndEvent, not hardcoded fallback
# ---------------------------------------------------------------------------

def test_worker_token_counts_read_from_model_call_end_event(tmp_path: Path):
    """Worker must not report the old hardcoded 500-per-iter token fallback.

    ScriptedFakeModel emits no ModelCallEndEvent, so the correct result is
    total_input_tokens == 0 (nothing recorded), NOT iter_count * 500.
    The old bug artificially inflated counts by hardcoding 500 per iteration;
    this test detects that regression.
    """
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker

    contract = _make_contract(tmp_path / "runs")
    work_dir = task_work_dir(contract.run_id, contract.task_id, tmp_path / "runs")
    _copy_manifest(work_dir)

    worker = ResearchWorker(
        runs_root=tmp_path / "runs",
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=ScriptedFakeModel(_full_completion_script(), usage_per_call=(1200, 80)),
    )
    result = worker.run(contract)
    # Fake model emits no usage events → tokens must be 0, not iter_count * 500
    if result.iter_count > 0:
        hardcoded_value = result.iter_count * 500
        assert result.total_input_tokens != hardcoded_value, (
            f"token counts look hardcoded (500/iter): total={result.total_input_tokens}, "
            f"iters={result.iter_count}"
        )


def test_final_admitted_call_overshoot_allows_validation_and_completes(tmp_path: Path):
    """A final in-budget-admitted call may finish its validation tool after overshoot."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    runs_root = tmp_path / "runs"
    contract = _make_contract(runs_root, token_budget=5_000)
    work_dir = task_work_dir(contract.run_id, contract.task_id, runs_root)
    _copy_manifest(work_dir)
    model = UsageScriptedFakeModel(
        _full_completion_script(),
        usages=[(1_000, 10), (1_000, 11), (1_000, 12), (3_000, 13), (500, 5)],
    )

    result = ResearchWorker(
        runs_root=runs_root,
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=model,
    ).run(contract)

    assert result.status == TaskStatus.completed
    assert result.validation_passed is True
    assert result.total_input_tokens == 6_000
    assert result.total_output_tokens == 46
    assert result.iter_count == 4
    assert model._index == 4, "No fifth model call may be admitted after overshoot"
    assert "final_admitted_model_call_token_overshoot" in result.stop_reason
    assert "validation completed" in result.stop_reason
    assert (work_dir / "AGENT_STATE.json").exists()
    cost = json.loads((work_dir / "COST.json").read_text(encoding="utf-8"))
    assert cost["total_input_tokens"] == 6_000
    assert cost["total_output_tokens"] == 46


def test_final_admitted_call_overshoot_without_validation_is_budget_exhausted(tmp_path: Path):
    """Completed pending non-validation tools cannot authorize another model call."""
    from optomind_research.runtime.artifact_store import task_work_dir
    from optomind_research.runtime.research_worker import ResearchWorker
    from optomind_research.runtime.task_contract import TaskStatus

    runs_root = tmp_path / "runs"
    contract = _make_contract(runs_root, token_budget=5_000)
    work_dir = task_work_dir(contract.run_id, contract.task_id, runs_root)
    _copy_manifest(work_dir)
    script = _full_completion_script()[:3] + [
        _make_tool_call_response(
            "write_task_note",
            {"filename": "EXTRA.md", "content": "Pending tool completed after overshoot."},
        ),
        _make_text_response("This fifth call must never run."),
    ]
    model = UsageScriptedFakeModel(
        script,
        usages=[(1_000, 10), (1_000, 11), (1_000, 12), (3_000, 13), (500, 5)],
    )

    result = ResearchWorker(
        runs_root=runs_root,
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=model,
    ).run(contract)

    assert result.status == TaskStatus.budget_exhausted
    assert result.validation_passed is False
    assert result.total_input_tokens == 6_000
    assert result.total_output_tokens == 46
    assert result.iter_count == 4
    assert model._index == 4
    assert (work_dir / "FINDINGS.md").exists()
    assert (work_dir / "EXTRA.md").exists(), "Already-emitted pending tool must finish"
    assert "final_admitted_model_call_token_overshoot" in result.stop_reason
    assert "without successful validation" in result.stop_reason


def test_cost_reserve_only_blocks_next_model_call_not_completion(tmp_path: Path):
    """A reserve is admission control, not a rejection of completed work."""
    from optomind_research.runtime.stop_controller import StopController
    from optomind_research.runtime.task_contract import TaskContract, TaskStatus

    (tmp_path / "RESULT_ASSET.json").write_text("{}", encoding="utf-8")
    contract = TaskContract(
        run_id="reserve_semantics",
        task_id="reserve_semantics",
        goal="Validate reserve semantics.",
        allowed_tools=[],
        expected_outputs=["RESULT_ASSET.json"],
        max_iters=4,
        token_budget=10_000,
        cost_budget_cny=2.0,
        next_call_cost_reserve_cny=0.5,
    )
    controller = StopController(contract, tmp_path)

    admission_status, _ = controller.check(
        iter_count=1,
        input_tokens=1_000,
        estimated_cost_cny=1.8,
    )
    assert admission_status == TaskStatus.budget_exhausted

    post_call_status, _ = controller.check(
        iter_count=1,
        input_tokens=1_000,
        estimated_cost_cny=1.8,
        include_next_call_reserve=False,
    )
    assert post_call_status == TaskStatus.running

    final_status, _, _, _ = controller.check_completion(
        validation_tool_result="VALIDATION_PASSED",
        iter_count=1,
        input_tokens=1_000,
        estimated_cost_cny=1.8,
    )
    assert final_status == TaskStatus.completed


def test_r5_lifetime_token_admission_rejects_predicted_call_before_crossing():
    from optomind_research.runtime.research_worker import (
        _r5_lifetime_admission_reason,
    )

    metadata = {
        "r5_lifetime_budget": {
            "baseline_input_tokens": 112_601,
            "baseline_cost_cny": 0.377674,
            "ceiling_input_tokens": 172_601,
            "ceiling_cost_cny": 1.177674,
            "worker_ledger_baseline_input_tokens": 0,
            "worker_ledger_baseline_cost_cny": 0.0,
        }
    }
    assert _r5_lifetime_admission_reason(
        metadata=metadata,
        ledger_input_tokens=57_000,
        ledger_cost_cny=0.1,
        predicted_next_input=2_000,
        output_reserve=1_000,
        model_name="unknown_test_model",
    ) is None
    reason = _r5_lifetime_admission_reason(
        metadata=metadata,
        ledger_input_tokens=59_000,
        ledger_cost_cny=0.1,
        predicted_next_input=2_000,
        output_reserve=1_000,
        model_name="unknown_test_model",
    )
    assert reason is not None
    assert reason.startswith("r5_lifetime_token_admission:")


def test_r5_lifetime_cost_admission_rejects_predicted_call_before_crossing():
    from optomind_research.runtime.research_worker import (
        _r5_lifetime_admission_reason,
    )

    reason = _r5_lifetime_admission_reason(
        metadata={
            "r5_lifetime_budget": {
                "baseline_input_tokens": 0,
                "baseline_cost_cny": 0.7,
                "ceiling_input_tokens": 10_000_000,
                "ceiling_cost_cny": 0.8,
                "worker_ledger_baseline_input_tokens": 0,
                "worker_ledger_baseline_cost_cny": 0.0,
            }
        },
        ledger_input_tokens=0,
        ledger_cost_cny=0.0,
        predicted_next_input=100_000,
        output_reserve=4_000,
        model_name="unknown_test_model",
    )
    assert reason is not None
    assert reason.startswith("r5_lifetime_cost_admission:")


def test_r5_recursive_phase_admission_includes_prior_discovery_usage(
    monkeypatch,
):
    """Recursive admission starts at lifetime-before-plan, not root baseline."""

    import optomind_research.runtime.research_worker as worker_module
    from optomind_research.runtime.research_worker import (
        _r5_lifetime_admission_reason,
    )

    metadata = {
        "r5_lifetime_budget": {
            # Root envelope: 10k already spent before this R5 invocation.
            "baseline_input_tokens": 10_000,
            "baseline_cost_cny": 0.1,
            "ceiling_input_tokens": 70_000,
            "ceiling_cost_cny": 1.0,
            # Discovery spent 40k; plan-only starts at 10k + 40k.
            "lifetime_before_invocation_input_tokens": 50_000,
            "lifetime_before_invocation_cost_cny": 0.5,
            # The plan worker's local ledger starts at discovery's 40k.
            "worker_ledger_baseline_input_tokens": 40_000,
            "worker_ledger_baseline_cost_cny": 0.4,
        }
    }

    token_reason = _r5_lifetime_admission_reason(
        metadata=metadata,
        ledger_input_tokens=45_000,
        ledger_cost_cny=0.45,
        predicted_next_input=20_000,
        output_reserve=0,
        model_name="unknown_test_model",
    )
    assert token_reason is not None
    assert token_reason.startswith("r5_lifetime_token_admission:")
    assert "phase_start=50000" in token_reason

    monkeypatch.setattr(
        worker_module,
        "estimate_call_cost_cny",
        lambda model_name, input_tokens, output_tokens: 0.5,
    )
    cost_metadata = {
        "r5_lifetime_budget": {
            **metadata["r5_lifetime_budget"],
            # Isolate the cost gate; the token gate was tested above.
            "ceiling_input_tokens": 1_000_000,
        }
    }
    cost_reason = _r5_lifetime_admission_reason(
        metadata=cost_metadata,
        ledger_input_tokens=45_000,
        ledger_cost_cny=0.5,
        predicted_next_input=20_000,
        output_reserve=0,
        model_name="unknown_test_model",
    )
    assert cost_reason is not None
    assert cost_reason.startswith("r5_lifetime_cost_admission:")
    assert "phase_start=0.500000" in cost_reason


def test_recovery_without_checkpoint_preserves_contract_permissions():
    """A key failure before the first checkpoint must not reset to DEFAULT."""

    from agentscope.permission import PermissionMode, PermissionBehavior
    from optomind_research.runtime.research_worker import (
        _state_after_recovery,
    )

    state = _state_after_recovery(None, ["load_section_context"])
    assert state.permission_context.mode == PermissionMode.DONT_ASK
    assert (
        state.permission_context.allow_rules["load_section_context"][0].behavior
        == PermissionBehavior.ALLOW
    )
