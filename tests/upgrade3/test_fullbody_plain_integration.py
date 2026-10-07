"""Independent, network-forbidden checks of the three ordinary full-BODY routes.

The three-chapter fixture exercises the real approved-packet/arrangement/CLI
handoff. The seven-chapter fixture uses unchanged public BODY40 files, with its
private locators deliberately unavailable. Generated prose is controlled wiring
evidence only; neither fixture establishes new scientific/editorial quality.
"""
from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from test_fullbody_integration import (
    ROOT, ORIGINAL_REQUEST, TARGET_READER, CaptureFactory,
    complete_case, dump, envelope, install_controlled_http, no_network,
    profile, prohibit_live_factory, read, recorded_calls,
)
from optomind_research.runtime.upgrade3 import fullbody_writer as engine
from optomind_research.runtime.upgrade3.fullbody_contracts import (
    fullbody_task_catalog, project_fullbody,
)
from scripts.upgrade3 import fullbody_writer as cli

ROUTES = ("plain_whole", "chapter_concat", "hierarchical_full")


def prose(book, ids, *, integrated=False):
    """A visibly different whole edit; content is intentionally synthetic."""
    catalog = fullbody_task_catalog(book)
    chapters = list(dict.fromkeys(catalog[key]["chapter_id"] for key in ids))
    prefix = "INTEGRATED" if integrated else "INDEPENDENT"
    paragraphs = [f"## {chapter}\n\n{prefix}_{chapter}: Controlled explanation with its original observation condition."
                  for chapter in chapters]
    if any(catalog[key]["kind"] == "table" for key in ids):
        paragraphs.append("| Observation | Boundary |\n| --- | --- |\n| Controlled recovery | Not field lifetime |")
    return {"body_markdown": "\n\n".join(paragraphs), "completed_task_ids": list(ids),
            "complete": True, "issues": []}


def recordings(path, book, route):
    catalog = fullbody_task_catalog(book)
    if route == "plain_whole":
        result = {"plain_whole": prose(book, list(catalog))}
    else:
        result = {f"chapter_{index:03d}": prose(book, [key for key, row in catalog.items()
                    if row["chapter_id"] == chapter["chapter_id"]])
                  for index, chapter in enumerate(book["chapters"], 1)}
        if route == "hierarchical_full":
            result["integrate_full_body"] = prose(book, list(catalog), integrated=True)
    return dump(path, result)


def messages_for(result):
    return [(stage, read(stage["messages_path"])) for stage in result["stages"]]


def assert_full_plan_and_local_material(payload, book, *, controlled=True):
    ids = payload["editable_task_ids"]
    expected = project_fullbody(book, ids)
    assert payload["chapters"] == expected["chapters"]
    assert payload["sources"] == expected["sources"]
    assert payload["shared_fullbody_context"] == expected["shared_fullbody_context"]
    assert payload["user_request"] == book["user_request"]
    assert payload["target_reader"] == book["target_reader"]
    assert [row["chapter_id"] for row in payload["shared_fullbody_context"]["chapters"]] == [
        row["chapter_id"] for row in book["chapters"]]
    if controlled:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert ORIGINAL_REQUEST in serialized and TARGET_READER in serialized
        for number in range(1, 4):
            assert f"TASK_TAIL_{number}" in serialized
        for source in payload["sources"]:
            number = int(source["source_handle"][1:])
            text = json.dumps(source, ensure_ascii=False)
            for marker in ("INLINE_A_TAIL", "FULL_B_BOUNDARY", "CARD_LIMIT_TAIL", "CARD_ORIGINAL_TEXT"):
                assert f"{marker}_{number}" in text


def assert_no_prior_prose(payload):
    assert payload["preceding_prose_included"] is False
    for key in ("accepted_body_markdown", "recent_prose_segments", "manuscript_navigation", "reread_segments",
                "reread_sources", "original_body_markdown"):
        assert key not in payload
    assert "INDEPENDENT_CH" not in json.dumps(payload)


@pytest.fixture
def forbid_advanced_prompts(monkeypatch):
    real_prompt = engine._prompt
    loaded = []
    def ordinary_only(*names):
        assert not {"writer", "whole_author", "continuous_author", "workbench", "reader", "revision"}.intersection(names)
        assert set(names) <= {"plain_writer", "plain_whole", "chapter_concat", "hierarchical_full"}
        loaded.extend(names)
        return real_prompt(*names)
    monkeypatch.setattr(engine, "_prompt", ordinary_only)
    return loaded


@pytest.mark.parametrize("route", ROUTES)
def test_real_cli_plain_routes_preserve_every_chapter_and_use_only_ordinary_prompts(
        tmp_path, complete_case, monkeypatch, forbid_advanced_prompts, route):
    prohibit_live_factory(monkeypatch)
    book, _ = cli.load_body_manifest(complete_case["manifest"], language="en")
    responses = recordings(tmp_path / "responses.json", book, route)
    config = dump(tmp_path / "profile.json", profile(max_tasks_per_window=1))
    output = tmp_path / route
    args = ["--manifest", str(complete_case["manifest"]), "--route", route, "--output", str(output),
            "--responses", str(responses), "--config", str(config), "--language", "en",
            "--key-file", "/must-not-read-any-credentials"]
    assert cli.main(args) == 0
    result = read(output / "CLI_RUN.json")
    expected_ids = list(fullbody_task_catalog(book))
    assert result["complete"] and result["body_complete"]
    assert result["completed_task_ids"] == expected_ids and result["pending_task_ids"] == []
    assert result["expected_chapter_ids"] == result["actual_chapter_ids"] == ["CH01", "CH02", "CH03"]
    assert len(recorded_calls(output)) == {"plain_whole": 1, "chapter_concat": 3, "hierarchical_full": 4}[route]
    assert result["current_run_cost_cny"] == 0 and result["paid_dispatch_count"] == 0
    assert read(output / "RUN_MANIFEST.json")["automatic_paid_retries"] is False
    assert read(output / "RUN_MANIFEST.json")["hidden_final_integration"] is False
    body = result["body_markdown"]
    marker = "INTEGRATED" if route == "hierarchical_full" else "INDEPENDENT"
    assert body.index(marker + "_CH01") < body.index(marker + "_CH02") < body.index(marker + "_CH03")
    assert (output / "FULL_BODY.md").read_text(encoding="utf-8") == body
    writers = []
    for stage, messages in messages_for(result):
        assert [message["role"] for message in messages] == ["system", "user"]
        payload = json.loads(messages[-1]["content"])
        assert_full_plan_and_local_material(payload, book)
        expected_prompt = "hierarchical_full" if stage["role"] == "reviser" else "plain_whole" if route == "plain_whole" else "chapter_concat"
        assert messages[0]["content"] == engine._prompt("plain_writer", expected_prompt)
        if stage["role"] == "writer":
            assert_no_prior_prose(payload)
            writers.append(payload)
        else:
            draft = read(result["independent_draft_result_path"])
            assert stage["stage_id"] == "integrate_full_body" and stage["role"] == "reviser"
            assert payload["original_body_markdown"] == draft["body_markdown"]
            assert payload["editable_task_ids"] == expected_ids
            assert payload["sources"] == book["sources"]
            assert payload["editing_scope"] == "entire_original_full_body"
    if route == "plain_whole":
        assert writers[0]["editable_task_ids"] == expected_ids
    else:
        assert [payload["current_chapter_ids"] for payload in writers] == [["CH01"], ["CH02"], ["CH03"]]
        assert [source["source_handle"] for payload in writers for source in payload["sources"]] == ["P0001", "P0002", "P0003"]
        baseline = read(result["independent_draft_result_path"])
        assert baseline["body_markdown"] == "\n\n".join(segment["body_markdown"] for segment in baseline["segments"])
        assert baseline["complete"] and baseline["effective_route"] == "chapter_concat"
        if route == "chapter_concat":
            assert body == baseline["body_markdown"] and result["global_integration_performed"] is False
        else:
            assert body != baseline["body_markdown"] and result["global_integration_performed"] is True
    immutable = {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}
    assert cli.main(args) == 0
    assert recorded_calls(output) == []
    assert (output / "FULL_BODY.md").read_text(encoding="utf-8") == body
    assert immutable == {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}


def make_engine_draft(tmp_path, case, *, output=None):
    book, _ = cli.load_body_manifest(case["manifest"], language="en")
    factory = CaptureFactory(lambda role, payload, index: prose(book, payload["editable_task_ids"], integrated=role == "reviser"))
    result = engine.run_fullbody_candidate(book, route="chapter_concat", output_dir=output or tmp_path / "base",
        config=profile(), client_factory=factory, run=True)
    assert result["complete"] and len(factory.calls) == 3
    return book, result, factory


def test_hierarchy_reuses_real_cli_baseline_and_only_calls_full_body_editor(tmp_path, complete_case, monkeypatch):
    prohibit_live_factory(monkeypatch)
    book, _ = cli.load_body_manifest(complete_case["manifest"], language="en")
    base_output, output = tmp_path / "base", tmp_path / "edit"
    config = dump(tmp_path / "profile.json", profile())
    common = ["--manifest", str(complete_case["manifest"]), "--config", str(config), "--language", "en"]
    base_responses = recordings(tmp_path / "base-responses.json", book, "chapter_concat")
    assert cli.main(common + ["--route", "chapter_concat", "--output", str(base_output), "--responses", str(base_responses)]) == 0
    baseline = read(base_output / "FULL_BODY_RESULT.json")
    original_files = {str(path): path.read_bytes() for path in base_output.rglob("*") if path.is_file()}
    editor_response = prose(book, list(fullbody_task_catalog(book)), integrated=True)
    only_editor = dump(tmp_path / "only-editor.json", {"integrate_full_body": editor_response})
    args = common + ["--route", "hierarchical_full", "--draft", str(base_output / "FULL_BODY_RESULT.json"),
                     "--output", str(output), "--responses", str(only_editor)]
    assert cli.main(args) == 0
    result = read(output / "CLI_RUN.json")
    assert recorded_calls(output) == ["integrate_full_body"]
    assert [stage["role"] for stage in result["stages"]] == ["reviser"]
    payload = json.loads(messages_for(result)[0][1][-1]["content"])
    assert payload["original_body_markdown"] == baseline["body_markdown"]
    assert payload["sources"] == book["sources"]
    assert payload["editable_task_ids"] == list(fullbody_task_catalog(book))
    assert result["body_markdown"] == editor_response["body_markdown"]
    assert result["cost_summary"]["base_cost_attribution"] == baseline["cost_summary"]
    assert result["cost_summary"]["base_reuse_charged_again"] is False
    assert original_files == {str(path): path.read_bytes() for path in base_output.rglob("*") if path.is_file()}
    assert cli.main(args) == 0 and recorded_calls(output) == []


def test_same_output_hierarchy_reuses_cached_independent_chapters(tmp_path, complete_case):
    output = tmp_path / "shared"
    book, baseline, factory = make_engine_draft(tmp_path, complete_case, output=output)
    saved_requests = [deepcopy(call["messages"]) for call in factory.calls]
    result = engine.run_fullbody_candidate(book, route="hierarchical_full", output_dir=output,
        config=profile(), client_factory=factory, run=True)
    assert result["complete"] and len(factory.calls) == 4
    assert [call["role"] for call in factory.calls] == ["writer"] * 3 + ["reviser"]
    assert result["model_calls"] == 1
    assert all(stage["cache_hit"] for stage in result["stages"][:3])
    assert [messages for _, messages in messages_for(result)[:3]] == saved_requests
    assert factory.calls[-1]["payload"]["original_body_markdown"] == baseline["body_markdown"]


@pytest.mark.parametrize("failure", ["timeout", "only_one_chapter", "invalid_json", "capacity"])
def test_pending_full_editor_preserves_complete_baseline_without_paid_fallback(tmp_path, complete_case, failure):
    book, baseline, _ = make_engine_draft(tmp_path, complete_case)
    catalog = fullbody_task_catalog(book)
    def respond(_role, payload, _index):
        value = prose(book, list(catalog), integrated=True)
        if failure == "timeout":
            return envelope(value, complete=False, finish_reason="timeout")
        if failure == "only_one_chapter":
            return prose(book, [key for key, row in catalog.items() if row["chapter_id"] == "CH01"], integrated=True)
        if failure == "invalid_json":
            return {"content": "{malformed editor response", "complete": True, "finish_reason": "stop"}
        pytest.fail("Capacity-blocked integration must never call a model")
    factory = CaptureFactory(respond)
    output = tmp_path / failure
    config = profile(**({"max_input_tokens": 1} if failure == "capacity" else {}))
    def run(**extra):
        return engine.run_fullbody_candidate(book, route="hierarchical_full", output_dir=output,
            config=config, client_factory=factory, run=True, base_result=baseline, **extra)
    result = run()
    assert result["complete"] is False and result["body_complete"] is True
    assert result["integration_pending"] is True and result["integration_complete"] is False
    assert result["body_markdown"] == baseline["body_markdown"]
    assert result["completed_task_ids"] == list(catalog) and result["pending_task_ids"] == []
    assert (output / "FULL_BODY.md").read_text(encoding="utf-8") == baseline["body_markdown"]
    assert read(result["independent_draft_result_path"])["body_markdown"] == baseline["body_markdown"]
    assert len(factory.calls) == (0 if failure == "capacity" else 1)
    saved_raw = {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}
    repeat = run()
    assert repeat["integration_pending"] and len(factory.calls) == (0 if failure == "capacity" else 1)
    assert saved_raw == {str(path): path.read_bytes() for path in output.rglob("RAW_RESPONSE.json")}
    if failure != "capacity":
        factory.respond = lambda role, payload, index: prose(book, list(catalog), integrated=True)
        recovered = run(retry_failed=True)
        assert recovered["complete"] and len(factory.calls) == 2
        assert [call["role"] for call in factory.calls] == ["reviser", "reviser"]
        for path, content in saved_raw.items():
            assert Path(path).read_bytes() == content


@pytest.mark.parametrize("route", ROUTES)
def test_preview_and_capacity_exhaustion_never_construct_provider(tmp_path, complete_case, monkeypatch, route):
    prohibit_live_factory(monkeypatch)
    config = dump(tmp_path / "blocked.json", profile(max_input_tokens=1))
    output = tmp_path / route
    assert cli.main(["--manifest", str(complete_case["manifest"]), "--route", route,
        "--output", str(output), "--config", str(config), "--key-file", "/unreadable/key"]) == 0
    result = read(output / "CLI_RUN.json")
    assert result["model_calls"] == result["paid_dispatch_count"] == 0
    assert result["complete"] is False and result["effective_route"] == route
    assert all(stage["status"] == "capacity_blocked" for stage in result["stages"])
    assert not list(output.rglob("RAW_RESPONSE.json"))


def test_actual_qwen_sse_profiles_and_shared_40_cny_accounting_include_earlier_cost(tmp_path, complete_case, monkeypatch):
    """The paid boundary is replaced by controlled HTTP; no external calls occur."""
    from optomind_research.runtime.upgrade3.module4 import runtime
    book, _ = cli.load_body_manifest(complete_case["manifest"], language="en")
    captured = install_controlled_http(monkeypatch, lambda payload: prose(book, payload["editable_task_ids"],
        integrated=bool(payload.get("editing_scope"))))
    config = dump(tmp_path / "profile.json", profile())
    ledger_path = tmp_path / "shared-budget.sqlite"
    ledger = runtime.GlobalBudgetLedger(limit_cny=40, path=ledger_path)
    earlier = ledger.reserve(9, "earlier-advanced-route")
    ledger.settle(earlier["reservation_id"], 9)
    unknown = ledger.reserve(2, "earlier-uncertain-attempt")
    ledger.settle(unknown["reservation_id"], None, uncertain=True)
    common = ["--manifest", str(complete_case["manifest"]), "--config", str(config), "--language", "en",
        "--run", "--budget-ledger", str(ledger_path), "--budget-limit", "40", "--key-file", "/does-not-exist"]
    whole, base, edited = tmp_path / "whole", tmp_path / "concat", tmp_path / "integrated"
    assert cli.main(common + ["--route", "plain_whole", "--output", str(whole)]) == 0
    assert cli.main(common + ["--route", "chapter_concat", "--output", str(base)]) == 0
    edit_args = common + ["--route", "hierarchical_full", "--output", str(edited),
        "--draft", str(base / "FULL_BODY_RESULT.json")]
    assert cli.main(edit_args) == 0
    assert len(captured) == 5
    assert [row["wire"]["thinking_budget"] for row in captured] == [1024] * 4 + [3072]
    assert [row["wire"]["max_completion_tokens"] for row in captured] == [5120] * 4 + [7168]
    assert all(row["wire"]["stream"] and row["wire"]["stream_options"]["include_usage"] and row["timeout"] == 33
               for row in captured)
    for row in captured:
        assert_full_plan_and_local_material(row["payload"], book)
    assert captured[-1]["payload"]["original_body_markdown"] == read(base / "FULL_BODY_RESULT.json")["body_markdown"]
    with sqlite3.connect(ledger_path) as db:
        rows = db.execute("SELECT call_id,status,actual_cny,request_metadata_json FROM reservations ORDER BY created_at").fetchall()
    assert len(rows) == 7 and rows[0][0:3] == ("earlier-advanced-route", "settled", 9)
    assert rows[1][0:3] == ("earlier-uncertain-attempt", "uncertain", None)
    for row in rows[2:]:
        assert row[1] == "settled" and row[2] > 0
        event = next(item for item in json.loads(row[3])["transport_events"] if item["stage"] == "request_open_start")
        assert event["overall_timeout_seconds"] == 99
    ledger._refresh_from_db()
    assert ledger.limit_cny == 40 and ledger.actual_cny > 9 and ledger.reserved_cny == 2
    assert len(list(tmp_path.rglob("*.sse.raw"))) == 5
    assert cli.main(edit_args) == 0 and len(captured) == 5
    with sqlite3.connect(ledger_path) as db:
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 7


@pytest.fixture
def public_seven_chapters(tmp_path):
    archive = ROOT / "docs/acceptance/body40-20261005"
    packets = tmp_path / "public-packets"
    (packets / "writer_packets").mkdir(parents=True)
    for number in range(1, 8):
        source = archive / f"planning/writer_packets/Ch{number}.json"
        content = source.read_bytes() if source.is_file() else gzip.decompress(Path(str(source) + ".gz").read_bytes())
        (packets / "writer_packets" / source.name).write_bytes(content)
    plan = gzip.decompress((archive / "planning/DETAILED_REVIEW_PLAN.json.gz").read_bytes())
    plan_path = packets / "DETAILED_REVIEW_PLAN.json"
    plan_path.write_bytes(plan)
    ids = [f"Ch{number}" for number in range(1, 8)]
    manifest = dump(tmp_path / "public-manifest.json", {"schema_version": cli.MANIFEST_SCHEMA,
        "packet_root": str(packets), "plan_path": str(plan_path), "expected_chapter_ids": ids,
        "chapters": [{"chapter_id": chapter_id,
            "arrangement_path": str(archive / f"body/arrangement_repaired/{chapter_id}/CHAPTER_ARRANGEMENT.json"),
            "view_path": str(archive / f"body/arrangement_repaired/{chapter_id}/ARRANGEMENT_INPUT.json")}
            for chapter_id in ids]})
    return manifest, plan_path, plan


@pytest.mark.parametrize("route", ROUTES)
def test_actual_seven_chapter_archive_replay_preserves_full_scope_with_controlled_meter(
        tmp_path, public_seven_chapters, monkeypatch, forbid_advanced_prompts, route):
    prohibit_live_factory(monkeypatch)
    manifest, plan_path, plan_bytes = public_seven_chapters
    # The exact archive exceeds the conservative byte meter on this executor.
    # A fixed controlled meter isolates routing/material delivery, not real
    # provider token capacity; the unmodified preflight is tested separately.
    monkeypatch.setattr(cli, "tokenizer_counter", lambda _value: (
        lambda _raw, _messages: 128,
        {"mode": "controlled_test_meter_not_provider_tokens", "actual_provider_token_count": False}))
    book, prepared = cli.load_body_manifest(manifest)
    ids = [f"Ch{number}" for number in range(1, 8)]
    responses = recordings(tmp_path / "recordings.json", book, route)
    output = tmp_path / route
    assert cli.main(["--manifest", str(manifest), "--route", route, "--output", str(output),
                    "--responses", str(responses)]) == 0
    result = read(output / "CLI_RUN.json")
    assert result["complete"] and result["actual_chapter_ids"] == result["expected_chapter_ids"] == ids
    assert result["semantic_quality_unreviewed"] is True
    assert result["meter"]["mode"] == "controlled_test_meter_not_provider_tokens"
    assert result["current_run_cost_cny"] == result["paid_dispatch_count"] == 0
    assert len(recorded_calls(output)) == {"plain_whole": 1, "chapter_concat": 7, "hierarchical_full": 8}[route]
    assert result["completed_task_ids"] == list(fullbody_task_catalog(book))
    marker = "INTEGRATED_" if route == "hierarchical_full" else "INDEPENDENT_"
    positions = [result["body_markdown"].index(marker + chapter) for chapter in ids]
    assert positions == sorted(positions)
    for stage, messages in messages_for(result):
        payload = json.loads(messages[-1]["content"])
        assert_full_plan_and_local_material(payload, book, controlled=False)
        if stage["role"] == "writer":
            assert_no_prior_prose(payload)
        else:
            assert payload["sources"] == book["sources"]
            assert payload["original_body_markdown"] == read(result["independent_draft_result_path"])["body_markdown"]
    assert plan_path.read_bytes() == plan_bytes
    assert any(row["exists"] is False for row in prepared["source_files"])


@pytest.mark.parametrize("route", ROUTES)
def test_public_archive_unmodified_byte_meter_reports_capacity_before_any_call(
        tmp_path, public_seven_chapters, monkeypatch, route):
    prohibit_live_factory(monkeypatch)
    manifest, plan_path, original_plan = public_seven_chapters
    output = tmp_path / "real-preflight"
    assert cli.main(["--manifest", str(manifest), "--route", route, "--output", str(output)]) == 0
    result = read(output / "CLI_RUN.json")
    assert result["expected_chapter_ids"] == result["actual_chapter_ids"] == [f"Ch{i}" for i in range(1, 8)]
    assert result["model_calls"] == result["paid_dispatch_count"] == 0
    assert result["complete"] is False and not list(output.rglob("RAW_RESPONSE.json"))
    book = read(result["input_path"])
    for stage, messages in messages_for(result):
        payload = json.loads(messages[-1]["content"])
        assert_full_plan_and_local_material(payload, book, controlled=False)
    if result["meter"]["mode"] == "utf8_byte_upper_bound":
        assert result["stages"][0]["status"] == "capacity_blocked"
        assert result["stages"][0]["estimate"]["fits"] is False
    assert plan_path.read_bytes() == original_plan
