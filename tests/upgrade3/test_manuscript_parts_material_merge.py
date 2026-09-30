"""Offline acceptance of supplemental materials after authoritative context resolution."""
import copy
import json
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import manuscript_front_back as fb
from optomind_research.runtime.upgrade3 import review_delivery as delivery
from test_manuscript_parts_delivery import context, fixture, write
from test_review_delivery_entry import _writer_packet, _plan_recordings


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network attempted during offline material merge acceptance")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def config(tmp_path, records=None, **extra):
    if records is None:
        records = [{"source_handle": "P0900", "paper_id": "background", "text": "ADDED BACKGROUND: equal-bandwidth calibration."}]
    write(tmp_path / "materials.json", records)
    write(tmp_path / "edit.json", {"changes": [], "unresolved_questions": []})
    write(tmp_path / "parts.json", fixture())
    return write(tmp_path / "config.json", {
        "schema": delivery.DELIVERY_CONFIG_SCHEMA,
        "material_records": {"path": "materials.json"},
        "text_edit": {"fixture": "edit.json"},
        "front_back": {"fixture": "parts.json"},
        "compile_pdf": False,
        **extra,
    })


def history(tmp_path, planning_context):
    arr = write(tmp_path / "arr.json", {"chapter_id": "CH01", "title": "Physical principles",
        "units": [{"unit_id": "CH01_U01", "focus": "Mathematical teaching"}]})
    result = write(tmp_path / "units/UNIT_RESULT.json", {"chapter_id": "CH01", "unit_id": "CH01_U01",
        "body_markdown": "Deep mathematics remains exactly unchanged.", "complete": True})
    write(tmp_path / "BATCH_JOBS.json", [{"chapter_id": "CH01", "unit_id": "CH01_U01",
        "arrangement": str(arr), "output": str(result.parent)}])
    manifest = {"review_title": "Tutorial", "chapters": [{"chapter_id": "CH01", "arrangement_path": str(arr)}]}
    if planning_context is not None:
        manifest["planning_context"] = planning_context
    return write(tmp_path / "manifest.json", manifest)


def assert_front_back_received_merge(out, original):
    report = json.loads((out / "03_front_back/FRONT_BACK_REPORT.json").read_text())
    assert report["status"] == "generated"
    merged = report["planning_context"]
    assert merged["source_identity_map"] == original["source_identity_map"]
    assert merged["manuscript_parts_plan"] == original["manuscript_parts_plan"]
    assert merged["material_records"][0]["text"].startswith("ADDED BACKGROUND")
    for stage in ("conclusion", "introduction", "abstract"):
        prompt = (out / f"03_front_back/messages/front_back_{stage}_messages.json").read_text()
        assert "ADDED BACKGROUND" in prompt
    assert report["model_calls"] == 0


def test_auto_manifest_context_plus_materials_real_delivery(tmp_path):
    original = context()
    manifest = history(tmp_path, original)
    cfg = config(tmp_path)
    loaded = delivery.load_delivery_config(cfg)
    assert loaded["planning_context"] is None  # deferred, not fabricated at config load
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="history", manifest_path=manifest,
        batch_root=tmp_path, out_dir=out, config_path=cfg)
    assert_front_back_received_merge(out, original)
    assert "05_publication" in report["stages"]
    assert "Deep mathematics remains exactly unchanged." in Path(report["stages"]["03_front_back"]["final_manuscript"]).read_text()
    assert json.loads(manifest.read_text())["planning_context"] == original


def test_auto_packet_plan_locator_plus_materials_real_delivery(tmp_path):
    original = context()
    write(tmp_path / "DETAILED_REVIEW_PLAN.json", original)
    packet = _writer_packet(tmp_path)
    data = json.loads(packet.read_text())
    data.update(manuscript_parts_contract_version="optomind.manuscript_parts_plan.v1",
                planning_result_path="DETAILED_REVIEW_PLAN.json")
    write(packet, data)
    out = tmp_path / "out"
    report = delivery.run_review_delivery(start="plan", packet_path=packet,
        recordings_path=_plan_recordings(tmp_path), config_path=config(tmp_path), out_dir=out)
    assert_front_back_received_merge(out, original)
    assert report["written_units"] == ["C1_U01", "C1_U02"]
    assert report["model_calls"] == 0 and report["external_requests"] == 0


def test_explicit_context_precedence_and_supplement_still_supported(tmp_path):
    original = context()
    cfg = config(tmp_path, planning_context=original)
    loaded = delivery.load_delivery_config(cfg)
    assert loaded["planning_context"]["source_identity_map"] == original["source_identity_map"]
    assert loaded["planning_context"]["material_records"] == loaded["material_records"]
    assert original["material_records"][0]["text"] == "Calibration depends on operating conditions."
    other = context()
    other["review_argument"] = "Manifest fallback must not override explicit context"
    manifest = history(tmp_path, other)
    out = tmp_path / "out"
    delivery.run_review_delivery(start="history", manifest_path=manifest,
        batch_root=tmp_path, out_dir=out, config_path=cfg)
    assert_front_back_received_merge(out, original)
    report = json.loads((out / "03_front_back/FRONT_BACK_REPORT.json").read_text())
    assert report["planning_context"]["review_argument"] == original["review_argument"]


def test_materials_without_any_context_fail_closed_in_real_entry(tmp_path):
    manifest = history(tmp_path, None)
    out = tmp_path / "out"
    with pytest.raises(delivery.DeliveryConfigError, match="material_records_require_planning_context"):
        delivery.run_review_delivery(start="history", manifest_path=manifest, batch_root=tmp_path,
                                     out_dir=out, config_path=config(tmp_path))
    assert not (out / "03_front_back").exists()
    assert not (out / "05_publication").exists()


def test_oversized_materials_after_auto_resolution_fail_loudly(tmp_path):
    manifest = history(tmp_path, context())
    cfg = config(tmp_path, [{"text": "x" * fb.MATERIAL_RECORD_CHAR_LIMIT}])
    assert delivery.load_delivery_config(cfg)["planning_context"] is None
    with pytest.raises(delivery.DeliveryConfigError, match="bounded_input_limit"):
        delivery.run_review_delivery(start="history", manifest_path=manifest, batch_root=tmp_path,
                                     out_dir=tmp_path / "out", config_path=cfg)
    assert not (tmp_path / "out/03_front_back").exists()


@pytest.mark.parametrize("record", [
    {"source_handle": "P0900", "paper_id": "paper-B", "text": "wrong identity"},
    {"source_handle": "P0900", "paper_id": "background", "doi": "10.0000/b", "text": "wrong DOI"},
])
def test_material_record_identity_collision_fails_without_mutating_map(tmp_path, record):
    original = context()
    original["source_identity_map"]["P0900"]["doi"] = "10.0000/a"
    before = copy.deepcopy(original)
    manifest = history(tmp_path, original)
    with pytest.raises(delivery.DeliveryConfigError, match="planning_identity_conflict:P0900"):
        delivery.run_review_delivery(start="history", manifest_path=manifest, batch_root=tmp_path,
            out_dir=tmp_path / "out", config_path=config(tmp_path, [record]))
    assert original == before
    assert not (tmp_path / "out/03_front_back").exists()


def test_existing_delivery_identity_catalog_collision_still_blocks_publication(tmp_path):
    manifest = history(tmp_path, context())
    cfg = config(tmp_path, identity_catalogs=[{"entries": [{"source_handle": "P0900", "paper_id": "paper-B"}]}])
    with pytest.raises(delivery.DeliveryConfigError, match="planning_identity_conflict:P0900"):
        delivery.run_review_delivery(start="history", manifest_path=manifest, batch_root=tmp_path,
            out_dir=tmp_path / "out", config_path=cfg)
    assert not (tmp_path / "out/04_figures_citations").exists()
    assert not (tmp_path / "out/05_publication").exists()


def test_material_identity_equivalent_doi_forms_are_not_false_collisions():
    original = context()
    original["source_identity_map"]["P0900"]["doi"] = "10.0000/ABC"
    record = {"source_handle": "P0900", "paper_id": "background",
              "doi": "https://doi.org/10.0000/abc", "text": "Same source"}
    merged = delivery._merge_material_records(original, [record])
    assert merged["source_identity_map"] == original["source_identity_map"]
    assert merged["material_records"] == [record]
