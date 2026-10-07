"""Offline checks at real CLI/material/transport boundaries, never quality claims."""
from __future__ import annotations

import argparse
import copy
import json
import socket
from pathlib import Path

import pytest

from scripts.upgrade3 import writer_candidates as cli
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.writer_candidates import validate_config

ROOT = Path(__file__).resolve().parents[2]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("writer candidate CLI tests forbid network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")


@pytest.fixture
def case(tmp_path):
    arrangement = dump(tmp_path / "input/CHAPTER_ARRANGEMENT.json", {
        "chapter_id": "CH01", "units": [{"unit_id": "U1", "focus": "Retain measurement setting",
            "paragraph_tasks": [{"paragraph_id": "P1", "point": "Condition A measured 12 units",
                "development": "Keep the original experimental condition and uncertainty.",
                "source_uses": [{"source_handle": "P0001"}]}], "table_tasks": []}],
        "source_catalog": {"P0001": {"source_handle": "P0001", "paper_id": "offline-paper",
            "title": "Controlled measurement", "study_summary_A": {"key_findings": "Condition A measured 12 units; one short observation."}}},
    })
    view = dump(arrangement.parent / "ARRANGEMENT_INPUT.json", {
        "chapter_id": "CH01", "title": "Measurement boundaries", "thesis": "Conditions constrain comparison", "units": [],
    })
    response = {"blocks": [{"block_id": "one", "task_ids": ["U1::P1"],
        "body_markdown": "Condition A measured 12 units in one short observation [P0001]."}], "issues": []}
    responses = dump(tmp_path / "responses.json", {"writer": response, "editor": response})
    return {"arrangement": arrangement, "view": view, "responses": responses, "response": response}


def args(**changes):
    value = argparse.Namespace(run=False, responses=None, allow_max=False, budget_limit=None, budget_ledger=None, key_file=None)
    value.__dict__.update(changes)
    return value


def test_default_cli_profile_is_plus_first():
    parsed = cli.parser().parse_args(["--output", "unused-preview"])
    assert Path(parsed.config) == ROOT / "config/writer_candidates/balanced.json"
    assert parsed.allow_max is False
    config = validate_config(cli.read_json(parsed.config))
    for role in ("writer", "editor"):
        assert config[role]["model"] == "qwen3.5-plus"
        assert config[role]["thinking_budget"] == 16384
        assert config[role]["max_output_tokens"] == 49152


@pytest.mark.parametrize("name,writer_model,editor_model,writer_budgets,editor_budgets", [
    ("economy", "qwen3.7-flash", "qwen3.5-plus", (16384, 49152), (16384, 49152)),
    ("balanced", "qwen3.5-plus", "qwen3.5-plus", (16384, 49152), (16384, 49152)),
    ("plus_reasoning", "qwen3.5-plus", "qwen3.5-plus", (32768, 32768), (32768, 32768)),
    ("selective_max", "qwen3.5-plus", "qwen3.8-max", (16384, 49152), (32768, 65536)),
    ("quality", "qwen3.8-max", "qwen3.8-max", (32768, 65536), (32768, 65536)),
])
def test_named_profiles_have_explicit_valid_capacities(name, writer_model, editor_model, writer_budgets, editor_budgets):
    config = validate_config(cli.read_json(ROOT / f"config/writer_candidates/{name}.json"))
    assert config["editor_pass"] is True
    for role, model, budgets in (("writer", writer_model, writer_budgets), ("editor", editor_model, editor_budgets)):
        profile = config[role]
        assert profile["model"] == model
        assert (profile["thinking_budget"], profile["max_output_tokens"]) == budgets
        assert sum(budgets) <= runtime.model_pricing(model)["max_output_tokens"]
        assert profile["thinking"] is True
        assert profile["json_mode"] is False
        assert profile["stream"] is True
        assert profile["timeout_seconds"] == 900
        assert profile["stream_overall_timeout_seconds"] == 3600


def test_current_plan_relative_pointer_resolves_from_pointer_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "pipeline/revisions/approved"
    root.mkdir(parents=True)
    pointer = dump(tmp_path / "pipeline/CURRENT_PLAN.json", {"packet_root": "revisions/approved"})
    monkeypatch.chdir(tmp_path)
    assert cli.resolve_packet_root(pointer) == root
    assert cli.resolve_packet_root(pointer.parent) == root
    assert cli.resolve_packet_root(root) == root


def test_current_plan_rejects_bad_missing_and_cyclic_pointers(tmp_path):
    pointer = dump(tmp_path / "CURRENT_PLAN.json", {"packet_root": "."})
    with pytest.raises(ValueError, match="cycle"):
        cli.resolve_packet_root(pointer)
    dump(pointer, {"revision": "missing target"})
    with pytest.raises(ValueError, match="packet_root_missing"):
        cli.resolve_packet_root(pointer)
    dump(pointer, {"packet_root": "missing"})
    with pytest.raises(ValueError, match="packet_root_missing"):
        cli.resolve_packet_root(pointer)


def test_missing_explicit_tokenizer_fails_and_default_fallback_is_labeled(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_TOKENIZER", tmp_path / "missing-default.json")
    counter, report = cli.tokenizer_counter()
    assert counter is None
    assert report["mode"] == "utf8_byte_upper_bound"
    assert report["actual_provider_token_count"] is False
    with pytest.raises(ValueError, match="explicit_local_tokenizer_missing"):
        cli.tokenizer_counter(tmp_path / "explicit-missing.json")


@pytest.mark.parametrize("config_name", [None, "quality"])
def test_preview_uses_real_builder_engine_without_provider_or_key_read(tmp_path, case, monkeypatch, config_name):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline preview constructed a live provider or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    output = tmp_path / "preview"
    argv = ["--arrangement", str(case["arrangement"]), "--output", str(output),
            "--key-file", "/never/read/secret"]
    if config_name:
        argv += ["--config", str(ROOT / f"config/writer_candidates/{config_name}.json")]
    assert cli.main(argv) == 0
    report = cli.read_json(output / "CLI_RUN.json")
    assert report["execution_mode"] == "preview"
    assert report["semantic_quality_unreviewed"] is True
    assert (output / "RUN_MANIFEST.json").is_file()
    assert "Condition A" in "\n".join(p.read_text() for p in output.rglob("*.json"))
    assert "/never/read/secret" not in "\n".join(p.read_text() for p in output.rglob("*.json"))


@pytest.mark.parametrize("route", ["chapter", "units_edit", "hierarchical"])
@pytest.mark.parametrize("config_name", [None, "quality"])
def test_recordings_drive_real_route_and_resume(tmp_path, case, monkeypatch, route, config_name):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline recording mode constructed a provider or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    output = tmp_path / route
    argv = ["--arrangement", str(case["arrangement"]), "--route", route,
            "--output", str(output), "--responses", str(case["responses"])]
    if config_name:
        argv += ["--config", str(ROOT / f"config/writer_candidates/{config_name}.json")]
    assert cli.main(argv) == 0
    first = cli.read_json(output / "CLI_RUN.json")
    assert first["execution_mode"] == "recording"
    assert first["complete"] is True
    assert first["current_run_cost_cny"] == 0
    assert first["recorded_response_calls"]
    assert "Condition A measured 12" in (output / "CHAPTER_BODY.md").read_text()
    assert cli.main(argv) == 0
    resumed = cli.read_json(output / "CLI_RUN.json")
    assert resumed["recorded_response_calls"] == []
    assert resumed["complete"] is True


@pytest.mark.parametrize("changes", [
    {}, {"run": True}, {"run": True, "budget_limit": 1},
    {"run": True, "budget_ledger": "absent-ledger"},
    {"run": True, "budget_limit": float("nan"), "budget_ledger": "ledger"},
    {"run": True, "budget_limit": float("inf"), "budget_ledger": "ledger"},
    {"run": True, "budget_limit": 0, "budget_ledger": "ledger"},
    {"run": True, "budget_limit": 1, "budget_ledger": "ledger", "responses": "fixture"},
])
def test_invalid_live_gate_never_constructs_provider_or_ledger(changes, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid live gate reached runtime boundary")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    with pytest.raises(ValueError):
        cli.make_live_factory(args(**changes))


@pytest.mark.parametrize("config_name,route,fallback", [
    ("quality", "chapter", False),
    ("quality", "units_edit", False),
    ("quality", "hierarchical", False),
    ("selective_max", "units_edit", False),
    ("selective_max", "hierarchical", False),
    ("selective_max", "chapter", True),
])
def test_live_max_gate_precedes_credentials_provider_and_ledger(tmp_path, case, monkeypatch, capsys,
                                                               config_name, route, fallback):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Max permission gate reached a live provider, credentials, or ledger")
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", forbidden)
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    monkeypatch.setattr(cli, "make_live_factory", forbidden)
    output, ledger = tmp_path / "blocked", tmp_path / "shared.sqlite"
    argv = ["--arrangement", str(case["arrangement"]), "--route", route, "--output", str(output),
            "--config", str(ROOT / f"config/writer_candidates/{config_name}.json"),
            "--run", "--budget-ledger", str(ledger), "--budget-limit", "60",
            "--key-file", "/never/read/secret"]
    if fallback:
        argv.append("--fallback-hierarchical")
    assert cli.main(argv) == 2
    assert "max_execution_requires_allow_max" in capsys.readouterr().err
    assert not ledger.exists()
    assert not output.exists()


def test_live_factory_max_gate_precedes_provider_or_ledger(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Unapproved Max profile constructed a live provider or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    factory = cli.make_live_factory(args(run=True, budget_limit=60, budget_ledger=str(tmp_path / "shared.sqlite")))
    profile = cli.read_json(ROOT / "config/writer_candidates/quality.json")["writer"]
    with pytest.raises(ValueError, match="max_execution_requires_allow_max:writer"):
        factory("writer", tmp_path / "attempt", profile)
    assert not (tmp_path / "shared.sqlite").exists()


def test_recording_mode_cannot_be_reused_as_live_cache(tmp_path):
    cli._execution_context(tmp_path, {"execution_mode": "preview"})
    cli._execution_context(tmp_path, {"execution_mode": "recording"})
    with pytest.raises(ValueError, match="output_execution_mode_conflict"):
        cli._execution_context(tmp_path, {"execution_mode": "live"})
    cli._execution_context(tmp_path, {"execution_mode": "preview"})
    assert cli.read_json(tmp_path / "CLI_CONTEXT.json")["execution_mode"] == "recording"


class StreamResponse:
    status = 200
    headers = {"x-request-id": "offline-wire", "content-type": "text/event-stream"}

    def __init__(self, body, content):
        events = [
            {"id": "offline-wire", "model": body["model"], "choices": [{"delta": {"content": content}, "finish_reason": None}]},
            {"id": "offline-wire", "model": body["model"], "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"id": "offline-wire", "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        self.lines = [line for event in events for line in (b"data: " + json.dumps(event).encode() + b"\n", b"\n")]
        self.lines += [b"data: [DONE]\n", b"\n"]

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Opener:
    def __init__(self, content):
        self.content = content
        self.requests = []

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append({"body": body, "timeout": timeout})
        return StreamResponse(body, self.content)


@pytest.mark.parametrize("config_name,route,allow_max", [
    (None, "chapter", False),
    (None, "units_edit", False),
    (None, "chapter", True),  # Permission does not upgrade a Plus profile.
    ("selective_max", "chapter", False),  # Its Max editor is unused.
    ("quality", "chapter", True),
    ("selective_max", "units_edit", True),
])
def test_live_cli_uses_selected_role_profiles_on_real_wire(tmp_path, case, monkeypatch, config_name, route, allow_max):
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    opener = Opener(json.dumps(case["response"]))
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    output = tmp_path / "candidate"
    argv = ["--arrangement", str(case["arrangement"]), "--route", route, "--output", str(output),
            "--run", "--budget-ledger", str(tmp_path / "shared.sqlite"), "--budget-limit", "60"]
    if config_name:
        argv += ["--config", str(ROOT / f"config/writer_candidates/{config_name}.json")]
    if allow_max:
        argv.append("--allow-max")
    assert cli.main(argv) == 0
    config = cli.read_json(ROOT / f"config/writer_candidates/{config_name or 'balanced'}.json")
    roles = ("writer",) if route == "chapter" else ("writer", "editor")
    assert len(opener.requests) == len(roles)
    for request, role in zip(opener.requests, roles):
        wire, profile = request["body"], config[role]
        assert wire["model"] == profile["model"]
        assert wire["thinking_budget"] == profile["thinking_budget"]
        assert wire["max_completion_tokens"] == profile["thinking_budget"] + profile["max_output_tokens"]
        assert wire["enable_thinking"] is True
        assert wire["stream"] is True
        assert "response_format" not in wire
        assert request["timeout"] == 900
    report = cli.read_json(output / "CLI_RUN.json")
    assert cli.read_json(report["cli_invocation"])["allow_max"] is allow_max


def test_real_qwen_wire_stream_profile_and_shared_durable_budget(tmp_path, monkeypatch):
    profile = cli.read_json(ROOT / "config/writer_candidates/quality.json")["writer"]
    counter = lambda *_: 100
    factory = cli.make_live_factory(args(run=True, allow_max=True, budget_limit=20,
        budget_ledger=str(tmp_path / "shared.sqlite"), key_file="never-opened"), token_counter=counter)
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    opener = Opener('{"blocks":[],"issues":[]}')
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    for i, role in enumerate(("writer", "editor")):
        stage = tmp_path / f"stage-{i}"
        client = factory(role, stage, profile)
        response = runtime.invoke_client(client, [{"role": "user", "content": "Offline wire test."}], call_id=f"offline-{i}")
        assert response["complete"] is True
        assert response["execution_mode"] == "live"
    assert len(opener.requests) == 2
    for request in opener.requests:
        wire = request["body"]
        assert wire["model"] == "qwen3.8-max"
        assert wire["stream"] is True
        assert wire["enable_thinking"] is True
        assert wire["thinking_budget"] == 32768
        assert wire["max_completion_tokens"] == 98304
        assert "response_format" not in wire
        assert request["timeout"] == 900
    ledger = runtime.GlobalBudgetLedger(path=tmp_path / "shared.sqlite").as_dict()
    assert ledger["limit_cny"] == 20
    assert len(ledger["reservations"]) == 2
    assert ledger["actual_cny"] == pytest.approx(2 * (100 * 12 + 20 * 36) / 1_000_000)
    assert all(row["status"] == "settled" for row in ledger["reservations"])
    with pytest.raises(runtime.QwenTransportError, match="global_budget_limit_conflict"):
        other = cli.make_live_factory(args(run=True, allow_max=True, budget_limit=30, budget_ledger=str(tmp_path / "shared.sqlite")))
        other("writer", tmp_path / "other", profile)


def test_live_factory_rejects_effective_profile_changes(tmp_path, monkeypatch):
    profile = cli.read_json(ROOT / "config/writer_candidates/balanced.json")["writer"]
    factory = cli.make_live_factory(args(run=True, budget_limit=20, budget_ledger=str(tmp_path / "ledger.sqlite")))
    client = factory("writer", tmp_path / "attempt", profile)
    with pytest.raises(ValueError, match="effective_profile_override_mismatch:stream"):
        client([], stream=False)


def _recorded_output(tmp_path, case, *, output_name="candidate", route="chapter"):
    output = tmp_path / output_name
    code = cli.main(["--arrangement", str(case["arrangement"]), "--route", route,
                    "--output", str(output), "--responses", str(case["responses"])])
    assert code == 0
    return output


def test_explicit_assembly_preserves_complete_cross_unit_prose(tmp_path, case):
    candidate = _recorded_output(tmp_path, case)
    manifest = dump(tmp_path / "selected.json", {"review_title": "Offline route check", "chapters": [
        {"chapter_id": "CH01", "title": "Chapter one", "result_path": "candidate/CHAPTER_RESULT.json"}]})
    assert cli.main(["--assemble-manifest", str(manifest), "--output", str(tmp_path / "assembled")]) == 0
    result = cli.read_json(tmp_path / "assembled/BODY_RESULT.json")
    chapter_body = (candidate / "CHAPTER_BODY.md").read_text().strip()
    assert chapter_body in (tmp_path / "assembled/BODY.md").read_text()
    assert result["complete"] is True
    assert result["execution_mode"] == "recording"
    assert result["semantic_quality_unreviewed"] is True
    assert not list((tmp_path / "assembled").rglob("UNIT_RESULT.json"))


def test_assembly_draft_gate_preserves_previous_complete_body(tmp_path, case):
    candidate = _recorded_output(tmp_path, case)
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(candidate / "CHAPTER_RESULT.json")}]})
    output = tmp_path / "assembled"
    cli.assemble_manifest(manifest, output)
    prior = (output / "BODY.md").read_bytes()
    run = cli.read_json(candidate / "RUN_MANIFEST.json")
    run["status"] = "pending"
    dump(candidate / "RUN_MANIFEST.json", run)
    with pytest.raises(ValueError, match="assembly_pending_chapter"):
        cli.assemble_manifest(manifest, output)
    result = cli.assemble_manifest(manifest, output, allow_pending_draft=True)
    assert result["complete"] is False
    assert result["previous_complete_body_preserved"] is True
    assert (output / "BODY.md").read_bytes() == prior
    assert (output / "PENDING_BODY.md").read_text().startswith("DRAFT:")


def test_assembly_rejects_unselected_duplicate_and_legacy_results(tmp_path, case):
    candidate = _recorded_output(tmp_path, case)
    row = {"chapter_id": "CH01", "result_path": str(candidate / "CHAPTER_RESULT.json")}
    manifest = dump(tmp_path / "selected.json", {"chapters": [row, row]})
    with pytest.raises(ValueError, match="duplicate_chapter"):
        cli.assemble_manifest(manifest, tmp_path / "out")
    dump(manifest, {"chapters": [{**row, "result_path": "UNIT_RESULT.json"}]})
    with pytest.raises(ValueError, match="only_accepts_CHAPTER_RESULT"):
        cli.assemble_manifest(manifest, tmp_path / "out")
    dump(manifest, {"chapters": [row]})
    run = cli.read_json(candidate / "RUN_MANIFEST.json")
    run["selected_version"] = "some-other-run"
    dump(candidate / "RUN_MANIFEST.json", run)
    with pytest.raises(ValueError, match="not_selected_version"):
        cli.assemble_manifest(manifest, tmp_path / "out", allow_pending_draft=True)


def test_two_named_configs_respect_current_model_registry():
    from optomind_research.runtime.upgrade3.writer_candidates import validate_config
    for name, model in (("quality", "qwen3.8-max"), ("balanced", "qwen3.5-plus")):
        config = validate_config(cli.read_json(ROOT / "config/writer_candidates" / (name + ".json")))
        for role in ("writer", "editor"):
            assert config[role]["model"] == model
            assert config[role]["stream"] is True
            assert config[role]["json_mode"] is False
            assert config[role]["max_output_tokens"] + config[role]["thinking_budget"] <= runtime.model_pricing(model)["max_output_tokens"]


def test_live_cli_real_engine_qwen_stream_and_same_ledger_on_resume(tmp_path, case, monkeypatch):
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    opener = Opener(json.dumps(case["response"]))
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    output = tmp_path / "live-boundary"
    ledger_path = tmp_path / "experiment.sqlite"
    command = ["--arrangement", str(case["arrangement"]), "--route", "units_edit",
               "--output", str(output), "--run", "--budget-ledger", str(ledger_path),
               "--budget-limit", "30", "--key-file", "never-read"]
    assert cli.main(command) == 0
    assert len(opener.requests) == 2
    report = cli.read_json(output / "CLI_RUN.json")
    assert report["execution_mode"] == "live" and report["complete"] is True
    assert len(report["budget"]["reservations"]) == 2
    for reservation in report["budget"]["reservations"]:
        opening = next(event for event in reservation["telemetry"]["transport_events"] if event["stage"] == "request_open_start")
        assert opening["timeout_seconds"] == 900
        assert opening["overall_timeout_seconds"] == 3600
    assert cli.main(command) == 0
    assert len(opener.requests) == 2
    assert len(runtime.GlobalBudgetLedger(path=ledger_path).as_dict()["reservations"]) == 2
    assert "never-read" not in (output / "CLI_CONTEXT.json").read_text()


def test_failed_recorded_stage_is_not_automatically_retried(tmp_path, case):
    dump(case["responses"], {"writer": {"content": "incomplete recorded text", "complete": False, "finish_reason": "length"}})
    output = tmp_path / "partial"
    command = ["--arrangement", str(case["arrangement"]), "--output", str(output), "--responses", str(case["responses"])]
    assert cli.main(command) == 3
    assert cli.read_json(output / "CLI_RUN.json")["recorded_response_calls"] == ["writer_chapter"]
    assert cli.main(command) == 3
    assert cli.read_json(output / "CLI_RUN.json")["recorded_response_calls"] == []
    assert cli.main(command + ["--retry-failed"]) == 3
    assert cli.read_json(output / "CLI_RUN.json")["recorded_response_calls"] == ["writer_chapter"]


def _second_case(tmp_path, case, *, doi=None, paper_id="second-paper"):
    arrangement = copy.deepcopy(cli.read_json(case["arrangement"]))
    arrangement["chapter_id"] = "CH02"
    arrangement["source_catalog"]["P0001"]["paper_id"] = paper_id
    if doi:
        arrangement["source_catalog"]["P0001"]["doi"] = doi
    other_arrangement = dump(tmp_path / "input2/CHAPTER_ARRANGEMENT.json", arrangement)
    view = copy.deepcopy(cli.read_json(case["view"]))
    view["chapter_id"] = "CH02"
    other_view = dump(other_arrangement.parent / "ARRANGEMENT_INPUT.json", view)
    return {**case, "arrangement": other_arrangement, "view": other_view}


def test_assembly_rejects_same_handle_with_conflicting_doi(tmp_path, case):
    arrangement = cli.read_json(case["arrangement"])
    arrangement["source_catalog"]["P0001"]["doi"] = "10.1234/first"
    dump(case["arrangement"], arrangement)
    one = _recorded_output(tmp_path, case, output_name="one")
    other = _second_case(tmp_path, case, doi="10.1234/different")
    two = _recorded_output(tmp_path, other, output_name="two", route="units_edit")
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(one / "CHAPTER_RESULT.json")},
        {"chapter_id": "CH02", "result_path": str(two / "CHAPTER_RESULT.json")},
    ]})
    with pytest.raises(ValueError, match="source_identity_conflict:P0001"):
        cli.assemble_manifest(manifest, tmp_path / "assembled")
    assert not (tmp_path / "assembled/BODY.md").exists()


def test_assembly_same_normalized_doi_different_local_ids_and_routes_allowed(tmp_path, case):
    arrangement = cli.read_json(case["arrangement"])
    arrangement["source_catalog"]["P0001"]["doi"] = "https://doi.org/10.1234/SAME"
    dump(case["arrangement"], arrangement)
    one = _recorded_output(tmp_path, case, output_name="one")
    other = _second_case(tmp_path, case, doi="10.1234/same", paper_id="different-local-record-id")
    two = _recorded_output(tmp_path, other, output_name="two", route="units_edit")
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(one / "CHAPTER_RESULT.json")},
        {"chapter_id": "CH02", "result_path": str(two / "CHAPTER_RESULT.json")},
    ]})
    result = cli.assemble_manifest(manifest, tmp_path / "assembled")
    assert result["complete"] is True
    assert [chapter["effective_route"] for chapter in result["chapters"]] == ["chapter", "units_edit"]
    identity = result["source_identity_map"]["P0001"]
    assert identity["material_record_variants"][0]["paper_id"] == "different-local-record-id"


def test_assembly_hash_verifies_archived_full_source_input(tmp_path, case):
    candidate = _recorded_output(tmp_path, case)
    result = cli.read_json(candidate / "CHAPTER_RESULT.json")
    input_path = candidate / "inputs" / result["input_hash"] / "CHAPTER_INPUT.json"
    chapter = cli.read_json(input_path)
    chapter["sources"][0]["paper_id"] = "modified-identity"
    dump(input_path, chapter)
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(candidate / "CHAPTER_RESULT.json")}]})
    with pytest.raises(ValueError, match="assembly_input_snapshot_hash_mismatch"):
        cli.assemble_manifest(manifest, tmp_path / "assembled")


def test_assembly_missing_stable_identity_is_reported(tmp_path, case):
    arrangement = cli.read_json(case["arrangement"])
    arrangement["source_catalog"]["P0001"].pop("paper_id")
    dump(case["arrangement"], arrangement)
    candidate = _recorded_output(tmp_path, case)
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(candidate / "CHAPTER_RESULT.json")}]})
    result = cli.assemble_manifest(manifest, tmp_path / "assembled")
    assert result["identity_warnings"][0]["code"] == "source_stable_identity_missing"


def test_assembly_refuses_live_recording_mixture(tmp_path, case, monkeypatch):
    one = _recorded_output(tmp_path, case, output_name="one")
    other = _second_case(tmp_path, case, paper_id="offline-paper")
    two = tmp_path / "two"
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder"])
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: Opener(json.dumps(case["response"])))
    assert cli.main(["--arrangement", str(other["arrangement"]), "--output", str(two),
                     "--run", "--budget-ledger", str(tmp_path / "ledger.sqlite"), "--budget-limit", "20"]) == 0
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "result_path": str(one / "CHAPTER_RESULT.json")},
        {"chapter_id": "CH02", "result_path": str(two / "CHAPTER_RESULT.json")},
    ]})
    with pytest.raises(ValueError, match="assembly_mixed_execution_modes_forbidden"):
        cli.assemble_manifest(manifest, tmp_path / "assembled")


@pytest.mark.parametrize("heading,expected_count", [("Measurement boundaries", 1), ("A different heading", 2)])
def test_assembly_avoids_only_exact_duplicate_leading_heading(tmp_path, case, heading, expected_count):
    response = copy.deepcopy(case["response"])
    response["blocks"][0]["body_markdown"] = "## " + heading + "\n\n" + response["blocks"][0]["body_markdown"]
    dump(case["responses"], {"writer": response, "editor": response})
    candidate = _recorded_output(tmp_path, case)
    manifest = dump(tmp_path / "selected.json", {"chapters": [
        {"chapter_id": "CH01", "title": "Measurement boundaries", "result_path": str(candidate / "CHAPTER_RESULT.json")}]})
    result = cli.assemble_manifest(manifest, tmp_path / "assembled")
    body = Path(result["body_path"]).read_text()
    assert body.count("## ") == expected_count
    assert response["blocks"][0]["body_markdown"] in body
