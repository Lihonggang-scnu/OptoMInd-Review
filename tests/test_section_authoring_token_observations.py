from __future__ import annotations

import json

from optomind_research.runtime.full_review_orchestrator import (
    _authoring_runtime_observations,
    _reset_short_context_after_schema_failures,
)


def test_authoring_observations_separate_workspace_call_and_cumulative_tokens(tmp_path):
    (tmp_path / "AUTHORING_WORKSPACE.json").write_text("x" * 400, encoding="utf-8")
    (tmp_path / "COST.json").write_text(
        json.dumps({"total_input_tokens": 999}), encoding="utf-8"
    )
    events = [
        {"input_tokens": 10},
        {"prompt_tokens": 20, "error": "schema invalid"},
        {"input_tokens": 30, "detail": "schema validation failed"},
    ]
    (tmp_path / "EVENTS.jsonl").write_text(
        "\n".join(json.dumps(row) for row in events), encoding="utf-8"
    )
    result = _authoring_runtime_observations(tmp_path)
    assert result["workspace_tokens_estimated"] == 100
    assert result["model_call_input_tokens"] == {
        "count": 3, "p50": 20, "p95": 30, "max": 30
    }
    assert result["section_cumulative_input_tokens"] == 999
    assert result["schema_format_failure_count"] == 2
    assert (tmp_path / "SECTION_INPUT_TOKEN_OBSERVATIONS.json").is_file()


def test_two_schema_failures_reset_only_runtime_dialogue(tmp_path):
    (tmp_path / "AGENT_STATE.json").write_text("runtime", encoding="utf-8")
    (tmp_path / "RESULT.json").write_text("{}", encoding="utf-8")
    (tmp_path / "SECTION_DRAFT_EN.md").write_text("draft", encoding="utf-8")
    (tmp_path / "COST.json").write_text(
        json.dumps({"total_input_tokens": 1234, "estimated_cost_cny": 0.42}),
        encoding="utf-8",
    )
    (tmp_path / "EVENTS.jsonl").write_text(
        json.dumps({"prompt_tokens": 20, "error": "schema invalid"})
        + "\n"
        + json.dumps({"input_tokens": 30, "error": "schema failed"}),
        encoding="utf-8",
    )
    archive = _reset_short_context_after_schema_failures(tmp_path)
    assert archive is not None
    assert not (tmp_path / "AGENT_STATE.json").exists()
    assert (archive / "RESULT.json").exists()
    assert (tmp_path / "SECTION_DRAFT_EN.md").exists()
    assert json.loads((tmp_path / "COST.json").read_text())["total_input_tokens"] == 1234
