"""Explicit BODY attachment; synthetic ledgers and clients, no network."""
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
from contextlib import closing

import pytest

from scripts.upgrade3 import legacy_unit_writer as cli
from optomind_research.runtime.upgrade3.dual_budget import DualBudgetLedger, open_account_ledger
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.legacy_unit_route import _profile


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {name: sorted(db.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def book():
    return {"language": "en", "source_aliases": {}, "chapters": [{
        "chapter_id": "CH1", "chapter_frame": {"chapter_title": "Synthetic chapter"},
        "sources": [{"source_handle": "P0001", "title": "Synthetic evidence", "study_summary_A": {"text": "Synthetic material"}}],
        "units": [{"unit_id": "U1", "focus": "Synthetic task", "paragraph_tasks": [{
            "paragraph_id": "p1", "point": "Explain", "source_uses": [{"source_handle": "P0001", "role": "support"}]}],
            "table_tasks": []}]}]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Shared project attachment tests must not use network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def project(tmp_path):
    project_path, account_path = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    mapping = tmp_path / "original-project-mappings"
    pair = DualBudgetLedger(runtime.GlobalBudgetLedger(path=project_path, limit_cny=80),
                            open_account_ledger(account_path, 300), mapping)
    settled = pair.reserve(12, "synthetic-planning-settled")
    pair.settle(settled["reservation_id"], 7, telemetry={"request_id": "synthetic-id", "usage": {"prompt_tokens": 123}})
    uncertain = pair.reserve(9, "synthetic-planning-unknown")
    pair.settle(uncertain["reservation_id"], None, uncertain=True)
    pair.reserve(5, "synthetic-planning-in-flight")
    for path in (project_path, account_path):
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO budget_meta VALUES ('retain_metadata','original project metadata')")
            db.commit()
    binding = {"schema_version": "project_body_budget.v1", "project_scope": "synthetic-project",
               "question_sha256": hashlib.sha256(b"synthetic question").hexdigest(),
               "project_ledger": str(project_path), "project_limit_cny": 80,
               "account_ledger": str(account_path), "account_limit_cny": 300,
               "account_mapping_dir": str(mapping), "predecessor_binding_sha256": "a" * 64,
               "retained_metadata": {"purpose": "continuation, not a new pool"}}
    source = dump(tmp_path / "immutable-project.json", binding)
    args = cli.parser().parse_args(["--input", str(dump(tmp_path / "book.json", book())),
        "--output-dir", str(tmp_path / "body"), "--run", "--ledger", str(project_path),
        "--budget-limit", "80", "--budget-scope", "synthetic-project", "--account-ledger", str(account_path),
        "--shared-project-binding", str(source), "--no-quality-control", "--no-article-edit"])
    return args, source, binding, pair


def test_normal_harness_attaches_existing_ledgers_and_restores_without_reset(project, monkeypatch, tmp_path):
    import run_review_harness as harness
    args, source, binding, _ = project
    before = {Path(args.ledger): database(Path(args.ledger)), Path(args.account_ledger): database(Path(args.account_ledger))}
    mapping = Path(binding["account_mapping_dir"])
    receipts = {p.name: p.read_bytes() for p in mapping.glob("*.json")}
    calls = []
    def fake_factory(options, **kwargs):
        assert options.budget_ledger == args.ledger
        assert options.account_ledger == args.account_ledger
        assert options.account_budget_limit == 300
        assert options.account_mapping_dir == str(mapping)
        def factory(role, stage, profile):
            def client(messages, **kwargs):
                calls.append(messages)
                return {"content": json.dumps({"body_markdown": "SYNTHETIC TEST ONLY: evidence [P0001].", "issues": []}),
                        "finish_reason": "stop", "complete": True, "usage": {}}
            return client
        return factory
    monkeypatch.setattr(cli, "make_live_factory", fake_factory)
    monkeypatch.setattr(cli, "tokenizer_counter", lambda *_: (lambda *_: 100, {"offline": True}))
    argv = ["--delivery-start", "body", "--delivery-input", args.input,
            "--delivery-out", str(tmp_path / "formal"), "--run", "--ledger", args.ledger,
            "--budget-limit", "80", "--budget-scope", "synthetic-project", "--account-ledger", args.account_ledger,
            "--shared-project-binding", str(source), "--no-quality-control", "--no-article-edit", "--body-version", "baseline"]
    assert harness._main_with_args(harness.build_parser().parse_args(argv)) == 0
    assert len(calls) == 1
    output = tmp_path / "formal/body"
    attachment = read(output / "SHARED_PROJECT_ATTACHMENT.json")
    assert attachment["source_binding"] == binding
    assert attachment["budget_binding"]["project_binding_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    snapshot = read(output / "RUN_REPORT.json")["ledger"]
    assert (snapshot["limit_cny"], snapshot["actual_cny"], snapshot["reserved_cny"], snapshot["remaining_cny"]) == (80, 7, 14, 59)
    assert harness._main_with_args(harness.build_parser().parse_args(argv)) == 0
    assert len(calls) == 1
    assert {path: database(path) for path in before} == before
    assert {p.name: p.read_bytes() for p in mapping.glob("*.json")} == receipts
    assert not Path(args.ledger + ".legacy-route.json").exists()


def test_question_hash_compatibility_is_exact_and_project_scope_has_priority(project, monkeypatch):
    args, source, binding, _ = project
    monkeypatch.setattr(cli, "make_live_factory", lambda *_a, **_k: lambda *_: None)
    # project_scope is authoritative even when a separate question hash exists.
    cli._dedicated_factory(args, book(), None)
    binding.pop("project_scope")
    dump(source, binding)
    args.output_dir = str(source.parent / "hash-bound-output")
    with pytest.raises(ValueError, match="shared_project_scope_conflict"):
        cli._dedicated_factory(args, book(), None)
    args.budget_scope = binding["question_sha256"]
    factory = cli._dedicated_factory(args, book(), None)
    assert factory.ledger_snapshot()["limit_cny"] == 80


@pytest.mark.parametrize("bad", ["no_authorization", "scope", "project_cap", "account_cap", "account_path", "mapping",
                                    "missing_project", "corrupt_project", "missing_account", "corrupt_account",
                                    "missing_mapping_field", "stored_cap", "invalid_hold"])
def test_invalid_attachment_refuses_before_factory_and_never_initializes_budget(project, monkeypatch, bad):
    args, source, binding, _ = project
    monkeypatch.setattr(cli, "make_live_factory", lambda *_a, **_k: pytest.fail("Factory reached before invalid attachment rejected"))
    if bad == "no_authorization":
        args.shared_project_binding = None
    elif bad == "scope":
        args.budget_scope = "another-project"
    elif bad == "project_cap":
        args.budget_limit = 81
    elif bad == "account_cap":
        args.account_budget_limit = 301
    elif bad == "account_path":
        args.account_ledger = str(source.parent / "other.sqlite")
    elif bad == "mapping":
        args.account_mapping_dir = str(source.parent / "different-mappings")
    elif bad in {"missing_project", "missing_account"}:
        Path(args.ledger if bad == "missing_project" else args.account_ledger).unlink()
    elif bad in {"corrupt_project", "corrupt_account"}:
        Path(args.ledger if bad == "corrupt_project" else args.account_ledger).write_bytes(b"invalid SQLite, must remain untouched")
    elif bad == "missing_mapping_field":
        binding.pop("account_mapping_dir")
        dump(source, binding)
    else:
        with closing(sqlite3.connect(args.ledger)) as db:
            if bad == "stored_cap":
                db.execute("UPDATE budget_meta SET value='81' WHERE key='limit_cny'")
            else:
                db.execute("UPDATE reservations SET status='unknown_status' WHERE status='uncertain'")
            db.commit()
    paths = [Path(args.ledger), Path(binding["account_ledger"])]
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    with pytest.raises(ValueError):
        cli._dedicated_factory(args, book(), None)
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not Path(args.ledger + ".legacy-route.json").exists()
    assert not (Path(args.output_dir) / "SHARED_PROJECT_ATTACHMENT.json").exists()


def test_real_lazy_factory_keeps_native_dual_ledger_and_all_holds_enforce_cap(project, monkeypatch):
    args, _, _, pair = project
    pair.reserve(59, "synthetic-fill-remaining-budget")
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda *_: ["synthetic-key"])
    factory = cli._dedicated_factory(args, book(), lambda *_: 100)
    callback = factory("writer", Path(args.output_dir) / "stage", _profile("qwen3.5-plus", 256, 0))
    assert factory.ledger_snapshot()["remaining_cny"] == 0
    with pytest.raises(runtime.QwenTransportError, match="global_budget_exceeded"):
        callback([{"role": "user", "content": "synthetic test"}], call_id="synthetic-body-blocked")
    assert factory.ledger_snapshot()["reserved_cny"] == 73
    assert factory.ledger_snapshot()["actual_cny"] == 7


def test_same_output_rejects_changed_immutable_attachment(project, monkeypatch):
    args, source, binding, _ = project
    monkeypatch.setattr(cli, "make_live_factory", lambda *_a, **_k: lambda *_: None)
    cli._dedicated_factory(args, book(), None)
    before = database(Path(args.ledger))
    binding["retained_metadata"]["purpose"] = "changed source binding"
    dump(source, binding)
    with pytest.raises(ValueError, match="shared_project_output_binding_conflict"):
        cli._dedicated_factory(args, book(), None)
    assert database(Path(args.ledger)) == before
