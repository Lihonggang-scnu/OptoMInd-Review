"""P1-4 regression tests: iteration accounting + awaiting-human context.

Core promise: the gauge that triggers ExceedMaxItersEvent (framework
react turns) is recorded next to the process-local model-call counter,
so RESULT.json can never again read as self-contradictory.  Also guards
the untouched behaviours: waiting_for_human mapping, hypothesis/focus
caps of 4, opportunity cap of 5, and normal completion.

No real API calls; ScriptedFakeModel plays back ChatResponses.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agentscope.model._base import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.credential._base import CredentialBase
from agentscope.message._block import TextBlock, ToolCallBlock, ToolCallState
from pydantic import BaseModel as PydanticBaseModel

from optomind_research.runtime.research_program_runner import (
    _finalize_discovery_needs_more_literature,
    run_research_program,
)
from optomind_research.runtime.research_program_tool_provider import (
    ResearchProgramContext,
)
from optomind_research.runtime.research_worker import ResearchWorker
from optomind_research.runtime.task_contract import ResultManifest, TaskStatus

FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "agent_harness"


class _FakeCredential(CredentialBase):
    type: str = "fake_credential"


def _text_only_formatter() -> SimpleNamespace:
    """Expose the formatter surface AgentScope 2.0.7 inspects on ``Agent``."""

    return SimpleNamespace(supported_input_media_types=[])


class ScriptedFakeModel(ChatModelBase):
    class Parameters(PydanticBaseModel):
        pass

    def __init__(self, script):
        super().__init__(
            credential=_FakeCredential(),
            model="fake-p14-scripted",
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
        )
        self.formatter = _text_only_formatter()
        self._script = script
        self._index = 0

    async def _call_api(self, model_name, messages, tools=None, **kwargs):
        resp = self._script[min(self._index, len(self._script) - 1)]
        self._index += 1
        return resp


def _tool_call_response(tool_name: str) -> ChatResponse:
    return _tool_call_response_with_args(tool_name, {})


def _tool_call_response_with_args(tool_name: str, args: dict) -> ChatResponse:
    tcb = ToolCallBlock(
        id=uuid.uuid4().hex[:8],
        name=tool_name,
        input=json.dumps(args),
        state=ToolCallState.PENDING,
    )
    return ChatResponse(content=[tcb], is_last=True)


def _make_contract(tmp_path: Path, *, max_iters: int, goal: str):
    from optomind_research.runtime.task_contract import TaskContract
    return TaskContract(
        run_id="p14_" + uuid.uuid4().hex[:6],
        task_id="task_" + uuid.uuid4().hex[:6],
        goal=goal,
        input_artifact_ids=["sample_manifest.json"],
        constraints=["Only read provided artifacts."],
        success_criteria=["sample_manifest.json has been read"],
        expected_outputs=["FINDINGS.md"],
        allowed_tools=[
            "list_task_artifacts", "read_task_artifact",
            "write_task_note", "validate_task_result",
            "TaskCreate", "TaskList", "TaskGet", "TaskUpdate",
        ],
        model_tier="standard_model",
        max_iters=max_iters,
        wall_time_budget_seconds=120.0,
        token_budget=200000,
    )


def _run_worker(runs_root: Path, contract, model):
    return ResearchWorker(
        runs_root=runs_root,
        skills_dir=PROJECT_ROOT / "skills",
        _model_override=model,
    ).run(contract)

def test_iter_count_agrees_with_stop_reason(tmp_path: Path) -> None:
    contract = _make_contract(
        tmp_path, max_iters=3, goal="Loop forever without submitting.",
    )
    model = ScriptedFakeModel(
        [_tool_call_response("list_task_artifacts")],
    )
    result = _run_worker(tmp_path, contract, model)
    assert result.status == TaskStatus.budget_exhausted
    assert result.react_iter_count == contract.max_iters == 3
    assert f"react_iter={contract.max_iters}" in (result.stop_reason or "")
    assert f"max_iters={contract.max_iters} exceeded" in (result.stop_reason or "")
    # Both gauges stay truthful and ordered: completed model calls can
    # never exceed react turns.  They may legitimately diverge within a
    # single process, which is exactly why both must be recorded.
    assert result.iter_count == model._index
    assert result.iter_count <= contract.max_iters


def test_iteration_cap_respected(tmp_path: Path) -> None:
    contract = _make_contract(
        tmp_path, max_iters=4, goal="Never converge on purpose.",
    )
    model = ScriptedFakeModel(
        [_tool_call_response("list_task_artifacts")],
    )
    result = _run_worker(tmp_path, contract, model)
    assert model._index <= contract.max_iters
    assert result.status != TaskStatus.running
    assert result.react_iter_count is not None


def test_successful_program_still_passes(tmp_path: Path) -> None:
    work = tmp_path / "run"
    work.mkdir(parents=True)
    (work / "sample_manifest.json").write_text(
        (FIXTURES_DIR / "sample_manifest.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    contract = _make_contract(
        tmp_path,
        max_iters=10,
        goal="Read sample_manifest.json and find missing fields.",
    )
    contract.success_criteria = [
        "sample_manifest.json has been read",
        "FINDINGS.md written",
    ]
    script = [
        _tool_call_response("list_task_artifacts"),
        _tool_call_response_with_args(
            "read_task_artifact",
            {"artifact_name": "sample_manifest.json"},
        ),
        _tool_call_response_with_args(
            "write_task_note",
            {
                "filename": "FINDINGS.md",
                "content": (
                    "# Findings\n\n"
                    "paper_002: missing title, authors (empty list), "
                    "abstract (empty), doi (empty)\n"
                ),
            },
        ),
        _tool_call_response_with_args(
            "validate_task_result",
            {
                "expected_outputs": '["FINDINGS.md"]',
                "success_criteria": (
                    "Read manifest, identified paper_002 missing fields, "
                    "wrote FINDINGS.md."
                ),
            },
        ),
        ChatResponse(content=[TextBlock(text="Task complete.")], is_last=True),
    ]
    result = _run_worker(work, contract, ScriptedFakeModel(script))
    assert result.status == TaskStatus.completed
    assert result.react_iter_count is None


def test_awaiting_human_record_carries_actionable_context(
    tmp_path: Path,
) -> None:
    context = ResearchProgramContext(
        blueprint_path=tmp_path / "blueprint.json",
        final_review_path=tmp_path / "review.md",
        coverage_root=tmp_path / "coverage",
        work_dir=tmp_path,
    )
    events = tmp_path / "EVENTS.jsonl"
    # P1-5: mirror the real logger exactly — key is `tool` (never
    # `tool_name`), and every call emits a start row plus a result row.
    # The context loader must NOT be counted (substring matching used to
    # misfire on the "search" inside its name).
    rows = [
        {"event": "tool_call", "tool": "read_review_sections_batch"},
        {"event": "tool_result", "tool": "read_review_sections_batch"},
        {"event": "tool_call", "tool": "inspect_research_evidence_batch"},
        {"event": "tool_result", "tool": "inspect_research_evidence_batch"},
        {"event": "tool_call", "tool": "load_research_program_context"},
        {"event": "tool_result", "tool": "load_research_program_context"},
    ]
    events.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "RESEARCH_OPPORTUNITY_MAP.json").write_text(
        json.dumps({"opportunities": [{"id": "o1"}, {"id": "o2"}]}),
        encoding="utf-8",
    )
    reason = (
        "max_iters=5 exceeded (react_iter=5; iter_count separately "
        "counts completed model calls)"
    )
    result = ResultManifest(
        run_id="r",
        task_id="research_program",
        status=TaskStatus.budget_exhausted,
        stop_reason=reason,
        iter_count=3,
        react_iter_count=5,
        output_paths={"events": str(events)},
    )
    final = _finalize_discovery_needs_more_literature(
        context,
        result,
        discovery_stage="opportunity",
        effective_max_iters=5,
    )
    status = json.loads(
        (tmp_path / "R5_DISCOVERY_STATUS.json").read_text(encoding="utf-8"),
    )
    assert status["iter_model_calls"] == 3
    assert status["react_iter_count"] == 5
    assert status["effective_max_iters"] == 5
    assert status["discovery_stage"] == "opportunity"
    artifacts = status["accepted_artifacts"]
    assert artifacts["opportunity_map_accepted"] is True
    assert artifacts["opportunity_count"] == 2
    batches = status["evidence_batches_tried"]
    assert batches["tracked"] is True
    # One call each — start+result pairs are not double counted, and the
    # context loader is not mistaken for an evidence read.
    assert batches["tool_call_counts"] == {
        "read_review_sections_batch": 1,
        "inspect_research_evidence_batch": 1,
    }
    assert "load_research_program_context" not in batches["tool_call_counts"]
    assert "focus_gate_missing" in status["gap_description"]
    assert status["gap_description"].strip() != ""
    assert final.status == TaskStatus.waiting_for_human
    md = (tmp_path / "RESULT.md").read_text(encoding="utf-8")
    assert "awaiting_human_review" in md


def test_needs_more_literature_still_maps_to_waiting_for_human() -> None:
    source = (PROJECT_ROOT / "run_review_harness.py").read_text(
        encoding="utf-8",
    )
    mapping = re.search(
        r"if result\.status == \"completed\":.*?return 3.*?return 1",
        source,
        re.S,
    )
    assert mapping is not None
    block = mapping.group(0)
    for literal in ("awaiting_human_review", "partial", "needs_more_literature"):
        assert literal in block


def test_max_iters_unchanged_for_hypothesis_and_focus() -> None:
    runner_source = inspect.getsource(run_research_program)
    assert '"opportunity": 5' in runner_source
    assert '"hypothesis": 4' in runner_source
    assert '"focus": 4' in runner_source


# ---------------------------------------------------------------------------
# P1-5 — caliber tests against the real reference-run artifact (read-only).
# ---------------------------------------------------------------------------

REF_EVENTS_JSONL = (
    PROJECT_ROOT
    / "outputs"
    / "research_harness_e2e"
    / "rhr_metasurface_broadband_20260823"
    / "research_program"
    / "EVENTS.jsonl"
)


def test_evidence_batches_caliber_on_real_reference_artifact(
    tmp_path: Path,
) -> None:
    """The counting must be right on the REAL log, not just my fixtures."""

    if not REF_EVENTS_JSONL.is_file():
        pytest.skip("reference-run EVENTS.jsonl not present on this machine")
    result = ResultManifest(
        run_id="ref",
        task_id="research_program",
        status=TaskStatus.pending,
        output_paths={"events": str(REF_EVENTS_JSONL)},
    )
    from optomind_research.runtime.research_program_runner import (
        _discovery_evidence_batches,
    )

    batches = _discovery_evidence_batches(tmp_path, result)
    assert batches == {
        "tracked": True,
        "tool_call_counts": {"read_review_sections_batch": 1},
    }
