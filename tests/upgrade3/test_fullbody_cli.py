"""Offline complete-BODY CLI, explicit input lineage and budget gate tests."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import socket

import pytest

from scripts.upgrade3 import fullbody_writer as cli
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.writer_candidates import _profile

ROOT = Path(__file__).resolve().parents[2]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Full-BODY CLI tests forbid live network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("OPTOMIND_ECONOMY_TEXT_CEILING", "0")


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "promoted/current"
    rows = []
    for chapter_id in ("CH01", "CH02"):
        source = {"source_handle": "P0001", "paper_id": "stable-paper",
                  "title": "Controlled evidence", "study_summary_A": {"finding": "Complete useful observation"}}
        packet = {"chapter_id": chapter_id, "chapter": {"chapter_id": chapter_id, "title": chapter_id},
            "research_question": "How do conditions constrain comparison?",
            "chapter_plan": {"chapter_id": chapter_id, "units": [{"unit_id": "U1", "substantive_point": "Keep conditions",
                "paragraph_briefs": [{"paragraph_id": "P1", "point": "Explain a conditional finding", "source_handles": ["P0001"]}]}]},
            "source_materials": [source]}
        dump(root / "writer_packets" / (chapter_id + ".json"), packet)
        arrangement = dump(tmp_path / "arranged" / chapter_id / "CHAPTER_ARRANGEMENT.json", {
            "chapter_id": chapter_id, "units": [{"unit_id": "U1", "focus": "Condition explanation",
                "paragraph_tasks": [{"paragraph_id": "P1", "point": "Explain a conditional finding",
                    "source_uses": [{"source_handle": "P0001"}]}], "table_tasks": []}],
            "source_catalog": {"P0001": source}})
        view = dump(arrangement.parent / "ARRANGEMENT_INPUT.json", {"chapter_id": chapter_id,
            "title": chapter_id, "research_question": "How do conditions constrain comparison?", "units": []})
        rows.append({"chapter_id": chapter_id, "arrangement_path": str(arrangement), "view_path": str(view)})
    plan = dump(root / "DETAILED_REVIEW_PLAN.json", {
        "research_question": "How do conditions constrain comparison?", "review_argument": "Conditions matter",
        "original_user_request": "Write the entire body", "target_reader": "New researchers", "language": "en",
        "shared_scope": {"include": "Both chapters"}, "shared_outline": [{"chapter_id": row["chapter_id"]} for row in rows],
        "writer_packets": [{"chapter_id": row["chapter_id"], "json_path": "writer_packets/" + row["chapter_id"] + ".json"} for row in rows]})
    pointer = dump(tmp_path / "promoted/CURRENT_PLAN.json", {"packet_root": "current"})
    manifest = dump(tmp_path / "MANIFEST.json", {"schema_version": cli.MANIFEST_SCHEMA,
        "packet_root": str(pointer), "expected_chapter_ids": [row["chapter_id"] for row in rows], "chapters": rows})
    return {"root": root, "rows": rows, "plan": plan, "pointer": pointer, "manifest": manifest}


def test_defaults_are_plus_first_explicit_fullbody_and_offline():
    args = cli.parser().parse_args(["--manifest", "explicit.json", "--output", "preview"])
    assert args.route == "whole_author"
    assert Path(args.config) == ROOT / "config/fullbody_writer/plus_first.json"
    assert args.run is False and args.allow_max is False
    assert args.language is None
    assert cli.make_live_factory is cli.shared.make_live_factory


@pytest.mark.parametrize("name,writer,reader,reviser,budgets", [
    ("plus_first", "qwen3.5-plus", "qwen3.5-plus", "qwen3.5-plus", (16384, 49152)),
    ("economy_flash", "qwen3.7-flash", "qwen3.5-plus", "qwen3.5-plus", (16384, 49152)),
    ("plus_reasoning", "qwen3.5-plus", "qwen3.5-plus", "qwen3.5-plus", (32768, 32768)),
    ("selective_max", "qwen3.5-plus", "qwen3.5-plus", "qwen3.8-max", (16384, 49152)),
])
def test_profiles_keep_effective_stream_caps_and_selective_models(name, writer, reader, reviser, budgets):
    config = cli.read_json(ROOT / f"config/fullbody_writer/{name}.json")
    for role, model in zip(("writer", "reader", "reviser"), (writer, reader, reviser)):
        profile = _profile(config[role], role)
        assert profile["model"] == model
        expected = (32768, 65536) if model == "qwen3.8-max" else budgets
        assert (profile["thinking_budget"], profile["max_output_tokens"]) == expected
        assert sum(expected) <= runtime.model_pricing(model)["max_output_tokens"]
        assert profile["stream"] is True and profile["thinking"] is True
        assert profile["timeout_seconds"] == 900
        assert profile["stream_overall_timeout_seconds"] == 3600


def test_manifest_reads_complete_scope_and_original_requirements(case):
    book, prepared = cli.load_body_manifest(case["manifest"])
    assert prepared["expected_chapter_ids"] == ["CH01", "CH02"]
    assert prepared["actual_chapter_ids"] == ["CH01", "CH02"]
    assert book["language"] == "en"
    assert book["user_request"] == "Write the entire body"
    assert book["target_reader"] == "New researchers"
    assert book["review_scope"] == {"include": "Both chapters"}
    paths = {row["path"]: row for row in prepared["source_files"]}
    assert paths[str(case["pointer"])]["sha256"] == cli.sha256_file(case["pointer"])
    assert paths[str(case["plan"])]["sha256"] == cli.sha256_file(case["plan"])
    for row in case["rows"]:
        assert paths[row["arrangement_path"]]["sha256"]
        assert paths[row["view_path"]]["sha256"]


def test_partial_or_reordered_manifest_is_not_complete_body(case):
    manifest = cli.read_json(case["manifest"])
    manifest["chapters"] = manifest["chapters"][:1]
    dump(case["manifest"], manifest)
    with pytest.raises(ValueError, match="scope_or_order_mismatch"):
        cli.load_body_manifest(case["manifest"])
    manifest["expected_chapter_ids"] = ["CH01"]
    dump(case["manifest"], manifest)
    with pytest.raises(ValueError, match="approved_plan_fullbody_chapter_scope_mismatch"):
        cli.load_body_manifest(case["manifest"])


def test_manual_expected_list_without_plan_is_explicit_and_recorded(case):
    case["plan"].unlink()
    _, prepared = cli.load_body_manifest(case["manifest"])
    assert prepared["scope_authority"] == "explicit_manifest_list_user_responsibility"
    value = cli.read_json(case["manifest"])
    value.pop("expected_chapter_ids")
    dump(case["manifest"], value)
    with pytest.raises(ValueError, match="expected_chapter_ids"):
        cli.load_body_manifest(case["manifest"])


def test_prepare_roundtrip_is_stable_no_credentials_or_ledger(tmp_path, case, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline preparation touched live transport or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    prepared_path = tmp_path / "PREPARED.json"
    assert cli.main(["--manifest", str(case["manifest"]), "--prepare-manifest", str(prepared_path),
                     "--key-file", "/never/read/secret"]) == 0
    before, first = cli.load_body_manifest(case["manifest"])
    after, second = cli.load_body_manifest(prepared_path)
    assert first == second
    assert before == after
    assert "/never/read/secret" not in prepared_path.read_text()


def test_prepared_manifest_refuses_changed_pointer_even_when_old_root_remains(tmp_path, case):
    _, prepared = cli.load_body_manifest(case["manifest"])
    selected = dump(tmp_path / "PINNED.json", prepared)
    dump(case["pointer"], {"packet_root": "a-different-root"})
    with pytest.raises(ValueError, match="manifest_file_hash_mismatch"):
        cli.load_body_manifest(selected)


def test_existing_card_locators_hashed_missing_card_allowed_and_later_appearance_detected(tmp_path, case):
    existing = tmp_path / "card.json"
    missing = tmp_path / "missing-card.json"
    dump(existing, {"paper_id": "stable-paper", "text": "Full card material"})
    for row, card in zip(case["rows"], (existing, missing)):
        value = cli.read_json(row["arrangement_path"])
        value["source_catalog"]["P0001"]["locator"] = {"card_path": str(card)}
        dump(Path(row["arrangement_path"]), value)
    book, prepared = cli.load_body_manifest(case["manifest"])
    paths = {row["path"]: row for row in prepared["source_files"]}
    assert paths[str(existing)]["sha256"] == cli.sha256_file(existing)
    assert paths[str(missing)]["exists"] is False
    assert book["sources"]  # Complete available inline evidence remains usable.
    selected = dump(tmp_path / "PINNED.json", prepared)
    dump(missing, {"text": "Newly arrived useful evidence"})
    with pytest.raises(ValueError, match="previously_missing_source_now_exists"):
        cli.load_body_manifest(selected)


def test_packet_mode_never_silently_uses_old_sibling_view_or_writes_input(case):
    value = cli.read_json(case["manifest"])
    for row in value["chapters"]:
        old_view = Path(row.pop("view_path"))
        dump(old_view, {"chapter_id": row["chapter_id"], "title": "OLD_SNAPSHOT_MUST_NOT_APPEAR", "units": []})
    dump(case["manifest"], value)
    first, _ = cli.load_body_manifest(case["manifest"])
    second, _ = cli.load_body_manifest(case["manifest"])
    assert first == second
    assert "OLD_SNAPSHOT_MUST_NOT_APPEAR" not in json.dumps(first)
    assert not (case["root"] / "chapter_arrangement/ID_MAP.json").exists()


def test_hash_mismatch_halts_before_material_builder(case):
    manifest = cli.read_json(case["manifest"])
    manifest["chapters"][0]["arrangement_sha256"] = "0" * 64
    dump(case["manifest"], manifest)
    with pytest.raises(ValueError, match="manifest_file_hash_mismatch"):
        cli.load_body_manifest(case["manifest"])


def test_reader_draft_never_accepts_chapter_or_pending_result(tmp_path):
    wrong = dump(tmp_path / "CHAPTER_RESULT.json", {"complete": True, "body_markdown": "Chapter only"})
    with pytest.raises(ValueError, match="FULL_BODY_RESULT"):
        cli.load_draft(wrong)
    pending = dump(tmp_path / "FULL_BODY_RESULT.json", {"complete": False, "body_markdown": "Partial body"})
    with pytest.raises(ValueError, match="complete_fullbody_draft"):
        cli.load_draft(pending)
    digest = hashlib.sha256(b"Complete body").hexdigest()
    dump(pending, {"complete": True, "body_markdown": "Complete body", "body_sha256": digest})
    loaded = cli.load_draft(pending)
    assert loaded["_loaded_from"]["sha256"] == cli.sha256_file(pending)
    assert loaded["_loaded_from"]["body_sha256"] == digest


def test_recording_factory_encodes_all_fullbody_response_types(tmp_path):
    responses = {"author_whole": {"body_markdown": "All chapters", "completed_task_ids": ["CH01::U1::P1"]},
                 "reader": {"issues": []}, "reviser": {"patches": [], "rejected_issues": [], "complete": True}}
    path = dump(tmp_path / "recordings.json", responses)
    factory = cli.RecordingFactory(path)
    for stage, role in (("author_whole", "writer"), ("reader_full_body", "reader"), ("revision_001", "reviser")):
        response = factory(role, tmp_path / "attempt", {})([], stage_id=stage)
        assert json.loads(response["content"]) == responses.get(stage, responses.get(role))
        assert response["current_run_cost_cny"] == 0


def test_existing_budget_cap_is_absolute_not_additional_credit(tmp_path, monkeypatch):
    ledger_path = tmp_path / "shared.sqlite"
    ledger = runtime.GlobalBudgetLedger(limit_cny=100, path=ledger_path)
    reservation = ledger.reserve(9, "earlier-route")
    ledger.settle(reservation["reservation_id"], 9)
    def forbidden(*args, **kwargs):
        pytest.fail("conflicting ledger cap reached provider")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    args = argparse.Namespace(run=True, responses=None, allow_max=False, budget_ledger=str(ledger_path),
                              budget_limit=109, key_file=None)
    factory = cli.make_live_factory(args)
    profile = cli.read_json(cli.DEFAULT_CONFIG)["writer"]
    with pytest.raises(runtime.QwenTransportError, match="global_budget_limit_conflict"):
        factory("writer", tmp_path / "attempt", profile)
    ledger._refresh_from_db()
    assert ledger.limit_cny == 100 and ledger.actual_cny == 9


def test_preview_uses_real_engine_without_keys_or_ledger(tmp_path, case, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline preview constructed transport or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    output = tmp_path / "preview"
    assert cli.main(["--manifest", str(case["manifest"]), "--output", str(output),
        "--key-file", "/never/read/secret"]) == 0
    report = cli.read_json(output / "CLI_RUN.json")
    assert report["execution_mode"] == "preview"
    assert report["expected_chapter_ids"] == ["CH01", "CH02"]
    assert (output / "SOURCE_MANIFEST.json").is_file()
    assert "/never/read/secret" not in "\n".join(p.read_text() for p in output.rglob("*.json"))


@pytest.mark.parametrize("route", ("whole_author", "continuous_author", "workbench"))
def test_recording_runs_entire_body_and_same_input_resumes_without_calls(tmp_path, case, route, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("recording constructed transport or ledger")
    monkeypatch.setattr(runtime, "QwenDirectClient", forbidden)
    monkeypatch.setattr(runtime, "GlobalBudgetLedger", forbidden)
    book, _ = cli.load_body_manifest(case["manifest"])
    ids = list(book["task_catalog"])
    def response(selected, text):
        return {"body_markdown": text, "completed_task_ids": selected, "complete": True, "issues": []}
    replay = dump(tmp_path / "replay.json", {
        "author_whole": response(ids, "Both chapter arguments explained [P0001]."),
        "author_001": response([key for key in ids if key.startswith("CH01::")], "First chapter argument [P0001]."),
        "author_002": response([key for key in ids if key.startswith("CH02::")], "Second chapter builds on the first [P0001].")})
    output = tmp_path / route
    argv = ["--manifest", str(case["manifest"]), "--route", route, "--output", str(output), "--responses", str(replay)]
    assert cli.main(argv) == 0
    result = cli.read_json(output / "CLI_RUN.json")
    assert result["complete"] is True
    assert result["recorded_response_calls"]
    assert cli.main(argv) == 0
    assert cli.read_json(output / "CLI_RUN.json")["recorded_response_calls"] == []
    # The same command cannot silently start using altered upstream bytes.
    original = Path(case["rows"][0]["view_path"])
    changed = cli.read_json(original)
    changed["title"] = "New accepted version requires an explicit new snapshot"
    dump(original, changed)
    assert cli.main(argv) == 2


def test_unapproved_max_revision_stops_before_reading_draft_or_provider(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Max gate should precede all material and paid work")
    monkeypatch.setattr(cli, "load_body_manifest", forbidden)
    monkeypatch.setattr(cli, "load_draft", forbidden)
    monkeypatch.setattr(cli, "make_live_factory", forbidden)
    assert cli.main(["--manifest", "not-read.json", "--output", str(tmp_path / "out"),
        "--route", "reader_revision", "--draft", "not-read/FULL_BODY_RESULT.json", "--run",
        "--config", str(ROOT / "config/fullbody_writer/selective_max.json"),
        "--budget-ledger", str(tmp_path / "shared.sqlite"), "--budget-limit", "100"]) == 2
    assert "max_execution_requires_allow_max:reviser" in capsys.readouterr().err
    assert not (tmp_path / "shared.sqlite").exists()


def test_revision_requires_explicit_fullbody_draft_before_any_execution(tmp_path, capsys):
    assert cli.main(["--manifest", "unread.json", "--output", str(tmp_path), "--route", "reader_revision"]) == 2
    assert "reader_revision_requires_explicit_draft" in capsys.readouterr().err


def test_live_revision_cannot_relabel_a_recorded_draft_as_live(tmp_path, case, monkeypatch, capsys):
    book, _ = cli.load_body_manifest(case["manifest"])
    draft = dump(tmp_path / "FULL_BODY_RESULT.json", {"complete": True,
        "body_markdown": "Complete controlled body", "completed_task_ids": list(book["task_catalog"]),
        "input_hash": cli._hash(book), "execution_mode": "recording"})
    def forbidden(*args, **kwargs):
        pytest.fail("recorded base must stop before creating a live factory")
    monkeypatch.setattr(cli, "make_live_factory", forbidden)
    assert cli.main(["--manifest", str(case["manifest"]), "--output", str(tmp_path / "live"),
        "--route", "reader_revision", "--draft", str(draft), "--run",
        "--budget-ledger", str(tmp_path / "shared.sqlite"), "--budget-limit", "100"]) == 2
    assert "live_reader_revision_requires_live_fullbody_draft" in capsys.readouterr().err


def test_optional_foreign_long_locator_roundtrips_without_losing_inline_material(tmp_path, case):
    foreign = "F:\\archive\\" + "old-nested-card-location\\" * 30 + "CARD.json"
    arrangement = Path(case["rows"][0]["arrangement_path"])
    value = cli.read_json(arrangement)
    value["source_catalog"]["P0001"]["locator"] = {"card_path": foreign}
    dump(arrangement, value)
    book, prepared = cli.load_body_manifest(case["manifest"])
    optional = next(row for row in prepared["source_files"] if foreign in row["path"])
    assert optional["exists"] is False and optional["sha256"] is None
    assert optional["unavailable_reason"] == "os_error"
    assert optional["unavailable_errno"] is not None
    assert "Complete useful observation" in json.dumps(book["sources"])
    snapshot = dump(tmp_path / "PREPARED.json", prepared)
    again, prepared_again = cli.load_body_manifest(snapshot)
    assert again == book and prepared_again == prepared


def test_required_unreadable_file_is_clear_error_not_optional_material(tmp_path):
    long_path = tmp_path / ("x" * 400)
    files = cli._Files()
    with pytest.raises(ValueError, match="manifest_file_unreadable"):
        files.add(long_path, role="accepted_arrangement")
