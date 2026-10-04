"""WO07 short chain 4: real owner dispatch, arrangement, writer and assembly.

Only the owner/model responses and network transport are controlled. These are
synthetic wiring checks, not scientific acceptance or authorization for a full
review. Production persistence is retained by setting BODY07_WRITER_EVIDENCE_ROOT.
"""

import copy
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3 import review_delivery as delivery
from scripts.upgrade3 import review_feedback_loop as feedback_cli


ARGUMENT = "Comparisons are supported only when settings and measurement boundaries are retained."
DEVELOPMENTS = {
    "BRIEF_A1": "A measured 12 units at condition A; retain that setting.",
    "BRIEF_A2": "A boundary is one short observation, not a long-term claim.",
    "BRIEF_B": "B measured 9 units at condition B; do not erase that distinction.",
}
PROSE = "Condition A gave 12 units [P0001]; condition B gave 9 units [P0002]."
TABLE = "| Condition | Result | Source |\n|---|---|---|\n| A | 12 | [P0001] |\n| B | 9 | [P0002] |"


def _dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _root(tmp_path, name):
    supplied = os.environ.get("BODY07_WRITER_EVIDENCE_ROOT")
    root = (Path(supplied) / name if supplied else tmp_path / name).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("WO07 forbids live network access")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def _packet():
    def brief(identity, handle):
        return {"paragraph_id": identity, "point": identity + " finding",
                "development": DEVELOPMENTS[identity], "source_handles": [handle]}
    return {
        "chapter_id": "CH01", "chapter": {"chapter_id": "CH01", "title": "Bounded conditions"},
        "research_question": "Which comparison remains supported under different settings?",
        "shared_scope": {"statement": "Two existing synthetic measurements; no new sources."},
        "review_argument": ARGUMENT, "review_argument_status": "calibrated",
        "review_argument_source": "whole_plan_improvement",
        "chapter_plan": {"chapter_id": "CH01", "thesis": "Retain measurement conditions.", "units": [
            {"unit_id": "UNIT_A", "substantive_point": "A finding and its boundary", "paragraph_briefs": [
                brief("BRIEF_A1", "P0001"), brief("BRIEF_A2", "P0001")]},
            {"unit_id": "UNIT_B", "substantive_point": "B finding", "paragraph_briefs": [brief("BRIEF_B", "P0002")]},
        ]},
        "source_materials": [{"source_handle": handle, "paper_id": "synthetic-" + handle,
            "title": "Synthetic controlled material " + handle,
            "study_summary_A": {"finding": finding}}
            for handle, finding in (("P0001", DEVELOPMENTS["BRIEF_A1"]), ("P0002", DEVELOPMENTS["BRIEF_B"]))],
    }


def _updated(packet):
    plan = copy.deepcopy(packet["chapter_plan"])
    old = plan["units"]
    plan["units"] = [{"unit_id": "MERGED_AB", "substantive_point": "Compare settings without merging experiments",
        "paragraph_briefs": [old[1]["paragraph_briefs"][0], *reversed(old[0]["paragraph_briefs"])]}]
    return plan


def _arranged(packet, *, merge=False, invalid=""):
    units = []
    for unit in packet["chapter_plan"]["units"]:
        tasks = [{"paragraph_id": brief["paragraph_id"], "source_briefs": [brief["paragraph_id"]],
                  "point": "Editor placement", "development": "Editor cannot replace the owner detail.",
                  "source_uses": [{"source_handle": h} for h in brief["source_handles"]]}
                 for brief in unit["paragraph_briefs"]]
        if merge:
            tasks = [tasks[0], {"source_briefs": ["BRIEF_A2", "BRIEF_A1"], "portion": "findings_with_limits",
                "point": "Merged placement", "development": "Owner details must be restored.",
                "source_uses": [{"source_handle": "P0001"}]}]
        units.append({"unit_id": unit["unit_id"], "unit_title": "Conditions and limits", "paragraph_tasks": tasks,
            "table_tasks": [{"table_id": unit["unit_id"] + "_TABLE", "purpose": "Compare the measurements",
                "columns": ["Condition", "Result", "Source"], "row_tasks": [
                    {"content": "A: 12", "source_uses": [{"source_handle": "P0001"}]},
                    {"content": "B: 9", "source_uses": [{"source_handle": "P0002"}]}]}]})
    return {"chapter_id": "WRONG" if invalid == "contract_failed" else "CH01", "units": units}


def _provider_response(content):
    # Independent body/table keys are nested under a realistic provider wrapper;
    # both arrangement and writer parse their actual serialized input.
    return {"content": json.dumps(content, ensure_ascii=False), "complete": True,
            "finish_reason": "stop", "model": "offline-controlled-WO07", "usage": {}}


def _feedback_case(root, monkeypatch, *, missing_table=False, invalid=""):
    packet = _packet()
    if invalid == "needs_arrangement":
        packet["source_materials"].append({"source_handle": "P0003", "paper_id": "synthetic-unplaced",
            "study_summary_A": {"finding": "Existing selected evidence still requires an explicit placement."}})
    updated_plan = _updated(packet)
    if invalid == "needs_arrangement":
        updated_plan["units"][0]["supporting_studies"] = [{"source_handle": "P0003",
            "contribution": "Selected existing result still requires arrangement."}]
    remap = {"MERGED_AB": ["UNIT_A", "UNIT_B"]}
    rebuilt_packet = {**copy.deepcopy(packet), "chapter_plan": updated_plan, "unit_id_remap": remap}
    arranged = _arranged(rebuilt_packet, merge=True, invalid=invalid)
    writer_response = {"body_markdown": PROSE}
    if missing_table:
        writer_response["table_tasks"] = [{"table_id": "MERGED_AB_TABLE", "columns": ["Condition", "Result", "Source"]}]
    else:
        writer_response["table_markdown"] = TABLE
    feedback = {"issues": [{"action": "chapter_owner", "problem": "Reorder B first and merge A with its boundary."}]}
    packet_path, arrangement_path = root / "INPUT_PACKET.json", root / "INPUT_FEEDBACK.json"
    _dump(packet_path, packet)
    _dump(arrangement_path, feedback)
    args = feedback_cli._parser().parse_args([
        "--packet", str(packet_path), "--arrangement", str(arrangement_path), "--issues", str(arrangement_path),
        "--output-root", str(root / "feedback"), "--key-file", str(root / "NO_CREDENTIALS"),
        "--unit", "UNIT_A", "--language", "en"])
    calls = {"owner": 0, "arrangement": 0, "writer": 0, "network": 0}

    def owner(stage, payload):
        assert stage == "affected_chapter_revision"
        calls["owner"] += 1
        _dump(root / "OWNER_MODEL_INPUT.json", payload)
        value = {"status": "updated", "updated_plan": copy.deepcopy(updated_plan), "unit_id_remap": remap}
        _dump(root / "OWNER_MODEL_RESPONSE.json", value)
        return value

    class ControlledProvider:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.stage = "arrangement" if kwargs["model"] == args.arrangement_model else "writer"

        def complete(self, messages, **_kwargs):
            calls[self.stage] += 1
            assert calls[self.stage] == 1, "This check permits at most one call to each model edge"
            _dump(root / (self.stage.upper() + "_MODEL_MESSAGES.json"), messages)
            response = _provider_response(arranged if self.stage == "arrangement" else writer_response)
            _dump(root / (self.stage.upper() + "_MODEL_RESPONSE.json"), response)
            return response

    monkeypatch.setattr(feedback_cli, "QwenDirectClient", ControlledProvider)
    # No live-client constructor, credential read, or budget reservation. The
    # actual adapters use their ordinary local fallback estimator (counter=None).
    loop = object.__new__(feedback_cli.FeedbackLoop)
    loop.args, loop.counter, loop.ledger = args, None, None

    def run():
        return planning.run_feedback_loop(packet_path=packet_path, arrangement_path=arrangement_path,
            arrangement=feedback, owner_planner=owner, arrangement_runner=loop.arrangement_runner,
            writer_runner=loop.writer_runner, output_dir=root / "feedback")

    result = run()
    _dump(root / "FEEDBACK_RESULT.json", result)
    _dump(root / "BOUNDARY_COUNTS.json", calls)
    return {"root": root, "packet": packet, "result": result, "calls": calls, "run": run,
            "arranged": arranged, "updated_plan": updated_plan, "remap": remap}


def _deliver_written(state):
    root, result = state["root"], state["result"]
    written = _read(result["writer"])
    batch = root / "batch"
    manifest = batch / "MANIFEST.json"
    _dump(manifest, {"review_title": "WO07 synthetic bounded export", "chapters": [
        {"chapter_id": "CH01", "arrangement_path": result["arrangement"]}]})
    _dump(batch / "BATCH_JOBS.json", [{"chapter_id": "CH01", "unit_id": written["unit_id"],
        "arrangement": result["arrangement"], "reused_result": result["writer"]}])
    return delivery.run_review_delivery(start="history", manifest_path=manifest,
        batch_root=batch, out_dir=root / "delivery", language="en")


def _assert_writer_handoff(state):
    root, result = state["root"], state["result"]
    updated = _read(result["updated_packet"])
    assert updated["chapter_plan"] == state["updated_plan"]
    assert updated["unit_id_remap"] == state["remap"]
    assert updated["source_materials"] == state["packet"]["source_materials"]
    assert updated["review_argument"] == ARGUMENT
    arranged = _read(result["arrangement"])
    assert arranged["validation"]["ok"] is True
    assert arranged["unit_id_remap"] == state["remap"]
    writer = _read(result["writer"])
    assert writer["unit_id"] == "MERGED_AB"  # selection of original UNIT_A was explicitly remapped
    persisted_messages = _read(writer["messages_path"])
    assert persisted_messages == _read(root / "WRITER_MODEL_MESSAGES.json")
    payload = json.loads(persisted_messages[-1]["content"])
    assert payload["chapter_frame"]["review_argument"] == ARGUMENT
    assert payload["chapter_frame"]["review_argument_status"] == "calibrated"
    assert payload["chapter_frame"]["review_argument_source"] == "whole_plan_improvement"
    assert payload["chapter_frame"]["shared_scope"] == state["packet"]["shared_scope"]
    tasks = payload["paragraph_tasks"]
    assert tasks[0]["paragraph_id"] == "BRIEF_B"
    assert tasks[1]["paragraph_id"].startswith("MERGED_AB__TASK_")
    assert tasks[1]["paragraph_id"] not in DEVELOPMENTS
    assert [task["source_briefs"] for task in tasks] == [["BRIEF_B"], ["BRIEF_A2", "BRIEF_A1"]]
    details = {row["paragraph_id"]: row["development"] for task in tasks for row in task["source_brief_details"]}
    assert details == DEVELOPMENTS
    assert {row["source_handle"] for row in payload["sources"]} == {"P0001", "P0002"}
    assert {row["source_handle"]: row["study_summary_A"] for row in payload["sources"]} == {
        row["source_handle"]: row["study_summary_A"] for row in state["packet"]["source_materials"]}
    assert payload["table_tasks"][0]["table_id"] == "MERGED_AB_TABLE"
    arrangement_payload = json.loads(_read(root / "ARRANGEMENT_MODEL_MESSAGES.json")[-1]["content"])
    assert arrangement_payload["review_argument"] == ARGUMENT
    _dump(root / "IDENTITY_AND_CACHE_KEYS.json", {
        "old_units": ["UNIT_A", "UNIT_B"], "unit_id_remap": arranged["unit_id_remap"],
        "original_task_ids": list(DEVELOPMENTS), "actual_writer_task_ids": [task["paragraph_id"] for task in tasks],
        "source_briefs": [task["source_briefs"] for task in tasks],
        "input_signature": result["input_signature"], "resume_signature": result["resume_signature"],
        "writer_messages_sha256": hashlib.sha256(Path(writer["messages_path"]).read_bytes()).hexdigest()})
    return writer


def test_owner_reorder_merge_reaches_writer_independent_table_and_local_export(tmp_path, monkeypatch):
    state = _feedback_case(_root(tmp_path, "complete"), monkeypatch)
    result = state["result"]
    assert result["status"] == "updated"
    writer = _assert_writer_handoff(state)
    assert writer["body_markdown"] == PROSE + "\n\n" + TABLE
    assert writer["issues"] == []
    assert writer["output_consumption_status"] == "consumed"
    assert writer["unknown_citations"] == []
    assert writer["content_review_status"] == "not_reviewed"
    report = _deliver_written(state)
    assert report["delivery_mode"] == "assembly_only"
    assert report["model_calls"] == report["external_requests"] == 0
    assert report["assembly"]["problems_resolved"] is True
    assert report["assembly"]["table_count"] == 1
    assert report["assembly"]["pending_problems"] == []
    handles = (state["root"] / "delivery/assembled/REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert PROSE in handles and TABLE in handles
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1, "network": 0}
    tracked = [Path(result[key]) for key in ("updated_packet", "arrangement", "writer")]
    tracked.extend([Path(writer["body_path"]), Path(writer["messages_path"])])
    before = {str(path): path.read_bytes() for path in tracked}
    resumed = state["run"]()
    assert resumed["status"] == "reused"
    assert resumed["input_signature"] == result["input_signature"]
    assert {str(path): path.read_bytes() for path in tracked} == before
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1, "network": 0}
    _dump(state["root"] / "RESUME_RESULT.json", resumed)
    _dump(state["root"] / "BOUNDARY_COUNTS_AFTER_RESUME.json", state["calls"])


def test_pending_writer_table_reaches_actual_assembly_with_usable_body(tmp_path, monkeypatch):
    state = _feedback_case(_root(tmp_path, "pending_table"), monkeypatch, missing_table=True)
    writer = _assert_writer_handoff(state)
    assert state["result"]["status"] == "partial"
    assert writer["complete"] is True  # transport completion is not content acceptance
    assert writer["output_consumption_status"] == "pending_table"
    assert writer["body_markdown"] == PROSE
    assert writer["issues"] == [{"code": "markdown_table_missing_or_invalid"}]
    report = _deliver_written(state)
    assert report["assembly"]["problems_resolved"] is False
    assert report["assembly"]["pending_problems"] == [
        {"unit_id": "MERGED_AB", "code": "writer_issues:1", "issues": writer["issues"]}]
    assert report["assembly"]["loaded_units"] == 1
    assert report["assembly"]["table_count"] == 0
    assert report == _read(state["root"] / "delivery/DELIVERY_REPORT.json")
    assert report["assembly"] == _read(state["root"] / "delivery/assembled/ASSEMBLY_SUMMARY.json")
    handles = (state["root"] / "delivery/assembled/REVIEW_DRAFT_HANDLES.md").read_text(encoding="utf-8")
    assert PROSE in handles
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1, "network": 0}
    assert not (state["root"] / "delivery/02_text_edit").exists()
    assert not (state["root"] / "delivery/05_publication").exists()


@pytest.mark.parametrize("invalid", ["needs_arrangement", "contract_failed"])
def test_pending_arrangement_reaches_delivery_entry_and_never_dispatches_writer(tmp_path, monkeypatch, invalid):
    state = _feedback_case(_root(tmp_path, "pending_" + invalid), monkeypatch, invalid=invalid)
    result = state["result"]
    assert result["status"] == "partial" and result["pending_arrangement"] is True
    assert result["arrangement_status"] == invalid
    assert result["writer"] == ""
    assert _read(result["updated_packet"])["chapter_plan"] == state["updated_plan"]
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 0, "network": 0}
    recordings = state["root"] / "DELIVERY_RECORDINGS.json"
    _dump(recordings, {"fixture": "WO07 synthetic controlled provider response, no paid model calls",
        "recordings": {"arrangement:CH01": {"response": _read(state["root"] / "ARRANGEMENT_MODEL_RESPONSE.json")}}})
    report = delivery.run_review_delivery(start="plan", packet_path=result["updated_packet"],
        recordings_path=recordings, out_dir=state["root"] / "delivery", language="en")
    assert report["written_units"] == [] and report["assembly"] == {}
    assert report["pending"][0]["status"] == "arrangement_validation_failed"
    assert set(report["replay_modes"]) == {"arrangement:CH01"}
    assert report["model_calls"] == report["external_requests"] == 0
    assert report == _read(state["root"] / "delivery/DELIVERY_REPORT.json")
    assert not (state["root"] / "delivery/writer").exists()
    assert not (state["root"] / "delivery/assembled").exists()
    resumed = state["run"]()
    assert resumed["pending_arrangement"] is True
    assert resumed["reuse_reason"] == "identical_feedback_pending_arrangement"
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 0, "network": 0}
    _dump(state["root"] / "RESUME_RESULT.json", resumed)
    _dump(state["root"] / "BOUNDARY_COUNTS_AFTER_RESUME.json", state["calls"])


def test_normal_no_recovery_no_new_material_stays_identical_on_resume(tmp_path):
    root = _root(tmp_path, "normal")
    packet = _packet()
    packet_path = root / "INPUT_PACKET.json"
    _dump(packet_path, packet)
    original_packet = packet_path.read_bytes()
    body = PROSE + "\n\n" + TABLE
    recordings = {"arrangement:CH01": {"response": _provider_response(_arranged(packet))}}
    recordings.update({"writer:" + uid: {"response": _provider_response({"body_markdown": body})}
                       for uid in ("UNIT_A", "UNIT_B")})
    recording_path = root / "DELIVERY_RECORDINGS.json"
    _dump(recording_path, {"fixture": "WO07 normal synthetic no-recovery/no-new-material path", "recordings": recordings})
    kwargs = {"start": "plan", "packet_path": packet_path, "recordings_path": recording_path,
              "out_dir": root / "delivery", "language": "en"}
    first = delivery.run_review_delivery(**kwargs)
    assert set(first["replay_modes"]) == {"arrangement:CH01", "writer:UNIT_A", "writer:UNIT_B"}
    assert first["pending"] == [] and first["assembly"]["pending_problems"] == []
    assert first["assembly"]["problems_resolved"] is True
    assert first["original_units"] == first["assembled_units"] == 2
    observed = {}
    for uid in ("UNIT_A", "UNIT_B"):
        unit_dir = root / "delivery/writer" / uid
        result = _read(unit_dir / "UNIT_RESULT.json")
        assert result["body_markdown"] == body and result["issues"] == []
        payload = json.loads(_read(unit_dir / "UNIT_MESSAGES.json")[-1]["content"])
        assert payload["chapter_frame"]["review_argument"] == ARGUMENT
        for name in ("UNIT_RESULT.json", "UNIT_BODY.md", "INPUT_MESSAGES_SHA256.txt"):
            path = unit_dir / name
            observed[path] = path.read_bytes()
    exported = root / "delivery/assembled/REVIEW_DRAFT_HANDLES.md"
    observed[exported] = exported.read_bytes()
    _dump(root / "FIRST_DELIVERY_REPORT.json", first)
    second = delivery.run_review_delivery(**kwargs)
    assert set(second["replay_modes"]) == {"arrangement:CH01"}, "Identical writer inputs reuse actual complete persisted results"
    assert {path: path.read_bytes() for path in observed} == observed
    assert packet_path.read_bytes() == original_packet
    assert second["assembly"]["problems_resolved"] is True
    assert second["model_calls"] == second["external_requests"] == 0
    _dump(root / "NORMAL_COMPARISON.json", {"owner_calls": 0, "recovery_calls": 0, "new_material_rows": 0,
        "largest_unit_task_count": 2, "first_replay_calls": len(first["replay_modes"]),
        "resume_replay_calls": len(second["replay_modes"]), "network_calls": 0,
        "packet_bytes_identical": True, "writer_and_export_bytes_identical": True,
        "input_hashes": {uid: (root / "delivery/writer" / uid / "INPUT_MESSAGES_SHA256.txt").read_text()
                         for uid in ("UNIT_A", "UNIT_B")}})
