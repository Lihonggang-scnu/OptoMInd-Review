"""Offline checks for both consumers of the shared checker -> 04 -> 05 path."""
import hashlib
import json
import socket
import sys
import types
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import manuscript_delivery as delivery
from optomind_research.runtime.upgrade3.review_delivery import load_delivery_config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import ssl  # load before socket guard
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("Network forbidden"))


def source(tmp_path):
    path = tmp_path / "03.md"
    path.write_text("# 审核标题\n\n## 摘要\n\n原始摘要。\n\n**关键词：** 条件\n\n## 引言\n\n背景 [P0001]。\n\n## 正文\n\n支持条件 [P0001]。\n\n## 结语\n\n条件综合。\n", encoding="utf-8")
    catalog = {"entries": [{"source_handle": "P0001", "paper_id": "P0001", "title": "Synthetic source", "doi": "10.1/synthetic"}]}
    config = {"front_back_mode": "post_body", "language": "zh", "identity_catalogs": [catalog],
        "manuscript_checker": {"enabled": True}, "chapter_roles": [], "post_body_context": {}}
    return path, config, {"status": "generated", "final_manuscript": str(path)}


def stub_checker(monkeypatch, callback):
    from optomind_research.runtime.upgrade3.manuscript_checker import build_checker_messages
    module = types.ModuleType("optomind_research.runtime.upgrade3.manuscript_checker")
    module.build_checker_messages = build_checker_messages
    module.run_manuscript_checker = callback
    monkeypatch.setitem(sys.modules, module.__name__, module)


def selected(path, status="edited"):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": status, "provenance": {"stage": "manuscript_checker"}}


def test_actual_selection_flows_to_reader_and_publication_metadata(tmp_path, monkeypatch):
    original, config, parts = source(tmp_path)
    chosen = tmp_path / "checker.md"
    chosen.write_text(original.read_text().replace("原始摘要。", "选用稿的精确摘要。"), encoding="utf-8")
    seen = {}
    def checker(**kwargs):
        seen.update(kwargs)
        return {"status": "complete_with_diagnostics", "selected_manuscript": selected(chosen),
            "applied": [{"edit_id": "E1"}], "pending": [{"edit_id": "E2"}], "diagnostics": ["retained"]}
    stub_checker(monkeypatch, checker)
    report = delivery.run_manuscript_delivery(config=config, front_back_report=parts, out_dir=tmp_path / "out")
    assert report["downstream_status"] == "complete"
    assert seen["draft_path"] == original
    r4, r5 = report["stages"]["04_figures_citations"], report["stages"]["05_publication"]
    assert "选用稿的精确摘要。" in Path(r4["reader_draft"]).read_text()
    assert "原始摘要。" not in Path(r5["published_markdown"]).read_text()
    assert r5["publication_metadata"]["abstract"] == "选用稿的精确摘要。"
    assert r5["manuscript_checker"]["pending"] == [{"edit_id": "E2"}]
    assert json.loads(Path(r5["manuscript_selection_path"]).read_text())["selected_manuscript"] == selected(chosen)
    assert "原始摘要。" in original.read_text()


def test_bad_selected_hash_stops_before_numbering(tmp_path, monkeypatch):
    original, config, parts = source(tmp_path)
    stub_checker(monkeypatch, lambda **kw: {"status": "edited", "selected_manuscript": {**selected(original), "sha256": "bad"}})
    with pytest.raises(ValueError, match="hash_mismatch"):
        delivery.run_manuscript_delivery(config=config, front_back_report=parts, out_dir=tmp_path / "out")
    assert not (tmp_path / "out/04_figures_citations").exists()


def test_preview_has_no_selected_fallback(tmp_path, monkeypatch):
    original, config, parts = source(tmp_path)
    stub_checker(monkeypatch, lambda **kw: {"status": "pending", "selected_manuscript": None})
    report = delivery.run_manuscript_delivery(config=config, front_back_report=parts, out_dir=tmp_path / "out")
    assert report["halt_reasons"] == ["03_manuscript_checker"]
    assert "04_figures_citations" not in report["stages"]


def test_config_default_switch_and_relative_inputs(tmp_path):
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps({"changes": []}))
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"schema": "review_v2_delivery.config.v1", "front_back": {"mode": "post_body"},
        "writing_requirements": {"genre": "primer"}, "manuscript_checker": {"fixture": "responses.json"}}))
    config = load_delivery_config(p)
    assert config["manuscript_checker"]["enabled"] is True
    assert config["manuscript_checker"]["fixture"] == {"changes": []}
    assert config["post_body_context"]["writing_requirements"] == {"genre": "primer"}
    p.write_text(json.dumps({"schema": "review_v2_delivery.config.v1"}))
    assert load_delivery_config(p)["manuscript_checker"]["enabled"] is False


def test_live_preview_reads_only_existing_budgets_and_binds_increment(tmp_path, monkeypatch):
    from argparse import Namespace
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    original, config, parts = source(tmp_path)
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    GlobalBudgetLedger(path=project, limit_cny=60)
    GlobalBudgetLedger(path=account, limit_cny=300)
    before = (project.read_bytes(), account.read_bytes())
    args = Namespace(output=tmp_path / "out", ledger=project, budget_limit=60,
        account_ledger=account, account_budget_limit=300, account_mapping_dir=tmp_path / "mappings",
        incremental_budget=5, run=False, retry_failed=False)
    stub_checker(monkeypatch, lambda **kw: {"status": "pending", "selected_manuscript": None})
    first = delivery.run_live_delivery(args=args, config=config, front_back_report=parts)
    second = delivery.run_live_delivery(args=args, config=config, front_back_report=parts)
    assert first["budget_binding"] == second["budget_binding"]
    assert before == (project.read_bytes(), account.read_bytes())
    args.incremental_budget = 6
    with pytest.raises(ValueError, match="binding_changed"):
        delivery.run_live_delivery(args=args, config=config, front_back_report=parts)


def test_real_checker_replay_changes_abstract_and_free_same_directory_resume(tmp_path):
    original, config, parts = source(tmp_path)
    seen = []
    def response(payload, number):
        seen.append(number)
        block = next(b for b in payload["blocks"] if "原始摘要。" in b["text"])
        return {"snapshot_id": payload["snapshot_id"], "changes": [{
            "target_block_id": block["block_id"], "operation": "replace",
            "original_text": "原始摘要。", "replacement_text": "选用稿的精确摘要。",
            "reason": "Synthetic expression correction"}], "unresolved_questions": []}
    config["manuscript_checker"]["fixture"] = response
    first = delivery.run_manuscript_delivery(config=config, front_back_report=parts, out_dir=tmp_path / "out")
    second = delivery.run_manuscript_delivery(config=config, front_back_report=parts, out_dir=tmp_path / "out")
    assert first["downstream_status"] == second["downstream_status"] == "complete"
    assert seen == [1]
    r5 = first["stages"]["05_publication"]
    assert r5["publication_metadata"]["abstract"] == "选用稿的精确摘要。"
    assert Path(first["selected_manuscript"]["path"]).read_bytes() != original.read_bytes()
    assert second["stages"]["03_manuscript_checker"]["cache_hit"]
    assert first["external_requests"] == second["external_requests"] == 0


def test_normal_coordinator_default_checker_preview_then_explicit_fixture(tmp_path, monkeypatch):
    import runpy
    from optomind_research.runtime.upgrade3 import review_delivery as coordinator
    helpers = runpy.run_path(str(Path(__file__).with_name("test_serial_parts_delivery_integration.py")))
    config, assembly, seen, _, _ = helpers["_mock_downstream"](tmp_path, monkeypatch)
    config.pop("manuscript_checker")
    preview = coordinator.run_downstream_delivery(config=config, assembly_report=assembly, out_dir=tmp_path / "preview")
    assert preview["halt_reasons"] == ["03_manuscript_checker"]
    assert "figures" not in seen
    config["manuscript_checker"] = {"fixture": lambda payload, number: {
        "snapshot_id": payload["snapshot_id"], "changes": [], "unresolved_questions": []}}
    complete = coordinator.run_downstream_delivery(config=config, assembly_report=assembly, out_dir=tmp_path / "complete")
    assert complete["downstream_status"] == "complete"
    assert complete["selected_manuscript"]["status"] == "no_change"
    assert complete["stages"]["03_manuscript_checker"]["logical_rounds"] == 1


def test_live_adapter_transport_retry_keeps_round_and_budget_binding(tmp_path):
    from argparse import Namespace
    from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
    original, config, parts = source(tmp_path)
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    GlobalBudgetLedger(path=project, limit_cny=60)
    GlobalBudgetLedger(path=account, limit_cny=300)
    args = Namespace(output=tmp_path / "out", ledger=project, budget_limit=60,
        account_ledger=account, account_budget_limit=300, account_mapping_dir=tmp_path / "mappings",
        incremental_budget=5, run=True, retry_failed=False)
    calls = []
    def factory(role, stage_dir, profile):
        def client(messages, **kwargs):
            calls.append(role)
            if len(calls) == 1:
                raise RuntimeError("synthetic stopped transport, no output")
            payload = json.loads(messages[1]["content"])
            return {"content": json.dumps({"snapshot_id": payload["snapshot_id"], "changes": [], "unresolved_questions": []}),
                "complete": True, "finish_reason": "stop", "returned_model": "qwen3.5-plus"}
        return client
    counter = lambda *a, **kw: 100
    first = delivery.run_live_delivery(args=args, config=config, front_back_report=parts,
        token_counter=counter, client_factory=factory)
    ordinary = delivery.run_live_delivery(args=args, config=config, front_back_report=parts,
        token_counter=counter, client_factory=factory)
    assert len(calls) == 1
    assert ordinary["halt_reasons"] == ["03_manuscript_checker"]
    args.retry_failed = True
    retried = delivery.run_live_delivery(args=args, config=config, front_back_report=parts,
        token_counter=counter, client_factory=factory)
    assert retried["downstream_status"] == "complete"
    assert len(calls) == 2
    assert retried["stages"]["03_manuscript_checker"]["logical_rounds"] == 1
    assert first["budget_binding"] == retried["budget_binding"]
    assert (args.output / "03_manuscript_checker/round_1/STAGE_ERROR.json").is_file()
    assert list((args.output / "03_manuscript_checker/round_1/retries").glob("attempt_*/RAW_RESPONSE.json"))
    recovered = delivery.run_live_delivery(args=args, config=config, front_back_report=parts,
        token_counter=counter, client_factory=lambda *a: pytest.fail("Factory on sealed recovery"))
    assert recovered["downstream_status"] == "complete"
    assert recovered["stages"]["03_manuscript_checker"]["model_calls"] == 0


def test_independent_cli_replay_preview_and_disabled(tmp_path):
    import subprocess
    original, config, parts = source(tmp_path)
    parts_path = tmp_path / "parts.json"
    parts_path.write_text(json.dumps(parts))
    cfg = tmp_path / "delivery.json"
    cfg.write_text(json.dumps({"schema": "review_v2_delivery.config.v1", "front_back": {"mode": "post_body"},
        "identity_catalogs": config["identity_catalogs"]}))
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps({"responses": {"round_1": {"snapshot_id": selected(original)["sha256"],
        "changes": [], "unresolved_questions": []}}}))
    root = Path(__file__).resolve().parents[2]
    base = [sys.executable, str(root / "scripts/upgrade3/manuscript_delivery.py"),
        "--parts-report", str(parts_path), "--config", str(cfg)]
    replay = subprocess.run([*base, "--responses", str(responses), "--output", str(tmp_path / "replay")], capture_output=True, text=True)
    assert replay.returncode == 0, replay.stderr
    payload = json.loads(replay.stdout)
    assert payload["selected_manuscript"]["status"] == "no_change"
    resume = subprocess.run([*base, "--responses", str(responses), "--output", str(tmp_path / "replay")], capture_output=True, text=True)
    assert resume.returncode == 0, resume.stderr
    assert json.loads(resume.stdout)["stages"]["03_manuscript_checker"]["cache_hit"]
    preview = subprocess.run([*base, "--output", str(tmp_path / "preview")], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["halt_reasons"] == ["03_manuscript_checker"]
    assert (tmp_path / "preview/03_manuscript_checker/round_1/MESSAGES.json").is_file()
    disabled = subprocess.run([*base, "--no-manuscript-checker", "--output", str(tmp_path / "disabled")], capture_output=True, text=True)
    assert disabled.returncode == 0, disabled.stderr
    assert json.loads(disabled.stdout)["selected_manuscript"]["status"] == "disabled"
    assert not (tmp_path / "disabled/03_manuscript_checker").exists()
