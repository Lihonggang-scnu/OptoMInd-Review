"""Offline recovery contracts: only fake model clients and temporary artifacts."""
import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import paper_reading_batch as batch
from optomind_research.runtime.upgrade3 import paper_reading_card as card
from optomind_research.runtime.upgrade3.module4 import runtime
from test_reader_helper_token_budgets import card_inputs
from test_planning_supplement_attempt_contract import CardModelBoundary


def response(contribution="无直接贡献。"):
    result = CardModelBoundary().complete([])
    result.update(complete=True, finish_reason="stop")
    result["content"]["review_planning"]["facet_contributions"] = [{
        "facet_id": "F1", "contribution": contribution,
        "possible_uses": "", "boundaries": "未涉及其他温度。",
    }]
    return result


class FakeProvider:
    def __init__(self, result=None, *, timeout=False):
        self.result = result or response()
        self.calls = []
        self.timeout = timeout

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.timeout:
            raise TimeoutError("LOCAL_ONLY timeout")
        return copy.deepcopy(self.result)


def frozen_files(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def forbid_live(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("live client/ledger/invoke touched during offline recovery")
    for module, names in ((card, ("GlobalBudgetLedger", "QwenDirectClient", "invoke_client")),
                          (runtime, ("GlobalBudgetLedger", "QwenDirectClient"))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    return forbidden


def create_failed(inputs, output, monkeypatch, *, contribution="无直接贡献。"):
    # Reproduce the old validator failure through the native writer.  No
    # artifacts, success state or accounting rows are fabricated.
    validator = card.validate_card_output
    with monkeypatch.context() as old:
        if contribution.startswith("无直接"):
            old.setattr(card, "validate_card_output", lambda *a, **k: (_ for _ in ()).throw(
                card.CardOutputError("facet_contribution_content_missing:F1")))
        with pytest.raises(card.CardOutputError):
            card.run_paper_reading_card(**inputs, output_dir=output, client=FakeProvider(response(contribution)))
    assert card.validate_card_output is validator
    return frozen_files(output)


@pytest.mark.parametrize("marker", ["无直接贡献。", "无直接相关贡献。", "无直接贡献"])
def test_explicit_negative_keeps_original_row_and_boundary(marker):
    raw = response(marker)
    row = raw["content"]["review_planning"]["facet_contributions"][0]
    normalized = card.validate_card_output(raw, {"review_plan": {"facets": [{"id": "F1"}]}})
    assert normalized["review_planning"]["facet_contributions"] == []
    diagnostic = normalized["normalization_diagnostics"][0]
    assert diagnostic["raw_row"] == row and diagnostic["row_index"] == 0
    assert normalized["review_planning"]["scope_interpretation_cautions"][-1] == "F1: 未涉及其他温度。"


@pytest.mark.parametrize("changed", [
    {"contribution": "The measured response doubles at the measured temperature."},
    {"possible_uses": None}, {"possible_uses": []}, {"possible_uses": {}},
    {"possible_uses": "", "uses": "Substantive conflicting use"},
    {"facet_id": "F9"}, {"contribution": ""},
])
def test_substantive_or_malformed_rows_still_fail(changed):
    raw = response()
    raw["content"]["review_planning"]["facet_contributions"][0].update(changed)
    with pytest.raises(card.CardOutputError):
        card.validate_card_output(raw, {"review_plan": {"facets": [{"id": "F1"}]}})


def test_saved_reparse_and_bound_cache_never_touch_live_resources(tmp_path, monkeypatch):
    inputs = card_inputs(tmp_path)
    output = tmp_path / "attempt-01"
    before = create_failed(inputs, output, monkeypatch)
    forbidden = forbid_live(monkeypatch)
    options = dict(**inputs, output_dir=output, saved_response_dir=output,
                   client=forbidden, key_file=tmp_path / "never-read.key",
                   budget_ledger_path=tmp_path / "never-create.sqlite", budget_limit_cny=80)
    result = card.run_paper_reading_card(**options)
    derived = Path(result["output_dir"])
    assert result["reused"] and result["reparsed"] and derived.parent == output / "derived"
    assert frozen_files(output) == before
    assert all((derived / name).read_bytes() == data for name, data in before.items())
    assert not options["budget_ledger_path"].exists()
    assert result["card"]["planning_view"]["scope_interpretation_cautions"][-1] == "F1: 未涉及其他温度。"
    receipt = json.loads((derived / "REPARSE_RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt["physical_calls"] == 0 and receipt["validator_version"] == card.VALIDATOR_VERSION
    again = card.run_paper_reading_card(**options)
    assert again["reused"] and again["output_dir"] == result["output_dir"]
    monkeypatch.setattr(card, "VALIDATOR_VERSION", "LOCAL_ONLY.validator.next")
    assert card.run_paper_reading_card(**options)["output_dir"] != result["output_dir"]
    assert frozen_files(output) == before


@pytest.mark.parametrize("artifact,field,value", [
    ("INPUT_PACKET.json", "snapshot_sha256", "different"),
    ("INPUT_PACKET.json", "paper_identity", {"canonical_paper_id": "different"}),
    ("INPUT_PACKET.json", "runtime_config", {}),
    ("PROMPT.json", "messages", []),
    ("INPUT_AUDIT.json", "runtime_config", {}),
    ("FAILURE.json", "phase", "transport"),
    ("RAW_RESPONSE.json", "complete", False),
    ("RAW_RESPONSE.json", "truncated", True),
    ("USAGE.json", "model", "different"),
])
def test_saved_source_must_match_current_input_and_complete_transport(tmp_path, monkeypatch, artifact, field, value):
    inputs = card_inputs(tmp_path)
    output = tmp_path / "attempt-01"
    create_failed(inputs, output, monkeypatch)
    path = output / artifact
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = frozen_files(output)
    forbid_live(monkeypatch)
    with pytest.raises((card.CardInputError, card.CardTransportError)):
        card.run_paper_reading_card(**inputs, output_dir=output, saved_response_dir=output)
    assert frozen_files(output) == before and not (output / "derived").exists()


def batch_options(tmp_path, inputs):
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"records": [{"paper_id": "fixture-paper", **{k: str(v) for k, v in inputs.items()}}]}), encoding="utf-8")
    return dict(manifest_path=manifest, output_root=tmp_path / "batch", mode="run",
                initial_workers=1, max_workers=1, retry_delay_seconds=0, poll_seconds=0)


def test_batch_explicit_reparse_preserves_physical_attempt_and_failure(tmp_path, monkeypatch):
    inputs = card_inputs(tmp_path)
    options = batch_options(tmp_path, inputs)
    provider = FakeProvider()
    with monkeypatch.context() as old:
        old.setattr(card, "validate_card_output", lambda *a, **k: (_ for _ in ()).throw(
            card.CardOutputError("facet_contribution_content_missing:F1")))
        failed = batch.run_paper_reading_batch(**options, client_factory=lambda *args: provider)
    item = next(iter(failed["state"]["items"].values()))
    assert item["status"] == "failed" and item["attempt"] == len(provider.calls) == 1
    attempt = Path(item["output_dir"])
    before, history = frozen_files(attempt), copy.deepcopy(item["attempts"])
    forbidden = forbid_live(monkeypatch)
    unchanged = batch.run_paper_reading_batch(**options, client_factory=forbidden)
    assert next(iter(unchanged["state"]["items"].values()))["status"] == "failed"
    recovered = batch.run_paper_reading_batch(**options, client_factory=forbidden, reparse_saved_responses=True)
    item = next(iter(recovered["state"]["items"].values()))
    assert item["status"] == "reused" and item["attempt"] == 1 and item["attempts"] == history
    assert item["reparse_history"][-1]["physical_calls"] == 0
    assert Path(item["output_dir"]).parent == attempt / "derived"
    assert frozen_files(attempt) == before and not attempt.with_name("attempt-02").exists()
    assert len(Path(recovered["planning_views"]).read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("legacy_pending", [False, True])
def test_invalid_saved_reparse_terminates_once_without_paid_retry(tmp_path, monkeypatch, legacy_pending):
    inputs = card_inputs(tmp_path)
    options = batch_options(tmp_path, inputs)
    provider = FakeProvider(response("The measured response changes under specified conditions."))
    failed = batch.run_paper_reading_batch(**options, client_factory=lambda *args: provider)
    item = next(iter(failed["state"]["items"].values()))
    assert item["status"] == "failed" and len(provider.calls) == 1
    history = copy.deepcopy(item["attempts"])
    if legacy_pending:
        item["status"] = "pending"  # Reproduce the old scheduler's persisted state, in this temporary fixture only.
        batch._atomic_json(Path(options["output_root"]) / "BATCH_STATE.json", failed["state"])
    forbidden = forbid_live(monkeypatch)
    calls, native = [], batch._call_one
    def capture(*args, **kwargs):
        calls.append(True)
        return native(*args, **kwargs)
    monkeypatch.setattr(batch, "_call_one", capture)
    result = batch.run_paper_reading_batch(**options, client_factory=forbidden, reparse_saved_responses=True)
    item = next(iter(result["state"]["items"].values()))
    assert len(calls) == 1 and item["status"] == "failed" and item["attempt"] == 1
    assert item["attempts"] == history and len(item["reparse_history"]) == 1
    assert not Path(item["output_dir"]).with_name("attempt-02").exists()


def test_success_commit_reuse_skips_factory_and_corruption_fails_closed(tmp_path, monkeypatch):
    inputs = card_inputs(tmp_path)
    options = batch_options(tmp_path, inputs)
    provider = CardModelBoundary()
    first = batch.run_paper_reading_batch(**options, client_factory=lambda *args: provider)
    item = next(iter(first["state"]["items"].values()))
    forbidden = forbid_live(monkeypatch)
    call_options = dict(config=first["state"]["config"], output_root=Path(options["output_root"]), client_factory=forbidden)
    assert batch._call_one(item, **call_options)["reused"]
    path = Path(item["output_dir"]) / "PAPER_READING_CARD.md"
    path.write_text("LOCAL_ONLY corrupt derived display", encoding="utf-8")
    outcome = batch._call_one(item, **call_options)
    assert not outcome["ok"] and not outcome["retryable"] and outcome["operation"] == "commit_reuse"


def test_transient_live_retries_remain_bounded(tmp_path):
    inputs = card_inputs(tmp_path)
    options = batch_options(tmp_path, inputs)
    provider = FakeProvider(timeout=True)
    result = batch.run_paper_reading_batch(**options, client_factory=lambda *args: provider)
    item = next(iter(result["state"]["items"].values()))
    assert item["status"] == "failed" and item["attempt"] == len(provider.calls) == 2


def test_interrupted_attempt_without_raw_keeps_files_and_uses_next_number(tmp_path):
    inputs = card_inputs(tmp_path)
    options = batch_options(tmp_path, inputs)
    preflight = batch.run_paper_reading_batch(**{**options, "mode": "preflight"})
    item = next(iter(preflight["state"]["items"].values()))
    config = {**preflight["state"]["config"], "mode": "run"}
    base = Path(options["output_root"]) / "cards" / "fixture-paper" / item["work_key"]
    source = base / "attempt-01"
    class Interrupted:
        def complete(self, *args, **kwargs):
            raise SystemExit("LOCAL_ONLY interrupted before provider call")
    with pytest.raises(SystemExit):
        card.run_paper_reading_card(**inputs, output_dir=source, client=Interrupted())
    before = frozen_files(source)
    assert "RAW_RESPONSE.json" not in before
    provider = CardModelBoundary()
    outcome = batch._call_one(item, config=config, output_root=Path(options["output_root"]),
                              client_factory=lambda *args: provider, reparse_saved_responses=True)
    assert outcome["ok"] and outcome["attempt"] == 2 and len(provider.calls) == 1
    assert frozen_files(source) == before


def test_native_batch_cli_explicit_reparse_flag_is_forwarded(monkeypatch, capsys):
    from scripts.upgrade3 import paper_reading_batch as cli
    assert not cli._parser().parse_args(["--manifest", "m", "--output-root", "o"]).reparse_saved_responses
    captured = []
    def fake_batch(**kwargs):
        captured.append(kwargs)
        return dict(output_root="o", counts={}, cards_index="i", planning_views="p")
    monkeypatch.setattr(cli, "run_paper_reading_batch", fake_batch)
    assert cli.main(["--manifest", "m", "--output-root", "o", "--run", "--reparse-saved-responses"]) == 0
    assert captured[0]["mode"] == "run" and captured[0]["reparse_saved_responses"] is True
    capsys.readouterr()


@pytest.mark.parametrize("keep_canonical", [False, True])
def test_top_level_AB_aliases_preserve_canonical_content_and_raw_reference(keep_canonical):
    canonical = response()
    card_input = {"review_plan": {"facets": [{"id": "F1"}]}}
    expected = card.validate_card_output(canonical, card_input)
    aliased = copy.deepcopy(canonical)
    content = aliased["content"]
    content["A"] = copy.deepcopy(content["general_understanding"])
    content["B"] = copy.deepcopy(content["review_planning"])
    if not keep_canonical:
        del content["general_understanding"], content["review_planning"]
    before = copy.deepcopy(aliased)
    actual = card.validate_card_output(aliased, card_input)
    assert aliased == before
    assert actual["general_understanding"] == expected["general_understanding"]
    assert actual["review_planning"] == expected["review_planning"]
    aliases = actual["normalization_diagnostics"][:2]
    assert [row["source_key"] for row in aliases] == ["A", "B"]
    for row in aliases:
        assert row["canonical_present"] is keep_canonical
        assert row["raw_artifact"] == "RAW_RESPONSE.json"
        assert row["decoded_content_path"] == "$." + row["source_key"]
        assert row["raw_value_sha256"] == card._sha(content[row["source_key"]])
        assert "raw_value" not in row and "raw_row" not in row
    assert actual["normalization_diagnostics"][2:] == expected["normalization_diagnostics"]


@pytest.mark.parametrize("case", ["conflicting_A", "conflicting_B", "invalid_canonical",
                                  "invalid_A", "invalid_B", "missing_B", "missing_A_field", "missing_B_field", "unknown_alias"])
def test_top_level_AB_alias_conflicts_missing_fields_and_wrong_types_fail(case):
    content = response()["content"]
    content["A"] = copy.deepcopy(content["general_understanding"])
    content["B"] = copy.deepcopy(content["review_planning"])
    if case == "conflicting_A":
        content["A"]["work_summary"] = "Different content"
    elif case == "conflicting_B":
        content["B"]["planning_summary"] = "Different content"
    elif case == "invalid_canonical":
        content["general_understanding"] = None
    elif case in {"invalid_A", "invalid_B"}:
        content[case[-1]] = []
    else:
        del content["general_understanding"], content["review_planning"]
        if case == "missing_B":
            del content["B"]
        elif case == "missing_A_field":
            del content["A"]["approach"]
        elif case == "missing_B_field":
            del content["B"]["planning_summary"]
        elif case == "unknown_alias":
            content["a"] = content.pop("A")
    with pytest.raises(card.CardOutputError):
        card.validate_card_output({"content": content}, {"review_plan": {"facets": [{"id": "F1"}]}})
