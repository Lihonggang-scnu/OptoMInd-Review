"""Independent full-BODY integration checks at real CLI/material/transport seams.

All newly generated prose is a deliberately controlled wiring fixture. Historical
owner/outline/response exports retain their published content and are labelled
replays; they are not new paid generations or complete private-material replays.
The tests use actual input builders, parsers, files, cache, Qwen SSE and SQLite
ledger paths. Only model responses and credentials/network are substituted.
"""
from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import sqlite3

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import review_unit_writer as unit_writer
from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input
from scripts.upgrade3 import chapter_arrangement as arrangement_cli

ROOT = Path(__file__).resolve().parents[2]
OWNER_ARCHIVE = ROOT / "docs/verification/on-demand-efficiency-20261007"
QUALITY_ARCHIVE = ROOT / "docs/acceptance/quality-capacity-20261006/evidence"
WRITING_ARCHIVE = ROOT / "docs/acceptance/writing-candidates-local-20261007"
ORIGINAL_REQUEST = "Explain how fictional recovery observations constrain lifetime claims, retaining all conditional findings."
TARGET_READER = "A materials researcher new to recovery measurements, familiar with basic experimental uncertainty."
TABLE = "| Setting | Finding |\n| --- | --- |\n| Indoor, 60 seconds | Recovery alone [P0002] |"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*_args, **_kwargs):
        raise AssertionError("Full-BODY integration tests must never reach a network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def profile(**changes):
    stage = {"model": "qwen3.5-plus", "thinking": True, "thinking_budget": 1024,
             "max_output_tokens": 4096, "json_mode": False, "stream": True,
             "timeout_seconds": 33, "stream_overall_timeout_seconds": 99}
    return {"writer": deepcopy(stage), "reader": {**stage, "thinking_budget": 2048},
            "reviser": {**stage, "thinking_budget": 3072}, "recent_prose_segments": 1,
            **changes}


def controlled_packet(chapter_id, number, card_path):
    """Synthetic scientific content; these markers test preservation, not quality."""
    handle = f"P{number:04d}"
    source = {"source_handle": handle, "paper_id": f"controlled-paper-{number}",
              "doi": f"10.0000/fullbody-test-{number}", "title": f"Controlled measurement {number}",
              "card_path": str(card_path),
              "study_summary_A": {"finding": f"Distinct recovery finding {number}",
                                  "condition": {"seconds": 30 * number},
                                  "tail": f"INLINE_A_TAIL_{number}"},
              "review_planning_B": {"planning_summary": f"Compare condition {number}",
                                    "scope_interpretation_cautions": f"FULL_B_BOUNDARY_{number}"}}
    return {"chapter_id": chapter_id,
            "chapter": {"chapter_id": chapter_id, "title": f"Recovery responsibility {number}"},
            "research_question": "What does recovery establish about lifetime?",
            "review_argument": "Measured recovery depends on the observation window.",
            "shared_scope": {"excluded": "No claim about uncontrolled field lifetime"},
            "chapter_plan": {"chapter_id": chapter_id, "reader_objective": f"Understand condition {number}",
                             "thesis": f"Keep condition {number} attached to its finding",
                             "units": [{"unit_id": "U1", "substantive_point": f"Explain condition {number}",
                                        "paragraph_briefs": [{"paragraph_id": "B1", "point": f"Finding {number}",
                                            "development": f"Complete explanation with TASK_TAIL_{number}",
                                            "source_handles": [handle],
                                            "finding_conditions": {"seconds": 30 * number}}],
                                        "case_objects": [{"source_handle": handle,
                                            "finding": f"OWNER_CASE_TAIL_{number}", "limits": ["Not field lifetime"]}],
                                        "synthesis_and_transition": {"boundary": f"Keep boundary {number}"}}]},
            "source_materials": [source]}


def export_arrangement(tmp_path, packet, packet_root, *, table=False):
    """Replay a controlled arranger response through its genuine CLI consumer."""
    chapter_id = packet["chapter_id"]
    path = dump(packet_root / f"writer_packets/{chapter_id}.json", packet)
    view = arranging.build_chapter_view(path)
    units = [{"unit_id": u.unit_id, "paragraph_tasks": [
        {"paragraph_id": brief.paragraph_id, "source_briefs": [brief.paragraph_id],
         "point": brief.point, "development": brief.development,
         "source_uses": [{"source_handle": handle, "role": "support", "use": "Keep full conditions"}
                         for handle in brief.source_handles]}
        for brief in u.paragraph_briefs]} for u in view.units]
    if table:
        units[0]["table_tasks"] = [{"table_id": "T1", "purpose": "State the observation window",
            "columns": ["Setting", "Finding"], "row_tasks": [{"content": "Indoor, 60 seconds",
                "source_uses": [{"source_handle": "P0002"}], "limits": ["Not field lifetime"]}]}]
    used = {use["source_handle"] for unit in units for task in unit["paragraph_tasks"] for use in task["source_uses"]}
    response = {"chapter_id": chapter_id, "units": units, "unused_sources": [
        {"source_handle": source.source_handle, "reason": "Available supporting context"}
        for source in view.sources if source.source_handle not in used]}
    replay = dump(tmp_path / f"CONTROLLED_{chapter_id}_ARRANGEMENT.json", response)
    output = tmp_path / "arrangement"
    assert arrangement_cli.main(["--packet-root", str(packet_root), "--chapter", chapter_id,
        "--output-root", str(output), "--reexport-from", str(replay)]) == 0
    arrangement = output / chapter_id / "CHAPTER_ARRANGEMENT.json"
    assert read(arrangement)["validation"]["ok"] is True
    return arrangement


@pytest.fixture
def complete_case(tmp_path):
    packet_root = tmp_path / "approved"
    chapters = []
    cards = []
    for number in range(1, 4):
        card = dump(tmp_path / f"cards/P{number:04d}.json", {
            "paper_id": f"controlled-paper-{number}",
            "general_understanding": {"contribution_and_limits": f"CARD_LIMIT_TAIL_{number}"},
            "material": {"extra_observation": f"CARD_ORIGINAL_TEXT_{number}"}})
        cards.append(card)
        chapters.append(controlled_packet(f"CH{number:02d}", number, card))
    plan = {"research_question": chapters[0]["research_question"],
            "review_argument": chapters[0]["review_argument"], "original_user_request": ORIGINAL_REQUEST,
            "target_reader": TARGET_READER, "shared_scope": chapters[0]["shared_scope"],
            "shared_outline": [chapter["chapter"] for chapter in chapters],
            "chapters": chapters,
            "writer_packets": [{"chapter_id": chapter["chapter_id"],
                "json_path": f"writer_packets/{chapter['chapter_id']}.json"} for chapter in chapters]}
    plan_path = dump(packet_root / "DETAILED_REVIEW_PLAN.json", plan)
    pointer = dump(tmp_path / "CURRENT_PLAN.json", {"packet_root": "approved"})
    arrangements = [export_arrangement(tmp_path, packet, packet_root, table=i == 1)
                    for i, packet in enumerate(chapters)]
    manifest = dump(tmp_path / "MANIFEST.json", {"schema_version": "optomind.fullbody_manifest.v1",
        "original_user_request": ORIGINAL_REQUEST, "target_reader": TARGET_READER,
        "research_question": plan["research_question"], "review_argument": plan["review_argument"],
        "shared_scope": plan["shared_scope"], "packet_root": str(pointer),
        "plan_path": str(plan_path), "expected_chapter_ids": [c["chapter_id"] for c in chapters],
        "chapters": [{"chapter_id": c["chapter_id"], "arrangement_path": str(a)}
                     for c, a in zip(chapters, arrangements)]})
    normalized = [build_chapter_input(a, packet_root=packet_root, language="en") for a in arrangements]
    return {"manifest": manifest, "arrangements": arrangements, "chapters": normalized,
            "plan_path": plan_path, "packet_root": packet_root, "pointer": pointer, "cards": cards}


def envelope(value, *, complete=True, finish_reason="stop"):
    return {"content": json.dumps(value, ensure_ascii=False), "complete": complete,
            "finish_reason": finish_reason, "usage": {"prompt_tokens": 100, "completion_tokens": 30}}


def author_value(payload):
    ids = payload["editable_task_ids"]
    order = list(dict.fromkeys(key.split("::")[0] for key in ids))
    body = "\n\n".join(f"## {chapter}\n\nACTUAL_PROSE_{chapter}: A distinct explanation with a retained condition."
                        for chapter in order)
    if any(key.endswith("::T1") for key in ids):
        body += "\n\n" + TABLE
    return {"body_markdown": body, "completed_task_ids": ids, "complete": True, "issues": []}


class CaptureFactory:
    execution_mode = "recording"

    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond or (lambda role, payload, index: author_value(payload))

    def __call__(self, role, stage_dir, effective):
        def invoke(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.calls.append({"role": role, "stage_dir": str(stage_dir), "messages": deepcopy(messages),
                               "payload": payload, "profile": deepcopy(effective), "kwargs": deepcopy(kwargs)})
            value = self.respond(role, payload, len(self.calls) - 1)
            return value if "content" in value else envelope(value)
        return invoke


def book_for(case):
    from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input
    return build_fullbody_input(case["chapters"], research_question=read(case["manifest"])["research_question"],
                               review_argument=read(case["manifest"])["review_argument"], language="en")


def recorded_calls(output):
    return read(output / "CLI_RUN.json")["recorded_response_calls"]


def stage_payloads(output):
    return [json.loads(read(path)[-1]["content"]) for path in sorted((output / "stages").rglob("MESSAGES.json"))]


def write_route_responses(path, book, route, *, reread=False):
    from optomind_research.runtime.upgrade3.fullbody_contracts import fullbody_task_catalog, project_fullbody
    catalog = fullbody_task_catalog(book)
    if route == "whole_author":
        responses = {"author_whole": author_value(project_fullbody(book))}
    else:
        responses = {}
        for index, chapter in enumerate(book["chapters"], 1):
            ids = [key for key, row in catalog.items() if row["chapter_id"] == chapter["chapter_id"]]
            responses[f"author_{index:03d}"] = author_value(project_fullbody(book, ids))
        if reread:
            responses["author_003_reread_01"] = responses["author_003"]
            responses["author_003"] = {"read_segment_ids": ["segment_0001"]}
    return dump(path, responses)


def prohibit_live_factory(monkeypatch):
    from optomind_research.runtime.upgrade3.module4 import runtime
    def forbidden(*_args, **_kwargs):
        pytest.fail("Offline full-BODY operation attempted to construct a provider or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)


@pytest.mark.parametrize("route", ["whole_author", "continuous_author", "workbench"])
def test_actual_cli_writes_all_approved_chapters_and_resumes_without_calls(tmp_path, complete_case, monkeypatch, route):
    from scripts.upgrade3 import fullbody_writer as cli
    from optomind_research.runtime.upgrade3.fullbody_contracts import fullbody_task_catalog
    prohibit_live_factory(monkeypatch)
    book, prepared = cli.load_body_manifest(complete_case["manifest"], language="en")
    responses = write_route_responses(tmp_path / "responses.json", book, route, reread=route == "workbench")
    config_path = dump(tmp_path / "profile.json", profile())
    output = tmp_path / route
    args = ["--manifest", str(complete_case["manifest"]), "--route", route, "--output", str(output),
            "--responses", str(responses), "--config", str(config_path), "--language", "en",
            "--key-file", "/never/read/credentials"]
    assert cli.main(args) == 0
    result = read(output / "FULL_BODY_RESULT.json")
    assert result["complete"] is True
    assert result["pending_task_ids"] == []
    assert set(result["completed_task_ids"]) == set(fullbody_task_catalog(book))
    body = (output / "FULL_BODY.md").read_text(encoding="utf-8")
    assert body.index("ACTUAL_PROSE_CH01") < body.index("ACTUAL_PROSE_CH02") < body.index("ACTUAL_PROSE_CH03")
    assert TABLE in body
    assert len(recorded_calls(output)) == {"whole_author": 1, "continuous_author": 3, "workbench": 4}[route]
    assert read(output / "CLI_RUN.json")["current_run_cost_cny"] == 0
    assert read(output / "RUN_MANIFEST.json")["hidden_final_integration"] is False
    payloads = stage_payloads(output)
    for payload in payloads:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert ORIGINAL_REQUEST in serialized and TARGET_READER in serialized
        assert "TASK_TAIL_3" in serialized  # Complete future intent remains visible.
        assert payload["shared_fullbody_context"]["intent_is_not_scientific_evidence"] is True
        for source in payload["sources"]:
            number = int(source["source_handle"][1:])
            assert f"INLINE_A_TAIL_{number}" in json.dumps(source)
            assert f"FULL_B_BOUNDARY_{number}" in json.dumps(source)
            assert f"CARD_LIMIT_TAIL_{number}" in json.dumps(source)
            assert f"CARD_ORIGINAL_TEXT_{number}" in json.dumps(source)
    if route == "whole_author":
        assert len(payloads) == 1 and payloads[0]["accepted_body_markdown"] == ""
        assert len(payloads[0]["chapters"]) == 3
    elif route == "continuous_author":
        assert "ACTUAL_PROSE_CH01" in payloads[1]["accepted_body_markdown"]
        assert "ACTUAL_PROSE_CH01" in payloads[2]["accepted_body_markdown"]
        assert "ACTUAL_PROSE_CH02" in payloads[2]["accepted_body_markdown"]
    else:
        prior = payloads[2]
        assert "ACTUAL_PROSE_CH01" not in prior["accepted_body_markdown"]
        assert "ACTUAL_PROSE_CH02" in prior["accepted_body_markdown"]
        delivered = payloads[3]["reread_segments"]
        assert delivered[0]["segment_id"] == "segment_0001"
        assert delivered[0]["body_markdown"] == result["segments"][0]["body_markdown"]
        assert "ACTUAL_PROSE_CH01" in delivered[0]["body_markdown"]
    immutable = {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}
    assert cli.main(args) == 0
    assert recorded_calls(output) == []
    assert (output / "FULL_BODY.md").read_text(encoding="utf-8") == body
    assert immutable == {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}


def test_cli_prepared_manifest_pins_actual_card_and_complete_plan(tmp_path, complete_case, monkeypatch):
    from scripts.upgrade3 import fullbody_writer as cli
    prohibit_live_factory(monkeypatch)
    prepared_path = tmp_path / "PREPARED.json"
    assert cli.main(["--manifest", str(complete_case["manifest"]), "--prepare-manifest", str(prepared_path)]) == 0
    prepared = read(prepared_path)
    assert prepared["actual_chapter_ids"] == ["CH01", "CH02", "CH03"]
    source_files = {row["path"]: row for row in prepared["source_files"]}
    for path in [complete_case["plan_path"], *complete_case["arrangements"], *complete_case["cards"]]:
        assert source_files[str(path.resolve())]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    before = complete_case["cards"][0].read_bytes()
    card = read(complete_case["cards"][0])
    card["material"]["extra_observation"] = "A new materially different boundary"
    dump(complete_case["cards"][0], card)
    with pytest.raises(ValueError, match="hash_mismatch"):
        cli.load_body_manifest(prepared_path)
    complete_case["cards"][0].write_bytes(before)
    incomplete = read(complete_case["manifest"])
    incomplete["chapters"] = incomplete["chapters"][:2]
    incomplete["expected_chapter_ids"] = ["CH01", "CH02"]  # A local claim must not override the complete plan.
    missing = dump(tmp_path / "MISSING_CHAPTER.json", incomplete)
    with pytest.raises(ValueError, match="scope.*mismatch"):
        cli.load_body_manifest(missing)


def test_fresh_reader_patch_route_keeps_every_other_byte_and_base_cost(tmp_path, complete_case, monkeypatch):
    from scripts.upgrade3 import fullbody_writer as cli
    prohibit_live_factory(monkeypatch)
    book, _ = cli.load_body_manifest(complete_case["manifest"], language="en")
    config_path = dump(tmp_path / "profile.json", profile())
    responses = write_route_responses(tmp_path / "draft-responses.json", book, "continuous_author")
    base_output = tmp_path / "base"
    common = ["--manifest", str(complete_case["manifest"]), "--language", "en", "--config", str(config_path)]
    assert cli.main(common + ["--route", "continuous_author", "--output", str(base_output), "--responses", str(responses)]) == 0
    base = read(base_output / "FULL_BODY_RESULT.json")
    anchor = "ACTUAL_PROSE_CH02: A distinct explanation with a retained condition."
    replacement = "ACTUAL_PROSE_CH02: The preceding observation motivates this distinct explanation under its retained condition."
    revision_responses = dump(tmp_path / "reader-responses.json", {
        "reader_full_body": {"understanding": "Recovery evidence remains conditional.", "complete": True,
            "issues": [{"issue_id": "I1", "anchor": anchor, "problem": "The connection to the prior observation is implicit.",
                        "reader_understanding": "I understand two observations but cannot reconstruct their relation.",
                        "suggested_action": "Explain the relation under the original condition."}]},
        "revision_001": {"patches": [{"issue_id": "I1", "anchor": anchor, "replacement": replacement}],
                         "rejected_issues": [], "complete": True}})
    output = tmp_path / "revision"
    args = common + ["--route", "reader_revision", "--output", str(output), "--draft", str(base_output / "FULL_BODY_RESULT.json"),
                     "--responses", str(revision_responses)]
    base_bytes = (base_output / "FULL_BODY.md").read_bytes()
    assert cli.main(args) == 0
    result = read(output / "FULL_BODY_RESULT.json")
    assert result["complete"] and result["reader_revision_complete"]
    assert result["body_markdown"] == base["body_markdown"].replace(anchor, replacement)
    assert "\n\n".join(segment["body_markdown"] for segment in result["segments"]) == result["body_markdown"]
    for segment in result["segments"]:
        assert Path(segment["full_text_path"]).read_text(encoding="utf-8") == segment["body_markdown"]
        assert segment["sha256"] == hashlib.sha256(segment["body_markdown"].encode("utf-8")).hexdigest()
    assert (base_output / "FULL_BODY.md").read_bytes() == base_bytes
    assert recorded_calls(output) == ["reader_full_body", "revision_001"]
    requests = stage_payloads(output)
    reader, reviser = requests
    assert reader["body_markdown"] == base["body_markdown"]
    assert TARGET_READER in json.dumps(reader) and ORIGINAL_REQUEST in json.dumps(reader)
    assert "sources" not in reader and "chapters" not in reader and "reader_issues" not in reader
    assert reviser["body_markdown"] == base["body_markdown"]
    assert "CARD_ORIGINAL_TEXT_2" in json.dumps(reviser)
    assert result["cost_summary"]["base_reuse_charged_again"] is False
    assert result["cost_summary"]["base_cost_attribution"] == base["cost_summary"]
    assert cli.main(args) == 0
    assert recorded_calls(output) == []


def test_pending_transport_is_retained_without_retry_and_good_body_survives(tmp_path, complete_case):
    from optomind_research.runtime.upgrade3 import fullbody_writer as engine
    book = book_for(complete_case)
    capture = CaptureFactory(lambda _role, payload, _index: envelope(author_value(payload), complete=False, finish_reason="timeout"))
    output = tmp_path / "pending"
    execute = lambda **kwargs: engine.run_fullbody_candidate(book, route="whole_author", output_dir=output,
        config=profile(), client_factory=capture, run=True, **kwargs)
    first = execute()
    assert first["complete"] is False and first["partial_unaccepted_prose"] is True
    assert "ACTUAL_PROSE_CH03" in first["body_markdown"]
    assert len(capture.calls) == 1
    raw_path = next(output.rglob("RAW_RESPONSE.json"))
    immutable = raw_path.read_bytes()
    again = execute()
    assert again["complete"] is False and len(capture.calls) == 1
    assert raw_path.read_bytes() == immutable
    capture.respond = lambda _role, payload, _index: author_value(payload)
    recovered = execute(retry_failed=True)
    assert recovered["complete"] is True and len(capture.calls) == 2
    assert raw_path.read_bytes() == immutable
    accepted = (output / "FULL_BODY.md").read_bytes()
    failed = engine.run_fullbody_candidate(book, route="whole_author", output_dir=output,
        config=profile(max_input_tokens=1), client_factory=capture, run=True)
    assert failed["complete"] is False and len(capture.calls) == 2
    assert (output / "FULL_BODY.md").read_bytes() == accepted
    manifest = read(output / "RUN_MANIFEST.json")
    assert manifest["previous_complete_version_preserved"] is True
    assert manifest["selected_version"] == recovered["run_id"]
    assert manifest["current_run_version"] != recovered["run_id"]


def test_cache_invalidation_tracks_source_task_profile_prompt_and_raw_recovery(tmp_path, complete_case, monkeypatch):
    from optomind_research.runtime.upgrade3 import fullbody_writer as engine
    from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input
    output = tmp_path / "cache"
    prompts = tmp_path / "prompts"
    shutil.copytree(engine.PROMPT_ROOT, prompts)
    monkeypatch.setattr(engine, "PROMPT_ROOT", prompts)
    book = book_for(complete_case)
    capture = CaptureFactory()
    config = profile()
    def run():
        return engine.run_fullbody_candidate(book, route="whole_author", output_dir=output,
            config=config, client_factory=capture, run=True)
    assert run()["complete"] and len(capture.calls) == 1
    raw = next(output.rglob("RAW_RESPONSE.json"))
    original = raw.read_bytes()
    (raw.parent / "RESULT.json").unlink()  # Crash after raw persistence, before parser persistence.
    assert run()["complete"] and len(capture.calls) == 1
    assert raw.read_bytes() == original and read(raw.parent / "RESULT.json")["complete"]
    config["writer"]["thinking_budget"] = 1536
    assert run()["complete"] and len(capture.calls) == 2
    assert capture.calls[-1]["kwargs"]["thinking_budget"] == 1536
    chapters = deepcopy(complete_case["chapters"])
    chapters[0]["sources"][0]["study_summary_A"]["new_condition"] = "NEW_SOURCE_CONDITION"
    book = build_fullbody_input(chapters)
    assert run()["complete"] and len(capture.calls) == 3
    assert "NEW_SOURCE_CONDITION" in json.dumps(capture.calls[-1]["payload"])
    chapters[0]["units"][0]["paragraph_tasks"][0]["development"] += " NEW_TASK_BOUNDARY"
    book = build_fullbody_input(chapters)
    assert run()["complete"] and len(capture.calls) == 4
    assert "NEW_TASK_BOUNDARY" in json.dumps(capture.calls[-1]["payload"])
    prompt = prompts / "whole_author.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nA test-local prompt revision.\n", encoding="utf-8")
    assert run()["complete"] and len(capture.calls) == 5
    assert "A test-local prompt revision." in capture.calls[-1]["messages"][0]["content"]
    assert raw.read_bytes() == original


def test_workbench_can_reread_remote_source_material_without_paid_lookup(tmp_path, complete_case):
    from optomind_research.runtime.upgrade3 import fullbody_writer as engine
    book = book_for(complete_case)
    def respond(_role, payload, _index):
        if payload["editable_task_ids"][0].startswith("CH03::") and not payload.get("reread_sources"):
            return {"read_source_handles": ["P0001"], "read_segment_ids": ["segment_0001"]}
        return author_value(payload)
    capture = CaptureFactory(respond)
    result = engine.run_fullbody_candidate(book, route="workbench", output_dir=tmp_path / "workbench",
        config=profile(), client_factory=capture, run=True)
    assert result["complete"] and len(capture.calls) == 4
    before, after = capture.calls[-2]["payload"], capture.calls[-1]["payload"]
    assert {source["source_handle"] for source in before["sources"]} == {"P0003"}
    assert "ACTUAL_PROSE_CH01" not in before["accepted_body_markdown"]
    original = next(source for source in book["sources"] if source["source_handle"] == "P0001")
    assert after["reread_sources"][0]["source_handle"] == "P0001"
    assert after["reread_sources"][0]["full_record_location"] == "sources"
    assert original in after["sources"]
    assert json.dumps(after).count("CARD_ORIGINAL_TEXT_1") == 1
    assert after["reread_segments"][0]["body_markdown"] == result["segments"][0]["body_markdown"]
    assert [row["role"] for row in result["stages"]] == ["writer"] * 4


class ControlledSSE(io.BytesIO):
    status = 200
    headers = {"content-type": "text/event-stream", "x-request-id": "controlled-fullbody-transport"}
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        self.close()


def install_controlled_http(monkeypatch, respond):
    """Real Qwen serializer/SSE/ledger; only credential and HTTP boundaries replaced."""
    from optomind_research.runtime.upgrade3.module4 import runtime
    calls = []
    monkeypatch.setattr(runtime.QwenDirectClient, "_keys", lambda self: ["offline-placeholder-not-a-credential"])
    class Opener:
        def open(self, request, timeout):
            wire = json.loads(request.data)
            payload = json.loads(wire["messages"][-1]["content"])
            calls.append({"wire": wire, "timeout": timeout, "payload": payload})
            body = json.dumps(respond(payload), ensure_ascii=False)
            events = [{"id": "controlled-request", "model": wire["model"], "choices": [
                {"index": 0, "delta": {"content": body}, "finish_reason": None}]},
                {"id": "controlled-request", "model": wire["model"], "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 30}}]
            return ControlledSSE(b"".join(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
                                         for event in events) + b"data: [DONE]\n\n")
    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_args: Opener())
    return calls


def test_actual_cli_qwen_wire_sse_and_shared_ledger_forward_all_role_profiles(tmp_path, complete_case, monkeypatch):
    from scripts.upgrade3 import fullbody_writer as cli
    anchor = "ACTUAL_PROSE_CH02: A distinct explanation with a retained condition."
    def respond(payload):
        if payload.get("reading_scope"):
            return {"understanding": "Conditional findings", "complete": True, "issues": [{"issue_id": "I1",
                "anchor": anchor, "problem": "Transition not explicit", "reader_understanding": "Two separate observations",
                "suggested_action": "Clarify the connection under its original condition"}]}
        if payload.get("reader_issues"):
            return {"complete": True, "patches": [{"issue_id": "I1", "anchor": anchor,
                "replacement": anchor + " The prior condition also limits this interpretation."}], "rejected_issues": []}
        return author_value(payload)
    captured = install_controlled_http(monkeypatch, respond)
    config_path = dump(tmp_path / "profile.json", profile())
    ledger = tmp_path / "controlled-budget.sqlite"
    common = ["--manifest", str(complete_case["manifest"]), "--config", str(config_path), "--language", "en",
              "--run", "--budget-ledger", str(ledger), "--budget-limit", "5",
              "--key-file", str(tmp_path / "intentionally-absent-credential-file")]
    base_output, output = tmp_path / "wire-base", tmp_path / "wire-revision"
    base_args = common + ["--route", "continuous_author", "--output", str(base_output)]
    assert cli.main(base_args) == 0
    revision_args = common + ["--route", "reader_revision", "--output", str(output),
                              "--draft", str(base_output / "FULL_BODY_RESULT.json")]
    assert cli.main(revision_args) == 0
    assert len(captured) == 5
    assert [row["wire"]["thinking_budget"] for row in captured] == [1024, 1024, 1024, 2048, 3072]
    assert [row["wire"]["max_completion_tokens"] for row in captured] == [5120, 5120, 5120, 6144, 7168]
    assert all(row["wire"]["stream"] is True and row["wire"]["stream_options"]["include_usage"] is True
               and row["timeout"] == 33 for row in captured)
    with sqlite3.connect(ledger) as db:
        rows = db.execute("SELECT status, actual_cny, request_metadata_json FROM reservations ORDER BY created_at").fetchall()
    assert len(rows) == 5 and all(row[0] == "settled" and row[1] > 0 for row in rows)
    for row in rows:
        metadata = json.loads(row[2])
        event = next(item for item in metadata["transport_events"] if item["stage"] == "request_open_start")
        assert event["overall_timeout_seconds"] == 99
    assert len(list(tmp_path.rglob("*.sse.raw"))) == 5
    assert cli.main(base_args) == 0 and cli.main(revision_args) == 0
    assert len(captured) == 5
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 5


def test_historical_ch2_projection_and_innerquote_prose_recovery_remain_lossless():
    """One actual historical chapter input/prose replay, not full-BODY quality evidence."""
    from optomind_research.runtime.upgrade3.fullbody_contracts import (
        build_fullbody_input, fullbody_task_catalog, parse_fullbody_response, project_fullbody,
    )
    from optomind_research.runtime.upgrade3.writer_candidates_contracts import (
        parse_candidate_response, project_chapter,
    )
    chapter = read(WRITING_ARCHIVE / "inputs/CHAPTER_INPUT_PUBLIC_PROJECTION.json")
    book = build_fullbody_input([chapter])
    old_payload = project_chapter(chapter)
    payload = project_fullbody(book)
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    # Prevent a return to duplicating the whole selected outline plus path-heavy
    # provenance. This measures serialization overhead, never provider tokens.
    assert size(payload) < size(old_payload) * 1.10
    assert payload["sources"] == old_payload["sources"]
    assert [unit["paragraph_tasks"] for unit in payload["chapters"][0]["units"]] == [
        unit["paragraph_tasks"] for unit in old_payload["units"]]
    raw = read(next((WRITING_ARCHIVE / "runs/chapter_live").rglob("MODEL_RETURN.json")))
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw["content"])
    recovered_chapter = parse_candidate_response(raw, chapter)
    assert recovered_chapter["complete"] is True
    body = recovered_chapter["body_markdown"]
    assert '"有菌即激活"' in body
    # Adapt only the old protocol envelope; the historical paid prose itself is
    # unchanged. Retain the same recoverable literal-quote failure in this new
    # single-body envelope so it is not mistaken for truncated useful prose.
    serialized_body = json.dumps(body, ensure_ascii=False).replace('\\"', '"')
    content = '{"body_markdown":' + serialized_body + ',"completed_task_ids":' + json.dumps(list(fullbody_task_catalog(book))) + ',"complete":true}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)
    result = parse_fullbody_response({"content": content, "finish_reason": "stop", "complete": True}, book)
    assert result["complete"] is True
    assert result["body_markdown"] == body
    assert result["pending_task_ids"] == []


def rebuild_historical_chapters(tmp_path):
    """Rebuilt Ch1/Ch2 public archives; this is a test selection, not an old complete plan."""
    accepted = read(QUALITY_ARCHIVE / "04_approved_arrangement.json")
    outline = read(QUALITY_ARCHIVE / "03_revised_outline.json")
    archived_payload = json.loads(read(QUALITY_ARCHIVE / "13_writer_U1_request_view.json")["request"]["messages"][-1]["content"])
    frame = archived_payload["chapter_frame"]
    packet = {"chapter_id": accepted["chapter_id"], "chapter_plan": outline,
              "chapter": {"chapter_id": accepted["chapter_id"], "title": frame["chapter_title"],
                          "purpose": frame["chapter_purpose"], "scope": frame["chapter_scope"]},
              "research_question": frame["research_question"], "review_argument": frame["review_argument"],
              "shared_scope": frame["shared_scope"], "source_materials": list(accepted["source_catalog"].values())}
    first = tmp_path / "Ch1"
    view = arranging.build_chapter_view(dump(first / "PUBLIC_PACKET.json", packet))
    arranging.write_view(view, first / "ARRANGEMENT_INPUT.json")
    first_arrangement = dump(first / "CHAPTER_ARRANGEMENT.json", accepted)
    text = read(OWNER_ARCHIVE / "integration_handoff/OWNER_MESSAGES.json")["messages"][1]["content"]
    owner = strengthening.expand_material_projection(json.JSONDecoder().raw_decode(text[text.index("{"):])[0])
    response = read(OWNER_ARCHIVE / "integration_handoff/OWNER_RESPONSE.json")["model_output"]["parsed_output"]
    plan = read(OWNER_ARCHIVE / "UPDATED_PLAN.json")
    assert response["chapter_updates"][0]["updated_plan"] == plan
    second_packet = strengthening.project_plan_for_arrangement(owner, {
        "status": "updated", "updated_plan": plan, "unit_id_remap": response["unit_id_remap"],
        "accepted_source_materials": owner["source_materials"]})
    second_root = tmp_path / "Ch2/packet"
    dump(second_root / "DETAILED_REVIEW_PLAN.json", {
        "shared_outline": [second_packet["chapter"]], "chapters": [second_packet],
        "review_argument": second_packet.get("review_argument", "")})
    second_arrangement = export_arrangement(tmp_path / "Ch2", second_packet, second_root)
    return [build_chapter_input(first_arrangement), build_chapter_input(second_arrangement)], owner, plan


def test_historical_owner_and_material_rebuild_reaches_actual_multi_chapter_requests(tmp_path):
    """Historical input replay plus controlled new prose; no external service/store claim."""
    from optomind_research.runtime.upgrade3.fullbody_contracts import build_fullbody_input
    from optomind_research.runtime.upgrade3 import fullbody_writer as engine
    chapters, owner, owner_plan = rebuild_historical_chapters(tmp_path)
    assert [row["chapter_id"] for row in chapters] == ["Ch1", "Ch2"]
    book = build_fullbody_input(chapters, research_question="Reconstructed public two-chapter integration input",
                               review_argument="Preserve accepted chapter intent and scientific conditions")
    def respond(_role, payload, _index):
        citation = payload["sources"][0]["source_handle"]
        return {"body_markdown": "Controlled continuation for " + payload["chapters"][0]["chapter_id"] + f" [{citation}].\n\n"
                    + f"| Setting | Finding |\n| --- | --- |\n| Controlled test | Retained [{citation}] |",
                "completed_task_ids": payload["editable_task_ids"], "complete": True}
    capture = CaptureFactory(respond)
    result = engine.run_fullbody_candidate(book, route="continuous_author", output_dir=tmp_path / "replay",
        config=profile(), client_factory=capture, run=True)
    assert result["complete"] is True and len(capture.calls) == 2
    assert capture.calls[1]["payload"]["accepted_body_markdown"] == result["segments"][0]["body_markdown"]
    second = capture.calls[1]["payload"]
    for original in owner_plan["units"]:
        for case in original["case_objects"]:
            assert case["finding"] in json.dumps(second, ensure_ascii=False)
        for study in original["supporting_studies"]:
            assert json.dumps(study["limits"], ensure_ascii=False) in json.dumps(second, ensure_ascii=False)
    first_sources = {source["source_handle"]: source for source in capture.calls[0]["payload"]["sources"]}
    for original in read(QUALITY_ARCHIVE / "11_materials_A_B.json")["sources"]:
        for field in ("study_summary_A", "review_planning_B"):
            if field in original:
                assert first_sources[original["source_handle"]][field] == original[field]
    assert "P0594" not in first_sources  # Rejected historical identity cannot become a valid source.
    assert result["paid_dispatch_count"] == 0


def test_unavailable_long_windows_locator_never_erases_available_inline_material(tmp_path, complete_case):
    """Real archive portability regression: one foreign Windows path can exceed POSIX NAME_MAX."""
    from scripts.upgrade3 import fullbody_writer as cli
    arrangement = complete_case["arrangements"][0]
    content = read(arrangement)
    unavailable = "F:\\\\OptoMind-Review-2\\\\historical\\\\" + ("long-nested-card-location\\\\" * 20) + "PAPER_READING_CARD.json"
    content["source_catalog"]["P0001"]["locator"]["card_path"] = unavailable
    dump(arrangement, content)
    book, prepared = cli.load_body_manifest(complete_case["manifest"])
    source = next(row for row in book["sources"] if row["source_handle"] == "P0001")
    assert source["study_summary_A"]["tail"] == "INLINE_A_TAIL_1"
    assert source["review_planning_B"]["scope_interpretation_cautions"] == "FULL_B_BOUNDARY_1"
    assert any(row["exists"] is False and row["path"].endswith("PAPER_READING_CARD.json")
               for row in prepared["source_files"])


@pytest.mark.parametrize("card_kind", ["valid", "foreign_identity", "unavailable_windows"])
def test_real_material_reader_retains_inline_and_checks_optional_card_identity(tmp_path, complete_case, card_kind):
    arrangement = complete_case["arrangements"][0]
    content = read(arrangement)
    if card_kind == "foreign_identity":
        card = dump(tmp_path / "foreign-card.json", {"paper_id": "unrelated-paper",
            "general_understanding": {"contribution_and_limits": "FOREIGN_SCIENCE_MUST_NOT_ENTER"}})
        content["source_catalog"]["P0001"]["locator"]["card_path"] = str(card)
    elif card_kind == "unavailable_windows":
        content["source_catalog"]["P0001"]["locator"]["card_path"] = "F:\\archive\\" + "nested\\" * 100 + "card.json"
    dump(arrangement, content)
    chapter = build_chapter_input(arrangement, packet_root=complete_case["packet_root"])
    source = next(row for row in chapter["sources"] if row["source_handle"] == "P0001")
    assert source["study_summary_A"]["tail"] == "INLINE_A_TAIL_1"
    assert source["review_planning_B"]["scope_interpretation_cautions"] == "FULL_B_BOUNDARY_1"
    if card_kind == "valid":
        assert "CARD_ORIGINAL_TEXT_1" in json.dumps(source)
        assert "CARD_LIMIT_TAIL_1" in json.dumps(source)
    elif card_kind == "foreign_identity":
        assert "FOREIGN_SCIENCE_MUST_NOT_ENTER" not in json.dumps(chapter)
        assert source["material_identity_conflict"] == "foreign_locator_card_ignored"
    else:
        assert "locator_file_unreadable" in json.dumps(chapter["warnings"])


def test_same_publication_explicit_alias_crosses_historical_chapter_canonical_scopes(complete_case):
    """Chapters may canonically name the same explicitly aliased DOI differently."""
    from optomind_research.runtime.upgrade3.fullbody_contracts import (
        build_fullbody_input, fullbody_task_catalog, parse_fullbody_response, project_fullbody,
    )
    chapters = deepcopy(complete_case["chapters"])
    first = chapters[0]["sources"][0]
    second = chapters[1]["sources"][0]
    second["doi"] = first["doi"]
    second["aliases"] = ["P0001"]
    before = deepcopy(chapters)
    book = build_fullbody_input(chapters)
    assert chapters == before
    catalog = fullbody_task_catalog(book)
    for chapter_id in ("CH01", "CH02"):
        ids = [key for key, row in catalog.items() if row["chapter_id"] == chapter_id]
        payload = project_fullbody(book, ids)
        material = json.dumps(payload["sources"])
        assert "INLINE_A_TAIL_1" in material and "INLINE_A_TAIL_2" in material
        assert "INLINE_A_TAIL_3" not in material
        assert material.count("INLINE_A_TAIL_1") == material.count("INLINE_A_TAIL_2") == 1
        result = parse_fullbody_response({"body_markdown": "Explicit same-publication references [P0001, P0002].\n\n" + TABLE,
            "completed_task_ids": ids, "complete": True}, book, ids)
        assert result["complete"]
        assert result["diagnostics"].get("unknown_citations", []) == []
    second["doi"] = "10.0000/a-truly-different-paper"
    with pytest.raises(ValueError, match="alias|identity"):
        build_fullbody_input(chapters)


def test_actual_seven_chapter_public_archive_reaches_complete_scope_cli_preflight(tmp_path, monkeypatch):
    """Exact published packets/plan/arrangements, with missing private locators labelled.

    This is genuine historical full-scope input/preflight replay. No new output,
    omitted private card restoration, or scientific quality verdict is claimed.
    """
    from scripts.upgrade3 import fullbody_writer as cli
    prohibit_live_factory(monkeypatch)
    archive = ROOT / "docs/acceptance/body40-20261005"
    packets = tmp_path / "public-packets"
    (packets / "writer_packets").mkdir(parents=True)
    for number in range(1, 8):
        name = f"Ch{number}.json"
        source = archive / "planning/writer_packets" / name
        content = source.read_bytes() if source.is_file() else gzip.decompress(Path(str(source) + ".gz").read_bytes())
        (packets / "writer_packets" / name).write_bytes(content)
    original_plan = gzip.decompress((archive / "planning/DETAILED_REVIEW_PLAN.json.gz").read_bytes())
    (packets / "DETAILED_REVIEW_PLAN.json").write_bytes(original_plan)
    ids = [f"Ch{number}" for number in range(1, 8)]
    manifest = dump(tmp_path / "historical-manifest.json", {"schema_version": "optomind.fullbody_manifest.v1",
        "packet_root": str(packets), "plan_path": str(packets / "DETAILED_REVIEW_PLAN.json"),
        "expected_chapter_ids": ids, "chapters": [{"chapter_id": chapter_id,
            "arrangement_path": str(archive / f"body/arrangement_repaired/{chapter_id}/CHAPTER_ARRANGEMENT.json"),
            "view_path": str(archive / f"body/arrangement_repaired/{chapter_id}/ARRANGEMENT_INPUT.json")}
            for chapter_id in ids]})
    output = tmp_path / "seven-chapter-preview"
    assert cli.main(["--manifest", str(manifest), "--route", "whole_author", "--output", str(output)]) == 0
    report = read(output / "CLI_RUN.json")
    assert report["execution_mode"] == "preview" and report["model_calls"] == 0
    assert report["complete"] is False  # Input readiness is not a freshly authored complete BODY.
    assert report["actual_chapter_ids"] == report["expected_chapter_ids"] == ids
    book = read(read(output / "RUN_MANIFEST.json")["input_path"])
    assert [chapter["chapter_id"] for chapter in book["chapters"]] == ids
    assert all(chapter["units"] and chapter["sources"] for chapter in book["chapters"])
    assert (packets / "DETAILED_REVIEW_PLAN.json").read_bytes() == original_plan
    stage = report["stages"][0]
    payload = json.loads(read(stage["messages_path"])[-1]["content"])
    assert len(payload["chapters"]) == 7 and len(payload["editable_task_ids"]) == len(book["task_catalog"])
    assert payload["sources"] == book["sources"]
    assert not list(output.rglob("RAW_RESPONSE.json"))
