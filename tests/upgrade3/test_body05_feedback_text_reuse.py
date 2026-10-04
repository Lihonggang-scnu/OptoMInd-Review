"""Bounded feedback persistence checks, with deterministic model callbacks.

These are engineering fixtures, not a full BODY run or scientific evaluation.
"""
import copy
import json
from pathlib import Path

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning


def test_identical_feedback_keeps_written_bytes_and_changed_argument_reopens(tmp_path):
    chapter_plan = {"units": [{"unit_id": "U-mechanism", "source_handles": ["P0001"],
                               "paragraph_briefs": [{"paragraph_id": "P-comparison",
                                                     "point": "Compare conditions"}]}]}
    packet = {"chapter": {"chapter_id": "CH01"}, "chapter_plan": chapter_plan,
              "review_argument": "The observed tradeoff depends on operating conditions.",
              "shared_scope": {"statement": "Compare two device methods."},
              "source_materials": [{"source_handle": "P0001", "paper_id": "synthetic-device",
                                    "study_summary_A": {"finding": "A measured tradeoff in the stated range."}}]}
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    arrangement = {"issues": [{"action": "chapter_owner", "problem": "Clarify the comparison condition"}],
                   "units": [{"unit_id": "U-mechanism"}]}
    arrangement_path = tmp_path / "arrangement.json"
    arrangement_path.write_text(json.dumps(arrangement), encoding="utf-8")
    calls = {"owner": 0, "arrangement": 0, "writer": 0}
    arguments = []

    def owner(stage, payload):
        calls["owner"] += 1
        updated = copy.deepcopy(chapter_plan)
        updated["units"][0]["paragraph_briefs"][0]["point"] = "Compare the measured operating conditions"
        return {"status": "updated", "updated_plan": updated}

    def arrange(current, previous, directory):
        calls["arrangement"] += 1
        assert current["chapter_plan"]["units"][0]["unit_id"] == "U-mechanism"
        return {"units": [{"unit_id": "U-mechanism", "source_handles": ["P0001"]}]}

    def write(current, arranged, directory):
        calls["writer"] += 1
        arguments.append(current["review_argument"])
        return {"body_markdown": "Synthetic preserved body: conditions matter. [P0001]\n\nSecond paragraph.",
                "complete": True}

    def run():
        return planning.run_feedback_loop(
            packet_path=packet_path, arrangement_path=arrangement_path, arrangement=arrangement,
            owner_planner=owner, arrangement_runner=arrange, writer_runner=write,
            output_dir=tmp_path / "feedback")

    first = run()
    assert first["status"] == "updated", first
    body_path = Path(first["writer"]).parent / "WRITTEN_BODY.md"
    original_bytes = body_path.read_bytes()
    written_bytes = Path(first["writer"]).read_bytes()
    second = run()
    assert second["status"] == "reused"
    assert second["writer"] == first["writer"]
    assert calls == {"owner": 1, "arrangement": 1, "writer": 1}
    assert body_path.read_bytes() == original_bytes
    assert Path(second["writer"]).read_bytes() == written_bytes

    packet["review_argument"] = "The comparison supports equivalence only within the measured range."
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    third = run()
    assert third["status"] == "updated", third
    assert calls == {"owner": 2, "arrangement": 2, "writer": 2}
    assert arguments[0] != arguments[1]
    assert third["writer"] != first["writer"]
    assert body_path.read_bytes() == original_bytes
