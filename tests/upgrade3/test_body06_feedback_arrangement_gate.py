"""WO06 feedback gate: real persistence, arrangement adapter and validator.

Only the owner/provider responses and writer callback are controlled offline
boundaries. The writer callback records whether paid writing would be launched;
these fixtures do not establish model quality or exercise a full BODY run.
"""

import copy
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from scripts.upgrade3 import review_feedback_loop as feedback_cli


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _scenario(tmp_path, monkeypatch, *, mode="needs_arrangement"):
    source = {"source_handle": "P0001", "paper_id": "synthetic-one",
              "study_summary_A": {"finding": "Measured result under condition A."}}
    candidate = {"source_handle": "P0002", "paper_id": "synthetic-two",
                 "supplement_material": {"usable_content": "Comparison result under condition B."}}
    packet = {
        "chapter_id": "CH01", "chapter": {"chapter_id": "CH01", "title": "Comparison"},
        "review_argument": "Outcomes depend on conditions.",
        "chapter_plan": {"chapter_id": "CH01", "thesis": "Compare the available results.", "units": [
            {"unit_id": "UNIT_A", "source_handles": ["P0001"], "paragraph_briefs": [
                {"paragraph_id": "BRIEF_A", "point": "Explain the measured result.",
                 "source_handles": ["P0001"]}]}]},
        "source_materials": [source], "candidate_materials": [candidate],
    }
    updated_plan = copy.deepcopy(packet["chapter_plan"])
    updated_plan["thesis"] = "Compare results with their different conditions."
    updated_plan["units"][0]["source_handles"].append("P0002")
    updated_plan["units"][0]["supporting_studies"] = [{"source_handle": "P0002",
        "contribution": "Comparison result under condition B."}]
    arrangement = {"issues": [{"action": "chapter_owner", "problem": "Consider the supplied comparison."}]}
    packet_path, arrangement_path = tmp_path / "packet.json", tmp_path / "arrangement.json"
    _dump(packet_path, packet)
    _dump(arrangement_path, arrangement)
    args = feedback_cli._parser().parse_args([
        "--packet", str(packet_path), "--arrangement", str(arrangement_path),
        "--issues", str(arrangement_path), "--output-root", str(tmp_path / "feedback"),
        "--key-file", str(tmp_path / "never-read-key.txt"),
    ])
    state = {"mode": mode, "calls": {"owner": 0, "arrangement": 0, "writer": 0}}

    def owner(stage, payload):
        state["calls"]["owner"] += 1
        assert stage == "affected_chapter_revision"
        return {"status": "updated", "updated_plan": copy.deepcopy(updated_plan)}

    class ControlledProvider:
        def __init__(self, **_kwargs):
            pass

        def complete(self, messages, **_kwargs):
            state["calls"]["arrangement"] += 1
            payload = json.loads(messages[-1]["content"])
            response = {"chapter_id": payload["chapter_id"], "units": [{
                "unit_id": "UNIT_A", "paragraph_tasks": [{
                    "paragraph_id": "BRIEF_A", "source_briefs": ["BRIEF_A"],
                    "source_uses": [{"source_handle": handle} for handle in (
                        ["P0001", "P0002"] if state["mode"] != "needs_arrangement" else ["P0001"]
                    )],
                }],
            }]}
            if state["mode"] == "contract_failed":
                response["chapter_id"] = "WRONG_CHAPTER"
            if state["mode"] == "empty":
                response["units"] = []
            _dump(tmp_path / "OBSERVED_PROVIDER_RESPONSE.json", response)
            return {"response": response, "complete": True, "finish_reason": "stop"}

    monkeypatch.setattr(feedback_cli, "QwenDirectClient", ControlledProvider)
    # Construct only the actual arrangement adapter; do not initialize live
    # owner/tool clients, a budget ledger or credential-reading transports.
    loop = object.__new__(feedback_cli.FeedbackLoop)
    loop.args, loop.ledger, loop.counter = args, None, lambda *_: 100

    def writer(current, rebuilt, directory):
        state["calls"]["writer"] += 1
        state["writer_packet"] = copy.deepcopy(current)
        state["writer_arrangement"] = copy.deepcopy(rebuilt)
        return copy.deepcopy(state.get("written", {
            "body_markdown": "Existing usable body under condition A. [P0001]",
            "complete": True,
        }))

    def run(**kwargs):
        result = planning.run_feedback_loop(
            packet_path=packet_path, arrangement_path=arrangement_path, arrangement=arrangement,
            owner_planner=state.get("owner", owner),
            arrangement_runner=state.get("arranger", loop.arrangement_runner),
            writer_runner=writer, output_dir=tmp_path / "feedback", **kwargs,
        )
        _dump(tmp_path / "OBSERVED_DISPATCH.json", {"calls": state["calls"], "result": result})
        return result

    state.update(run=run, packet=packet, updated_plan=updated_plan, arrangement=arrangement,
                 packet_path=packet_path, arrangement_path=arrangement_path)
    return state


@pytest.mark.parametrize("mode", ["needs_arrangement", "contract_failed"])
def test_real_validation_blocks_writer_and_persists_owner_material(tmp_path, monkeypatch, mode):
    state = _scenario(tmp_path, monkeypatch, mode=mode)
    result = state["run"]()
    rebuilt = _read(result["arrangement"])
    assert rebuilt["units"]
    assert rebuilt["validation"]["status"] == mode
    assert rebuilt["validation"]["ok"] is False
    assert state["calls"]["writer"] == 0, state["calls"]
    assert result["status"] == "partial"
    assert result["pending_arrangement"] is True
    assert result["arrangement_status"] == mode
    assert result["arrangement_validation"] == rebuilt["validation"]
    assert result["writer"] == ""
    current = _read(result["updated_packet"])
    assert current["chapter_plan"] == state["updated_plan"]
    assert current["source_materials"][1]["supplement_material"]["usable_content"] == "Comparison result under condition B."
    assert current["source_identity_map"]["P0002"]["paper_id"] == "synthetic-two"
    assert _read(state["packet_path"]) == state["packet"]
    active = _read(tmp_path / "feedback/FEEDBACK_ACTIVE.json")
    assert active["status"] == "partial"
    assert active["pending_arrangement"] is True
    assert active["arrangement_status"] == mode
    assert active["arrangement_validation"] == rebuilt["validation"]
    assert not (Path(active["artifact_dir"]) / "WRITTEN_RESULT.json").exists()
    again = state["run"]()
    assert again["pending_arrangement"] is True
    assert again["reuse_reason"] == "identical_feedback_pending_arrangement"
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 0}


def test_valid_production_arrangement_writes_then_reuses_exact_artifacts(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, mode="arranged")
    first = state["run"]()
    assert first["status"] == "updated"
    assert state["writer_arrangement"]["validation"]["ok"] is True
    packet = state["writer_packet"]
    assert packet["chapter_plan"] == state["updated_plan"]
    assert packet["source_materials"][1]["paper_id"] == "synthetic-two"
    paths = [Path(first[key]) for key in ("updated_packet", "arrangement", "writer")]
    paths.append(paths[-1].parent / "WRITTEN_BODY.md")
    before = {path: path.read_bytes() for path in paths}
    again = state["run"]()
    assert again["status"] == "reused"
    assert again["writer"] == first["writer"]
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1}
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("validation", [
    {"ok": True, "needs_arrangement": True},
    {"ok": True, "status": "needs_arrangement"},
    {"ok": True, "status": "contract_failed"},
    {"ok": True, "errors": ["units_missing:UNIT_B"]},
    {"ok": True, "contract_ok": False},
    {"ok": True, "sources_never_mentioned": ["P0002"]},
    {"ok": True, "missing_sources": ["P0002"]},
    {"ok": False, "status": "arranged"},
    {"contract_ok": True},
    {}, None, [],
])
def test_incomplete_or_contradictory_validation_never_dispatches(tmp_path, monkeypatch, validation):
    state = _scenario(tmp_path, monkeypatch)
    # A narrow callback supplies old/contradictory envelope variants that the
    # current production validator itself does not emit.
    state["arranger"] = lambda *_: {"units": [{"unit_id": "UNIT_A"}], "validation": validation}
    result = state["run"]()
    assert result["status"] == "partial"
    assert result["pending_arrangement"] is True
    assert result["arrangement_validation"] == validation
    assert state["calls"]["writer"] == 0


@pytest.mark.parametrize("validation", ["omitted", {"ok": True}, {"status": "arranged"}])
def test_legacy_missing_validation_and_existing_success_signals_remain_compatible(
    tmp_path, monkeypatch, validation,
):
    state = _scenario(tmp_path, monkeypatch)
    arranged = {"units": [{"unit_id": "UNIT_A"}]}
    if validation != "omitted":
        arranged["validation"] = validation
    state["arranger"] = lambda *_: arranged
    assert state["run"]()["status"] == "updated"
    assert state["run"]()["status"] == "reused"
    assert state["calls"]["writer"] == 1


def test_empty_real_arrangement_stays_partial(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, mode="empty")
    result = state["run"]()
    assert not _read(result["arrangement"])["units"]
    assert result["status"] == "partial"
    assert result["pending_arrangement"] is True
    assert state["calls"]["writer"] == 0


def test_invalid_completed_cache_reports_pending_without_calls_or_losing_good_artifacts(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, mode="arranged")
    first = state["run"]()
    arrangement_path = Path(first["arrangement"])
    old = _read(arrangement_path)
    old["validation"].update(ok=False, status="needs_arrangement", needs_arrangement=True,
                             sources_never_mentioned=["P0002"], missing_sources=["P0002"])
    _dump(arrangement_path, old)
    # Emulate a pre-fix completed pointer whose existing arrangement verdict
    # was ignored. Keep valid body/owner/material files next to that verdict.
    paths = [Path(first[key]) for key in ("updated_packet", "arrangement", "writer")]
    paths.append(paths[-1].parent / "WRITTEN_BODY.md")
    before = {path: path.read_bytes() for path in paths}
    for _ in range(2):
        pending = state["run"]()
        assert pending["status"] == "partial"
        assert pending["pending_arrangement"] is True
        assert pending["writer"] == ""
        assert pending["arrangement_status"] == "needs_arrangement"
        assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1}
        assert {path: path.read_bytes() for path in paths} == before
    active = _read(tmp_path / "feedback/FEEDBACK_ACTIVE.json")
    assert active["status"] == "partial" and active["pending_arrangement"] is True
    # An explicit changed execution input can reopen the existing bounded
    # loop; the pending report itself never makes an extra model call.
    changed = state["run"](execution_context={"revision": "new-authorized-input"})
    assert changed["status"] == "updated"
    assert state["calls"] == {"owner": 2, "arrangement": 2, "writer": 2}
    assert {path: path.read_bytes() for path in paths} == before


def test_failed_rebuild_preserves_previously_written_body(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, mode="arranged")
    first = state["run"]()
    saved_writer = Path(first["writer"])
    body_path = saved_writer.parent / "WRITTEN_BODY.md"
    original = (saved_writer.read_bytes(), body_path.read_bytes())
    state["mode"] = "contract_failed"
    pending = state["run"](resume=False)
    assert pending["status"] == "partial" and pending["pending_arrangement"] is True
    assert pending["writer"] == ""
    assert state["calls"]["writer"] == 1
    assert (saved_writer.read_bytes(), body_path.read_bytes()) == original


@pytest.mark.parametrize("written", [
    {"body_markdown": "Usable fragment.", "complete": False},
    {"body_markdown": "Usable fragment.", "completion_status": "partial_length"},
    {"body_markdown": "Usable fragment.", "issues": [{"problem": "Missing comparison"}]},
    {"body_markdown": "Usable fragment.", "status": "partial", "affected_units": ["UNIT_A"]},
])
def test_valid_arrangement_does_not_hide_existing_writer_partial(tmp_path, monkeypatch, written):
    state = _scenario(tmp_path, monkeypatch, mode="arranged")
    state["written"] = written
    result = state["run"]()
    assert result["status"] == "partial"
    assert not result.get("pending_arrangement")
    assert _read(result["writer"]) == written
    assert state["calls"]["writer"] == 1


def test_owner_no_change_and_empty_feedback_keep_legacy_reuse(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch)
    state["owner"] = lambda *_: {"status": "no_change"}
    assert state["run"]()["status"] == "reused"
    assert state["calls"]["arrangement"] == state["calls"]["writer"] == 0
    state["arrangement"]["issues"] = []
    assert state["run"]()["status"] == "reused"
    state["arrangement"].update(units=[{"unit_id": "UNIT_A"}], validation={"ok": False})
    result = state["run"]()
    assert result["status"] == "partial" and result["pending_arrangement"] is True
    assert state["calls"]["arrangement"] == state["calls"]["writer"] == 0


def test_owner_no_change_preserves_real_pending_input_and_reuses_without_calls(tmp_path, monkeypatch):
    producer = _scenario(tmp_path / "producer", monkeypatch)
    invalid = _read(producer["run"]()["arrangement"])
    state = _scenario(tmp_path / "consumer", monkeypatch)
    state["arrangement"].update(units=invalid["units"], validation=invalid["validation"])
    owner_calls = []

    def no_change(*_):
        owner_calls.append(True)
        return {"status": "no_change"}

    state["owner"] = no_change
    for kwargs in ({"resume": False}, {}):
        result = state["run"](**kwargs)
        assert result["status"] == "partial"
        assert result["pending_arrangement"] is True
        assert result["arrangement_status"] == "needs_arrangement"
        assert result["updated_packet"] == str(state["packet_path"])
        assert result["arrangement"] == str(state["arrangement_path"])
    assert len(owner_calls) == 1
    assert state["calls"]["arrangement"] == 0 and state["calls"]["writer"] == 0


def test_fulfilled_action_without_owner_keeps_pending_original_arrangement(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch)
    state["arrangement"].update(units=[{"unit_id": "UNIT_A"}],
                                validation={"ok": False, "status": "needs_arrangement"},
                                issues=[{"action": "local_backfill", "problem": "Check current source."}])
    action_calls = []

    def action(*_):
        action_calls.append(True)
        return {"status": "fulfilled"}

    for _ in range(2):
        result = state["run"](feedback_runner=action)
        assert result["status"] == "partial" and result["pending_arrangement"] is True
        assert result["writer"] == ""
    assert len(action_calls) == 1
    assert state["calls"] == {"owner": 0, "arrangement": 0, "writer": 0}


def test_invalid_cached_verdict_cannot_overwrite_newer_active_pointer(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch, mode="arranged")
    first = state["run"]()
    arrangement_path = Path(first["arrangement"])
    cached = _read(arrangement_path)
    cached["validation"].update(ok=False, status="needs_arrangement")
    _dump(arrangement_path, cached)
    active_path = tmp_path / "feedback/FEEDBACK_ACTIVE.json"
    newer = {"input_signature": "newer", "resume_signature": "newer",
             "artifact_dir": "newer-run", "status": "in_progress"}
    original_read = planning._read_json

    def raced_read(path):
        value = original_read(path)
        if Path(path) == arrangement_path:
            _dump(active_path, newer)
        return value

    monkeypatch.setattr(planning, "_read_json", raced_read)
    with pytest.raises(planning.ProgressivePlanError, match="feedback_result_stale_active_artifact"):
        state["run"]()
    assert _read(active_path) == newer
    assert state["calls"] == {"owner": 1, "arrangement": 1, "writer": 1}


def test_legacy_no_change_cache_pointer_is_marked_pending_for_invalid_original(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch)
    state["owner"] = lambda *_: {"status": "no_change"}
    assert state["run"]()["status"] == "reused"
    state["arrangement"].update(units=[{"unit_id": "UNIT_A"}],
                                validation={"ok": False, "status": "needs_arrangement"})
    assert state["run"]()["pending_arrangement"] is True
    active = _read(tmp_path / "feedback/FEEDBACK_ACTIVE.json")
    assert active["status"] == "partial" and active["pending_arrangement"] is True
    assert state["calls"]["arrangement"] == state["calls"]["writer"] == 0


def test_pending_original_does_not_suppress_unfinished_action_retry(tmp_path, monkeypatch):
    state = _scenario(tmp_path, monkeypatch)
    state["arrangement"].update(units=[{"unit_id": "UNIT_A"}],
                                validation={"ok": False, "status": "needs_arrangement"},
                                issues=[{"action": "local_backfill", "problem": "Check current source."}])
    calls = []

    def action(*_):
        calls.append(True)
        return {"status": "partial" if len(calls) == 1 else "fulfilled"}

    first = state["run"](feedback_runner=action)
    assert first["status"] == "partial" and first["pending_arrangement"] is True
    second = state["run"](feedback_runner=action)
    assert second["status"] == "partial" and second["pending_arrangement"] is True
    assert len(calls) == 2
    assert state["calls"] == {"owner": 0, "arrangement": 0, "writer": 0}
