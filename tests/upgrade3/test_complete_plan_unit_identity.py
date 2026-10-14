"""Stable current-plan identities at the free complete-plan consumer seam."""
import copy
import json

import pytest

from scripts.upgrade3 import outline_strengthening_pipeline as pipeline
from scripts.upgrade3 import outline_strengthening as cli
from optomind_research.runtime.upgrade3.chapter_arrangement import IdMap
from optomind_research.runtime.upgrade3.progressive_review_plan import _classify_owner_response


def packet(chapter_id, units):
    return {"chapter_id": chapter_id, "research_question": "Synthetic task",
            "chapter": {"chapter_id": chapter_id},
            "chapter_plan": {"thesis": "Keep the condition", "reader_objective": "Explain", "units": units},
            "source_materials": [{"source_handle": "P0001", "paper_id": "synthetic-paper",
                                  "study_summary_A": {"finding": "Synthetic finding", "conditions": "Synthetic condition"}}]}


def unit(**extra):
    return {"substantive_point": "Synthetic duty", "cases": [{"source_handle": "P0001", "conditions": "Keep exact"}],
            "paragraph_briefs": [{"point": "Synthetic point", "development": "Preserve the boundary",
                                  "source_handles": ["P0001"]}], **extra}


def root_with_packets(root, packets):
    for cid, value in packets.items():
        pipeline._dump(root / "writer_packets" / (cid + ".json"), value)
    pipeline._dump(root / "DETAILED_REVIEW_PLAN.json", {
        "research_question": "Synthetic task", "shared_outline": [{"chapter_id": cid} for cid in packets],
        "writer_packets": [{"chapter_id": cid, "json_path": "writer_packets/" + cid + ".json"} for cid in packets]})
    return root


def test_complete_plan_prepare_export_and_restore_only_initializes_missing_ids(tmp_path):
    originals = {"CH1": packet("CH1", [unit(unit_id="owner-explicit"), unit()]),
                 "CH2": packet("CH2", [unit(), unit(id="owner-alias")])}
    root = root_with_packets(tmp_path / "original", originals)
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    plan, packets = pipeline.load_complete_packets(root)
    expected = {"CH1": ["owner-explicit", "CH1_U02"], "CH2": ["CH2_U01", "owner-alias"]}
    for cid, row in packets.items():
        assert [u.get("unit_id") or u.get("id") for u in row["chapter_plan"]["units"]] == expected[cid]
        stripped = copy.deepcopy(row["chapter_plan"])
        for old, new in zip(originals[cid]["chapter_plan"]["units"], stripped["units"]):
            if not (old.get("unit_id") or old.get("id")):
                new.pop("unit_id")
        assert stripped == originals[cid]["chapter_plan"]
        assert row["source_materials"] == originals[cid]["source_materials"]
        assert row["unit_identity_contract"]["existing_unit_ids"] == expected[cid]
        assert row["modifiable_unit_ids"] == expected[cid]
    input_path = tmp_path / "COMPLETE.json"
    pipeline._dump(input_path, packets)
    args = cli.build_parser().parse_args(["--input", str(input_path), "--output", str(tmp_path / "selection"), "--mode", "select"])
    report = cli.prepare_selection(args)
    assert report["status"] == "prepared_no_paid_calls" and report["unit_count"] == 4
    request = pipeline._load(tmp_path / "selection/SELECTION_REQUEST.json")
    assert len(set(request["payload"]["all_unit_ids"])) == 4
    revision = pipeline.export_complete_plan(plan, packets, tmp_path / "export", provenance={"model_calls": 0})
    assert pipeline.load_complete_packets(revision)[1] == packets
    assert pipeline.load_complete_packets(root)[1] == packets
    assert {p: p.read_bytes() for p in before} == before


def test_memory_idmap_retains_existing_collision_rules_and_does_not_write(tmp_path):
    ids = IdMap(None)
    signature = IdMap.unit_signature("CH1", 1, "ignored scientific prose")
    assert ids.unit_id(signature, "CH1_U01", reserved={"CH1_U01"}) == "CH1_U01_LEGACY_02"
    assert ids.unit_id(signature, "CH1_U01", reserved={"CH1_U01"}) == "CH1_U01_LEGACY_02"
    ids.save()
    assert not list(tmp_path.iterdir())
    # Existing path mode still persists and reuses the same assignment.
    path = tmp_path / "ID_MAP.json"
    stored = IdMap(path)
    assert stored.unit_id(signature, "CH1_U01") == "CH1_U01"
    stored.save()
    assert IdMap(path).unit_id(signature, "different-fallback") == "CH1_U01"


@pytest.mark.parametrize("bad", ["duplicate", "conflicting_alias", "missing_with_remap", "unknown_remap_target", "unknown_scope"])
def test_explicit_and_historical_identity_conflicts_are_not_guessed(tmp_path, bad):
    items = [unit(unit_id="explicit"), unit()]
    value = packet("CH1", items)
    if bad == "duplicate":
        items[1]["unit_id"] = "explicit"
    elif bad == "conflicting_alias":
        items[0]["id"] = "another-explicit-ID"
    elif bad == "missing_with_remap":
        value["unit_id_remap"] = {"explicit": ["historical-ID"]}
    elif bad == "unknown_remap_target":
        items[1]["unit_id"] = "second-explicit"
        value["unit_id_remap"] = {"absent-target": ["historical-ID"]}
    else:
        value["modifiable_unit_ids"] = ["unknown-current-ID"]
    root = root_with_packets(tmp_path / "original", {"CH1": value})
    before = (root / "writer_packets/CH1.json").read_bytes()
    with pytest.raises(ValueError):
        pipeline.load_complete_packets(root)
    assert (root / "writer_packets/CH1.json").read_bytes() == before


def test_initialized_baseline_keeps_native_split_merge_mapping_requirements():
    packets = pipeline._initialize_missing_unit_ids({"CH1": packet("CH1", [unit(), unit()])})
    plan = packets["CH1"]["chapter_plan"]
    first, second = copy.deepcopy(plan["units"])
    child_a, child_b = copy.deepcopy(first), copy.deepcopy(first)
    child_a["unit_id"], child_b["unit_id"] = "child-A", "child-B"
    response = {"status": "updated", "chapter_updates": [{"chapter_id": "CH1", "updated_plan": {
        **plan, "units": [child_a, child_b, second]}}]}
    status, _, _, errors = _classify_owner_response(plan, response, packets["CH1"]["source_materials"], expected_chapter_id="CH1")
    assert status == "unresolved" and "structural_unit_change_requires_explicit_unit_ids_and_remap" in errors
    response["unit_id_remap"] = {"child-A": [first["unit_id"]], "child-B": [first["unit_id"]]}
    assert _classify_owner_response(plan, response, packets["CH1"]["source_materials"], expected_chapter_id="CH1")[0] == "updated"


def test_missing_local_id_keeps_valid_explicit_readonly_neighbor_from_other_chapter(tmp_path):
    local = packet("CH1", [unit()])
    local["read_only_unit_ids"] = ["CH2-explicit"]
    root = root_with_packets(tmp_path / "original", {
        "CH1": local, "CH2": packet("CH2", [unit(unit_id="CH2-explicit")])})
    _, packets = pipeline.load_complete_packets(root)
    assert packets["CH1"]["chapter_plan"]["units"][0]["unit_id"] == "CH1_U01"
    assert packets["CH1"]["read_only_unit_ids"] == ["CH2-explicit"]
    path = tmp_path / "COMPLETE.json"
    pipeline._dump(path, packets)
    args = cli.build_parser().parse_args(["--input", str(path), "--output", str(tmp_path / "selection"), "--mode", "select"])
    assert cli.prepare_selection(args)["unit_count"] == 2
