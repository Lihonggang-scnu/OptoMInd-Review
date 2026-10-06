"""Operational provenance move through the real offline case batch consumer."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket

import pytest

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from test_preflight_case_material_cache_consumer import CaseRun, dump, read


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("final case-path acceptance forbids network access")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


def test_actual_case_batch_reuses_moved_reading_but_invalidates_science_and_settings(tmp_path, monkeypatch):
    root = Path(os.environ.get("PREFLIGHT_FINAL_CASE_EVIDENCE_ROOT", tmp_path / "cases")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    driver = CaseRun(root, monkeypatch)
    original = root / "original/DIRECTED_READING.json"
    moved = root / "moved/DIRECTED_READING.json"
    dump(original, driver.deep)
    moved.parent.mkdir(parents=True)
    shutil.copyfile(original, moved)
    pool = [{**row, "_paper_id": row["paper_id"]} for row in driver.pool]
    a = planning.load_prior_readings([original], pool)
    b = planning.load_prior_readings([moved], pool)
    assert len(a) == len(b) == 1
    assert a[0]["reused_from"] != b[0]["reused_from"]
    assert {k: v for k, v in a[0].items() if k != "reused_from"} == {k: v for k, v in b[0].items() if k != "reused_from"}
    driver.planner.prior_readings = a
    _, first_calls = driver.run()
    first = [call for call in first_calls if call["stage"] == "case_groups"]
    assert len(first) == 1
    first_payload = first[0]["payload"]
    assert str(original) in json.dumps(first_payload)
    baseline = driver.batch()
    # Recreate the planner as on CLI restart; do not patch cache/resolver stages.
    driver.planner = planning.ProgressiveReviewPlanner(driver.planner.config, planner=driver.adapter, prior_readings=b)
    _, moved_calls = driver.run(resume=True)
    moved_case_calls = [call for call in moved_calls if call["stage"] == "case_groups"]
    moved_batch = driver.batch()
    result = {"provider_calls": 0, "network_allowed": False, "identical_saved_reading_bytes": original.read_bytes() == moved.read_bytes(),
              "initial_case_calls": len(first), "moved_case_calls": len(moved_case_calls),
              "before_contract": baseline["input_contract"], "moved_contract": moved_batch["input_contract"],
              "provenance_retained_in_actual_initial_request": str(original) in json.dumps(first_payload), "controls": []}
    dump(root / "CASE_PATH_RESULT.json", result)
    assert not moved_case_calls
    assert baseline["input_contract"] == moved_batch["input_contract"]
    assert str(moved) in json.dumps(read(root / "run/writer_packets/CH01.json"))

    # Exact projections from actual message/packet, captured before later controls.
    initial_artifact = next((root / "model_calls").glob("*_case_groups.json"))
    packet_snapshot = root / "MOVED_RESUME_WRITER_PACKET.json"
    shutil.copyfile(root / "run/writer_packets/CH01.json", packet_snapshot)
    request_message = first[0]["messages"][-1]["content"]
    request_payload, _ = json.JSONDecoder().raw_decode(request_message[request_message.index("{"):])
    def excerpt(row):
        deep = row["deep_read_material"]
        return {"source_handle": row["source_handle"], "paper_id": row["paper_id"],
                "reused_from": deep["reused_from"], "status": deep["status"],
                "question_material": deep["question_material"]}
    initial_row = next(row for row in request_payload["source_materials"] if row["source_handle"] == "P0002")
    resumed_row = next(row for row in read(packet_snapshot)["source_materials"] if row["source_handle"] == "P0002")
    def source(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    same_science = planning._case_selection_material_row(resumed_row)["deep_read_material"]["question_material"] == initial_row["deep_read_material"]["question_material"]
    assert same_science
    dump(root / "ACTUAL_CONTENT_EXCERPTS.json", {
        "description": "Extracted projection, not a full request. Actual initial user message decoded at provider boundary; resumed writer packet captured before scientific-change controls.",
        "initial_actual_message": {"source_artifact": source(initial_artifact), "row": excerpt(initial_row)},
        "moved_resume_writer_packet": {"source_artifact": source(packet_snapshot), "row": excerpt(resumed_row)},
        "comparison": {"same_saved_reading_bytes": original.read_bytes() == moved.read_bytes(),
            "provenance_changed": initial_row["deep_read_material"]["reused_from"] != resumed_row["deep_read_material"]["reused_from"],
            "same_scientific_selection_projection": same_science,
            "note": "Selection and writer both retain the full answer including UNCLIPPED_TAIL. Conditions and limitations are identical."}})

    for change in ("finding", "conditions", "task", "model", "output_tokens"):
        if change in {"finding", "conditions"}:
            driver.planner._read_materials.setdefault("paper-2", copy.deepcopy(b[0]))
            field = "explanation" if change == "finding" else "conditions"
            driver.planner._read_materials["paper-2"]["question_material"][0][field] = "CHANGED_" + change
        elif change == "task":
            plan = read(root / "PLAN.json")
            plan["question"] = "Which changed task conditions should the mechanism explain?"
            dump(root / "PLAN.json", plan)
        elif change == "model":
            driver.adapter.chapter_model = "offline-case-model-b"
        else:
            driver.adapter.output_tokens = 9000
        before = driver.batch()["input_contract"]
        _, calls = driver.run(resume=True)
        case_calls = [call for call in calls if call["stage"] == "case_groups"]
        after = driver.batch()["input_contract"]
        result["controls"].append({"change": change, "case_calls": len(case_calls), "contract_changed": before != after})
        dump(root / "CASE_PATH_RESULT.json", result)
        assert len(case_calls) == 1, change
        assert before != after, change


def test_scientific_reused_from_named_fields_remain_significant(tmp_path, monkeypatch):
    driver = CaseRun(tmp_path, monkeypatch)
    for scientific_field in ("study_summary_A", "review_planning_B", "conditions", "question_material", "required_outputs"):
        a = {scientific_field: {"reused_from": "scientific provenance A"}}
        b = {scientific_field: {"reused_from": "scientific provenance B"}}
        assert driver.planner._cache_contract("case_groups", a) != driver.planner._cache_contract("case_groups", b)


def test_normal_ab_and_deep_premerge_reaches_case_consumer(tmp_path, monkeypatch):
    driver = CaseRun(tmp_path, monkeypatch)
    driver.planner.prior_readings.append({"paper_id": "paper-1", "title": "Mechanism source",
        "question_material": [{"explanation": "NORMAL_PREMERGED_DEEP"}]})
    _, calls = driver.run()
    case = next(call for call in calls if call["stage"] == "case_groups")
    material = next(row for row in case["payload"]["source_materials"] if row["source_handle"] == "P0001")
    assert material["study_summary_A"] and material["review_planning_B"]
    assert "NORMAL_PREMERGED_DEEP" in json.dumps(material["deep_read_material"])


def test_saved_temporal_probe_remains_only_an_ordering_risk():
    archive = Path(__file__).resolve().parents[2] / "docs/acceptance/preflight-materials-local-20261004"
    packet = read(archive / "real_checks/results/ATTACHED_PACKET.json")
    actual = next(row for row in packet["source_materials"] if row["source_handle"] == "P0582")
    assert actual["study_summary_A"] and actual["review_planning_B"] and actual["deep_read_material"]
    synthetic = copy.deepcopy(actual)
    independent = synthetic.pop("deep_read_material")
    rows = planning._case_selection_material_rows(["P0582"], [{"source_materials": [synthetic]}], [],
                                                  {synthetic["paper_id"]: independent})
    assert rows[0]["study_summary_A"] and rows[0]["review_planning_B"]
    assert not rows[0].get("deep_read_material")
    # This constructed late-only map is not the saved real packet, which is complete.
