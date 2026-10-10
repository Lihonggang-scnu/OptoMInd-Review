"""Native offline BODY -> selected handles -> actual parts/citations/publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import socket
import sys

import pytest

import run_review_harness as harness
from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from optomind_research.runtime.upgrade3 import review_delivery as delivery
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import fullbody_writer, legacy_unit_writer as cli

ROOT = Path(__file__).resolve().parents[1]
WIRE = runpy.run_path(str(ROOT / "tests/test_chapter_coherence.py"))
MANIFEST = runpy.run_path(str(ROOT / "tests/upgrade3/test_fullbody_cli.py"))
PARTS = runpy.run_path(str(ROOT / "tests/upgrade3/test_serial_parts_delivery_integration.py"))
LEGACY_PARTS = runpy.run_path(str(ROOT / "tests/upgrade3/test_review_delivery_integration.py"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("closeout fixtures forbid external network")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


def normal_run(tmp_path, monkeypatch, *, input_mode="input", parts_mode=None, quality=True, extra=(), failure_stage=None):
    case = MANIFEST["case"].__wrapped__(tmp_path)
    book, _ = fullbody_writer.load_body_manifest(case["manifest"])
    source = tmp_path / "FULL_BODY_INPUT.json"
    route._write(source, book)
    key = tmp_path / "synthetic-keys.txt"
    key.write_text("synthetic-first\n", encoding="utf-8")
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    issue = {"code": "synthetic_scientific_diagnostic", "detail": "Retain this completed-stage diagnostic."}
    opener = WIRE["BodyOpener"](project, account, "baseline", pending_issue=issue)
    if failure_stage:
        normal_open = opener.open
        def stopped_open(request, timeout):
            user_text = json.loads(request.data)["messages"][1]["content"]
            is_quality = '"task_catalog"' in user_text
            is_editor = "【全文句柄稿】" in user_text
            if (failure_stage == "quality" and is_quality) or (failure_stage == "editor" and is_editor):
                raise RuntimeError("synthetic_required_stage_failure")
            return normal_open(request, timeout)
        opener.open = stopped_open
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr(cli, "tokenizer_counter", lambda *_: (WIRE["counter"], {"synthetic": True}))
    argv = ["--delivery-start", "body", "--delivery-out", str(tmp_path / "out"), "--run",
        "--ledger", str(project), "--budget-limit", "60", "--budget-scope", "synthetic-closeout",
        "--account-ledger", str(account), "--account-budget-limit", "300", "--key-file", str(key), "--key-index", "1"]
    argv += ["--delivery-manifest", str(case["manifest"])] if input_mode == "manifest" else ["--delivery-input", str(source)]
    if not quality:
        argv += ["--no-quality-control", "--no-article-edit"]
    if parts_mode:
        fixture = PARTS["_parts_fixture"]() if parts_mode == "post_body" else LEGACY_PARTS["_fb_fixture"]()
        route._write(tmp_path / "parts.json", fixture)
        route._write(tmp_path / "unrequested-edit.json", {"fixture": True, "changes": [{"operation": "replace",
            "original_text": "SYNTHETIC A", "replacement_text": "UNREQUESTED SECOND EDIT", "reason": "Must not run."}],
            "unresolved_questions": []})
        route._write(tmp_path / "config.json", {"schema": delivery.DELIVERY_CONFIG_SCHEMA, "language": "en",
            "research_question": book["research_question"], "front_back": {"mode": parts_mode, "fixture": "parts.json"},
            "text_edit": {"fixture": "unrequested-edit.json"}})
        argv += ["--delivery-config", str(tmp_path / "config.json")]
    argv += list(extra)
    monkeypatch.setattr(sys, "argv", ["run_review_harness.py", *argv])
    code = harness.main()
    report = route._read(tmp_path / "out/body/DELIVERY_REPORT.json")
    return argv, opener, report, code


@pytest.mark.parametrize("input_mode", ["input", "manifest"])
@pytest.mark.parametrize("parts_mode", ["legacy", "post_body"])
@pytest.mark.parametrize("quality", [True, False])
def test_actual_normal_inputs_parts_and_publication_keep_selected_body_and_diagnostics(
    tmp_path, monkeypatch, input_mode, parts_mode, quality,
):
    argv, opener, report, code = normal_run(tmp_path, monkeypatch,
        input_mode=input_mode, parts_mode=parts_mode, quality=quality)
    assert code == 0
    assert report["model_calls"] == len(opener.calls) == (5 if quality else 2)
    assert report["body_delivery"]["status"] == ("complete_with_diagnostics" if quality else "complete")
    assert report["body_delivery"]["ready"] and report["body_delivery"]["scientific_acceptance"] is False
    if quality:
        assert report["assembly"]["problems_resolved"] is False and report["assembly"]["pending_problems"]
    selected, stages = report["selected_body"], report["stages"]
    assert selected["source"] == ("article_edit" if quality else "assembly")
    assert stages["02_text_edit"]["status"] == "body_selected"
    assert stages["02_text_edit"]["edited_draft"] == selected["handles_draft"]
    assert not (tmp_path / "out/body/02_text_edit").exists()
    assert stages["03_front_back"]["source_draft"] == selected["handles_draft"]
    source_text = Path(selected["handles_draft"]).read_text(encoding="utf-8")
    assert "[P0001]" in source_text
    assert report["downstream_status"] == "complete"
    reader = Path(stages["04_figures_citations"]["reader_draft"])
    published = Path(stages["05_publication"]["published_markdown"])
    assert stages["05_publication"]["reader_draft"] == str(reader.resolve())
    assert published.read_text(encoding="utf-8") == reader.read_text(encoding="utf-8").rstrip() + "\n"
    assert "SYNTHETIC A and SYNTHETIC B [1]" in published.read_text(encoding="utf-8")
    assert "UNREQUESTED SECOND EDIT" not in published.read_text(encoding="utf-8")
    refs = route._read(Path(stages["04_figures_citations"]["references_path"]))
    assert {row["paper_id"] for row in refs["references"]} == {"stable-paper"}
    if parts_mode == "post_body":
        assert stages["03_front_back"]["source_identity_map"]["P0001"]["paper_id"] == "stable-paper"
        from optomind_research.runtime.upgrade3.serial_parts_application import extract_owned_parts, extract_body
        from optomind_research.runtime.upgrade3.article_text_editor import _numbering_projection
        original_parts = extract_owned_parts(Path(stages["03_front_back"]["final_manuscript"]).read_text(encoding="utf-8"))
        assert extract_owned_parts(reader.read_text(encoding="utf-8")) == original_parts
        assert extract_owned_parts(published.read_text(encoding="utf-8")) == original_parts
        assert published.read_text(encoding="utf-8").count("<!-- manuscript-part:conclusion:start -->") == 1
        assert published.read_text(encoding="utf-8").count("<!-- manuscript-part:conclusion:end -->") == 1
        projection = stages["04_figures_citations"]["input_projection"]
        projection_text = Path(projection["numbering_input"]).read_text(encoding="utf-8")
        assert projection["source_draft"] == stages["03_front_back"]["final_manuscript"]
        assert projection["source_sha256"] == hashlib.sha256(Path(projection["source_draft"]).read_bytes()).hexdigest()
        assert projection["numbering_input_sha256"] == hashlib.sha256(Path(projection["numbering_input"]).read_bytes()).hexdigest()
        assert route._read(reader.parent / "STAGE_REPORT.json")["input_projection"] == projection
        assert _numbering_projection(extract_body(source_text)).strip() == extract_body(projection_text).strip()
    else:
        assert "input_projection" not in stages["04_figures_citations"]
    original = [Path(row["attempt_dir"]) / "RAW_RESPONSE.json" for row in report["units"]]
    original_bytes = [path.read_bytes() for path in original]
    snapshots = [runtime.GlobalBudgetLedger(path=tmp_path / name).as_dict() for name in ("project.sqlite", "account.sqlite")]
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda *_: pytest.fail("Key read during BODY cache recovery"))
    monkeypatch.setattr(sys, "argv", ["run_review_harness.py", *argv])
    resumed_code = harness.main()
    resumed = route._read(tmp_path / "out/body/DELIVERY_REPORT.json")
    assert resumed["model_calls"] == 0 and len(opener.calls) == (5 if quality else 2)
    assert [path.read_bytes() for path in original] == original_bytes
    assert [runtime.GlobalBudgetLedger(path=tmp_path / name).as_dict() for name in ("project.sqlite", "account.sqlite")] == snapshots
    assert resumed["selected_body"] == selected
    assert [row["pending_problems"] for row in resumed["units"]] == [row["pending_problems"] for row in report["units"]]
    if parts_mode == "post_body":
        # The unchanged serial module explicitly requires a fresh parts dir.
        assert resumed_code == 2 and resumed["halt_reasons"] == ["03_front_back"]
        assert "out_dir_not_empty:no_resume_supported" in resumed["stages"]["03_front_back"]["error"]
        assert "05_publication" not in resumed["stages"]
    else:
        assert resumed_code == 0 and resumed["downstream_status"] == "complete"
    # BODY-only recovery remains available in the same directory after either
    # downstream result, without rerunning or replacing the selected body.
    index = argv.index("--delivery-config")
    monkeypatch.setattr(sys, "argv", ["run_review_harness.py", *(argv[:index] + argv[index + 2:])])
    assert harness.main() == 0
    assert route._read(tmp_path / "out/body/DELIVERY_REPORT.json")["model_calls"] == 0


@pytest.mark.parametrize("problem", ["missing_unit", "quality_planned", "quality_failed", "quality_missing",
    "editor_planned", "editor_failed", "numbering_pending", "selected_missing"])
def test_completed_numbering_cannot_hide_missing_body_or_required_stage(tmp_path, monkeypatch, problem):
    _, _, report, code = normal_run(tmp_path, monkeypatch)
    assert code == 0
    broken = deepcopy(report)
    if problem == "missing_unit":
        broken["generation_complete"] = False
        broken["missing_units"] = ["CH02:U1"]
    elif problem.startswith("quality_"):
        quality = broken["units"][0]["quality"]
        if problem == "quality_missing":
            broken["units"][0].pop("quality")
        elif problem == "quality_planned":
            quality["status"] = "planned"
        else:
            quality["stage_attempts"][0]["state"] = "failed_saved_attempt"
    elif problem.startswith("editor_"):
        broken["article_edit"]["status"] = "planned" if problem == "editor_planned" else "pending"
    elif problem == "numbering_pending":
        broken["selected_body"]["status"] = "pending"
    else:
        broken["selected_body"]["handles_draft"] = str(tmp_path / "missing-selected.md")
    broken["body_delivery"] = delivery.body_delivery_state(broken)
    assert broken["body_delivery"]["status"] == "incomplete" and broken["body_delivery"]["blocking_reasons"]
    assert delivery.body_delivery_exit_code(broken) == 2
    result = delivery.run_downstream_delivery(config={}, assembly_report=broken, out_dir=tmp_path / "blocked",
        selected_body=broken["selected_body"])
    assert result["halt_reasons"] == ["assembly_pending"] and result["stages"] == {}
    assert not (tmp_path / "blocked/03_front_back").exists()


def test_standalone_exit_accepts_diagnostic_body_and_keeps_old_stage_defaults(tmp_path, monkeypatch):
    argv, opener, report, _ = normal_run(tmp_path, monkeypatch)
    args = ["--input", str(tmp_path / "FULL_BODY_INPUT.json"), "--output-dir", str(tmp_path / "out/body"),
        "--quality-control", "--article-edit", *argv[argv.index("--run"):argv.index("--delivery-input")]]
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda *_: pytest.fail("Key read during standalone cache recovery"))
    assert cli.main(args) == 0 and len(opener.calls) == 5
    assert route._read(tmp_path / "out/body/DELIVERY_REPORT.json")["body_delivery"]["status"] == "complete_with_diagnostics"


def test_explicit_identity_catalog_remains_authoritative(tmp_path, monkeypatch):
    _, _, report, _ = normal_run(tmp_path, monkeypatch, quality=False)
    parts = tmp_path / "parts.json"
    route._write(parts, LEGACY_PARTS["_fb_fixture"]())
    config = {"front_back_fixture": parts, "identity_catalogs": [{"entries": [{"source_handle": "P0001",
        "paper_id": "explicit-paper", "title": "Explicit configured inventory"}]}]}
    result = delivery.run_downstream_delivery(config=config, assembly_report=report, out_dir=tmp_path / "explicit",
        selected_body=report["selected_body"])
    assert result["downstream_status"] == "complete"
    refs = route._read(Path(result["stages"]["04_figures_citations"]["references_path"]))
    assert {row["paper_id"] for row in refs["references"]} == {"explicit-paper"}


def test_explicit_empty_catalog_does_not_silently_gain_body_catalog(tmp_path, monkeypatch):
    _, _, report, _ = normal_run(tmp_path, monkeypatch, quality=False)
    route._write(tmp_path / "parts.json", LEGACY_PARTS["_fb_fixture"]())
    route._write(tmp_path / "explicit-empty.json", {"schema": delivery.DELIVERY_CONFIG_SCHEMA,
        "front_back": {"fixture": "parts.json"}, "identity_catalogs": []})
    config = delivery.load_delivery_config(tmp_path / "explicit-empty.json")
    assert config["identity_catalogs_explicit"]
    result = delivery.run_downstream_delivery(config=config, assembly_report=report, out_dir=tmp_path / "empty",
        selected_body=report["selected_body"])
    assert result["stages"]["04_figures_citations"]["status"] == "pending"
    assert result["downstream_status"] == "pending"
    assert delivery.body_delivery_exit_code({**report, **result}) == 2


@pytest.mark.parametrize("stage", ["quality", "editor"])
def test_actual_required_transport_stage_failure_is_nonzero_despite_readable_body(tmp_path, monkeypatch, stage):
    _, _, report, code = normal_run(tmp_path, monkeypatch, failure_stage=stage)
    assert code == 2 and report["generation_complete"]
    assert Path(report["selected_body"]["reader_draft"]).is_file()
    assert report["body_delivery"]["status"] == "incomplete"
    assert any(reason.startswith("body_quality_incomplete:" if stage == "quality" else "body_article_edit_incomplete")
        for reason in report["body_delivery"]["blocking_reasons"])


@pytest.mark.parametrize("flags,calls,source", [(["--no-quality-control"], 3, "article_edit"),
    (["--no-article-edit"], 4, "assembly")])
def test_normal_quality_and_edit_switches_work_independently(tmp_path, monkeypatch, flags, calls, source):
    _, opener, report, code = normal_run(tmp_path, monkeypatch, extra=flags)
    assert code == 0 and len(opener.calls) == calls
    assert report["selected_body"]["source"] == source
    assert report["body_delivery"]["ready"]


def test_body_partial_legacy_parts_halts_before_numbering_and_publication(tmp_path, monkeypatch):
    _, _, report, _ = normal_run(tmp_path, monkeypatch, quality=False)
    route._write(tmp_path / "partial-parts.json", {"fixture": True, "conclusion": {"conclusion": "A bounded conclusion."}})
    result = delivery.run_downstream_delivery(config={"front_back_fixture": tmp_path / "partial-parts.json"},
        assembly_report=report, out_dir=tmp_path / "partial", selected_body=report["selected_body"])
    assert result["stages"]["03_front_back"]["status"] == "partial"
    assert result["halt_reasons"] == ["03_front_back"] and "04_figures_citations" not in result["stages"]
    assert delivery.body_delivery_exit_code({**report, **result}) == 2
