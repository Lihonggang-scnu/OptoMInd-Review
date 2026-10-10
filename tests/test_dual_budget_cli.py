"""Formal dual-budget entry tests: synthetic credentials and transport only."""
from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.dual_budget import (
    DualBudgetLedger, account_mapping_dir, open_account_ledger, read_json)
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.legacy_unit_route import _profile
from scripts.upgrade3 import legacy_unit_writer as cli


def book():
    return {"language": "en", "source_aliases": {}, "chapters": [{
        "chapter_id": "CH1", "chapter_frame": {"chapter_title": "Chapter"}, "sources": [
            {"source_handle": "P0001", "title": "Evidence", "study_summary_A": {"text": "Evidence"}}],
        "units": [{"unit_id": "U1", "focus": "Focus", "paragraph_tasks": [
            {"paragraph_id": "p1", "point": "Point", "source_uses": [
                {"source_handle": "P0001", "role": "support", "use": "Explain"}]}], "table_tasks": []}]}]}


def arguments(tmp_path, *, project_cap=60, account_cap=300, output="out", account=True):
    argv = ["--input", "unused", "--output-dir", str(tmp_path / output), "--run",
            "--ledger", str(tmp_path / "project.sqlite"), "--budget-limit", str(project_cap),
            "--budget-scope", "synthetic-scope"]
    if account:
        argv += ["--account-ledger", str(tmp_path / "account.sqlite"),
                 "--account-budget-limit", str(account_cap)]
    return cli.parser().parse_args(argv)


class Response:
    status = 200
    headers = {"x-request-id": "synthetic-request"}

    def __init__(self, body, stream, usage):
        payload = {"model": "qwen3.5-plus", "usage": usage,
                   "choices": [{"message": {"content": body}, "finish_reason": "stop"}]}
        self.raw = json.dumps(payload).encode()
        event = {**payload, "choices": [{"delta": {"content": body}, "finish_reason": "stop"}]}
        self.lines = io.BytesIO(b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n")

    def read(self):
        return self.raw

    def readline(self):
        return self.lines.readline()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, args, usage=None):
        self.args = args
        self.usage = {"prompt_tokens": 100, "completion_tokens": 20} if usage is None else usage
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        project = runtime.GlobalBudgetLedger(path=self.args.ledger).as_dict()
        assert project["reserved_cny"] > 0
        if self.args.account_ledger:
            account = runtime.GlobalBudgetLedger(path=self.args.account_ledger).as_dict()
            assert account["reserved_cny"] == project["reserved_cny"]
        assert request.get_header("Authorization") == "Bearer synthetic-first"
        wire = json.loads(request.data)
        assert wire["stream"] is True
        return Response(json.dumps({"body_markdown": "Evidence [P0001].", "issues": []}), True, self.usage)


def synthetic_key(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-keys.txt"
    path.write_text("synthetic-first\nsynthetic-second\n", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-env")
    monkeypatch.setenv("QWEN_API_KEY", "synthetic-env")
    return path


def test_formal_cli_real_factory_stream_keys_and_zero_call_resume(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    source = tmp_path / "input.json"
    source.write_text(json.dumps(book()), encoding="utf-8")
    key = synthetic_key(tmp_path, monkeypatch)
    args.key_file, args.key_index = str(key), 1
    opener = Opener(args)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_a: opener)
    monkeypatch.setattr(cli, "tokenizer_counter", lambda *_a: (lambda *_a: 100, {"offline": True}))
    argv = ["--input", str(source), "--output-dir", args.output_dir, "--run", "--ledger", args.ledger,
            "--budget-limit", "60", "--budget-scope", "synthetic-scope", "--account-ledger", args.account_ledger,
            "--account-budget-limit", "300", "--key-file", str(key), "--key-index", "1",
            "--output-tokens", "256", "--thinking-budget", "0"]
    assert cli.main(argv) == 0
    costs = [runtime.GlobalBudgetLedger(path=p).as_dict()["actual_cny"] for p in (args.ledger, args.account_ledger)]
    assert costs[0] == costs[1] > 0 and opener.calls == 1
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda *_a: pytest.fail("key read during cache replay"))
    assert cli.main(argv) == 0 and opener.calls == 1
    assert read_json(Path(args.output_dir) / "ACCOUNT_BUDGET_BINDING.json")["limit_cny"] == 300


@pytest.mark.parametrize("refused", ["project", "account"])
def test_formal_factory_each_cap_blocks_before_http(tmp_path, monkeypatch, refused):
    args = arguments(tmp_path, project_cap=0.000001 if refused == "project" else 60,
                     account_cap=0.000001 if refused == "account" else 300)
    args.key_file, args.key_index = str(synthetic_key(tmp_path, monkeypatch)), 1
    factory = cli._dedicated_factory(args, book(), lambda *_a: 100)
    opener = Opener(args)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_a: opener)
    callback = factory("writer", tmp_path / "stage", _profile("qwen3.5-plus", 64, 0))
    with pytest.raises(runtime.QwenTransportError, match="global_budget_exceeded"):
        callback([{"role": "user", "content": "offline"}], call_id="refusal")
    assert opener.calls == 0
    for path in (args.ledger, args.account_ledger):
        snapshot = runtime.GlobalBudgetLedger(path=path).as_dict()
        assert snapshot["reserved_cny"] == snapshot["actual_cny"] == 0


def test_default_factory_keeps_single_ledger_and_old_key_order(tmp_path, monkeypatch):
    args = arguments(tmp_path, account=False)
    constructed = []
    original = runtime.QwenDirectClient

    class Capture(original):
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(runtime, "QwenDirectClient", Capture)
    factory = cli._dedicated_factory(args, book(), lambda *_a: 1)
    factory("writer", tmp_path / "stage", _profile("qwen3.5-plus", 64, 0))
    assert isinstance(constructed[0]["budget_ledger"], runtime.GlobalBudgetLedger)
    assert constructed[0]["key_index"] is None
    assert not (Path(args.output_dir) / "ACCOUNT_BUDGET_BINDING.json").exists()
    monkeypatch.setattr("config.qwen_config.get_qwen_api_key_candidates_ordered",
                        lambda *_a: [{"api_key": "synthetic-env"}, {"api_key": "synthetic-file"}])
    assert original(model="qwen3.5-plus", json_mode=False)._keys() == ["synthetic-env", "synthetic-file"]


def test_cross_output_and_project_share_account_cap_and_old_mapping_format(tmp_path):
    args = arguments(tmp_path, account_cap=1)
    factory = cli._dedicated_factory(args, book(), lambda *_a: 1)
    factory("writer", tmp_path / "resumed-stage", _profile("qwen3.5-plus", 64, 0))
    project = runtime.GlobalBudgetLedger(path=args.ledger)
    total = runtime.GlobalBudgetLedger(path=args.account_ledger)
    historical = project.reserve(11, "old-key")
    project.settle(historical["reservation_id"], 10.0824572)
    # Original launcher's JSON schema is reused verbatim, including its custom directory.
    old_maps = tmp_path / "original-mappings"
    dual = DualBudgetLedger(project, total, old_maps)
    pair = dual.reserve(1, "first-key")
    dual.settle(pair["reservation_id"], 0.25, telemetry={"request_id": "known"})
    args.output_dir, args.account_mapping_dir = str(tmp_path / "second-output"), str(old_maps)
    cli._dedicated_factory(args, book(), lambda *_a: 1)
    DualBudgetLedger(project, total, old_maps).recover_settlements()
    assert project.as_dict()["actual_cny"] == pytest.approx(10.3324572)
    assert total.as_dict()["actual_cny"] == 0.25
    other = runtime.GlobalBudgetLedger(limit_cny=60, path=tmp_path / "other-project.sqlite")
    shared = DualBudgetLedger(other, total, old_maps)
    shared.recover_settlements()  # Foreign project mappings do not break recovery.
    with pytest.raises(runtime.QwenTransportError, match="global_budget_exceeded"):
        shared.reserve(1, "other-project-over-account-cap")
    assert other.as_dict()["reserved_cny"] == 0
    assert total.as_dict()["actual_cny"] == 0.25
    with pytest.raises(runtime.QwenTransportError, match="conflicting_settlement"):
        dual.settle(pair["reservation_id"], 0)


def test_unknown_receipt_and_interrupted_settlement_preserve_both_dimensions(tmp_path, monkeypatch):
    project = runtime.GlobalBudgetLedger(limit_cny=60, path=tmp_path / "project.sqlite")
    total = open_account_ledger(tmp_path / "account.sqlite", 300)
    dual = DualBudgetLedger(project, total, account_mapping_dir(total.path))
    pair = dual.reserve(1, "unknown")
    dual.settle(pair["reservation_id"], None, uncertain=True)
    resumed = DualBudgetLedger(runtime.GlobalBudgetLedger(path=project.path), open_account_ledger(total.path), dual.mapping_dir)
    resumed.recover_settlements()
    assert project.as_dict()["reserved_cny"] == total.as_dict()["reserved_cny"] == 1
    assert total.as_dict()["reservations"][0]["actual_cny"] is None
    known = dual.reserve(1, "known")
    original = total.settle
    monkeypatch.setattr(total, "settle", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(runtime.QwenTransportError, match="settlement_incomplete"):
        dual.settle(known["reservation_id"], 0.2)
    monkeypatch.setattr(total, "settle", original)
    dual.recover_settlements()
    assert project.as_dict()["actual_cny"] == total.as_dict()["actual_cny"] == 0.2
    assert total.as_dict()["reserved_cny"] == 1


def test_account_binding_forbids_reset_change_or_omission(tmp_path):
    args = arguments(tmp_path)
    cli._dedicated_factory(args, book(), lambda *_a: 1)
    changed = deepcopy(args)
    changed.account_budget_limit = 301
    with pytest.raises(ValueError, match="budget_limit_conflict"):
        cli._dedicated_factory(changed, book(), lambda *_a: 1)
    changed.account_ledger = str(tmp_path / "replacement.sqlite")
    with pytest.raises(ValueError, match="binding_conflict"):
        cli._dedicated_factory(changed, book(), lambda *_a: 1)
    assert not Path(changed.account_ledger).exists()
    omitted = arguments(tmp_path, account=False)
    with pytest.raises(ValueError, match="required_for_bound_output"):
        cli._dedicated_factory(omitted, book(), lambda *_a: 1)
    Path(args.account_ledger).unlink()
    with pytest.raises(ValueError, match="missing:no_budget_reset"):
        cli._dedicated_factory(args, book(), lambda *_a: 1)
    args.output_dir = str(tmp_path / "another-output")
    with pytest.raises(ValueError, match="missing:no_budget_reset"):
        cli._dedicated_factory(args, book(), lambda *_a: 1)


def test_formal_entry_recovers_saved_known_receipt_without_constructing_provider(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    args.account_mapping_dir = str(tmp_path / "original-mappings")
    cli._dedicated_factory(args, book(), lambda *_a: 1)
    project = runtime.GlobalBudgetLedger(path=args.ledger)
    total = runtime.GlobalBudgetLedger(path=args.account_ledger)
    dual = DualBudgetLedger(project, total, args.account_mapping_dir)
    pair = dual.reserve(1, "known-interrupted")
    original = total.settle
    monkeypatch.setattr(total, "settle", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(runtime.QwenTransportError, match="settlement_incomplete"):
        dual.settle(pair["reservation_id"], 0.2, telemetry={"request_id": "saved"})
    monkeypatch.setattr(total, "settle", original)
    monkeypatch.setattr(runtime, "QwenDirectClient", lambda *_a, **_k: pytest.fail("provider during receipt recovery"))
    args.output_dir = str(tmp_path / "second-output")
    factory = cli._dedicated_factory(args, book(), lambda *_a: 1)
    assert factory.account_ledger_snapshot()["actual_cny"] == 0.2
    assert total.as_dict()["reserved_cny"] == 0
    assert project.as_dict()["actual_cny"] == 0.2


def test_invalid_existing_account_cannot_be_reinitialized(tmp_path):
    import sqlite3
    path = tmp_path / "broken.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE budget_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO budget_meta VALUES ('limit_cny', '300')")
    with pytest.raises(ValueError, match="invalid:no_budget_reset"):
        open_account_ledger(path, 300)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reservations'").fetchone() is None


def test_index_is_file_candidate_1_based_and_has_no_fallback(tmp_path, monkeypatch):
    key = synthetic_key(tmp_path, monkeypatch)
    cls = runtime.QwenDirectClient
    assert cls(model="qwen3.5-plus", json_mode=False, key_file=key, key_index=1)._keys() == ["synthetic-first"]
    assert cls(model="qwen3.5-plus", json_mode=False, key_file=key, key_index=2)._keys() == ["synthetic-second"]
    with pytest.raises(runtime.MissingCredentialError, match="out_of_range"):
        cls(model="qwen3.5-plus", json_mode=False, key_file=key, key_index=3)._keys()
    for index in (0, -1, True):
        with pytest.raises(ValueError, match="1_based"):
            cls(model="qwen3.5-plus", key_file=key, key_index=index)
