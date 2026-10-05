"""Effective call settings and receipts, using only injected offline clients."""
from copy import deepcopy
import json

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from scripts.upgrade3 import review_unit_writer as writer_cli
from scripts.upgrade3 import review_feedback_loop as feedback_cli
from test_owner_condition_relation_handoff import _packet, _view, _arranged, _writer


RECEIPT = {"model": "recorded-model", "enable_thinking": True, "thinking_budget": 17,
           "answer_tokens": 29, "max_completion_tokens": 46, "response_format": "text"}
PRESSURE = {"thinking_cap_reached": True, "completion_cap_reached": False}


class Capture:
    def __init__(self, response):
        self.calls = []
        self.response = response

    def __call__(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), deepcopy(kwargs)))
        return {"content": json.dumps(self.response), "complete": True, "finish_reason": "stop",
                "effective_request": deepcopy(RECEIPT), "cap_pressure": deepcopy(PRESSURE)}


def _run(stage, tmp_path, client_options=None, **kwargs):
    view = _view(tmp_path, _packet("setting", "relation"))
    if stage == "arrangement":
        client = Capture(_arranged(view))
        for key, value in (client_options or {}).items():
            setattr(client, key, value)
        result = arranging.run_arrangement(view, client=client, model="offline", prompt="Fixture", **kwargs)
        return client.calls[-1][1], result["call"], None, None
    arranged = arranging.validate_arrangement(_arranged(view), view)
    wview = _writer(tmp_path, view, arranged)
    client = Capture({"body_markdown": "Fixture output [P0001].", "status": "appended", "covered_task_ids": ["B1"]})
    for key, value in (client_options or {}).items():
        setattr(client, key, value)
    if stage == "writer":
        result = writing.run_unit_writing(wview, client=client, model="offline", prompt="Fixture", **kwargs)
    else:
        result = writing.run_unit_completion(wview, existing_body="Original", task_ids=["B1"],
                                            client=client, model="offline", prompt="Fixture", **kwargs)
    return client.calls[-1][1], result, wview, client


@pytest.mark.parametrize("stage,thought", [("arrangement", 16384), ("writer", 8192), ("completion", 8192)])
def test_quality_defaults_reach_actual_call_and_preserve_transport_receipt(tmp_path, stage, thought):
    actual, result, _, _ = _run(stage, tmp_path)
    assert actual["max_output_tokens"] == 32768
    assert actual["thinking_budget"] == thought
    assert actual["thinking"] is True
    assert result["effective_request"] == RECEIPT
    assert result["cap_pressure"] == PRESSURE


@pytest.mark.parametrize("stage", ["arrangement", "writer", "completion"])
@pytest.mark.parametrize("kwargs,thought,enabled", [
    ({"thinking_budget": 20480, "output_tokens": 40000}, 20480, True),
    ({"thinking_budget": 0, "output_tokens": 40000}, 0, False),
    ({"thinking": False, "output_tokens": 40000}, 0, False),
])
def test_explicit_overrides_and_thinking_off_reach_actual_call(tmp_path, stage, kwargs, thought, enabled):
    actual, _, _, _ = _run(stage, tmp_path, **kwargs)
    assert actual["thinking_budget"] == thought
    assert actual["thinking"] is enabled
    assert actual["max_output_tokens"] == 40000


@pytest.mark.parametrize("stage", ["arrangement", "writer", "completion"])
@pytest.mark.parametrize("enabled", [False, True])
def test_supplied_client_settings_survive_omitted_wrapper_options(tmp_path, stage, enabled):
    actual, _, _, _ = _run(stage, tmp_path, client_options={
        "max_output_tokens": 43000, "thinking_budget": 18000, "thinking": enabled})
    assert actual["max_output_tokens"] == 43000
    assert actual["thinking_budget"] == (18000 if enabled else 0)
    assert actual["thinking"] is enabled


def test_cli_defaults_and_explicit_zero():
    args = arrangement_cli._parser().parse_args([])
    assert (args.thinking_budget, args.output_tokens) == (16384, 32768)
    args = writer_cli._parser().parse_args(["--arrangement", "unused"])
    assert (args.thinking_budget, args.output_tokens) == (8192, 32768)
    assert writer_cli._parser().parse_args(["--arrangement", "unused", "--thinking-budget", "0"]).thinking_budget == 0
    args = feedback_cli._parser().parse_args([
        "--packet", "unused", "--arrangement", "unused", "--issues", "unused", "--output-root", "unused"])
    assert (args.arrangement_thinking_budget, args.arrangement_output_tokens) == (16384, 32768)
    assert (args.writer_thinking_budget, args.writer_output_tokens) == (8192, 32768)
    assert (args.owner_thinking_budget, args.owner_output_tokens) == (16384, 32768)


@pytest.mark.parametrize("stage", ["writer", "completion"])
def test_effective_transport_metadata_is_written_without_reconstructing_from_estimates(tmp_path, stage):
    _, result, view, _ = _run(stage, tmp_path)
    if stage == "completion":
        writing.write_unit_completion(view, result, tmp_path / "output", estimate={"output_tokens": 32768}, language="en")
        path = tmp_path / "output" / "COMPLETION_RESULT.json"
    else:
        writing.write_unit_output(
            view, result["body_markdown"], tmp_path / "output", model="offline", language="en", mode="fake",
            used_messages=result["messages"], estimate={"output_tokens": 32768},
            effective_request=result["effective_request"], cap_pressure=result["cap_pressure"])
        path = tmp_path / "output" / "UNIT_RESULT.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["effective_request"] == RECEIPT
    assert saved["cap_pressure"] == PRESSURE


def test_alias_serialization_is_stable_without_changing_mapping_or_ambiguity():
    # P001 and P0001 share the short alias 1, which must remain unresolved.
    handles = ["P001", "P0001", "P0002", "P0030"]
    expected = {"001": "P001", "0001": "P0001", "0002": "P0002", "2": "P0002",
                "0030": "P0030", "30": "P0030"}
    one = writing._local_numeric_citation_map(handles)
    two = writing._local_numeric_citation_map(reversed(handles))
    assert one == two == expected
    assert "1" not in one
    assert list(one) == sorted(expected)
    assert json.dumps(one) == json.dumps(two)


def test_arrangement_cli_forwards_overrides_and_keeps_actual_receipt_on_cache_reuse(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3.module4 import runtime
    packet = _packet("setting", "relation")
    packet_root = tmp_path / "packet"
    (packet_root / "writer_packets").mkdir(parents=True)
    (packet_root / "writer_packets" / "CH01.json").write_text(json.dumps(packet))
    (packet_root / "DETAILED_REVIEW_PLAN.json").write_text(json.dumps({"shared_outline": [{"chapter_id": "CH01"}]}))
    captured = []
    class Ledger:
        def __init__(self, *args, **kwargs):
            pass
        def round_state(self):
            return {"offline": True}
    class Client:
        def __init__(self, **kwargs):
            pass
        def complete(self, messages, **kwargs):
            captured.append(kwargs)
            payload = json.loads(messages[-1]["content"])
            response = {"chapter_id": "CH01", "units": [{"unit_id": "U1", "paragraph_tasks": [
                {"paragraph_id": b["paragraph_id"], "source_briefs": [b["paragraph_id"]]}
                for b in payload["units"][0]["existing_paragraph_tasks"]]}]}
            return {"response": response, "complete": True, "finish_reason": "stop",
                    "effective_request": RECEIPT, "cap_pressure": PRESSURE}
    monkeypatch.setattr(arrangement_cli, "RoundCappedLedger", Ledger)
    monkeypatch.setattr(runtime, "QwenDirectClient", Client)
    args = ["--packet-root", str(packet_root), "--output-root", str(tmp_path / "out"),
            "--chapter", "CH01", "--run", "--planning-revision", "--output-tokens", "39000", "--thinking-budget", "0"]
    assert arrangement_cli.main(args) == 0
    path = tmp_path / "out/CH01/CHAPTER_ARRANGEMENT.json"
    first = json.loads(path.read_text())["call"]
    assert first["effective_request"] == RECEIPT
    assert first["cap_pressure"] == PRESSURE
    assert arrangement_cli.main(args) == 0
    assert json.loads(path.read_text())["call"] == first
    assert len(captured) == 1
    assert captured[0]["max_output_tokens"] == 39000
    assert captured[0]["thinking_budget"] == 0
    assert captured[0]["thinking"] is False


@pytest.mark.parametrize("stage,default_thought", [("arrangement", 16384), ("writer", 8192), ("completion", 8192)])
@pytest.mark.parametrize("configured", [None, 17000])
def test_explicit_thinking_reenables_a_disabled_client_allowance(tmp_path, stage, default_thought, configured):
    options = {"thinking": False, "thinking_budget": 0}
    if configured is not None:
        options["_configured_thinking_budget"] = configured
    actual, _, _, _ = _run(stage, tmp_path, client_options=options, thinking=True)
    assert actual["thinking"] is True
    assert actual["thinking_budget"] == (configured if configured is not None else default_thought)
