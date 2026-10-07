"""Formal prepare/run recovery at the real HTTP serialization + SQLite boundary.

Only HTTP and local tokenization are controlled; no credential reads or paid calls.
"""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.upgrade3 import outline_strengthening as cli
from optomind_research.runtime.upgrade3 import outline_on_demand as od
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3.module4 import runtime


def _payload():
    return strengthening.build_strengthening_payload(
        research_question="Compare supplied studies and their conditions.", chapter_id="CH01",
        chapter_plan={"units": [{"unit_id": "CH01_U01", "substantive_point": "Compare under supplied conditions",
                                  "source_handles": ["P0001"]}]},
        source_materials=[{"source_handle": "P0001", "paper_id": "paper-1", "title": "Study 1",
                           "study_summary_A": {"key_findings": [{"finding": "Measured finding", "conditions": "Original condition"}]}}],
        modifiable_unit_ids=["CH01_U01"], call_id="formal-offline",
    )


class Response:
    status = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, model, content, stream):
        usage = {"prompt_tokens": 100, "completion_tokens": 10}
        if stream:
            events = [
                {"id": "offline", "model": model, "choices": [{"delta": {"content": json.dumps(content)}, "finish_reason": "stop"}]},
                {"id": "offline", "choices": [], "usage": usage},
            ]
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        else:
            data = json.dumps({"id": "offline", "model": model, "choices": [{"message": {"content": json.dumps(content)},
                                                                                  "finish_reason": "stop"}], "usage": usage})
        self.data = io.BytesIO(data.encode())

    def read(self):
        return self.data.read()

    def readline(self):
        return self.data.readline()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opener:
    def __init__(self):
        self.calls = []
        self.invalid_owner = False
        self.owner_contents = []

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.calls.append({"body": body, "timeout": timeout, "bytes": request.data})
        content = {"status": "no_change", "material_requests": []} if body["model"] == "qwen3.5-plus" else (
            {"status": "updated"} if self.invalid_owner else {"status": "no_change", "chapter_updates": []})
        if body["model"] == "qwen3.8-max" and self.owner_contents:
            content = self.owner_contents.pop(0)
        return Response(body["model"], content, body["stream"])


@pytest.fixture
def formal(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_payload()), encoding="utf-8")
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("offline tokenizer asset", encoding="utf-8")
    args = cli.build_parser().parse_args([
        "--input", str(input_path), "--output", str(tmp_path / "out"), "--mode", "on_demand",
        "--budget-ledger", str(tmp_path / "budget.sqlite"), "--budget-limit", "20",
        "--key-file", str(tmp_path / "unused-key"), "--tokenizer", str(tokenizer),
    ])
    opener = Opener()
    monkeypatch.setattr(cli.planning, "qwen_local_token_counter", lambda _: lambda *_: 100)
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda _: ["offline-placeholder"])
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")
    return args, opener


def _rows(args):
    with sqlite3.connect(args.budget_ledger) as connection:
        return connection.execute("select call_id,amount_cny,actual_cny,status,request_metadata_json from reservations order by created_at,reservation_id").fetchall()


def test_formal_policy_on_wire_meter_and_budget_pause_resume(formal):
    args, opener = formal
    args.budget_limit = 2.0
    prepared = cli.prepare_on_demand(args)
    assert prepared["execution_policy"] == {"stream": True, "timeout_seconds": 1800.0, "stream_overall_timeout_seconds": 3600.0}
    with pytest.raises(runtime.QwenTransportError, match="global_budget_exceeded"):
        cli.run_on_demand_cli(args)
    assert len(opener.calls) == 1
    access_bytes = (Path(args.output) / "stages" / "ACCESS_STAGE.json").read_bytes()
    before = _rows(args)
    assert len(before) == 1 and before[0][3] == "settled"
    assert before[0][1] == pytest.approx(prepared["access_estimate"]["estimated_cost_cny"])
    import hashlib
    assert prepared["access_estimate"]["wire_body_sha256"] == hashlib.sha256(opener.calls[0]["bytes"]).hexdigest()
    # Test-only simulation of an explicitly authorized extension in this isolated ledger.
    with sqlite3.connect(args.budget_ledger) as connection:
        connection.execute("update budget_meta set value='4.0' where key='limit_cny'")
    args.budget_limit = 4.0
    report = cli.run_on_demand_cli(args)
    assert report["status"] == "no_change" and report["access_reused"] is True
    assert (Path(args.output) / "stages" / "ACCESS_STAGE.json").read_bytes() == access_bytes
    assert len(opener.calls) == 2
    assert len(_rows(args)) == 2 and all(row[3] == "settled" for row in _rows(args))
    assert Path(report["arrangement_input"]).is_file()
    for call, row, role in zip(opener.calls, _rows(args), ("access", "owner")):
        assert call["body"]["stream"] is True and call["timeout"] == 1800
        assert call["body"]["thinking_budget"] == 32768
        assert call["body"]["max_completion_tokens"] == 65536
        events = json.loads(row[4])["transport_events"]
        opened = next(event for event in events if event["stage"] == "request_open_start")
        assert opened["overall_timeout_seconds"] == 3600
        execution = report["execution_provenance"][role]
        assert execution["original_execution"]["policy"]["timeout_seconds"] == 1800
        assert execution["original_execution"]["source"] == "observed_transport"
    assert report["execution_provenance"]["access"]["requested_policy_exercised_this_run"] is False
    assert report["execution_provenance"]["owner"]["requested_policy_exercised_this_run"] is True


@pytest.mark.parametrize("drift", ["owner_prompt", "json_mode", "profile_timeout", "transport", "tokenizer"])
def test_prepare_drift_blocks_before_dispatch(formal, monkeypatch, drift):
    args, opener = formal
    cli.prepare_on_demand(args)
    error = "prepared_profile_changed"
    if drift == "owner_prompt":
        original = od.owner_messages
        def changed(*positional, **keywords):
            messages = original(*positional, **keywords)
            messages[0]["content"] += "\nOffline contract change"
            return messages
        monkeypatch.setattr(od, "owner_messages", changed)
        error = "prepared_owner_messages_changed"
    elif drift in {"json_mode", "profile_timeout"}:
        original = cli.load_quality_profile
        def changed(role):
            profile = original(role)
            if role == args.profile:
                profile["json_mode" if drift == "json_mode" else "timeout_seconds"] = True if drift == "json_mode" else 1234
            return profile
        monkeypatch.setattr(cli, "load_quality_profile", changed)
    elif drift == "transport":
        args.on_demand_stream = False
        error = "prepared_execution_policy_changed"
    else:
        Path(args.tokenizer).write_text("changed tokenizer", encoding="utf-8")
        error = "prepared_metering_changed"
    with pytest.raises(SystemExit, match=error):
        cli.run_on_demand_cli(args)
    assert opener.calls == []
    assert not Path(args.budget_ledger).exists()


@pytest.mark.parametrize("raw_only", [False, True])
def test_transport_change_reuses_semantic_result_with_original_policy(formal, raw_only):
    args, opener = formal
    args.on_demand_stream = False
    args.on_demand_request_timeout = 900
    cli.prepare_on_demand(args)
    first = cli.run_on_demand_cli(args)
    assert first["status"] == "no_change"
    if raw_only:
        for path in (Path(args.output) / "stages").glob("*STAGE*.json"):
            if "RAW" not in path.name:
                path.unlink()
    args.on_demand_stream = True
    args.on_demand_request_timeout = 1800
    cli.prepare_on_demand(args)
    resumed = cli.run_on_demand_cli(args)
    assert resumed["status"] == "no_change" and len(opener.calls) == 2
    assert len(_rows(args)) == 2
    for role in ("access", "owner"):
        provenance = resumed["execution_provenance"][role]
        assert provenance["original_execution"] == first["execution_provenance"][role]["original_execution"]
        assert provenance["original_execution"]["policy"]["stream"] is False
        assert provenance["original_execution"]["policy"]["timeout_seconds"] == 900
        assert provenance["requested_policy"]["stream"] is True
        assert provenance["matches_requested_policy"] is False
        assert provenance["requested_policy_exercised_this_run"] is False


def test_legacy_checkpoint_reuses_without_inventing_transport_evidence(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    for path in (Path(args.output) / "stages").glob("*.json"):
        stage = json.loads(path.read_text())
        stage.pop("execution", None)
        raw = (stage.get("result") or {}).get("raw_response", stage.get("raw_response", {}))
        raw.pop("effective_request", None)
        raw.pop("transport_events", None)
        path.write_text(json.dumps(stage))
    result = cli.run_on_demand_cli(args)
    assert len(opener.calls) == 2
    for role in ("access", "owner"):
        execution = result["execution_provenance"][role]
        assert execution["original_execution"]["source"] == "legacy_response_only"
        assert execution["matches_requested_policy"] is None
        assert execution["requested_policy_exercised_this_run"] is False


def test_scientific_change_invalidates_and_failed_attempt_preserves_success(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    stages = Path(args.output) / "stages"
    valid = (stages / "OWNER_STAGE.last_valid.json").read_bytes()
    updated = _payload()
    updated["chapter_plan"]["units"][0]["substantive_point"] = "New scientific duty"
    updated["input_integrity"] = strengthening._input_integrity(updated)
    Path(args.input).write_text(json.dumps(updated))
    opener.invalid_owner = True
    cli.prepare_on_demand(args)
    assert cli.run_on_demand_cli(args)["status"] == "unresolved"
    assert len(opener.calls) == 4
    assert (stages / "OWNER_STAGE.last_valid.json").read_bytes() == valid
    assert any(json.loads(path.read_text()).get("result", {}).get("status") == "no_change"
               for path in (stages / "history").glob("OWNER_STAGE.*.json"))


def test_checkpoint_replace_failure_leaves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "stage.json"
    od._write_checkpoint(path, {"old": "complete"})
    before = path.read_bytes()
    monkeypatch.setattr(od.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("interrupted replace")))
    with pytest.raises(OSError, match="interrupted replace"):
        od._write_checkpoint(path, {"new": "complete"})
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_owner_stage_recovers_last_valid_without_new_call(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    (Path(args.output) / "stages" / "OWNER_STAGE.json").write_text("{truncated")
    report = cli.run_on_demand_cli(args)
    assert report["status"] == "no_change" and report["owner_reused"]
    assert len(opener.calls) == 2


def test_explicit_retry_replaces_one_invalid_owner_and_keeps_access(formal):
    args, opener = formal
    opener.invalid_owner = True
    cli.prepare_on_demand(args)
    assert cli.run_on_demand_cli(args)["status"] == "unresolved"
    assert len(opener.calls) == 2
    access = (Path(args.output) / "stages" / "ACCESS_STAGE.json").read_bytes()
    opener.invalid_owner = False
    assert cli.run_on_demand_cli(args)["status"] == "unresolved"
    assert len(opener.calls) == 2  # Ordinary recovery never silently pays to retry.
    args.retry_unresolved = True
    report = cli.run_on_demand_cli(args)
    assert report["status"] == "no_change" and report["access_reused"] is True
    assert len(opener.calls) == 3 and len(_rows(args)) == 3
    assert (Path(args.output) / "stages" / "ACCESS_STAGE.json").read_bytes() == access
    archived = list((Path(args.output) / "stages" / "history").glob("*.rejected-*.json"))
    assert {path.name.split(".rejected-")[0] for path in archived} == {"OWNER_STAGE", "OWNER_RAW_STAGE"}
    assert cli.run_on_demand_cli(args)["status"] == "no_change"
    assert len(opener.calls) == 3  # The flag never discards a valid stage.


def test_explicit_retry_does_not_loop_on_another_invalid_owner(formal):
    args, opener = formal
    opener.invalid_owner = True
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    args.retry_unresolved = True
    assert cli.run_on_demand_cli(args)["status"] == "unresolved"
    assert len(opener.calls) == 3


def test_explicit_retry_of_invalid_selector_is_bounded(formal):
    args, opener = formal
    opener.invalid_owner = True  # The selector also uses Max; this is invalid for either contract.
    cli.prepare_selection(args)
    assert cli.run_selection_cli(args)["status"] == "invalid"
    assert len(opener.calls) == 1
    opener.invalid_owner = False
    assert cli.run_selection_cli(args)["status"] == "invalid"
    assert len(opener.calls) == 1
    args.retry_unresolved = True
    assert cli.run_selection_cli(args)["status"] == "no_change"
    assert len(opener.calls) == 2 and len(_rows(args)) == 2
    assert cli.run_selection_cli(args)["status"] == "no_change"
    assert len(opener.calls) == 2


def test_cached_owner_closure_is_revalidated_offline_before_retry(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    path = Path(args.output) / "stages" / "OWNER_STAGE.json"
    cached = json.loads(path.read_text())
    original_execution = cached["execution"]
    cached["result"]["accepted_source_materials"] = []
    cached["result"]["status"] = "unresolved"  # Simulate an older local closure/validation bug.
    cached["result"]["structural_errors"] = ["old_material_closure_failure"]
    path.write_text(json.dumps(cached))
    args.retry_unresolved = True
    report = cli.run_on_demand_cli(args)
    result = json.loads((Path(args.output) / "RESULT.json").read_text())
    assert report["status"] == "no_change" and len(opener.calls) == 2
    assert result["accepted_source_materials"] == _payload()["source_materials"]
    assert result["on_demand_strengthening"]["owner_revalidated_from_raw"] is True
    assert report["execution_provenance"]["owner"]["original_execution"] == original_execution


def test_explicit_retry_only_replaces_invalid_continuation(formal):
    args, opener = formal
    opener.owner_contents = [
        {"status": "needs_materials", "material_requests": [{"access_id": "source_materials[0]"}]},
        {"status": "updated"},
    ]
    cli.prepare_on_demand(args)
    assert cli.run_on_demand_cli(args)["status"] == "unresolved"
    stages = Path(args.output) / "stages"
    first_owner = (stages / "OWNER_STAGE.json").read_bytes()
    args.retry_unresolved = True
    report = cli.run_on_demand_cli(args)
    assert report["status"] == "no_change" and report["access_reused"] and report["owner_reused"]
    assert len(opener.calls) == 4
    assert (stages / "OWNER_STAGE.json").read_bytes() == first_owner
    assert len(list((stages / "continuation" / "history").glob("*.rejected-*.json"))) == 2
    assert not list((stages / "history").glob("*.rejected-*.json"))


def test_retry_flag_preserves_unparsed_raw_instead_of_paying_again(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    stages = Path(args.output) / "stages"
    (stages / "OWNER_STAGE.json").unlink()
    (stages / "OWNER_STAGE.last_valid.json").unlink()
    raw_path = stages / "OWNER_RAW_STAGE.json"
    raw_stage = json.loads(raw_path.read_text())
    raw_stage["raw_response"]["content"] = "{unparsed"
    raw_path.write_text(json.dumps(raw_stage))
    before = raw_path.read_bytes()
    args.retry_unresolved = True
    with pytest.raises(cli.planning.ProgressivePlanError, match="planner_response_invalid_json"):
        cli.run_on_demand_cli(args)
    assert len(opener.calls) == 2
    assert raw_path.read_bytes() == before


def test_profile_transport_change_alone_is_semantically_compatible(formal):
    args, opener = formal
    cli.prepare_on_demand(args)
    first = cli.run_on_demand_cli(args)
    for path in (Path(args.output) / "stages").glob("*.json"):
        stage = json.loads(path.read_text())
        stage["signature"]["profile"]["timeout_seconds"] = 777
        path.write_text(json.dumps(stage))
    resumed = cli.run_on_demand_cli(args)
    assert len(opener.calls) == 2
    for role in ("access", "owner"):
        assert resumed["execution_provenance"][role]["original_execution"] == first["execution_provenance"][role]["original_execution"]


@pytest.mark.parametrize("fallback", ["matching_raw_checkpoint", "saved_parsed_response", "none"])
def test_legacy_owner_without_embedded_raw_never_forces_paid_regeneration(formal, fallback):
    args, opener = formal
    cli.prepare_on_demand(args)
    cli.run_on_demand_cli(args)
    stages = Path(args.output) / "stages"
    stage_path = stages / "OWNER_STAGE.json"
    stage = json.loads(stage_path.read_text())
    original_execution = stage.pop("execution")
    stage["result"].pop("raw_response")
    if fallback != "none":
        stage["result"]["accepted_source_materials"] = []  # Old closure bug, recoverable offline.
    if fallback != "matching_raw_checkpoint":
        (stages / "OWNER_RAW_STAGE.json").unlink()
    if fallback == "none":
        stage["result"].pop("parsed_response")
    stage_path.write_text(json.dumps(stage))
    args.retry_unresolved = True
    report = cli.run_on_demand_cli(args)
    result = json.loads((Path(args.output) / "RESULT.json").read_text())
    assert report["status"] == "no_change" and len(opener.calls) == 2
    assert result["accepted_source_materials"] == _payload()["source_materials"]
    metadata = result["on_demand_strengthening"]
    assert metadata["owner_revalidation_source"] == (None if fallback == "none" else fallback)
    execution = report["execution_provenance"]["owner"]
    assert execution["requested_policy_exercised_this_run"] is False
    if fallback == "matching_raw_checkpoint":
        assert execution["original_execution"] == original_execution
    else:
        assert execution["matches_requested_policy"] is None
        if fallback == "saved_parsed_response":
            assert execution["original_execution"]["source"] == "reconstructed_from_saved_parsed_response"
            assert result["raw_response"]["_planner_call"] is True
            assert metadata["owner_revalidated_from_raw"] is False
