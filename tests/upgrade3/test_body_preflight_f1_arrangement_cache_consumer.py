"""F1: actual arrangement CLI cache and standalone writer, entirely offline.

Only the arrangement provider/ledger boundaries are replaced. Validator, cache,
exports, writer CLI, messages and output files are real. Set
BODY_PREFLIGHT_F1_EVIDENCE_ROOT to retain the before/after artifacts.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
from optomind_research.runtime.upgrade3 import review_unit_writer as writing
from optomind_research.runtime.upgrade3.module4 import runtime as transport
from scripts.upgrade3 import chapter_arrangement as arrangement_cli
from scripts.upgrade3 import review_unit_writer as writer_cli


ROOT = Path(__file__).resolve().parents[2]
BRIEFS = [
    {"paragraph_id": "B1", "point": "Finding one", "development": "Condition one and its boundary",
     "source_handles": ["P0001"]},
    {"paragraph_id": "B2", "point": "Finding two", "development": "Condition two and its boundary",
     "source_handles": ["P0002"]},
]
ISSUES = [{"issue_id": "I1", "unit_id": "U1", "problem": "Keep both conditions",
           "source_handles": ["P0001", "P0002"], "action": "chapter_owner", "status": "open"}]


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _root(tmp_path, name):
    supplied = os.environ.get("BODY_PREFLIGHT_F1_EVIDENCE_ROOT")
    root = (Path(supplied) / name if supplied else tmp_path / name).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("F1 is offline; live network access is forbidden")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def _packet(root):
    packet_root = root / "packet"
    packet = {
        "chapter_id": "CH01", "research_question": "Which conditions are supported?",
        "review_argument": "Retain conditions and limits.",
        "chapter_plan": {"thesis": "Retain both findings.", "units": [{
            "unit_id": "U1", "substantive_point": "Compare the conditions",
            "paragraph_briefs": copy.deepcopy(BRIEFS),
        }]},
        "source_materials": [
            {"source_handle": "P0001", "paper_id": "paper-1", "title": "Paper one",
             "study_summary_A": {"finding": "Finding one under condition one"}},
            {"source_handle": "P0002", "paper_id": "paper-2", "title": "Paper two",
             "deep_read_material": {"question_material": "Review-derived finding two with citation identity"}},
        ],
    }
    _dump(packet_root / "writer_packets/CH01.json", packet)
    _dump(packet_root / "DETAILED_REVIEW_PLAN.json", {
        "shared_outline": [{"chapter_id": "CH01", "title": "Conditions"}],
        "review_argument": packet["review_argument"],
    })
    return packet_root


def _response(kind):
    if kind == "normal":
        tasks = [{"paragraph_id": brief["paragraph_id"], "source_briefs": [brief["paragraph_id"]]}
                 for brief in BRIEFS]
    elif kind == "merged":
        tasks = [{"source_briefs": ["B2", "B1"], "portion": "findings and limits together"}]
    else:
        tasks = [{"source_briefs": ["B1"], "portion": portion} for portion in ("finding", "boundary")]
        tasks.append({"paragraph_id": "B2", "source_briefs": ["B2"]})
    for task in tasks:
        task.update(point="Editor placement", development="Editor does not replace the owner's explanation")
    return {"chapter_id": "CH01", "chapter_argument": "Placement", "units": [{
        "unit_id": "U1", "unit_title": "Conditions and limits", "paragraph_tasks": tasks,
        "table_tasks": [{"table_id": "T1", "purpose": "Compare", "columns": ["Finding", "Source"],
                         "row_tasks": [{"content": "Both findings", "source_uses": [
                             {"source_handle": "P0001"}, {"source_handle": "P0002"}]}]}],
    }], "issues": copy.deepcopy(ISSUES)}


def _writer_subprocess(root, arrangement_path, *, name, extra=()):
    output = root / name
    command = [sys.executable, str(ROOT / "scripts/upgrade3/review_unit_writer.py"),
               "--arrangement", str(arrangement_path), "--unit", "U1", "--planning-revision",
               "--output-root", str(output), *extra]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    _dump(root / (name + "_CLI.json"), {"command": command, "exit_code": result.returncode,
                                       "stdout": result.stdout, "stderr": result.stderr})
    return result, output


@pytest.mark.parametrize("kind", ["normal", "merged", "split"])
@pytest.mark.parametrize("planning_revision", [False, True], ids=["legacy_text_mode", "owner_brief_mode"])
def test_actual_fresh_cache_cli_preserves_tasks_issues_and_writer_messages(
    tmp_path, monkeypatch, kind, planning_revision,
):
    root = _root(tmp_path, f"chain_{kind}_{planning_revision}")
    packet_root = _packet(root)
    output = root / "arrangement"
    calls = []

    class OfflineLedger:
        def __init__(self, *_args, **_kwargs):
            pass

        def round_state(self):
            return {"offline": True, "spent_cny": 0}

    class OfflineProvider:
        def __init__(self, **_kwargs):
            pass

        def complete(self, messages, **_kwargs):
            calls.append(copy.deepcopy(messages))
            assert len(calls) == 1, "cache restore must not call the provider again"
            _dump(root / "ARRANGEMENT_MODEL_MESSAGES.json", messages)
            response = {"response": _response(kind), "complete": True, "finish_reason": "stop"}
            _dump(root / "ARRANGEMENT_MODEL_RESPONSE.json", response)
            return response

    monkeypatch.setattr(arrangement_cli, "RoundCappedLedger", OfflineLedger)
    monkeypatch.setattr(transport, "QwenDirectClient", OfflineProvider)
    argv = ["--packet-root", str(packet_root), "--output-root", str(output), "--chapter", "CH01", "--run"]
    if planning_revision:
        argv.append("--planning-revision")
    assert arrangement_cli.main(argv) == 0
    path = output / "CH01/CHAPTER_ARRANGEMENT.json"
    fresh = _read(path)
    _dump(root / "FRESH_ARRANGEMENT.json", fresh)
    _dump(root / "FRESH_RUN.json", _read(output / "ARRANGEMENT_RUN.json"))
    # Run the real standalone writer command, without a live model or key.
    fresh_cli, fresh_writer = _writer_subprocess(root, path, name="fresh_writer")
    assert arrangement_cli.main(argv) == 0
    cached = _read(path)
    _dump(root / "CACHED_ARRANGEMENT.json", cached)
    cached_run = _read(output / "ARRANGEMENT_RUN.json")
    _dump(root / "CACHED_RUN.json", cached_run)
    cached_cli, cached_writer = _writer_subprocess(root, path, name="cached_writer")
    _dump(root / "RESULT.json", {
        "provider_calls": len(calls), "live_calls": 0,
        "fresh_validation": fresh["validation"], "cached_validation": cached["validation"],
        "fresh_issue_count": len(fresh["issues"]), "cached_issue_count": len(cached["issues"]),
        "fresh_writer_exit": fresh_cli.returncode, "cached_writer_exit": cached_cli.returncode,
    })
    assert fresh["validation"]["ok"] is True
    assert cached["validation"]["ok"] is True, cached["validation"]
    assert len(calls) == 1 and cached_run["model_calls"] == 0
    assert cached_run["chapters"][0]["status"] == "reused_arranged"
    assert cached["issues"] == fresh["issues"] == ISSUES
    assert cached["units"] == fresh["units"]
    assert fresh_cli.returncode == cached_cli.returncode == 0
    fresh_messages = _read(fresh_writer / "CH01_U1/UNIT_MESSAGES.json")
    cached_messages = _read(cached_writer / "CH01_U1/UNIT_MESSAGES.json")
    assert cached_messages == fresh_messages
    payload = json.loads(cached_messages[-1]["content"])
    tasks = payload["paragraph_tasks"]
    assert [task["source_briefs"] for task in tasks] == [task["source_briefs"] for task in _response(kind)["units"][0]["paragraph_tasks"]]
    assert [task.get("portion") for task in tasks] == [task.get("portion") for task in _response(kind)["units"][0]["paragraph_tasks"]]
    assert {brief["paragraph_id"]: brief["development"] for task in tasks
            for brief in task["source_brief_details"]} == {brief["paragraph_id"]: brief["development"] for brief in BRIEFS}
    assert {source["source_handle"] for source in payload["sources"]} == {"P0001", "P0002"}
    sources = {source["source_handle"]: source for source in payload["sources"]}
    assert sources["P0002"]["deep_read_material"]["question_material"]
    fake_body = "Finding one [P0001] and review-derived finding two [P0002]."
    fake_path = root / "FAKE_WRITER_RESPONSE.json"
    _dump(fake_path, {"body_markdown": fake_body,
                      "table_markdown": "| Finding | Source |\n|---|---|\n| One | [P0001] |\n| Two | [P0002] |"})
    fake_cli, fake_output = _writer_subprocess(root, path, name="fake_writer", extra=("--fake-client", str(fake_path)))
    assert fake_cli.returncode == 0, fake_cli.stderr
    fake_run = _read(fake_output / "UNIT_WRITING_RUN.json")
    assert fake_run["model_calls"] == 0 and fake_run["mode"] == "fake"
    assert fake_run["units"][0]["status"] == "simulated"
    assert fake_body in Path(fake_run["units"][0]["body_path"]).read_text(encoding="utf-8")
    assert _read(fake_output / "_simulated/CH01_U1/UNIT_MESSAGES.json") == cached_messages


def _export(root, *, invalid=""):
    packet_root = _packet(root)
    view = arranging.build_chapter_view(packet_root / "writer_packets/CH01.json", id_map_path=root / "ID_MAP.json")
    payload = _response("normal")
    if invalid == "contract_failed":
        payload["chapter_id"] = "WRONG"
    elif invalid == "needs_arrangement":
        payload["units"][0]["paragraph_tasks"] = [payload["units"][0]["paragraph_tasks"][0]]
        payload["units"][0]["paragraph_tasks"][0]["source_uses"] = [{"source_handle": "P0001"}]
        payload["units"][0]["table_tasks"] = []
    arrangement = arranging.validate_arrangement(payload, view, planning_revision=invalid != "needs_arrangement")
    arranging.write_view(view, root / "ARRANGEMENT_INPUT.json")
    arrangement_cli._export(root, view, arrangement)
    return root / "CHAPTER_ARRANGEMENT.json", view


@pytest.mark.parametrize("invalid", ["contract_failed", "needs_arrangement"])
@pytest.mark.parametrize("mode", ["preview", "fake", "run", "completion"])
def test_invalid_arrangement_blocks_actual_cli_before_writer_or_existing_output(
    tmp_path, monkeypatch, invalid, mode,
):
    root = _root(tmp_path, f"blocked_{invalid}_{mode}")
    path, _ = _export(root, invalid=invalid)
    assert _read(path)["validation"]["status"] == invalid
    output = root / "writer"
    unit_dir = output / ("_simulated" if mode == "fake" else "") / "CH01_U1"
    unit_dir.mkdir(parents=True, exist_ok=True)
    body_name = "UNIT_BODY.simulated.md" if mode == "fake" else "UNIT_BODY.md"
    for name in (body_name, "UNIT_INPUT.json", "UNIT_MESSAGES.json", "UNIT_RESULT.json"):
        (unit_dir / name).write_bytes(b"EXISTING GOOD ARTIFACT\r\n")
    before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    calls = {"client_factory": 0, "writer": 0}

    def client_factory(*_args, **_kwargs):
        calls["client_factory"] += 1

        def client(*_args, **_kwargs):
            calls["writer"] += 1
            return {"content": "Replacement [P0001]", "complete": True, "finish_reason": "stop"}
        return client

    monkeypatch.setattr(writer_cli, "_real_client", client_factory)
    monkeypatch.setattr(writer_cli, "_fake_client", client_factory)
    argv = ["--arrangement", str(path), "--unit", "U1", "--output-root", str(output)]
    if mode == "fake":
        argv += ["--fake-client", "OFFLINE_STUB"]
    elif mode == "run":
        argv += ["--run"]
    elif mode == "completion":
        argv += ["--complete-task", "B1", "--existing-body", str(unit_dir / body_name), "--run"]
    code = writer_cli.main(argv)
    after = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    _dump(root / "RESULT.json", {"exit_code": code, "calls": calls, "existing_bytes_preserved": after == before,
                                 "existing_body_preserved": after[unit_dir / body_name] == before[unit_dir / body_name],
                                 "body_before_sha256": hashlib.sha256(before[unit_dir / body_name]).hexdigest(),
                                 "body_after_sha256": hashlib.sha256(after[unit_dir / body_name]).hexdigest(),
                                 "validation": _read(path)["validation"]})
    assert code == 2
    assert calls == {"client_factory": 0, "writer": 0}
    assert after == before
    with pytest.raises(writing.UnitWritingError, match="arrangement_not_ready"):
        writing.build_unit_view(path, "U1")


@pytest.mark.parametrize("invalid", ["contract_failed", "needs_arrangement"])
def test_invalid_subprocess_run_refuses_before_credentials_or_output(tmp_path, invalid):
    root = _root(tmp_path, "subprocess_" + invalid)
    path, _ = _export(root, invalid=invalid)
    result, output = _writer_subprocess(root, path, name="writer", extra=(
        "--run", "--budget-ledger", str(root / "ABSENT_LEDGER"), "--key-file", str(root / "ABSENT_KEY")))
    assert result.returncode == 2
    assert "arrangement_not_ready:" + invalid in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("validation", [
    {"ok": True, "needs_arrangement": True}, {"ok": True, "contract_ok": False},
    {"ok": True, "status": "needs_arrangement"}, {"ok": True, "status": "contract_failed"},
    {"ok": True, "errors": ["units_missing:U2"]},
    {"ok": True, "sources_never_mentioned": ["P0002"]}, {"ok": True, "missing_sources": ["P0002"]},
    {"ok": False, "status": "arranged"}, {"contract_ok": True}, {}, None, [],
])
def test_explicit_invalid_or_contradictory_verdict_is_not_a_legacy_export(tmp_path, validation):
    path, _ = _export(tmp_path)
    arrangement = _read(path)
    arrangement["validation"] = validation
    _dump(path, arrangement)
    with pytest.raises(writing.UnitWritingError, match="arrangement_not_ready"):
        writing.build_unit_view(path, "U1")


@pytest.mark.parametrize("validation", ["omitted", {"ok": True}, {"status": "arranged"}])
def test_legacy_statusless_or_positive_verdict_remains_compatible(tmp_path, validation):
    path, _ = _export(tmp_path)
    arrangement = _read(path)
    if validation == "omitted":
        arrangement.pop("validation")
    else:
        arrangement["validation"] = validation
    _dump(path, arrangement)
    assert writing.build_unit_view(path, "U1").unit_id == "U1"


@pytest.mark.parametrize("kind", ["normal", "merged", "split"])
def test_legacy_export_reimport_preserves_existing_relationships_and_issues(tmp_path, kind):
    _, view = _export(tmp_path)
    original = arranging.validate_arrangement(_response(kind), view, planning_revision=True)
    payload, meta = arrangement_cli._payload_from_saved(original)
    assert meta["legacy_cache"] is True
    restored = arranging.validate_arrangement(payload, view, planning_revision=True)
    assert restored["units"] == original["units"]
    assert restored["issues"] == original["issues"]
    assert restored["validation"]["ok"] is True


@pytest.mark.parametrize("kind", ["merged", "split"])
def test_old_lossy_cache_cannot_fabricate_missing_merge_or_split_relationships(tmp_path, monkeypatch, kind):
    root = _root(tmp_path, "lossy_cache_" + kind)
    _, view = _export(root)
    original = arranging.validate_arrangement(_response(kind), view, planning_revision=True)
    lossy = arrangement_cli.saved_payload_from(original)
    for task in lossy["units"][0]["paragraph_tasks"]:
        task.pop("source_briefs", None)
        task.pop("portion", None)
    output = root / "recovery"
    argv = ["--packet-root", str(root / "packet"), "--output-root", str(output),
            "--chapter", "CH01", "--planning-revision"]
    assert arrangement_cli.main(argv) == 0
    signature = _read(output / "ARRANGEMENT_RUN.json")["chapters"][0]["signature"]
    wrapped = arrangement_cli._cache_entry(lossy, model="offline", signature=signature)
    _dump(output / "CH01/_cache" / (signature + ".json"), wrapped)

    class OfflineLedger:
        def __init__(self, *_args, **_kwargs):
            pass

        def round_state(self):
            return {"offline": True, "spent_cny": 0}

    def unexpected_provider(**_kwargs):
        pytest.fail("an old incomplete cache must not automatically trigger a paid retry")

    monkeypatch.setattr(arrangement_cli, "RoundCappedLedger", OfflineLedger)
    monkeypatch.setattr(transport, "QwenDirectClient", unexpected_provider)
    assert arrangement_cli.main([*argv, "--run"]) == 0
    report = _read(output / "ARRANGEMENT_RUN.json")
    assert report["model_calls"] == 0
    assert report["chapters"][0]["status"] == "reused_contract_failed"
    invalid = _read(output / "CH01/CHAPTER_ARRANGEMENT.json")
    assert invalid["validation"]["status"] == "contract_failed"
    assert invalid["validation"]["errors"] == ["briefs_unclaimed:U1:" + ("B1,B2" if kind == "merged" else "B1")]
    with pytest.raises(writing.UnitWritingError, match="arrangement_not_ready"):
        writing.build_unit_view(output / "CH01/CHAPTER_ARRANGEMENT.json", "U1")
    result, writer_output = _writer_subprocess(root, output / "CH01/CHAPTER_ARRANGEMENT.json", name="writer")
    assert result.returncode == 2 and "arrangement_not_ready:contract_failed" in result.stderr
    assert not writer_output.exists()
