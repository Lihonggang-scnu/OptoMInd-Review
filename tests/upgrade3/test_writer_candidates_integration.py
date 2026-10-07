"""Independent integration checks with real builders, parser, cache and CLI.

The historical OWNER response and its public materials are authentic archived
exports, but sanitized. They are not the original private paid request. All new
model outputs below are deliberately controlled wiring fixtures. No scientific
quality conclusion or paid-call authorization follows from these tests.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
import socket
import sqlite3

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import review_unit_writer as existing_writer
from scripts.upgrade3 import chapter_arrangement as arrangement_cli

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/verification/on-demand-efficiency-20261007"
QUALITY_ARCHIVE = ROOT / "docs/acceptance/quality-capacity-20261006/evidence"
TABLE = "| Setting | Result | Source |\n|---|---|---|\n| 30 s indoor window | Recovery only | [P0001] |"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Writer candidate integration tests must stay offline")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def profile(**extra):
    stage = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
             "max_output_tokens": 4096, "json_mode": False, "timeout_seconds": 33,
             "stream": True, "stream_overall_timeout_seconds": 99}
    return {"writer": deepcopy(stage), "editor": {**stage, "thinking_budget": 2048},
            "editor_pass": True, **extra}


def recorded_packet():
    text = read(ARCHIVE / "integration_handoff/OWNER_MESSAGES.json")["messages"][1]["content"]
    owner = strengthening.expand_material_projection(json.JSONDecoder().raw_decode(text[text.index("{"):])[0])
    response = read(ARCHIVE / "integration_handoff/OWNER_RESPONSE.json")["model_output"]["parsed_output"]
    plan = read(ARCHIVE / "UPDATED_PLAN.json")
    assert response["chapter_updates"][0]["updated_plan"] == plan
    packet = strengthening.project_plan_for_arrangement(owner, {
        "status": "updated", "updated_plan": plan, "unit_id_remap": response["unit_id_remap"],
        "accepted_source_materials": owner["source_materials"],
    })
    assert len(owner["source_materials"]) == 18
    return packet, plan


def synthetic_packet(*, long_material=False):
    """Solar-cell context is a synthetic transport control, not research data."""
    units = []
    for index, uid in enumerate(("CON:recovery/first", "LPT9.boundary\\second")):
        briefs = []
        for ordinal in (1, 2):
            number = index * 2 + ordinal
            briefs.append({"paragraph_id": f"{uid}:P{ordinal}",
                           "point": f"Explain measurement {number}",
                           "development": f"Retain condition {number} and distinguish recovery from lifetime",
                           "source_handles": [f"P000{number}"],
                           "finding_conditions": {"window_s": 30 * number, "outside_field": False},
                           "unabridged_task_tail": f"TASK_TAIL_{number}"})
        units.append({"unit_id": uid, "substantive_point": "Conditional solar-cell recovery",
                      "paragraph_briefs": briefs,
                      "synthesis_and_transition": {"boundary": "Recovery does not establish field lifetime"}})
    sources = []
    for number in range(1, 5):
        sources.append({"source_handle": f"P000{number}", "paper_id": f"solar-{number}",
                        "doi": f"10.0000/synthetic-solar-{number}", "title": f"Synthetic solar measurement {number}",
                        "study_summary_A": {"finding": f"Observed recovery {number}",
                                            "method": (f"MEASUREMENT_{number} " * 700 if long_material else "Indoor measurement"),
                                            "tail": f"MATERIAL_TAIL_{number}", "negative": [], "uncertainty": None},
                        "review_planning_B": {"planning_summary": f"Keep boundary {number}", "field_lifetime_supported": False,
                                              "zero_change": 0, "conditions": {"window_s": 30 * number}}})
    alias = deepcopy(sources[0])
    alias.update(source_handle="P0091", paper_id="solar-alias",
                 study_summary_A={"finding": "Complementary alias-only recovery result", "tail": "ALIAS_MATERIAL_TAIL"},
                 review_planning_B={"planning_summary": "Alias adds a separate window", "conditions": {"window_s": 120}})
    sources.append(alias)
    units[0]["paragraph_briefs"][0]["source_handles"].append("P0091")
    return {"chapter_id": "SOLAR", "chapter": {"chapter_id": "SOLAR", "title": "Measured recovery"},
            "research_question": "What does recovery establish?", "review_argument": "Measurement windows constrain interpretation",
            "chapter_plan": {"chapter_id": "SOLAR", "thesis": "Recovery does not establish lifetime", "units": units},
            "source_materials": sources}


def formal_arrangement(tmp_path, packet, *, table=False):
    """Exercise the existing production arranger and saved-view contract."""
    chapter_id = packet["chapter_id"]
    root = tmp_path / "packets"
    packet_path = dump(root / f"writer_packets/{chapter_id}.json", packet)
    dump(root / "DETAILED_REVIEW_PLAN.json", {
        "shared_outline": [{"chapter_id": chapter_id, "title": "Controlled consumer fixture"}],
        "review_argument": packet.get("review_argument", ""), "chapters": [packet],
        "writer_packets": [{"chapter_id": chapter_id, "json_path": f"writer_packets/{chapter_id}.json"}],
    })
    view = arranging.build_chapter_view(packet_path)
    units = [{"unit_id": u.unit_id, "paragraph_tasks": [
        {"paragraph_id": b.paragraph_id, "source_briefs": [b.paragraph_id],
         "point": b.point, "development": b.development,
         "source_uses": [{"source_handle": h, "role": "support", "use": "Retain full owner task"}
                         for h in b.source_handles]} for b in u.paragraph_briefs]} for u in view.units]
    if table:
        units[0]["table_tasks"] = [{"table_id": "AUX:measurement/table", "purpose": "Separate measured recovery from lifetime",
            "columns": ["Setting", "Result", "Source"], "row_tasks": [{"content": "30 s indoor recovery",
                "source_uses": [{"source_handle": "P0001"}], "conditions": {"window_s": 30},
                "limits": ["No outdoor lifetime claim"]}]}]
    used = {use["source_handle"] for unit in units for task in unit["paragraph_tasks"] for use in task["source_uses"]}
    controlled = {"chapter_id": chapter_id, "units": units, "unused_sources": [
        {"source_handle": source.source_handle, "reason": "Context material remains available"}
        for source in view.sources if source.source_handle not in used]}
    response = dump(tmp_path / "CONTROLLED_ARRANGEMENT.json", controlled)
    out = tmp_path / "arrangement"
    assert arrangement_cli.main(["--packet-root", str(root), "--chapter", chapter_id,
        "--output-root", str(out), "--reexport-from", str(response)]) == 0
    path = out / chapter_id / "CHAPTER_ARRANGEMENT.json"
    assert read(path)["validation"]["ok"] is True
    return path, root


def fixtures_for_payload(payload, *, editor=False, omit_table=False):
    ids = payload["editable_task_ids"]
    tables = {str(u["unit_id"]) + "::" + str(t["table_id"])
              for u in payload.get("units", []) for t in u.get("table_tasks", [])}
    citation = str(payload["sources"][0]["source_handle"]) if payload.get("sources") else "P0001"
    blocks = [{"block_id": f"{'edited' if editor else 'draft'}-{index}", "task_ids": [task_id],
               "body_markdown": ("Table mentioned without rows." if omit_table else TABLE) if task_id in tables
               else f"{'EDITED' if editor else 'DRAFT'} fixture for {task_id}. [{citation}]"}
              for index, task_id in enumerate(ids)]
    return {"blocks": blocks, "issues": []}


class CaptureFactory:
    def __init__(self, *, omit_editor_table=False, fail_editor=False):
        self.calls = []
        self.omit_editor_table = omit_editor_table
        self.fail_editor = fail_editor

    def __call__(self, role, stage_dir, effective):
        def invoke(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"role": role, "stage_dir": str(stage_dir), "messages": deepcopy(messages),
                               "payload": payload, "profile": deepcopy(effective), "kwargs": deepcopy(kwargs)})
            if role == "editor" and self.fail_editor:
                raise RuntimeError("controlled_editor_interruption")
            response = fixtures_for_payload(payload, editor=role == "editor",
                                            omit_table=role == "editor" and self.omit_editor_table)
            return {"content": json.dumps(response, ensure_ascii=False), "complete": True, "finish_reason": "stop",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30}}
        return invoke


def assert_full_payload(chapter, payload, selected=None):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import task_catalog
    expected = task_catalog(chapter)
    actual = task_catalog(payload)
    wanted = set(expected if selected is None else selected)
    assert set(actual) == wanted
    assert set(payload["editable_task_ids"]) == wanted
    for key in wanted:
        assert actual[key] == expected[key]
    source_by_id = {row["source_handle"]: row for row in chapter["sources"]}
    for source in payload["sources"]:
        assert source == source_by_id[source["source_handle"]]


def is_windows_component(name):
    return (bool(name) and name not in {".", ".."} and not re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            and not name.endswith((".", " ")) and not PureWindowsPath(name).is_reserved()
            and len(name.encode("utf-16-le")) // 2 <= 255)


def test_recorded_owner_chapter_cli_preserves_full_tasks_and_materials(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, project_chapter
    from scripts.upgrade3 import writer_candidates as candidate_cli
    packet, original = recorded_packet()
    arrangement, _ = formal_arrangement(tmp_path, packet)
    chapter = build_chapter_input(arrangement)
    expected = project_chapter(chapter)
    # The recorded 7 tasks carry distinctive full cases and limits, not just IDs.
    assert sum(len(u["paragraph_tasks"]) for u in chapter["units"]) == 7
    for owner, unit in zip(original["units"], chapter["units"]):
        for field in ("case_objects", "supporting_studies", "synthesis_and_transition", "argument_relations"):
            assert unit["owner_unit_context"][field] == owner[field]
        legacy = existing_writer.build_unit_view(arrangement, unit["unit_id"])
        assert unit["paragraph_tasks"] == legacy.paragraph_tasks
    fixture = dump(tmp_path / "responses.json", {"writer_chapter": fixtures_for_payload(expected)})
    out = tmp_path / "candidate"
    args = ["--arrangement", str(arrangement), "--route", "chapter", "--output", str(out), "--responses", str(fixture)]
    assert candidate_cli.main(args) == 0
    messages = list((out / "stages").rglob("MESSAGES.json"))
    assert len(messages) == 1  # one whole-chapter draft, no hidden unit calls
    actual = json.loads(read(messages[0])[-1]["content"])
    assert_full_payload(chapter, actual)
    for owner in original["units"]:
        for case in owner["case_objects"]:
            assert case["finding"] in json.dumps(actual, ensure_ascii=False)
        for study in owner["supporting_studies"]:
            assert json.dumps(study["limits"], ensure_ascii=False) in json.dumps(actual, ensure_ascii=False)
    result = read(out / "CHAPTER_RESULT.json")
    assert result["complete"] is True
    assert result["pending_task_ids"] == []
    assert read(out / "CLI_CONTEXT.json")["execution_mode"] == "recording"
    assert read(out / "CLI_RUN.json")["recorded_response_calls"] == ["writer_chapter"]
    before = {str(p): p.read_bytes() for p in out.rglob("RAW_RESPONSE.json")}
    assert candidate_cli.main(args) == 0
    assert before == {str(p): p.read_bytes() for p in out.rglob("RAW_RESPONSE.json")}


def test_units_route_really_edits_complete_drafts_and_retains_table(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, task_catalog
    from optomind_research.runtime.upgrade3.writer_candidates import run_candidate
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet(), table=True)
    chapter = build_chapter_input(arrangement)
    capture = CaptureFactory()
    out = tmp_path / "units"
    result = run_candidate(chapter, route="units_edit", output_dir=out, config=profile(), client_factory=capture, run=True)
    assert result["complete"] is True, result
    assert [call["role"] for call in capture.calls] == ["writer", "writer", "editor"]
    catalog = task_catalog(chapter)
    for call in capture.calls[:2]:
        assert_full_payload(chapter, call["payload"], call["payload"]["editable_task_ids"])
        serialized = json.dumps(call["payload"], ensure_ascii=False)
        assert {role["task_id"] for role in call["payload"]["shared_chapter_context"]["task_roles"]} == set(catalog)
        assert call["kwargs"]["stream"] is True
    editor = capture.calls[-1]
    assert_full_payload(chapter, editor["payload"])
    editor_text = json.dumps(editor["payload"], ensure_ascii=False)
    draft_text = "\n".join(block["body_markdown"] for block in editor["payload"]["draft_blocks"])
    assert all(f"DRAFT fixture for {key}" in draft_text for key, entry in catalog.items() if entry["kind"] == "paragraph")
    final_text = (out / "CHAPTER_BODY.md").read_text(encoding="utf-8")
    assert "EDITED fixture" in final_text and "DRAFT fixture" not in final_text
    assert TABLE in final_text
    assert "ALIAS_MATERIAL_TAIL" in editor_text
    assert sum(source["source_handle"] == "P0001" for source in chapter["sources"]) == 1
    source = next(row for row in chapter["sources"] if row["source_handle"] == "P0001")
    assert "P0091" in source["aliases"]
    assert source["study_summary_A_variants"][0]["tail"] == "ALIAS_MATERIAL_TAIL"
    for path in out.rglob("*"):
        assert is_windows_component(path.name), path


def test_invalid_editor_remains_pending_and_retry_reuses_successful_drafts(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
    from optomind_research.runtime.upgrade3.writer_candidates import run_candidate
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet(), table=True)
    chapter = build_chapter_input(arrangement)
    out = tmp_path / "pending-editor"
    invalid = CaptureFactory(omit_editor_table=True)
    result = run_candidate(chapter, route="units_edit", output_dir=out, config=profile(), client_factory=invalid, run=True)
    assert [c["role"] for c in invalid.calls] == ["writer", "writer", "editor"]
    assert result["complete"] is False and result["status"] != "complete"
    assert result["editor_complete"] is False
    assert TABLE in (out / "CHAPTER_BODY.md").read_text(encoding="utf-8")
    assert "DRAFT fixture" in result["body_markdown"]
    immutable = {str(p): p.read_bytes() for p in out.rglob("RAW_RESPONSE.json")}
    no_retry = CaptureFactory()
    again = run_candidate(chapter, route="units_edit", output_dir=out, config=profile(), client_factory=no_retry, run=True)
    assert again["complete"] is False and no_retry.calls == []
    retry = CaptureFactory()
    recovered = run_candidate(chapter, route="units_edit", output_dir=out, config=profile(), client_factory=retry,
                              run=True, retry_failed=True)
    assert recovered["complete"] is True
    assert [c["role"] for c in retry.calls] == ["editor"]
    assert TABLE in recovered["body_markdown"]
    assert all(Path(path).read_bytes() == value for path, value in immutable.items())
    assert len(list(out.rglob("RAW_RESPONSE.json"))) == len(immutable) + 1


def test_resume_invalidates_changed_config_material_task_and_prompt(tmp_path, monkeypatch):
    import shutil
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
    from optomind_research.runtime.upgrade3 import writer_candidates as engine
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet())
    chapter = build_chapter_input(arrangement)
    prompts = tmp_path / "copied-prompts"
    shutil.copytree(engine.PROMPT_ROOT, prompts)
    monkeypatch.setattr(engine, "PROMPT_ROOT", prompts)  # real files/loader/hash; no mocked result or cache
    out = tmp_path / "resume"
    config = profile()
    capture = CaptureFactory()

    def execute():
        result = engine.run_candidate(chapter, route="chapter", output_dir=out, config=config,
                                      client_factory=capture, run=True)
        assert result["complete"] is True, result
        return result

    execute()
    assert len(capture.calls) == 1
    immutable = {str(p): p.read_bytes() for p in out.rglob("RAW_RESPONSE.json")}
    execute()
    assert len(capture.calls) == 1
    config["writer"]["thinking_budget"] = 1536
    execute()
    assert len(capture.calls) == 2 and capture.calls[-1]["kwargs"]["thinking_budget"] == 1536
    chapter["sources"][0]["study_summary_A"]["new_boundary"] = "MATERIAL_CHANGE_NOT_IN_PREVIOUS_CACHE"
    execute()
    assert len(capture.calls) == 3
    assert "MATERIAL_CHANGE_NOT_IN_PREVIOUS_CACHE" in json.dumps(capture.calls[-1]["payload"])
    chapter["units"][0]["paragraph_tasks"][0]["development"] += " TASK_CHANGE_NOT_IN_PREVIOUS_CACHE"
    execute()
    assert len(capture.calls) == 4
    prompt = prompts / "writer.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nTest-local prompt revision.\n", encoding="utf-8")
    execute()
    assert len(capture.calls) == 5
    assert "Test-local prompt revision." in capture.calls[-1]["messages"][0]["content"]
    assert all(Path(path).read_bytes() == value for path, value in immutable.items())


def count_characters_as_test_tokens(_raw, messages):
    """Deterministic fake meter for capacity mechanics, not a provider tokenizer."""
    return sum(len(message["content"]) for message in messages) // 4 + 1


def test_hierarchical_capacity_splits_full_tasks_and_scoped_editor_windows(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, task_catalog
    from optomind_research.runtime.upgrade3 import writer_candidates as engine
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet(long_material=True))
    chapter = build_chapter_input(arrangement)
    config = profile(max_input_tokens=7500)
    for role in ("writer", "editor"):
        config[role].update(prompt_token_multiplier=1.0, prompt_token_framing_margin=0)
    out = tmp_path / "hierarchical"
    capture = CaptureFactory()
    result = engine.run_candidate(chapter, route="hierarchical", output_dir=out, config=config,
                                  client_factory=capture, run=True, token_counter=count_characters_as_test_tokens)
    assert result["complete"] is True, result
    writers = [call for call in capture.calls if call["role"] == "writer"]
    editors = [call for call in capture.calls if call["role"] == "editor"]
    assert len(writers) > len(chapter["units"])  # actual inside-unit partition, not a route label
    wanted = list(task_catalog(chapter))
    assigned = [key for call in writers for key in call["payload"]["editable_task_ids"]]
    assert assigned == wanted and len(set(assigned)) == len(wanted)
    for call in writers + editors:
        assert_full_payload(chapter, call["payload"], call["payload"]["editable_task_ids"])
        assert count_characters_as_test_tokens(b"", call["messages"]) <= config["max_input_tokens"]
        assert {role["task_id"] for role in call["payload"]["shared_chapter_context"]["task_roles"]} == set(wanted)
        for row in call["payload"]["sources"]:
            expected = next(source for source in chapter["sources"] if source["source_handle"] == row["source_handle"])
            assert row["study_summary_A"] == expected["study_summary_A"]
            assert "MATERIAL_TAIL_" in json.dumps(row)
    assert len(editors) > 1
    assert all(call["payload"]["edit_scope"] == "window" for call in editors)
    assert any(call["payload"]["read_only_neighbors"] for call in editors)
    assert "EDITED fixture" in result["body_markdown"]
    assert result["pending_task_ids"] == []
    manifest = read(out / "RUN_MANIFEST.json")
    assert [key for row in manifest["partition"] for key in row["task_ids"]] == wanted


def test_oversized_atom_and_implicit_fallback_never_dispatch(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, task_catalog
    from optomind_research.runtime.upgrade3 import writer_candidates as engine
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet(long_material=True))
    chapter = build_chapter_input(arrangement)
    capture = CaptureFactory()
    config = profile(max_input_tokens=100)
    for route in ("chapter", "hierarchical"):
        out = tmp_path / route
        result = engine.run_candidate(chapter, route=route, output_dir=out, config=config,
                                      client_factory=capture, run=True, token_counter=count_characters_as_test_tokens)
        assert result["complete"] is False
        assert result["effective_route"] == route  # no silent algorithm substitution
        assert set(result["pending_task_ids"]) == set(task_catalog(chapter))
        assert capture.calls == []
        manifest = read(out / "RUN_MANIFEST.json")
        assert manifest["material_manifest"]["material_preserved"] is True
        assert all(row["sha256"] and row["utf8_bytes"] > 0 for row in manifest["material_manifest"]["source_records"])
        saved = read(Path(manifest["input_path"]))
        assert saved == chapter


class ControlledSSE(io.BytesIO):
    status = 200
    headers = {"content-type": "text/event-stream", "x-request-id": "offline-transport-control"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def install_controlled_http(monkeypatch, *, fail=False):
    """Replace only credentials/network; real Qwen wire, SSE and ledger run."""
    from optomind_research.runtime.upgrade3.module4 import runtime
    captured = []
    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder-never-sent"])

    class Opener:
        def open(self, request, timeout):
            wire = json.loads(request.data)
            captured.append({"wire": wire, "timeout": timeout})
            if fail:
                raise runtime.urllib.error.HTTPError(request.full_url, 503, "Controlled unavailable", {}, io.BytesIO(b"{}"))
            payload = json.loads(wire["messages"][-1]["content"])
            body = json.dumps(fixtures_for_payload(payload, editor="draft_blocks" in payload), ensure_ascii=False)
            content_events = [{"id": "offline-request", "model": wire["model"], "choices": [
                {"index": 0, "delta": {"content": body}, "finish_reason": None}]},
                {"id": "offline-request", "model": wire["model"], "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 30}}]
            encoded = b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
                               for event in content_events) + b"data: [DONE]\n\n"
            return ControlledSSE(encoded)
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_args: Opener())
    return captured


def live_cli_arguments(tmp_path, arrangement, *, limit=5.0):
    config_path = dump(tmp_path / "configuration.json", profile())
    return ["--arrangement", str(arrangement), "--route", "units_edit", "--output", str(tmp_path / "candidate"),
            "--config", str(config_path), "--run", "--budget-ledger", str(tmp_path / "shared-budget.sqlite"),
            "--budget-limit", str(limit), "--key-file", str(tmp_path / "nonexistent-credential-file")]


def test_real_cli_qwen_sse_wire_phase_budgets_and_shared_ledger(tmp_path, monkeypatch):
    from scripts.upgrade3 import writer_candidates as candidate_cli
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet(), table=True)
    captured = install_controlled_http(monkeypatch)
    args = live_cli_arguments(tmp_path, arrangement)
    assert candidate_cli.main(args) == 0
    assert len(captured) == 3
    assert [request["wire"]["thinking_budget"] for request in captured] == [1024, 1024, 2048]
    assert [request["wire"]["max_completion_tokens"] for request in captured] == [5120, 5120, 6144]
    assert all(request["wire"]["stream"] is True and request["wire"]["stream_options"]["include_usage"] is True
               and request["timeout"] == 33 for request in captured)
    with sqlite3.connect(tmp_path / "shared-budget.sqlite") as db:
        rows = db.execute("SELECT status, actual_cny, request_metadata_json FROM reservations ORDER BY created_at").fetchall()
    assert len(rows) == 3 and all(row[0] == "settled" and row[1] > 0 for row in rows)
    for row in rows:
        metadata = json.loads(row[2])
        assert metadata["stream"] is True
        opened = next(event for event in metadata["transport_events"] if event["stage"] == "request_open_start")
        assert opened["overall_timeout_seconds"] == 99
    out = tmp_path / "candidate"
    assert len(list(out.rglob("*.sse.raw"))) == 3
    assert TABLE in (out / "CHAPTER_BODY.md").read_text(encoding="utf-8")
    assert candidate_cli.main(args) == 0
    assert len(captured) == 3  # compatible completed stages do not charge again
    with sqlite3.connect(tmp_path / "shared-budget.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 3


def test_real_ledger_refuses_before_network_and_failure_is_not_automatic_retry(tmp_path, monkeypatch):
    from scripts.upgrade3 import writer_candidates as candidate_cli
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet())
    captured = install_controlled_http(monkeypatch)
    args = live_cli_arguments(tmp_path, arrangement, limit=0.000001)
    assert candidate_cli.main(args) == 3
    assert captured == []
    report = read(tmp_path / "candidate/CLI_RUN.json")
    assert report["complete"] is False
    assert report["budget"]["actual_cny"] == 0
    assert report["budget"]["reservations"] == []
    assert report["paid_dispatch_count"] == 0
    assert "global_budget_exceeded" in json.dumps(report)
    assert candidate_cli.main(args) == 3
    assert captured == []


def test_real_http_failure_sends_one_attempt_and_retains_uncertain_budget(tmp_path, monkeypatch):
    from scripts.upgrade3 import writer_candidates as candidate_cli
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet())
    captured = install_controlled_http(monkeypatch, fail=True)
    args = live_cli_arguments(tmp_path, arrangement)
    assert candidate_cli.main(args) == 3
    # Each independent writer gets one attempt; neither retries nor editor run.
    assert len(captured) == 2
    report = read(tmp_path / "candidate/CLI_RUN.json")
    assert report["complete"] is False and report["budget"]["reserved_cny"] > 0
    assert len(report["budget"]["reservations"]) == 2
    assert all(row["status"] == "uncertain" for row in report["budget"]["reservations"])
    assert candidate_cli.main(args) == 3
    assert len(captured) == 2


def test_current_plan_pointer_drives_real_missing_view_builder(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, project_chapter
    from scripts.upgrade3 import writer_candidates as candidate_cli
    arrangement, packet_root = formal_arrangement(tmp_path, synthetic_packet())
    chapter = build_chapter_input(arrangement)
    fixture = dump(tmp_path / "responses.json", {"writer_chapter": fixtures_for_payload(project_chapter(chapter))})
    (arrangement.parent / "ARRANGEMENT_INPUT.json").unlink()
    promoted = tmp_path / "promoted"
    pointer = dump(promoted / "CURRENT_PLAN.json", {"packet_root": "../packets"})
    out = tmp_path / "from-current-plan"
    assert candidate_cli.main(["--arrangement", str(arrangement), "--packet-root", str(pointer),
        "--output", str(out), "--responses", str(fixture)]) == 0
    assert read(out / "CLI_CONTEXT.json")["packet_root"] == str(packet_root.resolve())
    messages = read(next((out / "stages").rglob("MESSAGES.json")))
    assert_full_payload(chapter, json.loads(messages[-1]["content"]))


def test_actual_cli_assembly_preserves_cross_unit_prose_order_and_blocks_pending_final(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, task_catalog
    from scripts.upgrade3 import writer_candidates as candidate_cli
    rows = []
    outputs = []
    args_by_chapter = []
    for index, chapter_id in enumerate(("SOLAR", "SOLAR_2")):
        packet = synthetic_packet()
        packet["chapter_id"] = packet["chapter"]["chapter_id"] = packet["chapter_plan"]["chapter_id"] = chapter_id
        arrangement, _ = formal_arrangement(tmp_path / chapter_id, packet)
        chapter = build_chapter_input(arrangement)
        # One coherent block intentionally spans all tasks and both units.
        body = f"Cross-unit continuous prose {index} [P0001].\n\nA second paragraph in the same block."
        response = {"blocks": [{"block_id": "cross-unit", "task_ids": list(task_catalog(chapter)), "body_markdown": body}], "issues": []}
        fixture = dump(tmp_path / chapter_id / "responses.json", {"writer_chapter": response})
        out = tmp_path / chapter_id / "candidate"
        args = ["--arrangement", str(arrangement), "--route", "chapter", "--output", str(out), "--responses", str(fixture)]
        assert candidate_cli.main(args) == 0
        assert read(out / "CHAPTER_RESULT.json")["blocks"][0]["task_ids"] == list(task_catalog(chapter))
        rows.append({"chapter_id": chapter_id, "result_path": str(out / "CHAPTER_RESULT.json")})
        outputs.append(body)
        args_by_chapter.append(args)
    # Assembly respects the explicit chapter selection order, not directory order.
    selected = dump(tmp_path / "selected.json", {"chapters": list(reversed(rows))})
    out = tmp_path / "body"
    assert candidate_cli.main(["--assemble-manifest", str(selected), "--output", str(out)]) == 0
    body = (out / "BODY.md").read_text(encoding="utf-8")
    assert body == outputs[1] + "\n\n" + outputs[0] + "\n"
    assert read(out / "BODY_RESULT.json")["complete"] is True
    assert not list(out.rglob("UNIT_RESULT.json"))
    # A real capacity failure after success retains the previous good chapter,
    # but its current pending run must prevent accidental final publication.
    tiny = dump(tmp_path / "too-small.json", profile(max_input_tokens=1))
    assert candidate_cli.main(args_by_chapter[0] + ["--config", str(tiny)]) == 3
    assert read(Path(rows[0]["result_path"]))["complete"] is True
    assert candidate_cli.main(["--assemble-manifest", str(selected), "--output", str(out)]) == 2
    assert (out / "BODY.md").read_text(encoding="utf-8") == body
    assert candidate_cli.main(["--assemble-manifest", str(selected), "--output", str(out), "--allow-pending-draft"]) == 0
    assert (out / "BODY.md").read_text(encoding="utf-8") == body
    pending = read(out / "PENDING_BODY_RESULT.json")
    assert pending["complete"] is False and pending["status"] == "pending_draft"
    assert (out / "PENDING_BODY.md").read_text(encoding="utf-8").startswith("DRAFT:")


def test_quality_archive_keeps_public_ab_and_rejected_handle_only_as_provenance(tmp_path):
    """Rebuild a view from public artifacts; do not claim original-request replay."""
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import (
        build_chapter_input, parse_candidate_response, project_chapter, task_catalog,
    )
    from optomind_research.runtime.upgrade3.writer_candidates import run_candidate
    accepted = read(QUALITY_ARCHIVE / "04_approved_arrangement.json")
    outline = read(QUALITY_ARCHIVE / "03_revised_outline.json")
    archived_payload = json.loads(read(QUALITY_ARCHIVE / "13_writer_U1_request_view.json")["request"]["messages"][-1]["content"])
    frame = archived_payload["chapter_frame"]
    packet = {"chapter_id": accepted["chapter_id"], "chapter_plan": outline,
              "chapter": {"chapter_id": accepted["chapter_id"], "title": frame["chapter_title"],
                          "purpose": frame["chapter_purpose"], "scope": frame["chapter_scope"]},
              "research_question": frame["research_question"], "review_argument": frame["review_argument"],
              "shared_scope": frame["shared_scope"], "source_materials": list(accepted["source_catalog"].values())}
    packet_path = dump(tmp_path / "PUBLIC_PACKET.json", packet)
    # The real arranger builder restores normalized brief identities from the
    # public outline. Scientific fields and the accepted arrangement stay exact.
    view = arranging.build_chapter_view(packet_path)
    arranging.write_view(view, tmp_path / "ARRANGEMENT_INPUT.json")
    arrangement = dump(tmp_path / "CHAPTER_ARRANGEMENT.json", accepted)
    chapter = build_chapter_input(arrangement)
    projected = project_chapter(chapter)
    source_map = {source["source_handle"]: source for source in projected["sources"]}
    assert "P0594" not in source_map  # rejected original handle must not regain citation authority
    for original in read(QUALITY_ARCHIVE / "11_materials_A_B.json")["sources"]:
        for key in ("study_summary_A", "review_planning_B"):
            if key in original:
                assert source_map[original["source_handle"]][key] == original[key]
    assert chapter["chapter_tool_materials"] == accepted["chapter_tool_materials"]
    rejected = [row for tool in chapter["chapter_tool_materials"] for row in tool.get("sources", [])
                if row.get("original_source_handle") == "P0594"]
    assert rejected and rejected[0]["identity_status"] == "conflicting_handle"
    assert "source_handle" not in rejected[0]
    one_task = next(iter(task_catalog(chapter)))
    untrusted = parse_candidate_response({"blocks": [{"block_id": "claim", "task_ids": [one_task],
                                          "body_markdown": "An untrusted citation [P0594]."}]}, chapter, [one_task])
    assert "P0594" in json.dumps(untrusted["diagnostics"]["unknown_citations"])
    capture = CaptureFactory()
    result = run_candidate(chapter, route="chapter", output_dir=tmp_path / "candidate", config=profile(),
                           client_factory=capture, run=True)
    assert result["complete"] is True and len(capture.calls) == 1
    assert_full_payload(chapter, capture.calls[0]["payload"])
    assert chapter["chapter_frame"]["chapter_argument"] == accepted["chapter_argument"]


def test_saved_raw_response_recovers_after_parse_interruption_without_new_call(tmp_path):
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
    from optomind_research.runtime.upgrade3.writer_candidates import run_candidate
    arrangement, _ = formal_arrangement(tmp_path, synthetic_packet())
    chapter = build_chapter_input(arrangement)
    capture = CaptureFactory()
    out = tmp_path / "raw-recovery"
    result = run_candidate(chapter, route="chapter", output_dir=out, config=profile(), client_factory=capture, run=True)
    assert result["complete"] is True and len(capture.calls) == 1
    raw_path = next((out / "stages").rglob("RAW_RESPONSE.json"))
    raw = raw_path.read_bytes()
    (raw_path.parent / "RESULT.json").unlink()  # simulate a crash after raw persistence and before parse save
    recovered = run_candidate(chapter, route="chapter", output_dir=out, config=profile(), client_factory=capture, run=True)
    assert recovered["complete"] is True and len(capture.calls) == 1
    assert raw_path.read_bytes() == raw
    assert read(raw_path.parent / "RESULT.json")["complete"] is True
    assert recovered["body_markdown"] == result["body_markdown"]
