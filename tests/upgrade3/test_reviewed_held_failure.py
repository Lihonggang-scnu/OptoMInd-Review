"""Offline explicit recovery: original unknown holds and receipts remain intact."""
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenTransportError
from optomind_research.runtime.upgrade3.dual_budget import DualBudgetLedger
from optomind_research.runtime.upgrade3.regional_recognition import RegionalRecognitionConfig, RegionalRecognizer
from scripts.upgrade3.writer_candidates import make_live_factory


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Recording:
    def __init__(self, dual, verifier, unknown=()):
        self.dual, self.held_failure_receipt, self.unknown, self.calls = dual, verifier, unknown, []

    def __call__(self, role, stage_dir, profile):
        def call(messages, **kwargs):
            name = messages[0]["content"]
            if isinstance(messages[-1]["content"], list):
                name = "table" if "table faithfully" in messages[-1]["content"][-1]["text"] else "formula"
            self.calls.append(name)
            reservation = self.dual.reserve(.1, kwargs["call_id"] + ":key0:attempt0")
            if name in self.unknown:
                self.dual.settle(reservation["reservation_id"], None, uncertain=True)
                raise QwenTransportError("synthetic_unknown", record={"call_id": kwargs["call_id"], "complete": False})
            self.dual.settle(reservation["reservation_id"], .01)
            content = {"caption": "Caption μm", "columns": ["Header"], "rows": [["12"]], "footnotes": ["Footnote"], "uncertain_regions": []} if name == "table" else {"latex": name, "label": "", "uncertain_regions": []}
            return {"content": json.dumps(content),
                    "complete": True, "finish_reason": "stop", "execution_mode": "recording"}
        return call

    def ledger_snapshot(self):
        return self.dual.project.as_dict()


def _setup(tmp_path):
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    dual = DualBudgetLedger(GlobalBudgetLedger(path=project, limit_cny=80), GlobalBudgetLedger(path=account, limit_cny=300), tmp_path / "maps")
    args = SimpleNamespace(run=True, responses=None, budget_ledger=project, budget_limit=80, allow_max=False,
        account_ledger=account, account_mapping_dir=dual.mapping_dir)
    verifier = make_live_factory(args, thread_safe=True).held_failure_receipt
    config = RegionalRecognitionConfig(cache_root=tmp_path / "cache", concurrency=1)
    def jobs(names):
        return [{"binding": {"config": config.fingerprint(), "region": {"region_id": name}},
                 "messages": [{"role": "user", "content": name}], "kind": "formula"} for name in names]
    seed = Recording(dual, verifier, unknown={"old_unknown"})
    initial = jobs(["saved_success", "old_unknown", "later"])
    RegionalRecognizer(config, provider_factory=seed)._run_regions(initial, time.monotonic() + 60)
    assert seed.calls == ["saved_success", "old_unknown"] and initial[-1]["result"]["status"] == "pending"
    attempt = initial[1]["attempt"]
    allowlist = tmp_path / "reviewed.json"
    allowlist.write_text(json.dumps({"failures": [{"cache_key": attempt.parent.name, "attempt": 1, "result_sha256": digest(attempt / "RESULT.json")}]}))
    return config, dual, verifier, jobs, attempt, allowlist


def test_reviewed_old_failure_no_retry_success_free_other_same_source_continues(tmp_path):
    config, dual, verifier, jobs, attempt, allowlist = _setup(tmp_path)
    originals = {p: p.read_bytes() for p in config.cache_root.rglob("*") if p.is_file()}
    held_before = [copy.deepcopy(ledger.as_dict()["reservations"]) for ledger in (dual.project, dual.total)]
    factory = Recording(dual, verifier)
    resumed = jobs(["saved_success", "old_unknown", "later"])
    RegionalRecognizer(config, provider_factory=factory, reviewed_failures=allowlist)._run_regions(resumed, time.monotonic() + 60)
    assert factory.calls == ["later"]
    assert [job["result"]["status"] for job in resumed] == ["recognized", "error", "recognized"]
    assert resumed[1]["result"]["reviewed_held_failure"]["paid_calls"] == 0
    assert list(attempt.glob("REVIEWED_HELD_*.json")) and not (attempt.parent / "attempt-2").exists()
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    for ledger, before in zip((dual.project, dual.total), held_before):
        rows = ledger.as_dict()["reservations"]
        assert all(row in rows for row in before)
        assert sum(row["amount_cny"] for row in rows if row["status"] == "uncertain") == .1
    def no_send(*_args, **_kwargs):
        raise AssertionError("cached success or reviewed unknown constructed provider")
    no_send.held_failure_receipt = verifier
    replay = jobs(["saved_success", "old_unknown", "later"])
    recognizer = RegionalRecognizer(config, provider_factory=no_send, reviewed_failures=allowlist)
    recognizer._run_regions(replay, time.monotonic() + 60)
    assert [j["result"]["status"] for j in replay] == ["recognized", "error", "recognized"]
    saved = json.loads((replay[0]["attempt"] / "REQUEST.json").read_bytes())
    current = copy.deepcopy(saved["binding"])
    current["config"]["implementation_sha256"] = "new-parser-implementation"
    assert recognizer._same_request(saved, current, saved["messages"], "formula")
    for field, changed in (("model", "different-model"), ("prompts", {"formula": "different prompt"})):
        other = copy.deepcopy(current); other["config"][field] = changed
        assert not recognizer._same_request(saved, other, saved["messages"], "formula")


def test_new_unknown_still_stops_shared_dispatch(tmp_path):
    config, dual, verifier, jobs, attempt, allowlist = _setup(tmp_path)
    factory = Recording(dual, verifier, unknown={"new_unknown"})
    resumed = jobs(["old_unknown", "new_unknown", "after_new"])
    recognizer = RegionalRecognizer(config, provider_factory=factory, reviewed_failures=allowlist)
    recognizer._run_regions(resumed, time.monotonic() + 60)
    assert factory.calls == ["new_unknown"] and recognizer._unknown_halt
    assert resumed[-1]["result"]["status"] == "pending"
    assert sum(row["status"] == "uncertain" for row in dual.project.as_dict()["reservations"]) == 2


@pytest.mark.parametrize("tamper", ["hash", "project_hold", "account_hold"])
def test_bad_allowlist_or_no_longer_held_rejected_before_dispatch(tmp_path, tamper):
    import sqlite3
    config, dual, verifier, jobs, attempt, allowlist = _setup(tmp_path)
    if tamper == "hash":
        value = json.loads(allowlist.read_bytes()); value["failures"][0]["result_sha256"] = "0" * 64
        allowlist.write_text(json.dumps(value))
    else:
        target = dual.project.path if tamper == "project_hold" else dual.total.path
        with sqlite3.connect(target) as db:
            db.execute("UPDATE reservations SET status='settled', actual_cny=0 WHERE status='uncertain'")
    factory = Recording(dual, verifier)
    with pytest.raises(ValueError, match="reviewed_failure"):
        RegionalRecognizer(config, provider_factory=factory, reviewed_failures=allowlist)
    assert factory.calls == []


def test_reviewed_hold_does_not_bypass_remaining_budget(tmp_path):
    config, dual, verifier, jobs, attempt, allowlist = _setup(tmp_path)
    # The original .1 unknown remains consumed. A .11 cap cannot admit .1 more.
    dual.project.limit_cny = .11
    factory = Recording(dual, verifier)
    resumed = jobs(["old_unknown", "later", "after_budget"])
    recognizer = RegionalRecognizer(config, provider_factory=factory, reviewed_failures=allowlist)
    recognizer._run_regions(resumed, time.monotonic() + 60)
    assert [j["result"]["status"] for j in resumed] == ["error", "budget_stopped", "pending"]
    assert factory.calls == ["later"] and not recognizer._unknown_halt
    assert sum(row["amount_cny"] for row in dual.project.as_dict()["reservations"] if row["status"] == "uncertain") == .1


def test_formal_cli_flag_is_explicit():
    from scripts.upgrade3.build_local_materials import _build_parser
    args = _build_parser().parse_args(["--input", "paper.pdf", "--output-root", "out", "--recognition-reviewed-failures", "reviewed.json"])
    assert args.recognition_reviewed_failures == Path("reviewed.json")


def test_reviewed_receipt_published_with_original_error_snapshot(tmp_path):
    from test_docling_native_bridge import _fixture
    from optomind_research.runtime.upgrade3.docling_adapter import DoclingCliAdapter
    from optomind_research.runtime.upgrade3.material_acquisition import AcquisitionConfig, MaterialAcquirer
    from optomind_research.runtime.upgrade3.document_snapshot import validate_snapshot
    config, dual, verifier, jobs, _attempt, _allowlist = _setup(tmp_path)
    fixture_dir = tmp_path / "pdf"; fixture_dir.mkdir()
    data, _, _, doc_config = _fixture(fixture_dir)
    seed = Recording(dual, verifier, unknown={"table"})
    specs = DoclingCliAdapter(doc_config).prepare(data)
    seeded = RegionalRecognizer(config, provider_factory=seed).prepare(specs)
    attempt = next(p.parent for p in config.cache_root.glob("*/attempt-1/RESULT.json")
                   if json.loads(p.read_bytes()).get("transport_error", {}).get("call_id") and json.loads((p.parent / "REQUEST.json").read_bytes())["binding"]["region"].get("type") == "table")
    original = (attempt / "RESULT.json").read_bytes()
    allowlist = tmp_path / "snapshot-reviewed.json"
    allowlist.write_text(json.dumps({"failures": [{"cache_key": attempt.parent.name, "attempt": 1, "result_sha256": digest(attempt / "RESULT.json")}]}))
    resumed = Recording(dual, verifier)
    adapter = DoclingCliAdapter(doc_config, recognizer=RegionalRecognizer(config, provider_factory=resumed, reviewed_failures=allowlist))
    providers = {name: None for name in ("grobid", "openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv")}
    providers["docling"] = adapter
    acquirer = MaterialAcquirer(tmp_path / "snapshots", config=AcquisitionConfig(cache_root=tmp_path / "acquisition", docling_enabled=True), providers=providers)
    result = acquirer.acquire_local({"canonical_paper_id": "held-review"}, pdf_bytes=data)
    assert result.snapshot and validate_snapshot(result.snapshot.root)["valid"]
    assert resumed.calls == ["formula"] and (attempt / "RESULT.json").read_bytes() == original
    receipts = [json.loads(p.read_bytes()) for p in result.snapshot.root.rglob("*") if p.is_file() and p.suffix == ".json" and b"saved_unknown_isolated_no_retry_hold_retained" in p.read_bytes()]
    assert receipts
    table = next(a for a in result.snapshot.assets if a.get("parser") == "docling" and a["type"] == "table")
    assert table["recognition_status"] == "error" and table["recognition_provenance"]["reviewed_held_failure"]["result_sha256"] == hashlib.sha256(original).hexdigest()
