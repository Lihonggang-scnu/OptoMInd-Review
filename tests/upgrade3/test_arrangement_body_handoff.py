"""Bounded arrangement -> normal BODY handoff, using only existing offline fixtures."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.upgrade3 import chapter_arrangement as cli
from scripts.upgrade3 import fullbody_writer as body
from optomind_research.runtime.upgrade3.module4 import runtime
from test_body_preflight_f1_arrangement_cache_consumer import (
    _dump, _packet, _read, _response, no_network,  # noqa: F401
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def handoff(tmp_path):
    supplied = os.environ.get("ARRANGEMENT_BODY_HANDOFF_EVIDENCE_ROOT")
    root = (Path(supplied) / tmp_path.name if supplied else tmp_path).resolve()
    packet_root = _packet(root)
    packet = _read(packet_root / "writer_packets/CH01.json")
    other = copy.deepcopy(packet)
    other["chapter_id"] = "CH02"
    _dump(packet_root / "writer_packets/CH02.json", other)
    plan = _read(packet_root / "DETAILED_REVIEW_PLAN.json")
    # BODY export order is explicit; neither lexicographic nor incidental
    # shared_outline order may replace it.
    plan.update(writer_packets=[{"chapter_id": item} for item in ("CH02", "CH01")],
                shared_outline=[{"chapter_id": item} for item in ("CH01", "CH02")],
                original_user_request="Explain both findings with their conditions.",
                target_reader="Researchers", shared_scope={"include": "Both chapters"}, language="en")
    _dump(packet_root / "DETAILED_REVIEW_PLAN.json", plan)
    pointer = root / "CURRENT_PLAN.json"
    _dump(pointer, {"packet_root": "packet"})
    responses = []
    for chapter_id in ("CH02", "CH01"):
        response = _response("normal")
        response["chapter_id"] = chapter_id
        path = root / (chapter_id + "_RESPONSE.json")
        _dump(path, response)
        responses.append(path)
    return {"root": root, "packet_root": packet_root, "pointer": pointer, "responses": responses,
            "output": root / "arranged"}


def arguments(case, *extra):
    return ["--packet-root", str(case["pointer"]), "--output-root", str(case["output"]),
            "--planning-revision", *extra]


def fake_provider(monkeypatch, *, failure=None):
    calls = []

    class Ledger:
        def __init__(self, *_args, **_kwargs):
            pass

        def round_state(self):
            return {"offline": True, "spent_cny": 0}

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def complete(self, messages, **_kwargs):
            payload = json.loads(messages[-1]["content"])
            calls.append(copy.deepcopy(payload))
            if failure == "budget":
                raise cli.RoundBudgetExceeded("offline_refused_round_budget")
            if failure == "exception":
                raise RuntimeError("offline_failed_chapter")
            response = _response("normal")
            response["chapter_id"] = payload["chapter_id"]
            if failure == "invalid":
                response["units"][0]["paragraph_tasks"][0]["source_briefs"] = ["missing-brief"]
            return {"response": response, "complete": True, "finish_reason": "stop"}

    monkeypatch.setattr(cli, "RoundCappedLedger", Ledger)
    monkeypatch.setattr(runtime, "QwenDirectClient", Provider)
    return calls


@pytest.mark.parametrize("selector", ["pointer", "root", "packet_root"])
def test_saved_response_real_command_exports_ordered_body_manifest(handoff, selector):
    case = handoff
    command = [sys.executable, "-X", "utf8", str(ROOT / "scripts/upgrade3/chapter_arrangement.py"),
               "--packet-root", str(case[selector]), "--output-root", str(case["output"]),
               "--all-chapters", "--planning-revision", "--key-file", str(case["root"] / "never-read.key")]
    for response in case["responses"]:
        command.extend(["--reexport-from", str(response)])
    original = {path: path.read_bytes() for path in case["packet_root"].rglob("*.json")}
    result = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", capture_output=True, check=False)
    _dump(case["root"] / "REEXPORT_COMMAND.json", {"command": command, "exit_code": result.returncode,
                                                  "stdout": result.stdout, "stderr": result.stderr})
    assert result.returncode == 0, result.stderr
    report = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert report["body_ready"] is True and report["model_calls"] == 0
    assert report["successful_chapter_ids"] == ["CH02", "CH01"]
    book, prepared = body.load_body_manifest(report["body_manifest"])
    assert prepared["actual_chapter_ids"] == ["CH02", "CH01"]
    assert book["user_request"] == "Explain both findings with their conditions."
    assert book["target_reader"] == "Researchers"
    assert book["review_scope"] == {"include": "Both chapters"}
    serialized = json.dumps(book, ensure_ascii=False)
    assert "Condition one and its boundary" in serialized
    assert "Review-derived finding two with citation identity" in serialized
    assert all(path.read_bytes() == value for path, value in original.items())
    if selector != "packet_root":
        assert str(case["pointer"]) in {row["path"] for row in prepared["source_files"]}


def test_cache_reuse_keeps_signature_and_feeds_normal_body_preview(handoff, monkeypatch):
    import run_review_harness as harness
    from scripts.upgrade3 import legacy_unit_writer as unit_cli
    case = handoff
    calls = fake_provider(monkeypatch)
    argv = arguments(case, "--all-chapters", "--run")
    assert cli.main(argv) == 0
    before = _read(case["output"] / "ARRANGEMENT_RUN.json")
    before_book, before_prepared = body.load_body_manifest(before["body_manifest"])
    assert len(calls) == 2
    assert cli.main(argv) == 0
    after = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert len(calls) == 2 and after["model_calls"] == 0 and after["body_ready"] is True
    assert [row["signature"] for row in after["chapters"]] == [row["signature"] for row in before["chapters"]]
    assert [row["status"] for row in after["chapters"]] == ["reused_arranged", "reused_arranged"]
    after_book, after_prepared = body.load_body_manifest(after["body_manifest"])
    assert after_book == before_book
    assert after_prepared == before_prepared
    assert after["body_manifest"] == before["body_manifest"]
    monkeypatch.setattr(unit_cli, "_dedicated_factory", lambda *_: pytest.fail("BODY preview created a live provider"))
    args = harness.build_parser().parse_args([
        "--delivery-start", "body", "--delivery-manifest", after["body_manifest"],
        "--delivery-out", str(case["root"] / "body_preview"), "--no-quality-control", "--no-article-edit"])
    assert harness._main_with_args(args) == 0
    assert harness._main_with_args(args) == 0
    delivery = _read(case["root"] / "body_preview/body/DELIVERY_REPORT.json")
    assert delivery["model_calls"] == 0 and delivery["full_unit_count"] == 2


@pytest.mark.parametrize("changed", ["material", "condition"])
def test_real_input_change_refreshes_accepted_body_identity(handoff, monkeypatch, changed):
    case = handoff
    calls = fake_provider(monkeypatch)
    argv = arguments(case, "--all-chapters", "--run")
    assert cli.main(argv) == 0
    before = _read(case["output"] / "ARRANGEMENT_RUN.json")
    before_book, _ = body.load_body_manifest(before["body_manifest"])
    packet_path = case["packet_root"] / "writer_packets/CH01.json"
    packet = _read(packet_path)
    if changed == "material":
        packet["source_materials"][0]["study_summary_A"]["finding"] += " Changed supported observation."
    else:
        packet["chapter_plan"]["units"][0]["paragraph_briefs"][0]["development"] += " Changed actual condition."
    _dump(packet_path, packet)
    assert cli.main(argv) == 0
    after = _read(case["output"] / "ARRANGEMENT_RUN.json")
    after_book, _ = body.load_body_manifest(after["body_manifest"])
    # Keep the existing arrangement cache contract: this fixture's raw A
    # finding is consumed through the packet locator by BODY, while a changed
    # owner condition is also an arrangement-model input.
    new_calls = 0 if changed == "material" else 1
    assert len(calls) == 2 + new_calls and after["model_calls"] == new_calls
    assert after["body_manifest"] != before["body_manifest"] and after_book != before_book
    assert "Changed supported observation" in json.dumps(after_book) if changed == "material" else \
        "Changed actual condition" in json.dumps(after_book)
    with pytest.raises(ValueError, match="manifest_file_hash_mismatch"):
        body.load_body_manifest(before["body_manifest"])


def test_changed_source_use_refreshes_body_identity_without_model_calls(handoff):
    case = handoff
    argv = arguments(case, "--all-chapters")
    for path in case["responses"]:
        argv.extend(["--reexport-from", str(path)])
    assert cli.main(argv) == 0
    before = _read(case["output"] / "ARRANGEMENT_RUN.json")
    before_book, _ = body.load_body_manifest(before["body_manifest"])
    response = _read(case["responses"][1])
    response["units"][0]["paragraph_tasks"][0]["source_uses"] = [
        {"source_handle": "P0001", "role": "boundary", "use": "Changed actual source use"}]
    _dump(case["responses"][1], response)
    assert cli.main(argv) == 0
    after = _read(case["output"] / "ARRANGEMENT_RUN.json")
    after_book, _ = body.load_body_manifest(after["body_manifest"])
    assert after["model_calls"] == 0 and after["body_manifest"] != before["body_manifest"]
    assert after_book != before_book and "Changed actual source use" in json.dumps(after_book)
    body.load_body_manifest(before["body_manifest"])


@pytest.mark.parametrize("failure", ["budget", "exception", "invalid"])
def test_failed_complete_retry_clears_current_ready_and_preserves_valid_products(handoff, monkeypatch, failure):
    case = handoff
    fake_provider(monkeypatch)
    assert cli.main(arguments(case, "--all-chapters", "--run")) == 0
    previous = _read(case["output"] / "ARRANGEMENT_RUN.json")
    canonical = {path: path.read_bytes() for chapter in ("CH02", "CH01")
                 for path in (case["output"] / chapter).glob("*.json")}
    old_manifest = Path(previous["body_manifest"])
    original_manifest = old_manifest.read_bytes()
    fake_provider(monkeypatch, failure=failure)
    assert cli.main(arguments(case, "--all-chapters", "--run", "--force")) == 2
    failed = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert failed["status"] == "failed" and failed["body_ready"] is False
    assert failed["body_manifest"] is None and failed["pending_chapter_ids"] == ["CH02", "CH01"]
    assert old_manifest.read_bytes() == original_manifest
    assert all(path.read_bytes() == value for path, value in canonical.items())
    body.load_body_manifest(old_manifest)


def test_partial_and_preview_never_publish_full_ready_manifest(handoff, monkeypatch):
    case = handoff
    fake_provider(monkeypatch)
    assert cli.main(arguments(case, "--chapter", "CH01", "--reexport-from", str(case["responses"][1]))) == 0
    partial = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert partial["status"] == "partial_arranged" and partial["complete"] is True
    assert partial["body_ready"] is False and partial["body_manifest"] is None
    assert partial["pending_chapter_ids"] == ["CH02"]
    assert cli.main(arguments(case, "--all-chapters", "--run")) == 0
    previous = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert cli.main(arguments(case, "--all-chapters")) == 0
    preview = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert preview["status"] == "preview_only" and preview["body_ready"] is False
    assert preview["body_manifest"] is None and preview["model_calls"] == 0
    body.load_body_manifest(previous["body_manifest"])


def test_pointer_change_during_arrangement_cannot_publish_mixed_versions(handoff, monkeypatch):
    case = handoff
    fake_provider(monkeypatch)
    original_run = cli.run_arrangement

    def change_pointer(*args, **kwargs):
        result = original_run(*args, **kwargs)
        _dump(case["pointer"], {"packet_root": "a-different-version"})
        return result

    monkeypatch.setattr(cli, "run_arrangement", change_pointer)
    assert cli.main(arguments(case, "--all-chapters", "--run")) == 2
    report = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert report["body_ready"] is False and report["body_manifest"] is None
    assert "manifest_file_hash_mismatch" in report["error"]
    assert not list(case["output"].rglob("BODY_MANIFEST.json"))


def test_early_failure_replaces_stale_success_report(handoff):
    case = handoff
    _dump(case["output"] / "ARRANGEMENT_RUN.json", {"body_ready": True, "body_manifest": "stale.json"})
    _dump(case["pointer"], {"packet_root": "missing"})
    assert cli.main(arguments(case, "--all-chapters")) == 2
    report = _read(case["output"] / "ARRANGEMENT_RUN.json")
    assert report["status"] == "failed" and report["body_ready"] is False and report["body_manifest"] is None
