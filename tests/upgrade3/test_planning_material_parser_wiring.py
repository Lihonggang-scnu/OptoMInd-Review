"""Offline initial/supplement parser wiring; all external boundaries are fake."""
import hashlib
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.upgrade3 import build_local_materials as materials, progressive_review_plan as cli, writer_candidates
from optomind_research.runtime.upgrade3 import docling_adapter, planning_supplement as supplement
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning, paper_reading_card
from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
from optomind_research.runtime.upgrade3.material_acquisition import MaterialAcquirer
from test_docling_native_bridge import _fixture
from test_regional_recognition import Factory, TABLE
from test_planning_supplement_attempt_contract import CardModelBoundary, supplement_inputs


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network_or_keys_forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    from optomind_research.runtime.upgrade3.module4.runtime import QwenDirectClient
    monkeypatch.setattr(QwenDirectClient, "_keys", forbidden)
    monkeypatch.setattr(MaterialAcquirer, "_load_default_providers", lambda self: self.providers.update({
        name: None for name in ("openalex", "semantic_scholar", "crossref", "unpaywall", "core", "arxiv", "grobid")}))
    monkeypatch.setattr(MaterialAcquirer, "_europepmc_routes", lambda *_: ([], ""))


def configured(tmp_path, monkeypatch, factory=None, *, command=False):
    data, document, run, saved = _fixture(tmp_path)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(data)
    doc_flags = ["--docling-document-json", str(saved.document_path), "--docling-run-json", str(saved.run_metadata_path)]
    if command:
        config = tmp_path / "docling-cli.json"
        config.write_text(json.dumps({"parser_version": saved.parser_version, "config": dict(saved.config),
            "command": ["fake-native-parser", "{input}", "{output}"]}))
        doc_flags = ["--docling-cli-config", str(config)]
    recognition = tmp_path / "regional.json"
    recognition.write_text('{"concurrency":8}')
    for name in ("project.sqlite", "account.sqlite", "NEVER_READ.key", "tokenizer.json"):
        (tmp_path / name).touch()
    factory = factory or Factory()
    monkeypatch.setattr(writer_candidates, "make_live_factory", lambda *_args, **_kwargs: factory)
    monkeypatch.setattr(writer_candidates, "tokenizer_counter", lambda *_: (lambda *_: 100, {"offline": True}))
    monkeypatch.setattr(MaterialAcquirer, "_default_transport", lambda *_args, **_kwargs: (200, {"content-type": "application/pdf"}, data))
    argv = ["--input", str(pdf), "--output-root", str(tmp_path / "initial"), "--canonical-paper-id", "fixture-paper",
        "--cache-root", str(tmp_path / "acquisition"), "--disable-rescue", *doc_flags,
        "--docling-max-processes", "2", "--recognition-config", str(recognition), "--recognition-run",
        "--recognition-ceiling-cny", "80", "--budget-ledger", str(tmp_path / "project.sqlite"), "--budget-limit", "80",
        "--account-ledger", str(tmp_path / "account.sqlite"), "--account-budget-limit", "300",
        "--key-file", str(tmp_path / "NEVER_READ.key"), "--key-index", "1", "--tokenizer", str(tmp_path / "tokenizer.json")]
    return data, document, run, argv, factory


def test_initial_and_planning_supplement_share_parser_route_to_real_card(tmp_path, monkeypatch, capsys):
    data, _, _, argv, regional = configured(tmp_path, monkeypatch)
    assert materials.main(argv) == 0
    initial = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert len(regional.calls) == 2
    factory = materials.make_material_acquirer_factory(materials._build_parser().parse_args(argv))
    request = supplement_inputs(tmp_path / "inputs")
    cfg = planning.ProgressivePlannerConfig(topic_id="parser-wiring", pool_path=tmp_path / "inputs/POOL.jsonl",
        plan_path=tmp_path / "inputs/PLAN.json", output_dir=tmp_path / "planning")
    client = CardModelBoundary()
    original_resolve = MaterialAcquirer.resolve_identity
    def resolve(self, record, **kwargs):
        identity = original_resolve(self, record, **kwargs)
        return replace(identity, provider_records=(*identity.provider_records,
            {"source": "local_fixture", "pdf_url": "https://fixture.invalid/paper.pdf", "is_oa": True, "doi": "10.1234/fixture"}))
    monkeypatch.setattr(MaterialAcquirer, "resolve_identity", resolve)
    monkeypatch.setattr(MaterialAcquirer, "_ensure_grobid", lambda _: SimpleNamespace(parse_pdf=lambda *_a, **_kw: {
        "tei_xml": '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt><title>Synthetic device response</title></titleStmt><publicationStmt><idno type="DOI">10.1234/fixture</idno></publicationStmt></fileDesc></teiHeader><text><body><p>The synthetic device operating conditions are retained.</p></body></text></TEI>'}))
    monkeypatch.setattr(supplement, "run_qwen_fulfillment_judge", lambda **_: lambda **_: {
        "status": "fulfilled", "useful_material": "Conditions retained from regional table."})
    monkeypatch.setattr(supplement, "CompositeS2OpenAlexGateway", lambda *_: None)
    native_card = paper_reading_card.run_paper_reading_card
    monkeypatch.setattr(paper_reading_card, "run_paper_reading_card", lambda *a, **kw: native_card(*a, **kw, client=client))
    runner = planning.make_planning_supplement_runner(cfg, key_file=tmp_path / "ABSENT.key",
        budget_ledger_path=tmp_path / "project.sqlite", budget_limit_cny=80, acquirer_factory=factory)
    result = runner([{"gap_id": "G1", "gap_question": "Which conditions apply?", "known_papers": [{
        "paper_id": "fixture-paper", "doi": "10.1234/fixture", "title": "Synthetic device response"}]}],
        phase="level1", pool_rows=[], plan=json.loads((request.parent / "PLAN.json").read_bytes()), skip_local_triage=True)
    assert result["results"], result
    index = json.loads((Path(result["results"][0]["output_dir"]) / "SUPPLEMENT_INDEX.json").read_bytes())
    source = index["source_units"][0]
    snapshot = PreparedSnapshotProvider(source["snapshot_path"]).load()
    assert snapshot.root != Path(initial["snapshot_path"])
    assert len(client.calls) == 1 and len(regional.calls) == 2  # successful native regional cache reused
    message = client.calls[0][0][-1]["content"]
    for value in (TABLE["footnotes"][0], "Length (μm)", "−1.5", "normalized_latex:", "recognition_status=recognized"):
        assert value in message
    assert any(a.get("parser") == "docling" and a.get("recognition_status") == "recognized" for a in snapshot.assets)
    before = len(regional.calls)
    native = factory(tmp_path / "xml").acquire_local({"canonical_paper_id": "native"},
        xml_bytes=b"<article><body><p>Native XML operating conditions.</p></body></article>", pdf_bytes=data)
    assert native.snapshot and len(regional.calls) == before


def test_per_source_factories_share_docling_two_and_regional_eight(tmp_path, monkeypatch):
    guard, barrier = threading.Lock(), threading.Barrier(8)
    state = {"local": 0, "local_peak": 0, "regional": 0, "regional_peak": 0}
    class Parallel(Factory):
        def __call__(self, *args):
            call = super().__call__(*args)
            def wrapped(*a, **kw):
                with guard:
                    state["regional"] += 1
                    state["regional_peak"] = max(state["regional_peak"], state["regional"])
                try:
                    barrier.wait(timeout=8)
                    return call(*a, **kw)
                finally:
                    with guard: state["regional"] -= 1
            return wrapped
    data, document, run, argv, provider = configured(tmp_path, monkeypatch, Parallel(), command=True)
    def local_runner(command, **_):
        with guard:
            state["local"] += 1
            state["local_peak"] = max(state["local_peak"], state["local"])
        try:
            time.sleep(0.04)
            output = Path(command[2])
            (output / "document.json").write_text(json.dumps(document))
            (output / "run.json").write_text(json.dumps({**run, "source_sha256": hashlib.sha256(Path(command[1]).read_bytes()).hexdigest()}))
        finally:
            with guard: state["local"] -= 1
    monkeypatch.setattr(docling_adapter.subprocess, "run", local_runner)
    factory = materials.make_material_acquirer_factory(materials._build_parser().parse_args(argv))
    acquirers = [factory(tmp_path / f"paper-{i}") for i in range(4)]
    assert len({id(a.providers["docling"]) for a in acquirers}) == 1
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda item: item[1].acquire_local({"canonical_paper_id": f"parallel-{item[0]}"},
            pdf_bytes=data + f"\n% source {item[0]}".encode()), enumerate(acquirers)))
    assert all(r.snapshot for r in results)
    assert state["local_peak"] == 2 and state["regional_peak"] == 8
    assert len(provider.calls) == 8


def test_formal_planning_parser_exposes_identical_runtime_flags(tmp_path):
    args = cli.parser().parse_args(["--pool", "pool", "--plan", "plan", "--output-dir", "output",
        "--docling-cli-config", "docling.json", "--docling-max-processes", "2", "--recognition-config", "plus.json",
        "--recognition-concurrency", "8", "--recognition-run", "--recognition-ceiling-cny", "80",
        "--budget-ledger", "project.sqlite", "--budget-limit-cny", "80", "--account-ledger", "account.sqlite",
        "--account-budget-limit", "300", "--key-file", "NEVER_READ.key", "--key-index", "1", "--tokenizer", "tokenizer.json"])
    assert args.docling_max_processes == 2 and args.recognition_concurrency == 8 and args.recognition_run
    assert args.account_budget_limit == 300 and args.budget_limit_cny == 80
    defaults = cli.parser().parse_args(["--pool", "pool", "--plan", "plan", "--output-dir", "output"])
    assert not defaults.docling_cli_config and not defaults.recognition_run


@pytest.mark.parametrize("enabled", [False, True])
def test_formal_planning_main_injects_factory_only_when_explicit(tmp_path, monkeypatch, enabled):
    from test_progressive_review_plan_outline_strengthening import setup_cli
    argv, _, _, _ = setup_cli(tmp_path, monkeypatch)
    captured = {}
    shared_factory = lambda root: None
    constructor = object()
    def builder(args, **kwargs):
        captured["args"], captured["constructor"] = args, kwargs["regional_client_constructor"]
        return shared_factory
    monkeypatch.setattr(cli, "make_material_acquirer_factory", builder)
    monkeypatch.setattr(planning, "make_planning_supplement_runner", lambda *a, **kw: captured.update(factory=kw["acquirer_factory"]))
    flags = ["--docling-cli-config", "explicit.json", "--docling-max-processes", "2", "--recognition-config", "plus.json",
        "--recognition-concurrency", "8", "--recognition-run"] if enabled else []
    assert cli.main([*argv, "--stop-after", "level1", *flags], regional_client_constructor=constructor) == 0
    assert captured["factory"] is (shared_factory if enabled else None)
    assert ("args" in captured) is enabled
    if enabled:
        assert captured["constructor"] is constructor and captured["args"].recognition_concurrency == 8


@pytest.mark.parametrize("flags", [["--recognition-run"], ["--docling-document-json", "unpaired.json"]])
def test_initial_invalid_combinations_still_exit_two(flags, capsys):
    with pytest.raises(SystemExit) as exc:
        materials.main(["--input", "unread.pdf", *flags])
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
