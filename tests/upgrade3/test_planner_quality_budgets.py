"""Offline checks of effective planner limits, cache keys and reader overrides."""
from __future__ import annotations

import copy
import inspect
import math
from dataclasses import replace

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as p
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import progressive_review_plan as cli


STAGES = ["provisional_scope", "source_routing", "chapter_proposals", "chapter_details",
          "case_groups", "whole_plan_improvement", "affected_chapter_revision"]


def adapter(tmp_path, *, thinking=16_384, output=32_768, tokens=100):
    model = object.__new__(p.QwenProgressivePlanner)
    model.model = "qwen3.5-plus"
    model.chapter_model = "qwen3.7-flash"
    model.output_tokens = output
    model.thinking_budget = thinking
    model.counter = lambda _encoding, _messages: tokens
    model.ledger = object()
    model.key_file = tmp_path / "NEVER_READ.key"
    model.output_dir = tmp_path
    model.timeout_seconds = 900
    return model


def config(tmp_path, **kwargs):
    return p.ProgressivePlannerConfig("quality", tmp_path / "POOL.jsonl", tmp_path / "PLAN.json", tmp_path / "run", **kwargs)


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("thinking,answer", [(16_384, 32_768), (27_000, 35_000), (1_024, 2_048), (0, 80)])
def test_every_stage_passes_requested_budgets_without_hidden_reductions(tmp_path, monkeypatch, stage, thinking, answer):
    seen = {}

    class CaptureClient:
        def __init__(self, **kwargs):
            seen["constructor"] = kwargs

        def __call__(self, messages, **kwargs):
            seen["call"] = kwargs
            seen["messages"] = messages
            return {"content": "{}", "finish_reason": "stop"}

    monkeypatch.setattr(runtime, "QwenDirectClient", CaptureClient)
    model = adapter(tmp_path, thinking=thinking, output=answer)
    result = model(stage, {"topic_id": "quality"})
    assert result["_planner_call"] is True
    for settings in (seen["constructor"], seen["call"]):
        assert settings["max_output_tokens"] == answer
        assert settings["thinking_budget"] == thinking
        assert settings["thinking"] is bool(thinking)
        assert settings["model"] == (model.chapter_model if stage in {"chapter_details", "case_groups"} else model.model)


def test_capacity_and_batch_signature_use_actual_limits(tmp_path, monkeypatch):
    model = adapter(tmp_path, thinking=24_000, output=36_000, tokens=1234)
    payload = {"source_materials": [], "chapter": {"chapter_id": "C1"}}
    estimate = p._chapter_details_capacity(model, payload)
    assert estimate["thinking_tokens"] == 24_000
    assert estimate["output_tokens"] == 36_000
    assert estimate["total_context"] == math.ceil(1234 * p.TOKEN_MARGIN_MULTIPLIER) + p.TOKEN_FRAMING_MARGIN + 60_000
    contracts = []
    monkeypatch.setattr(p, "_material_content_signature", lambda value: contracts.append(copy.deepcopy(value)) or "signature")
    p._chapter_details_cache_signature(payload, planner=model)
    assert contracts[0]["thinking_tokens"] == estimate["thinking_tokens"]
    assert contracts[0]["output_tokens"] == estimate["output_tokens"]
    assert contracts[0]["model"] == model.chapter_model


def test_large_budget_context_overflow_is_detected_before_client_creation(tmp_path, monkeypatch):
    model = adapter(tmp_path, thinking=24_000, output=36_000, tokens=840_000)
    monkeypatch.setattr(runtime, "QwenDirectClient", lambda **_: pytest.fail("preflight must run before constructing a client"))
    estimate = p._chapter_details_capacity(model, {})
    assert estimate["input_ok"] is True
    assert estimate["fits"] is False
    with pytest.raises(p.ProgressivePlanError, match="planner_context_preflight_exceeded"):
        model("chapter_details", {})


@pytest.mark.parametrize("stage", STAGES)
def test_stage_cache_changes_with_both_effective_limits(tmp_path, stage):
    model = adapter(tmp_path)
    engine = p.ProgressiveReviewPlanner(config(tmp_path), planner=model)
    payload = {"topic_id": "quality", "source_materials": []}
    original = engine._cache_contract(stage, payload)
    model.thinking_budget += 4096
    thinking = engine._cache_contract(stage, payload)
    model.output_tokens += 4096
    answer = engine._cache_contract(stage, payload)
    assert len({original, thinking, answer}) == 3


def test_batch_cache_changes_with_both_effective_limits(tmp_path):
    model = adapter(tmp_path)
    payload = {"source_materials": []}
    first = p._chapter_details_cache_signature(payload, planner=model)
    model.thinking_budget += 4096
    second = p._chapter_details_cache_signature(payload, planner=model)
    model.output_tokens += 4096
    third = p._chapter_details_cache_signature(payload, planner=model)
    assert len({first, second, third}) == 3


def test_quality_defaults_are_consistent_across_api_and_cli(tmp_path):
    cfg = config(tmp_path)
    args = cli.parser().parse_args(["--pool", "pool", "--plan", "plan", "--output-dir", "out"])
    defaults = inspect.signature(p.QwenProgressivePlanner).parameters
    assert cfg.thinking_budget == args.thinking_budget == defaults["thinking_budget"].default == 16_384
    assert cfg.planner_output_tokens == args.output_tokens == defaults["output_tokens"].default == 32_768
    assert cfg.reader_thinking_budget == args.reader_thinking_budget == 8192
    assert cfg.chapter_output_tokens == args.reader_output_tokens == 20_000
    custom = cli.parser().parse_args(["--pool", "pool", "--plan", "plan", "--output-dir", "out", "--reader-thinking-budget", "12000", "--reader-output-tokens", "24000"])
    assert custom.reader_thinking_budget == 12_000 and custom.reader_output_tokens == 24_000


def test_directed_cache_requires_actual_reader_limits_but_explicit_history_remains_usable(tmp_path):
    cfg = config(tmp_path)
    prior = {"paper_id": "p1", "runtime_config": p._directed_reader_settings(cfg), "question_material": [{"finding": "useful"}]}
    assert p._directed_material_runtime_compatible(prior, cfg)
    changed = replace(cfg, reader_thinking_budget=12_000, chapter_output_tokens=24_000)
    assert not p._directed_material_runtime_compatible(prior, changed)
    assert p._directed_material_runtime_compatible(prior, changed, {"p1"})
    assert prior["question_material"] == [{"finding": "useful"}]


def test_answer_floor_is_shared_by_call_capacity_and_cache(tmp_path):
    model = adapter(tmp_path, output=1, thinking=0)
    assert p._planner_call_settings(model, "chapter_details")["output_tokens"] == 64
    assert p._chapter_details_capacity(model, {})["output_tokens"] == 64
    low = p._chapter_details_cache_signature({}, planner=model)
    model.output_tokens = 64
    assert p._chapter_details_cache_signature({}, planner=model) == low


def test_planner_stage_preserves_wire_budget_and_cap_pressure_telemetry(tmp_path, monkeypatch):
    effective = {"answer_tokens": 32768, "thinking_budget": 16384, "wire_max_tokens": 49152}
    pressure = {"thinking": {"limit_hit": True}}
    monkeypatch.setattr(runtime, "QwenDirectClient", lambda **kwargs: lambda messages, **call: {
        "content": "{}", "effective_request": effective, "cap_pressure": pressure})
    record = adapter(tmp_path)("chapter_details", {})
    assert record["telemetry"]["effective_request"] == effective
    assert record["telemetry"]["cap_pressure"] == pressure
    assert record["telemetry"]["runtime_config"]["thinking_budget"] == 16384


def test_query_refinement_passes_quality_helper_limits(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import planning_retrieval_loop as loop
    seen = {}

    class CaptureClient:
        def __init__(self, **kwargs):
            seen["constructor"] = kwargs
        def __call__(self, messages, **kwargs):
            seen["call"] = kwargs
            return {"content": '{"targeted_queries":[]}'}

    def run_loop(needs, config, *, refine_queries, **kwargs):
        refine_queries(needs[0], 1, [], "", "Unresolved condition")
        return {"needs": []}

    monkeypatch.setattr(runtime, "QwenDirectClient", CaptureClient)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", lambda **kwargs: object())
    monkeypatch.setattr(loop, "run_retrieval_loop", run_loop)
    runner = p.make_retrieval_loop_runner(config(tmp_path), key_file=tmp_path / "NEVER_READ.key",
        budget_ledger_path=tmp_path / "NEVER_CREATE.sqlite", budget_limit_cny=10)
    runner(phase="query", supplement_requests=[{"gap_question": "Which condition changes the mechanism?"}], plan={"question": "Conditions"})
    for settings in (seen["constructor"], seen["call"]):
        assert settings["max_output_tokens"] == 4096
        assert settings["thinking_budget"] == 4096
