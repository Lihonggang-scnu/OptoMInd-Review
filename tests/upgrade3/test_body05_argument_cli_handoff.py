"""WO05 offline CLI seams: global argument stays distinct from scope/purpose.

The preview runs the actual command. Feedback tests retain the CLI adapters,
message construction, parser, validation and persisted view; only the local
token counter and model transport are controlled boundaries.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.upgrade3 import review_feedback_loop as feedback_cli


ROOT = Path(__file__).resolve().parents[2]
SCOPE = {"statement": "SCOPE_ONLY", "exclusions": ["outside this review"]}


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _packet(argument=None):
    packet = {
        "chapter_id": "CH01",
        "research_question": "Which conditions explain the findings?",
        "shared_scope": SCOPE,
        "chapter": {"chapter_id": "CH01", "title": "Comparison", "purpose": "CHAPTER_PURPOSE_ONLY"},
        "chapter_plan": {
            "chapter_id": "CH01", "thesis": "CHAPTER_THESIS_ONLY",
            "units": [{"unit_id": "CH01_U01", "substantive_point": "Compare conditions", "paragraph_briefs": [
                {"paragraph_id": "CH01_U01_P01", "point": "Compare the stated conditions", "source_handles": []},
            ]}],
        },
        "source_materials": [],
    }
    if argument is not None:
        packet.update(review_argument=argument, review_argument_status="calibrated",
                      review_argument_source="whole_plan_improvement")
    return packet


def _preview(tmp_path, plan_argument, packet_argument, *, planning_revision=False):
    packet_root = tmp_path / "plan"
    plan = {"shared_scope": SCOPE, "shared_outline": [_packet()["chapter"]]}
    if plan_argument is not None:
        plan.update(review_argument=plan_argument, review_argument_status="calibrated",
                    review_argument_source="whole_plan_improvement")
    _dump(packet_root / "DETAILED_REVIEW_PLAN.json", plan)
    _dump(packet_root / "writer_packets" / "CH01.json", _packet(packet_argument))
    output_root = tmp_path / "preview"
    command = [sys.executable, str(ROOT / "scripts/upgrade3/chapter_arrangement.py"),
               "--packet-root", str(packet_root), "--output-root", str(output_root), "--chapter", "CH01"]
    if planning_revision:
        command.append("--planning-revision")
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output_root / "ARRANGEMENT_RUN.json").read_text())
    assert report["mode"] == "preview" and report["model_calls"] == 0
    view = json.loads((output_root / "CH01/ARRANGEMENT_INPUT.json").read_text())
    return view, report


@pytest.mark.parametrize("planning_revision", [False, True])
@pytest.mark.parametrize("plan_argument,packet_argument,expected", [
    ("FINAL_CALIBRATED_ARGUMENT", "STALE_PACKET_ARGUMENT", "FINAL_CALIBRATED_ARGUMENT"),
    (None, "PACKET_CALIBRATED_ARGUMENT", "PACKET_CALIBRATED_ARGUMENT"),
    (None, None, ""),
])
def test_arrangement_preview_uses_argument_not_scope_or_purpose(
    tmp_path, planning_revision, plan_argument, packet_argument, expected,
):
    view, _ = _preview(tmp_path, plan_argument, packet_argument, planning_revision=planning_revision)
    assert view["review_argument"] == expected
    assert view["shared_scope"] == SCOPE
    assert view["purpose"] == "CHAPTER_PURPOSE_ONLY"
    assert view["review_argument_status"] == ("calibrated" if expected else "missing")
    assert view["review_argument_source"] == ("whole_plan_improvement" if expected else "missing")


@pytest.mark.parametrize("prefix", ["", "shared context " * 40], ids=["short", "different_after_source_char_limit"])
def test_same_scope_distinct_arguments_change_real_cli_cache_signature(tmp_path, prefix):
    first, first_run = _preview(tmp_path / "first", prefix + "ARGUMENT_A", None)
    second, second_run = _preview(tmp_path / "second", prefix + "ARGUMENT_B", None)
    assert first["review_argument"] == prefix + "ARGUMENT_A"
    assert second["review_argument"] == prefix + "ARGUMENT_B"
    assert first["shared_scope"] == second["shared_scope"] == SCOPE
    assert first_run["chapters"][0]["signature"] != second_run["chapters"][0]["signature"]


def _feedback_args(tmp_path, packet):
    packet_path = tmp_path / "packet.json"
    arrangement_path = tmp_path / "arrangement.json"
    issues_path = tmp_path / "issues.json"
    _dump(packet_path, packet)
    _dump(arrangement_path, {"chapter_id": "CH01", "units": []})
    _dump(issues_path, {"issues": [{"action": "chapter_owner", "problem": "Retain the global argument"}]})
    return ["--packet", str(packet_path), "--arrangement", str(arrangement_path),
            "--issues", str(issues_path), "--output-root", str(tmp_path / "feedback"),
            "--budget-ledger", str(tmp_path / "absent-budget.sqlite"),
            "--key-file", str(tmp_path / "never-read-key.txt")]


@pytest.mark.parametrize("argument", ["CALIBRATED_A", "CALIBRATED_B", None])
def test_feedback_preflight_messages_keep_packet_argument(tmp_path, monkeypatch, argument):
    args = _feedback_args(tmp_path, _packet(argument))
    monkeypatch.setattr(feedback_cli.planning, "qwen_local_token_counter", lambda _path: lambda *_: 100)
    original = feedback_cli.arranging.arrangement_messages
    messages_path = tmp_path / "OBSERVED_PREFLIGHT_MESSAGES.json"

    def observe_messages(*args, **kwargs):
        messages = original(*args, **kwargs)
        _dump(messages_path, messages)
        return messages

    def unexpected_client(**_kwargs):
        pytest.fail("offline preview attempted model-client construction")

    monkeypatch.setattr(feedback_cli.arranging, "arrangement_messages", observe_messages)
    monkeypatch.setattr(feedback_cli, "QwenDirectClient", unexpected_client)
    assert feedback_cli.main(args) == 0
    result = json.loads((tmp_path / "feedback/FEEDBACK_LOOP_RUN.json").read_text())
    assert result["status"] == "preflight_only" and result["network_calls"] == 0
    payload = json.loads(json.loads(messages_path.read_text())[-1]["content"])
    assert payload["review_argument"] == (argument or "")
    assert payload["shared_scope"] == SCOPE
    assert payload["purpose"] == "CHAPTER_PURPOSE_ONLY"
    assert payload["review_argument_status"] == ("calibrated" if argument else "missing")


@pytest.mark.parametrize("argument", ["CALIBRATED_A", "CALIBRATED_B", None])
def test_feedback_arrangement_adapter_persists_real_argument(tmp_path, monkeypatch, argument):
    packet = _packet(argument)
    args = feedback_cli._parser().parse_args(_feedback_args(tmp_path, packet))
    output = args.output_root
    _dump(output / "UPDATED_WRITER_PACKET.json", packet)
    messages_path = output / "OBSERVED_ARRANGEMENT_MESSAGES.json"

    class ControlledClient:
        def __init__(self, **_kwargs):
            pass

        def complete(self, messages, **_kwargs):
            _dump(messages_path, messages)
            payload = json.loads(messages[-1]["content"])
            return {"response": {"chapter_id": payload["chapter_id"], "units": [
                {"unit_id": unit["unit_id"], "paragraph_tasks": [
                    {"paragraph_id": brief["paragraph_id"], "source_briefs": [brief["paragraph_id"]]}
                    for brief in unit["existing_paragraph_tasks"]
                ]} for unit in payload["units"]
            ]}, "complete": True, "finish_reason": "stop"}

    monkeypatch.setattr(feedback_cli, "QwenDirectClient", ControlledClient)
    # Exercise the actual adapter without initializing unrelated live owner/tool clients.
    loop = object.__new__(feedback_cli.FeedbackLoop)
    loop.args, loop.ledger, loop.counter = args, None, lambda *_: 100
    result = loop.arrangement_runner(packet, {}, output)
    assert result["validation"]["ok"], result["validation"]
    view = json.loads((output / "ARRANGEMENT_INPUT.json").read_text())
    payload = json.loads(json.loads(messages_path.read_text())[-1]["content"])
    assert view["review_argument"] == payload["review_argument"] == (argument or "")
    assert view["shared_scope"] == payload["shared_scope"] == SCOPE
    assert view["purpose"] == payload["purpose"] == "CHAPTER_PURPOSE_ONLY"
    assert view["review_argument_status"] == ("calibrated" if argument else "missing")
