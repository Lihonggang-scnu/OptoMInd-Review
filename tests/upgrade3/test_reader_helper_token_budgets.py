"""Offline effective-budget contracts; replace only model clients, never call providers."""
import inspect
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import paper_reading_batch as batch
from optomind_research.runtime.upgrade3 import paper_reading_card as card
from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3.module4 import runtime
from test_planning_supplement_attempt_contract import AcquisitionBoundary, CardModelBoundary, supplement_inputs


@pytest.fixture
def captured_clients(tmp_path, monkeypatch):
    calls, constructors = [], []

    class Client:
        def __init__(self, **kwargs):
            constructors.append(kwargs)

        def complete(self, messages, **kwargs):
            calls.append({**kwargs, "messages": messages})
            if "candidate-selection" in kwargs["call_id"]:
                content = {"selected_indices": [0]}
            elif "material-interpretation" in kwargs["call_id"]:
                content = {"usable_content": "Synthetic comparison interpretation"}
            elif "material-triage" in kwargs["call_id"]:
                content = {"decision": "direct_use", "usable_content": "Synthetic result", "answers_requested_question": True,
                           "quantitative_comparisons": [{"result": "Synthetic pair"}]}
            else:
                content = {"status": "fulfilled", "useful_material": "Synthetic result"}
            return {"content": content}

    monkeypatch.setattr(runtime, "QwenDirectClient", Client)
    options = {"key_file": tmp_path / "never-read.key", "budget_ledger_path": tmp_path / "ledger.sqlite", "budget_limit_cny": 10}
    return options, constructors, calls


def assert_budget(call, answer, thinking):
    assert call["max_output_tokens"] == answer
    assert call["thinking_budget"] == thinking
    assert call["thinking"] is True


def test_fulfillment_judge_and_abstract_selector_actual_call_defaults(captured_clients, tmp_path):
    options, constructors, calls = captured_clients
    judge = supplement.run_qwen_fulfillment_judge(**options)
    judge(gap={"gap_id": "G1", "gap_question": "What changed?"}, snapshot=None, reading_text="Synthetic result",
          card={}, source_unit={"source_unit_id": "S1", "output_dir": str(tmp_path / "unit")}, plan={})
    selector = supplement.QwenCandidateSelector(**options)
    assert selector(gap={"gap_id": "G1"}, candidates=[{"title": "Synthetic study"}], limit=1) == [0]
    assert_budget(calls[0], 8192, 8192)
    assert_budget(constructors[0], 8192, 8192)
    assert_budget(calls[1], 4096, 4096)
    assert_budget(constructors[1], 4096, 4096)
    contract = supplement.planning_helper_runtime_config()
    for key, obj in (("fulfillment_judge", judge), ("candidate_selector", selector)):
        assert contract[key]["max_output_tokens"] == obj.max_output_tokens
        assert contract[key]["thinking_budget"] == obj.thinking_budget


def test_helper_overrides_reach_actual_client(captured_clients, tmp_path):
    options, constructors, calls = captured_clients
    judge = supplement.QwenFulfillmentJudge(**options, max_output_tokens=9000, thinking_budget=10000)
    judge(gap={}, snapshot=None, reading_text="Synthetic result", card={}, source_unit={"output_dir": str(tmp_path)}, plan={})
    supplement.QwenCandidateSelector(**options, max_output_tokens=6000, thinking_budget=7000)(gap={}, candidates=[{}], limit=1)
    assert_budget(calls[0], 9000, 10000)
    assert_budget(calls[1], 6000, 7000)


def test_local_triage_and_optional_interpretation_actual_defaults(captured_clients):
    options, constructors, calls = captured_clients
    gap = triage.LocalGap("G1", "Compare the conditions", intended_use="quantification")
    judge = triage.QwenLocalTriageJudge(**options)
    result = judge(gap, triage.LocalReadingBundle(gap))
    assert result["usable_content"] == "Synthetic comparison interpretation"
    assert len(calls) == 2 and judge.last_call_count == 2
    assert_budget(constructors[0], 12000, 8192)
    assert_budget(calls[0], 12000, 8192)
    assert_budget(calls[1], 8192, 8192)
    assert calls[0]["model"] == calls[1]["model"] == "qwen3.5-plus"
    ordinary_gap = triage.LocalGap("G2", "Explain mechanism")
    judge(ordinary_gap, triage.LocalReadingBundle(ordinary_gap))
    assert len(calls) == 3 and calls[-1]["model"] == "qwen3.7-flash"
    assert_budget(calls[-1], 12000, 8192)


@pytest.mark.parametrize("setting", ["max_output_tokens", "thinking_budget", "interpretation_max_output_tokens", "interpretation_thinking_budget"])
def test_local_checkpoint_keys_all_effective_budgets(captured_clients, tmp_path, setting):
    options, _, calls = captured_clients
    gap = triage.LocalGap("G1", "Compare the conditions", intended_use="quantification")
    bundle = triage.LocalReadingBundle(gap)
    first = triage.QwenLocalTriageJudge(**options)
    root = tmp_path / "cache"
    supplement._LocalJudgeCheckpoint(first, root)(gap, bundle)
    frozen = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    supplement._LocalJudgeCheckpoint(triage.QwenLocalTriageJudge(**options), root)(gap, bundle)
    assert len(calls) == 2
    second = triage.QwenLocalTriageJudge(**options, **{setting: getattr(first, setting) + 128})
    supplement._LocalJudgeCheckpoint(second, root)(gap, bundle)
    assert len(calls) == 4
    call = calls[3] if setting.startswith("interpretation_") else calls[2]
    assert call[setting.removeprefix("interpretation_")] == getattr(first, setting) + 128
    assert all(p.read_bytes() == data for p, data in frozen.items())
    assert len(list(root.glob("*/attempt-*/RESULT.json"))) == 2


def card_inputs(tmp_path):
    supplement_inputs(tmp_path / "inputs")
    snapshot = AcquisitionBoundary().factory(tmp_path / "materials").acquire({}).snapshot
    return {"snapshot_dir": snapshot.root, "plan_path": tmp_path / "inputs" / "PLAN.json"}


def test_card_defaults_actual_kwargs_preflight_and_cache(tmp_path):
    inputs = card_inputs(tmp_path)
    client = CardModelBoundary()
    output = tmp_path / "card"
    result = card.run_paper_reading_card(**inputs, output_dir=output, client=client)
    assert_budget(client.calls[0][1], 12288, 4096)
    commit = json.loads((output / "COMMIT.json").read_text())
    assert_budget(commit["runtime_config"], 12288, 4096)
    preflight = card.preflight_card(**inputs)
    assert preflight["max_output_tokens"] == 12288 and preflight["thinking_budget"] == 4096
    assert card.run_paper_reading_card(**inputs, output_dir=output, client=client)["reused"]
    before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    for changed in ({"max_output_tokens": 16000}, {"thinking_budget": 8192}):
        with pytest.raises(card.CardInputError, match="different_runtime_or_prompt"):
            card.run_paper_reading_card(**inputs, output_dir=output, client=client, **changed)
    assert len(client.calls) == 1
    assert all(p.read_bytes() == data for p, data in before.items())
    assert result["card"]["general_understanding"]


def test_card_no_thinking_cache_uses_effective_zero(tmp_path):
    inputs = card_inputs(tmp_path)
    client = CardModelBoundary()
    output = tmp_path / "card"
    card.run_paper_reading_card(**inputs, output_dir=output, client=client, thinking=False, thinking_budget=512)
    assert client.calls[0][1]["thinking_budget"] == 0
    assert card.run_paper_reading_card(**inputs, output_dir=output, client=client, thinking=False, thinking_budget=8192)["reused"]
    assert len(client.calls) == 1


def test_card_cli_and_batch_defaults_are_shared():
    from scripts.upgrade3 import paper_reading_batch as batch_cli
    single = card._parser().parse_args(["--snapshot", "s", "--plan", "p", "--output-dir", "o"])
    bulk = batch_cli._parser().parse_args(["--manifest", "m", "--output-root", "o"])
    assert single.max_output_tokens == bulk.max_output_tokens == 12288
    assert single.thinking_budget == bulk.thinking_budget == 4096
    assert inspect.signature(batch.run_paper_reading_batch).parameters["max_output_tokens"].default == 12288
    assert inspect.signature(batch.run_paper_reading_batch).parameters["thinking_budget"].default == 4096
    assert supplement.planning_helper_runtime_config()["paper_card"]["max_output_tokens"] == 12288


def test_batch_default_call_and_changed_capacity_never_overwrite_old_cards(tmp_path):
    inputs = card_inputs(tmp_path)
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"records": [{"paper_id": "fixture-paper", **{k: str(v) for k, v in inputs.items()}}]}))
    client = CardModelBoundary()
    options = dict(manifest_path=manifest, output_root=tmp_path / "batch", mode="run",
                   client_factory=lambda *args: client, initial_workers=1, max_workers=1, poll_seconds=0.01)
    batch.run_paper_reading_batch(**options)
    assert len(client.calls) == 1
    assert_budget(client.calls[0][1], 12288, 4096)
    old_cards = {p: p.read_bytes() for p in (tmp_path / "batch" / "cards").rglob("*") if p.is_file()}
    assert old_cards
    batch.run_paper_reading_batch(**options)
    assert len(client.calls) == 1
    with pytest.raises(batch.PaperReadingBatchError, match="batch_config_mismatch"):
        batch.run_paper_reading_batch(**options, thinking_budget=8192)
    assert len(client.calls) == 1
    assert all(p.read_bytes() == data for p, data in old_cards.items())


def test_card_telemetry_retains_effective_budgets_and_cap_pressure():
    metadata = {"effective_request": {"answer_tokens": 12288, "thinking_budget": 4096, "total_output_tokens": 16384},
                "cap_pressure": {"thinking": True}}
    extracted = card._extract_telemetry(metadata)
    assert all(extracted[key] == value for key, value in metadata.items())
