"""No network: restore-route execution, recovery, isolation and budget tests."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenTransportError
from scripts.upgrade3 import legacy_unit_writer as cli


def book(count=2):
    return {"language": "en", "source_aliases": {}, "chapters": [{
        "chapter_id": "CH1", "chapter_frame": {"chapter_title": "Chapter"}, "sources": [
            {"source_handle": "P0001", "title": "Evidence", "aliases": [], "study_summary_A": {"text": "Evidence"}}],
        "units": [{"unit_id": f"U{i}", "focus": f"Focus {i}", "paragraph_tasks": [
            {"paragraph_id": f"p{i}", "point": "Point", "source_uses": [{"source_handle": "P0001", "role": "support", "use": "Explain"}]}],
            "table_tasks": []} for i in range(count)]}]}


def counter(raw, messages):
    return 100


class Factory:
    execution_mode = "injected"
    def __init__(self, finish="stop", failure=None):
        self.calls = 0
        self.finish = finish
        self.failure = failure
    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls += 1
            if self.failure:
                raise self.failure
            assert kwargs["model"] == "qwen3.5-plus"
            payload = json.loads(messages[-1]["content"])
            assert "previous_body" not in payload
            return {"content": json.dumps({"body_markdown": "Evidence [P0001].", "issues": []}),
                    "complete": self.finish == "stop", "finish_reason": self.finish,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "model": profile["model"]}
        call.prompt_token_counter = counter
        return call


def test_preview_exact_requests_no_factory_or_keys(tmp_path):
    factory = Factory()
    result = route.run_legacy_units(book(), output_dir=tmp_path, client_factory=factory, token_counter=counter)
    assert result["model_calls"] == factory.calls == 0
    assert result["original_units"] == 2
    for row in result["units"]:
        messages = json.loads(Path(row["messages_path"]).read_text(encoding="utf-8"))
        payload = json.loads(messages[-1]["content"])
        assert payload["planning_revision_mode"] is True
        assert "body_markdown" in messages[0]["content"]
        assert "previous_body" not in payload


def test_completed_resume_never_calls_and_fake_stays_fake(tmp_path):
    factory = Factory()
    first = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert first["complete_units"] == factory.calls == 2
    second = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert factory.calls == 2
    assert all(row["cache_hit"] for row in second["units"])
    for row in second["units"]:
        saved = route._read(Path(row["attempt_dir"]) / "UNIT_RESULT.json")
        assert saved["simulated"] and saved["mode"] == "fake"
    preview = route.run_legacy_units(book(), output_dir=tmp_path, token_counter=counter)
    assert preview["execution_mode"] == "injected"
    assert preview["assembly"] == {}


def test_raw_recovery_without_charged_retry(tmp_path):
    factory = Factory()
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    (attempt / "UNIT_RESULT.json").unlink()
    (attempt / "RESULT_SEAL.json").unlink()
    recovered = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert recovered["complete_units"] == 1
    assert factory.calls == 1


def test_partial_resume_keeps_prose_and_requires_explicit_retry(tmp_path):
    factory = Factory(finish="length")
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert first["complete_units"] == 0
    second = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert factory.calls == 1
    assert second["missing_units"] == []
    assert second["units"][0]["status"] == "pending_retry_approval"
    factory.finish = "stop"
    final = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, retry_failed=True, client_factory=factory, token_counter=counter)
    assert final["complete_units"] == 1 and factory.calls == 2


def test_failure_stops_next_units_and_resume_is_not_retry(tmp_path):
    factory = Factory(failure=RuntimeError("uncertain request"))
    first = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert factory.calls == 1
    assert [r["status"] for r in first["units"]] == ["blocked", "not_started"]
    # A subsequent explicitly resumed run may attempt the never-started unit,
    # but cannot retry the uncertain first unit without --retry-failed.
    second = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert second["units"][0]["status"] == "pending_retry_approval"
    assert factory.calls == 2


def test_changed_input_profile_and_tampered_body_rejected(tmp_path):
    factory = Factory()
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    changed = book(1)
    changed["chapters"][0]["units"][0]["focus"] = "changed"
    with pytest.raises(ValueError, match="identity_changed"):
        route.run_legacy_units(changed, output_dir=tmp_path, token_counter=counter)
    with pytest.raises(ValueError, match="identity_changed"):
        route.run_legacy_units(book(1), output_dir=tmp_path, output_tokens=1000, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    saved = route._read(attempt / "UNIT_RESULT.json")
    (attempt / saved["body_path"]).write_text("historical unrelated body")
    with pytest.raises(ValueError, match="integrity_failure"):
        route.run_legacy_units(book(1), output_dir=tmp_path, token_counter=counter)


def test_capacity_preflight_blocks_without_call(tmp_path):
    factory = Factory()
    result = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory,
                                    token_counter=lambda raw, messages: 2_000_000)
    assert len(result["capacity_blocked_units"]) == 2
    assert result["model_calls"] == factory.calls == 0


@pytest.mark.parametrize("model", ["qwen3.7-flash", "qwen3.8-max", "unknown"])
def test_route_never_switches_model(tmp_path, model):
    with pytest.raises(ValueError, match="requires_explicit"):
        route.run_legacy_units(book(), output_dir=tmp_path, model=model)


def test_cli_preview_no_ledger_no_factory(tmp_path, monkeypatch):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(book()))
    monkeypatch.setattr(cli, "_dedicated_factory", lambda *a: pytest.fail("live factory in preview"))
    assert cli.main(["--input", str(source), "--output-dir", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "out" / "BUDGET.sqlite").exists()


def test_cannot_adopt_previous_experiment_ledger(tmp_path):
    ledger = tmp_path / "old.sqlite"
    GlobalBudgetLedger(limit_cny=40, path=ledger)
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "out"), "--run", "--ledger", str(ledger)])
    with pytest.raises(ValueError, match="not_owned"):
        cli._dedicated_factory(args, book(), counter)
    assert GlobalBudgetLedger(path=ledger).as_dict()["limit_cny"] == 40


def test_new_ledger_ownership_cannot_change_budget_or_book(tmp_path):
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "out"), "--run", "--ledger", str(tmp_path / "out" / "BUDGET.sqlite")])
    factory = cli._dedicated_factory(args, book(), counter)
    assert factory.execution_mode == "live"
    args.budget_limit = 29
    with pytest.raises(ValueError, match="scope_conflict"):
        cli._dedicated_factory(args, book(), counter)


@pytest.mark.parametrize("limit", [float("inf"), float("nan"), 0, -1, True])
def test_budget_limit_must_be_finite_positive(tmp_path, limit):
    with pytest.raises(ValueError, match="finite_positive"):
        route.run_legacy_units(book(), output_dir=tmp_path, budget_limit=limit)


def test_unsettled_hold_is_retained_while_independent_calls_use_remaining_cap(tmp_path, monkeypatch):
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "out"), "--run", "--retry-failed", "--ledger", str(tmp_path / "out" / "BUDGET.sqlite")])
    calls = []
    monkeypatch.setattr(cli, "make_live_factory", lambda *a, **k: lambda *a, **k: calls.append("provider"))
    factory = cli._dedicated_factory(args, book(), counter)
    ledger = GlobalBudgetLedger(limit_cny=30, path=tmp_path / "out" / "BUDGET.sqlite")
    hold = ledger.reserve(1.0, "uncertain-old-call")
    ledger.settle(hold["reservation_id"], None, uncertain=True)
    factory("writer", tmp_path / "attempt", route._profile("qwen3.5-plus", 32768, 8192))
    assert calls == ["provider"]
    assert factory.ledger_snapshot()["reserved_cny"] == 1.0
    ledger.reserve(29.0, "fills-cap")
    with pytest.raises(QwenTransportError, match="global_budget_exceeded"):
        ledger.reserve(0.01, "over-cap")


def test_local_raw_recovery_does_not_construct_provider_even_with_hold(tmp_path):
    factory = Factory()
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    (attempt / "UNIT_RESULT.json").unlink()
    class BlockedFactory:
        execution_mode = "injected"
        def __call__(self, *args, **kwargs):
            pytest.fail("Local response recovery must not construct a provider")
    recovered = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=BlockedFactory(), token_counter=counter)
    assert recovered["complete_units"] == 1 and recovered["model_calls"] == 0


def test_failed_retry_preserves_partial_and_later_complete_cache(tmp_path):
    class SequentialFactory(Factory):
        def __call__(self, role, path, profile):
            base = super().__call__(role, path, profile)
            def call(messages, **kwargs):
                result = base(messages, **kwargs)
                if self.calls == 1:
                    result.update(complete=False, finish_reason="length")
                return result
            call.prompt_token_counter = counter
            return call
    factory = SequentialFactory()
    first = route.run_legacy_units(book(), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert first["complete_units"] == 1
    factory.failure = RuntimeError("provider failed retry")
    resumed = route.run_legacy_units(book(), output_dir=tmp_path, run=True, retry_failed=True, client_factory=factory, token_counter=counter)
    assert factory.calls == 3
    assert resumed["complete_units"] == 1
    assert resumed["missing_units"] == []
    assert resumed["units"][1]["cache_hit"] is True


@pytest.mark.parametrize("corruption", ["delete", "empty", "corrupt", "cap"])
def test_existing_ledger_damage_is_not_a_new_budget(tmp_path, corruption):
    import sqlite3
    ledger = tmp_path / "budget.sqlite"
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "out"), "--run", "--ledger", str(ledger)])
    cli._dedicated_factory(args, book(), counter)
    if corruption == "delete":
        ledger.unlink()
    elif corruption == "empty":
        ledger.write_bytes(b"")
    elif corruption == "corrupt":
        ledger.write_bytes(b"corrupt-not-sqlite")
    else:
        with sqlite3.connect(ledger) as db:
            db.execute("UPDATE budget_meta SET value='31' WHERE key='limit_cny'")
    before = ledger.read_bytes() if ledger.exists() else None
    with pytest.raises(ValueError, match="ledger_missing|invalid_sqlite|limit_changed"):
        cli._dedicated_factory(args, book(), counter)
    assert (ledger.read_bytes() if ledger.exists() else None) == before


def test_same_budget_survives_new_output_directory(tmp_path):
    ledger = tmp_path / "budget.sqlite"
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "first"), "--run", "--ledger", str(ledger)])
    cli._dedicated_factory(args, book(), counter)
    durable = GlobalBudgetLedger(limit_cny=30, path=ledger)
    hold = durable.reserve(2, "prior")
    durable.settle(hold["reservation_id"], 1.5)
    args.output_dir = str(tmp_path / "after-code-fix")
    next_factory = cli._dedicated_factory(args, book(), counter)
    assert next_factory.ledger_snapshot()["actual_cny"] == 1.5
    assert next_factory.ledger_snapshot()["remaining_cny"] == 28.5


def test_crash_after_result_before_seal_recovers_from_raw_for_free(tmp_path):
    factory = Factory()
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    assert (attempt / "UNIT_RESULT.json").is_file()
    (attempt / "RESULT_SEAL.json").unlink()
    recovered = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    assert recovered["complete_units"] == 1
    assert recovered["model_calls"] == 0 and factory.calls == 1
    assert (attempt / "RESULT_SEAL.json").is_file()


def _sse(content="Evidence [P0001].", *, finish="stop", done=True, usage=None):
    event = {"id": "stream-receipt", "model": "qwen3.5-plus", "choices": [{
        "delta": {"content": content, "reasoning_content": "reasoning"}, "finish_reason": finish}]}
    raw = "data: " + json.dumps(event) + "\n\n"
    if usage is not None:
        raw += "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
    return (raw + ("data: [DONE]\n\n" if done else "")).encode()


@pytest.mark.parametrize("finish,done,complete", [("stop", True, True), ("stop", False, False), ("length", True, False)])
def test_raw_sse_only_recovery_uses_no_factory_and_preserves_terminal_state(tmp_path, finish, done, complete):
    factory = Factory()
    first = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, client_factory=factory, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    for name in ("RAW_RESPONSE.json", "UNIT_RESULT.json", "RESULT_SEAL.json"):
        (attempt / name).unlink()
    transport = attempt / "transport"
    transport.mkdir()
    raw_path = transport / "request.sse.partial"
    raw = _sse(finish=finish, done=done)
    raw_path.write_bytes(raw)
    class NoCallFactory:
        execution_mode = "injected"
        def __call__(self, *args, **kwargs):
            pytest.fail("raw-only recovery constructed provider")
    recovered = route.run_legacy_units(book(1), output_dir=tmp_path, run=True, retry_failed=False,
                                       client_factory=NoCallFactory(), token_counter=counter)
    assert recovered["model_calls"] == 0
    assert recovered["complete_units"] == int(complete)
    snapshot = route._read(attempt / "RAW_RESPONSE.json")
    assert snapshot["complete"] is complete and snapshot["stream_done"] is done
    assert snapshot["content"] == "Evidence [P0001]." and snapshot["reasoning_content"] == "reasoning"
    assert snapshot["usage"] == {} and snapshot["request_id"] == "stream-receipt"
    assert raw_path.read_bytes() == raw
    assert recovered["missing_units"] == []


def test_cache_survives_code_repair_and_directory_migration(tmp_path, monkeypatch):
    import shutil
    original, moved = tmp_path / "original", tmp_path / "moved"
    factory = Factory()
    factory.execution_mode = "live"
    first = route.run_legacy_units(book(1), output_dir=original, run=True, client_factory=factory, token_counter=counter)
    attempt = Path(first["units"][0]["attempt_dir"])
    # Exercise legacy absolute metadata as well as new portable metadata.
    result = route._read(attempt / "UNIT_RESULT.json")
    result["body_path"] = str(attempt / result["body_path"])
    route._write(attempt / "UNIT_RESULT.json", result)
    seal = route._read(attempt / "RESULT_SEAL.json")
    seal["sha256"] = route._hash(result)
    route._write(attempt / "RESULT_SEAL.json", seal)
    original_result_bytes = (attempt / "UNIT_RESULT.json").read_bytes()
    original_seal_bytes = (attempt / "RESULT_SEAL.json").read_bytes()
    shutil.copytree(original, moved)
    Path(result["body_path"]).write_text("unrelated stale absolute body", encoding="utf-8")
    monkeypatch.setattr(route, "_code_hash", lambda: "repaired-code")
    recovered = route.run_legacy_units(book(1), output_dir=moved, run=True, client_factory=factory, token_counter=counter)
    assert recovered["complete_units"] == 1 and recovered["model_calls"] == 0 and factory.calls == 1
    migrated_attempt = Path(recovered["units"][0]["attempt_dir"])
    assert (migrated_attempt / "UNIT_RESULT.json").read_bytes() == original_result_bytes
    assert (migrated_attempt / "RESULT_SEAL.json").read_bytes() == original_seal_bytes
    assembled = (moved / "assembled" / "REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert "Evidence [P0001]." in assembled and "unrelated stale absolute body" not in assembled
    assert route._read(moved / "CODE_PROVENANCE.json")["current_code_sha256"] == "repaired-code"


def test_explicit_scope_shares_new_60_cap_across_books_without_reset(tmp_path):
    ledger = tmp_path / "shared.sqlite"
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "pilot"),
        "--run", "--ledger", str(ledger), "--budget-limit", "60", "--budget-scope", "body-experiment"])
    cli._dedicated_factory(args, book(1), counter)
    durable = GlobalBudgetLedger(limit_cny=60, path=ledger)
    row = durable.reserve(20, "pilot")
    durable.settle(row["reservation_id"], 10)
    hold = durable.reserve(15, "unknown")
    durable.settle(hold["reservation_id"], None, uncertain=True)
    args.output_dir = str(tmp_path / "holdout")
    changed = book(2)
    changed["chapters"][0]["chapter_id"] = "HOLDOUT"
    factory = cli._dedicated_factory(args, changed, counter)
    assert factory.ledger_snapshot()["remaining_cny"] == 35
    assert factory.ledger_snapshot()["reserved_cny"] == 15
    durable.reserve(35, "full")
    with pytest.raises(QwenTransportError, match="budget_exceeded"):
        durable.reserve(0.01, "over-limit")
    args.ledger = str(tmp_path / "replacement.sqlite")
    with pytest.raises(ValueError, match="output_ledger_changed"):
        cli._dedicated_factory(args, changed, counter)
    assert not Path(args.ledger).exists()


def test_new_60_budget_is_allowed_but_existing_30_cannot_be_raised(tmp_path):
    preview = route.run_legacy_units(book(1), output_dir=tmp_path / "preview", budget_limit=60, token_counter=counter)
    assert preview["budget_limit_cny"] == 60
    ledger = tmp_path / "old.sqlite"
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "run"), "--run", "--ledger", str(ledger)])
    cli._dedicated_factory(args, book(1), counter)
    args.output_dir = str(tmp_path / "new-output")
    args.budget_limit = 60
    with pytest.raises(ValueError, match="scope_conflict"):
        cli._dedicated_factory(args, book(1), counter)
    with pytest.raises(QwenTransportError, match="limit_conflict"):
        GlobalBudgetLedger(limit_cny=60, path=ledger)
    assert GlobalBudgetLedger(path=ledger).as_dict()["limit_cny"] == 30


def test_cli_audited_reconcile_is_repeatable_and_rejects_conflict(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    args = cli.parser().parse_args(["--input", "unused", "--output-dir", str(tmp_path / "run"), "--run", "--ledger", str(ledger)])
    cli._dedicated_factory(args, book(1), counter)
    durable = GlobalBudgetLedger(path=ledger)
    hold = durable.reserve(2, "uncertain")
    durable.settle(hold["reservation_id"], None, uncertain=True, telemetry={"request_id": "provider-request"})
    receipt = {"reservation_id": hold["reservation_id"], "actual_cny": 0.5,
        "evidence": {"source": "provider-billing-export", "receipt_id": "receipt-1", "request_id": "provider-request"},
        "actor": "operator", "reason": "Match official billing receipt"}
    receipt_path = tmp_path / "receipt.json"
    route._write(receipt_path, receipt)
    argv = ["--ledger", str(ledger), "--reconcile-receipt", str(receipt_path)]
    assert cli.main(argv) == cli.main(argv) == 0
    saved = GlobalBudgetLedger(path=ledger).as_dict()
    assert saved["actual_cny"] == 0.5 and saved["reserved_cny"] == 0
    assert saved["reservations"][0]["reconciliation"]["actor"] == "operator"
    receipt["actual_cny"] = 0
    route._write(receipt_path, receipt)
    with pytest.raises(QwenTransportError, match="reconciliation_conflict"):
        cli.main(argv)
    assert GlobalBudgetLedger(path=ledger).as_dict()["actual_cny"] == 0.5
