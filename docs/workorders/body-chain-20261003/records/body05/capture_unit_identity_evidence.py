"""Bounded offline production payload capture. No model, retrieval, or full chain.

The accepted baseline is loaded from git into temporary modules, not a second
planner or edited worktree. The fabricated packet is intentionally tiny.
"""
import importlib.util
import json
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile


def deny(*args, **kwargs):
    raise AssertionError("WO05 evidence forbids network")


socket.create_connection = deny
socket.socket.connect = deny
from optomind_research.runtime.upgrade3 import chapter_arrangement as after_arrange
from optomind_research.runtime.upgrade3 import review_unit_writer as after_write

BASE = "3d244f23166c9ba2e948fd8efd8257d00dbebc51"
ROOT = Path("docs/workorders/body-chain-20261003/records/body05/unit_identity")
fixture = runpy.run_path("tests/upgrade3/test_body05_unit_identity_continuity.py")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def baseline(name, directory):
    source = subprocess.check_output(["git", "show", f"{BASE}:optomind_research/runtime/upgrade3/{name}.py"])
    path = Path(directory) / f"{name}.py"
    path.write_bytes(source)
    module_name = f"optomind_research.runtime.upgrade3._wo05_before_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.__file__ = str(Path(f"optomind_research/runtime/upgrade3/{name}.py").resolve())
    return module


def capture(label, arrange, writer):
    root = ROOT / label
    packet = fixture["_packet"]()
    observations = []
    for stage in ("initial", "reordered"):
        if stage == "reordered":
            packet["chapter_plan"]["units"].reverse()
            packet["chapter_plan"]["units"][1]["paragraph_briefs"].reverse()
        directory = root / stage
        packet_path = directory / "PACKET.json"
        save(packet_path, packet)
        view = arrange.build_chapter_view(packet_path, id_map_path=root / "ID_MAP.json")
        arrange.write_view(view, directory / "ARRANGEMENT_INPUT.json")
        save(directory / "ARRANGEMENT_MESSAGES.json", arrange.arrangement_messages(view.arrangement_payload()))
        response = fixture["_payload"](view)
        result = arrange.validate_arrangement(response, view, planning_revision=True)
        result["source_catalog"] = arrange.build_source_catalog(view, result)
        save(directory / "CHAPTER_ARRANGEMENT.json", result)
        unit = next(u for u in view.units if u.substantive_point == "A claim")
        wview = writer.build_unit_view(directory / "CHAPTER_ARRANGEMENT.json", unit.unit_id)
        messages = writer.unit_messages(wview, planning_revision=True)
        writer.write_unit_input(wview, messages, directory / "writer", estimate={}, language="en")
        writer.write_unit_output(wview, "Fixture A setting and limit [P0001].", directory / "writer",
                                 model="offline-fixture", language="en", mode="fake",
                                 used_messages=messages, estimate={})
        observations.append({"stage": stage, "unit_id_for_A": unit.unit_id,
                             "paragraph_identity": {b.point: b.paragraph_id for b in unit.paragraph_briefs},
                             "writer_argument": wview.chapter_frame.get("review_argument"),
                             "writer_shared_scope": wview.chapter_frame.get("shared_scope"),
                             "validation": result["validation"]})
    save(root / "OBSERVED.json", observations)
    return observations


with tempfile.TemporaryDirectory(prefix="wo05-accepted-base-") as temporary:
    before = capture("before", baseline("chapter_arrangement", temporary), baseline("review_unit_writer", temporary))
after = capture("after", after_arrange, after_write)
# Explicit split/merge references and portions receive task IDs independent of
# output position. Persist the actual production response and writer messages.
root = ROOT / "split_merge"
root.mkdir(parents=True, exist_ok=True)
view = fixture["_view"](root)
response = fixture["_payload"](view)
refs = [p.paragraph_id for p in view.units[0].paragraph_briefs]
response["units"][0]["paragraph_tasks"] = [
    {"source_briefs": refs, "portion": "settings"},
    {"source_briefs": refs, "portion": "limits"}]
result = after_arrange.validate_arrangement(response, view, planning_revision=True)
fixture["_writer"](root, view, result, "UNIT_A")
save(root / "ARRANGEMENT_RESPONSE.json", response)
save(ROOT / "SUMMARY.json", {
    "baseline": BASE, "material": "invented offline fixture", "model_calls": 0,
    "full_chain_run": False, "before": before, "after": after,
    "split_merge_task_identities": [{k: task[k] for k in ("paragraph_id", "source_briefs", "portion", "source_brief_details")}
                                    for task in result["units"][0]["paragraph_tasks"]],
    "historical_real_run_material": "LOCAL_ONLY",
    "cache_limit": "Writer output here is persisted simulated transport evidence, not reuse. Full-message hash cache stays unchanged; reorder may invalidate position/sibling context. Separate bounded feedback checks verify compatible unchanged-text reuse.",
})
print(ROOT / "SUMMARY.json")
