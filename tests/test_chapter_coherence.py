"""Synthetic full-outline controls through actual messages and normal factories."""
from copy import deepcopy
import io
import json
from pathlib import Path
import runpy
import socket

import pytest

from optomind_research.runtime.upgrade3 import legacy_unit_route as route
from optomind_research.runtime.upgrade3 import review_unit_writer as writer
from optomind_research.runtime.upgrade3 import unit_realization as quality
from optomind_research.runtime.upgrade3 import review_delivery as delivery
from optomind_research.runtime.upgrade3.module4 import runtime
from scripts.upgrade3 import legacy_unit_writer as cli


def book(subject="synthetic battery"):
    source = {"source_handle": "P0001", "doi": "10.1234/coherence-fixture", "title": "Synthetic source",
        "study_summary_A": {"text": "SYNTHETIC A and SYNTHETIC B are supported at setting 7.",
                            "long_material": "Full evidence preserved. " * 1800},
        "review_planning_B": {"boundary": "Independent settings require indirect comparison."}}
    pool_only = {"source_handle": "P0002", "doi": "10.1234/pool-only", "title": "Unused shared pool record"}
    chapters = []
    for chapter_id, focus, use in (("C1", "Mechanism", "Explain mechanism"),
                                  ("C2", "Translation", "Assess transfer conditions")):
        chapters.append({"chapter_id": chapter_id, "chapter_frame": {"chapter_title": focus,
            "chapter_purpose": f"{subject}: {focus}", "chapter_argument": f"{focus} argument"},
            "sources": deepcopy([source, pool_only]), "units": [{"unit_id": f"{chapter_id}_U1", "focus": focus,
                "paragraph_tasks": [{"paragraph_id": f"{chapter_id}_p1", "point": "Explain SYNTHETIC A",
                    "development": "Preserve independent setting 7.",
                    "source_brief_details": [{"point": "Original independent task", "finding_conditions": {"setting": 7}}],
                    "source_uses": [{"source_handle": "P0001", "role": "case", "use": use,
                                     "future_condition": {"setting": 7}}],
                    "future_scientific_field": {"verbatim": "Keep complete task"}},
                    {"paragraph_id": f"{chapter_id}_p2", "point": "Explain SYNTHETIC B",
                     "source_uses": [{"source_handle": "P0001", "role": "explanation", "use": "Explain B"}]}],
                "table_tasks": [], "owner_unit_context": {"conditions": "Owner setting 7"}}]})
    return {"language": "en", "source_aliases": {}, "chapters": chapters}


def counter(*args):
    return 100


def response(data):
    return {"content": json.dumps(data), "model": "qwen3.5-plus", "finish_reason": "stop", "complete": True,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def test_baseline_messages_keep_frozen_request_hashes():
    original_book = runpy.run_path(str(Path(__file__).with_name("test_legacy_unit_route_review.py")))["book"]()
    view = route.build_unit_views(original_book, body_version="baseline")[0]
    payload = route.build_legacy_payload(view)
    assert "body_version" not in payload and "chapter_responsibilities" not in payload
    assert route._hash(payload) == "5671c0497b9c39ce5a2810fdae6ef8797d265f8ce56bd509339358d916ae313a"
    assert route._hash(writer.unit_messages(view, payload=payload, planning_revision=True)) == \
        "3a8ac58f4fa2c046661e9351033a14025377a804bce1a80c493ca7b844f00d1b"
    assert route._hash(quality.assessment_messages(view, payload, "Baseline body")) == \
        "b5d4f07187ced6ad882edb95373125280f3f64800ed9cf91f1f60141ffa0e313"
    assert route._hash(writer.completion_messages(view, "Baseline body", ["p1"], planning_revision=True)) == \
        "9f414b89cd39135bcee4aa6ab01ba1bd959ad0059ad522d3a3978fc856d5f36b"


@pytest.mark.parametrize("subject", ["synthetic battery", "synthetic climate"])
def test_actual_full_outline_and_distinct_task_uses_reach_all_consumers(subject):
    data = book(subject)
    original = deepcopy(data)
    view = route.build_unit_views(data, body_version="chapter_coherence")[0]
    payload = route.build_legacy_payload(view)
    context = payload["chapter_responsibilities"]
    assert [row["chapter_id"] for row in context["chapter_outline"]] == ["C1", "C2"]
    assert context["chapter_outline"][0]["chapter_frame"] == data["chapters"][0]["chapter_frame"]
    assert [row["source_handle"] for row in context["shared_source_uses"]] == ["P0001"]
    assert {row["source_use"]["use"] for row in context["shared_source_uses"][0]["uses"]} >= \
        {"Explain mechanism", "Assess transfer conditions"}
    assert payload["paragraph_tasks"] == data["chapters"][0]["units"][0]["paragraph_tasks"]
    baseline = route.build_legacy_payload(route.build_unit_views(data)[0])
    assert payload["sources"] == baseline["sources"]
    for field, value in data["chapters"][0]["sources"][0].items():
        assert payload["sources"][0][field] == value
    author = writer.unit_messages(view, payload=payload, planning_revision=True)
    assessor = quality.assessment_messages(view, payload, "SYNTHETIC A")
    completion = writer.completion_messages(view, "SYNTHETIC A", ["C1_p2"], planning_revision=True)
    assert json.loads(author[1]["content"])["chapter_responsibilities"] == context
    assert json.loads(assessor[1]["content"])["original_writer_payload"] == payload
    assert json.loads(completion[1]["content"])["chapter_responsibilities"] == context
    assert "共享案例可以跨章使用" in author[0]["content"]
    assert "独立论点" in assessor[0]["content"] and "局部引用" in assessor[0]["content"]
    assert "already_covered" in completion[0]["content"]
    assert data == original


class QualityFactory:
    execution_mode = "injected"
    def __init__(self, missing=False, citation=False):
        self.calls, self.missing, self.citation = [], missing, citation

    def __call__(self, role, path, profile):
        def call(messages, **kwargs):
            self.calls.append((role, deepcopy(messages)))
            payload = json.loads(messages[1]["content"])
            if role == "completion":
                assert payload["requested_task_ids"] == ["C1_p2"]
                assert [task["paragraph_id"] for task in payload["paragraph_tasks"]] == ["C1_p2"]
                assert "chapter_responsibilities" in payload
                return response({"status": "appended", "body_markdown": "SYNTHETIC B [P0001].",
                                 "covered_task_ids": ["C1_p2"], "issues": []})
            absent = self.missing and role == "assessment"
            rows = [{"task_id": "C1_p1", "status": "covered", "body_quote": "SYNTHETIC A",
                     "explanation": "A is already substantive.", "material_handles": ["P0001"]},
                    {"task_id": "C1_p2", "status": "missing" if absent else "covered",
                     "body_quote": "" if absent else "SYNTHETIC B", "explanation": "B is missing." if absent else "B is substantive.",
                     "material_handles": ["P0001"]}]
            changes = [{"operation": "replace", "original_text": "SYNTHETIC A.",
                "replacement_text": "SYNTHETIC A [P0001].", "reason": "Local citation only.",
                "material_handles": ["P0001"], "material_quote": "SYNTHETIC A and SYNTHETIC B"}] \
                if self.citation and role == "assessment" else []
            return response({"tasks": rows, "changes": changes, "issues": []})
        call.prompt_token_counter = counter
        return call


@pytest.mark.parametrize("case", ["covered", "citation_only", "missing"])
def test_new_quality_repairs_only_real_gaps_and_resumes_without_rebuy(tmp_path, case):
    view = route.build_unit_views(book(), body_version="chapter_coherence")[0]
    payload = route.build_legacy_payload(view)
    factory = QualityFactory(missing=case == "missing", citation=case == "citation_only")
    body = "SYNTHETIC A. " + ("" if case == "missing" else "SYNTHETIC B.")
    result = quality.run_unit_quality(view, payload=payload, existing_body=body, output_dir=tmp_path,
        client_factory=factory, run=True, token_counter=counter, experiment_label="synthetic_coherence")
    roles = [role for role, _ in factory.calls]
    assert roles == (["assessment", "completion", "post_assessment"] if case == "missing" else
                     ["assessment", "post_assessment"] if case == "citation_only" else ["assessment"])
    if case == "covered":
        assert result["body_markdown"] == body
    elif case == "citation_only":
        assert result["body_markdown"] == "SYNTHETIC A [P0001]. SYNTHETIC B."
    else:
        assert result["body_markdown"] == body + "\n\nSYNTHETIC B [P0001]."
    resumed = quality.run_unit_quality(view, payload=payload, existing_body=body, output_dir=tmp_path,
        client_factory=factory, run=True, token_counter=counter, experiment_label="synthetic_coherence")
    assert resumed["model_calls"] == 0 and len(factory.calls) == len(roles)


def test_baseline_identity_omits_new_version_and_new_mode_cannot_rebind_it(tmp_path):
    data = book()
    route.run_legacy_units(data, output_dir=tmp_path, token_counter=counter)
    identity = route._read(tmp_path / "RUN_IDENTITY.json")
    assert "body_version" not in identity
    assert identity["prompt_sha256"] == route._hash(writer.load_writer_prompt(planning_revision=True))
    with pytest.raises(ValueError, match="identity_changed"):
        route.run_legacy_units(data, output_dir=tmp_path, token_counter=counter, body_version="chapter_coherence")
    again = route.run_legacy_units(data, output_dir=tmp_path, token_counter=counter, body_version="baseline")
    assert again["model_calls"] == 0 and route._read(tmp_path / "RUN_IDENTITY.json") == identity


class StreamResponse:
    status = 200
    headers = {"x-request-id": "synthetic-body-request"}
    def __init__(self, data):
        event = {"model": "qwen3.5-plus", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "choices": [{"delta": {"content": json.dumps(data)}, "finish_reason": "stop"}]}
        self.lines = io.BytesIO(b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n")
    def readline(self):
        return self.lines.readline()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class BodyOpener:
    def __init__(self, project, account):
        self.project, self.account, self.calls = project, account, []
    def open(self, request, timeout):
        wire = json.loads(request.data)
        self.calls.append(wire)
        assert request.get_header("Authorization") == "Bearer synthetic-first"
        assert wire["stream"] is True
        assert runtime.GlobalBudgetLedger(path=self.project).as_dict()["reserved_cny"] > 0
        assert runtime.GlobalBudgetLedger(path=self.account).as_dict()["reserved_cny"] > 0
        user_text = wire["messages"][1]["content"]
        try:
            user = json.loads(user_text)
        except ValueError:
            user = {"article_text": user_text}
        if "task_catalog" in user:
            assert wire["thinking_budget"] == 16384
            data = {"tasks": [{"task_id": task["task_id"], "status": "covered",
                "body_quote": "SYNTHETIC A and SYNTHETIC B", "explanation": "Both knowledge tasks are substantive.",
                "material_handles": ["P0001"]} for task in user["task_catalog"]], "changes": [], "issues": []}
        elif "paragraph_tasks" in user:
            assert wire["thinking_budget"] == 8192 and wire["max_completion_tokens"] == 40960
            assert user["body_version"] == "chapter_coherence"
            data = {"body_markdown": "SYNTHETIC A and SYNTHETIC B [P0001].", "issues": []}
        else:
            data = {"changes": [{"operation": "replace", "original_text": "SYNTHETIC A and SYNTHETIC B [P0001].",
                     "replacement_text": "Edited SYNTHETIC A and SYNTHETIC B [P0001].", "reason": "Synthetic local test."}],
                    "unresolved_questions": [], "no_change": False}
            # Two units repeat this synthetic anchor; edit one unique heading instead.
            text = user.get("draft_text", user.get("draft", ""))
            if not text:
                text = next(value for value in user.values() if isinstance(value, str) and "SYNTHETIC A" in value)
            heading = next(line for line in text.splitlines() if line.startswith("# "))
            data["changes"][0].update(original_text=heading, replacement_text=heading + " edited")
        return StreamResponse(data)


def test_normal_harness_uses_actual_factory_dual_budget_and_delivers_selected_edit(tmp_path, monkeypatch):
    import run_review_harness as harness
    source = tmp_path / "book.json"
    route._write(source, book())
    key = tmp_path / "synthetic-keys.txt"
    key.write_text("synthetic-first\nsynthetic-second\n", encoding="utf-8")
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    opener = BodyOpener(project, account)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: pytest.fail("Network outside synthetic transport"))
    monkeypatch.setattr(cli, "tokenizer_counter", lambda *_: (counter, {"synthetic": True}))
    argv = ["--delivery-start", "body", "--delivery-input", str(source), "--delivery-out", str(tmp_path / "out"),
        "--body-version", "chapter_coherence", "--quality-control", "--article-edit", "--run",
        "--ledger", str(project), "--budget-limit", "60", "--budget-scope", "synthetic-round",
        "--account-ledger", str(account), "--account-budget-limit", "300", "--key-file", str(key), "--key-index", "1"]
    assert harness._main_with_args(harness.build_parser().parse_args(argv)) == 0
    report = route._read(tmp_path / "out" / "body" / "DELIVERY_REPORT.json")
    assert report["model_calls"] == len(opener.calls) == 5
    assert report["effective_settings"] == {"body_version": "chapter_coherence", "quality_control": True,
        "article_edit": True, "model": "qwen3.5-plus", "thinking_budget": 8192, "output_tokens": 32768}
    selected = report["selected_body"]
    assert selected["source"] == "article_edit"
    assert selected["handles_draft"] == str(Path(report["article_edit"]["edited_draft"]).resolve())
    assert selected["reader_draft"] == report["article_edit"]["numbering"]["reader_draft"]
    assert "edited" in Path(selected["reader_draft"]).read_text(encoding="utf-8")
    assert "[1]" in Path(selected["reader_draft"]).read_text(encoding="utf-8")
    assert route._read(Path(selected["references_path"]))["references"]
    snapshots = [runtime.GlobalBudgetLedger(path=path).as_dict() for path in (project, account)]
    assert snapshots[0]["actual_cny"] == snapshots[1]["actual_cny"] > 0
    assert len(snapshots[0]["reservations"]) == len(snapshots[1]["reservations"]) == 5
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda *_: pytest.fail("Key read on exact cache resume"))
    assert harness._main_with_args(harness.build_parser().parse_args(argv)) == 0
    assert len(opener.calls) == 5
    assert route._read(tmp_path / "out" / "body" / "DELIVERY_REPORT.json")["model_calls"] == 0
    attempt = Path(report["units"][0]["attempt_dir"])
    (attempt / "UNIT_RESULT.json").unlink()
    (attempt / "RESULT_SEAL.json").unlink()
    assert harness._main_with_args(harness.build_parser().parse_args(argv)) == 0
    assert len(opener.calls) == 5 and (attempt / "RESULT_SEAL.json").is_file()
    second_edit = tmp_path / "second_edit.json"
    selected_heading = next(line for line in Path(selected["handles_draft"]).read_text(encoding="utf-8").splitlines()
                            if line.startswith("# "))
    route._write(second_edit, {"changes": [{"operation": "replace", "original_text": selected_heading,
        "replacement_text": "# UNREQUESTED_SECOND_EDIT", "reason": "Must not execute."}], "unresolved_questions": []})
    config = tmp_path / "delivery_config.json"
    route._write(config, {"schema": delivery.DELIVERY_CONFIG_SCHEMA, "text_edit": {"fixture": str(second_edit)}})
    assert harness._main_with_args(harness.build_parser().parse_args(argv + ["--delivery-config", str(config)])) == 0
    configured = route._read(tmp_path / "out" / "body" / "DELIVERY_REPORT.json")
    assert configured["stages"]["02_text_edit"]["source"] == "article_edit"
    assert configured["stages"]["02_text_edit"]["edited_draft"] == selected["handles_draft"]
    assert not (tmp_path / "out" / "body" / "02_text_edit").exists()
    assert "UNREQUESTED_SECOND_EDIT" not in Path(configured["selected_body"]["reader_draft"]).read_text(encoding="utf-8")
    assert len(opener.calls) == 5


@pytest.mark.parametrize("refused", ["project", "account"])
def test_normal_harness_enforces_each_real_factory_cap_before_http(tmp_path, monkeypatch, refused):
    import run_review_harness as harness
    data = book()
    data["chapters"] = data["chapters"][:1]
    source = tmp_path / "book.json"
    route._write(source, data)
    key = tmp_path / "synthetic-keys.txt"
    key.write_text("synthetic-first\n", encoding="utf-8")
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    opener = BodyOpener(project, account)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr(cli, "tokenizer_counter", lambda *_: (counter, {"synthetic": True}))
    args = harness.build_parser().parse_args(["--delivery-start", "body", "--delivery-input", str(source),
        "--delivery-out", str(tmp_path / "out"), "--run", "--no-quality-control", "--no-article-edit",
        "--ledger", str(project), "--budget-limit", "0.000001" if refused == "project" else "60",
        "--account-ledger", str(account), "--account-budget-limit", "0.000001" if refused == "account" else "300",
        "--key-file", str(key), "--key-index", "1"])
    assert harness._main_with_args(args) == 2
    assert not opener.calls
    for path in (project, account):
        snapshot = runtime.GlobalBudgetLedger(path=path).as_dict()
        assert snapshot["reserved_cny"] == snapshot["actual_cny"] == 0


def test_normal_body_preview_keeps_entire_book_and_explicit_off_flags(tmp_path, monkeypatch):
    import run_review_harness as harness
    source = tmp_path / "book.json"
    route._write(source, book())
    monkeypatch.setattr(cli, "_dedicated_factory", lambda *_: pytest.fail("Live factory during preview"))
    args = harness.build_parser().parse_args(["--delivery-start", "body", "--delivery-input", str(source),
        "--delivery-out", str(tmp_path / "out"), "--body-version", "chapter_coherence", "--only-unit", "C1:C1_U1",
        "--no-quality-control", "--no-article-edit"])
    assert harness._main_with_args(args) == 0
    report = route._read(tmp_path / "out" / "body" / "DELIVERY_REPORT.json")
    assert report["model_calls"] == 0 and report["full_unit_count"] == 2
    assert not report["effective_settings"]["quality_control"] and not report["effective_settings"]["article_edit"]
    messages = route._read(Path(report["units"][0]["messages_path"]))
    assert len(json.loads(messages[1]["content"])["chapter_responsibilities"]["chapter_outline"]) == 2
    assert report["selected_body"]["status"] == "not_generated"
