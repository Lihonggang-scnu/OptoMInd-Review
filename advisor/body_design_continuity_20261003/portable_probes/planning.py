"""Small offline probes for BODY_DESIGN_CONTINUITY_AUDIT claims.

Only the planner/model callable is faked.  Stage files, chapter caches, the
real chapter view/CLI and the real feedback filtering stay on the code path.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKTREE = ROOT
import tempfile
OUT = Path(tempfile.mkdtemp(prefix="optomind-planning-probe-"))
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(WORKTREE))

from optomind_research.runtime.upgrade3 import progressive_review_plan as planning  # noqa: E402
from optomind_research.runtime.upgrade3.chapter_arrangement import build_chapter_view  # noqa: E402


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def stage_without_inputs() -> dict:
    root = OUT / "stage_no_inputs"
    planner = planning.ProgressiveReviewPlanner(
        planning.ProgressivePlannerConfig(
            topic_id="continuity-stage", pool_path=root / "pool.jsonl", plan_path=root / "plan.json", output_dir=root
        ),
        planner=lambda _stage, _payload: {},
    )
    state: dict = {}
    first = planner._stage("provisional_scope", lambda: {"value": "first"}, resume=False, state=state)
    second = planner._stage("provisional_scope", lambda: {"value": "second"}, resume=True, state=state)
    return {"first": first, "second": second, "restored_old_without_cache_inputs": second == first}


def packet(chapter_units: list[dict]) -> dict:
    return {
        "chapter_id": "CH03",
        "research_question": "RQ",
        "chapter": {"chapter_id": "CH03", "title": "Chapter"},
        "chapter_plan": {"chapter_id": "CH03", "title": "Chapter", "thesis": "T", "units": chapter_units},
        "source_materials": [],
    }


def positional_ids() -> dict:
    root = OUT / "positional_ids"
    root.mkdir(parents=True, exist_ok=True)
    id_map = root / "ID_MAP.json"
    units = [
        {"unit_id": "SEM_A", "substantive_point": "A", "paragraph_briefs": [{"point": "PA"}]},
        {"unit_id": "SEM_B", "substantive_point": "B", "paragraph_briefs": [{"point": "PB"}]},
    ]
    first_path = root / "first.json"
    first_path.write_text(json.dumps(packet(units), ensure_ascii=False), encoding="utf-8")
    first = build_chapter_view(first_path, id_map_path=id_map)
    swapped_path = root / "swapped.json"
    swapped_path.write_text(json.dumps(packet(list(reversed(units))), ensure_ascii=False), encoding="utf-8")
    swapped = build_chapter_view(swapped_path, id_map_path=id_map)
    first_ids = {u.substantive_point: {"unit_id": u.unit_id, "paragraph_id": u.paragraph_briefs[0].paragraph_id} for u in first.units}
    swapped_ids = {u.substantive_point: {"unit_id": u.unit_id, "paragraph_id": u.paragraph_briefs[0].paragraph_id} for u in swapped.units}
    return {"first": first_ids, "swapped": swapped_ids, "semantic_ids_drifted_after_reorder": first_ids != swapped_ids}


def argument_cli() -> dict:
    root = OUT / "argument_cli"
    packet_root = root / "packet_root"
    (packet_root / "writer_packets").mkdir(parents=True, exist_ok=True)
    plan = {
        "shared_outline": [{"chapter_id": "CH03", "title": "Chapter", "purpose": "P"}],
        "shared_scope": {"statement": "SCOPE_STATEMENT"},
        "review_argument": "FINAL_CALIBRATED_ARGUMENT",
    }
    dump(packet_root / "DETAILED_REVIEW_PLAN.json", plan)
    dump(packet_root / "writer_packets" / "CH03.json", packet([{"unit_id": "SEM_A", "substantive_point": "A", "paragraph_briefs": []}]))
    output_root = root / "arrangement"
    cli = WORKTREE / "scripts" / "upgrade3" / "chapter_arrangement.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(WORKTREE) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(cli), "--packet-root", str(packet_root), "--output-root", str(output_root), "--chapter", "CH03"],
        cwd=str(WORKTREE), env=env, text=True, capture_output=True, check=True,
    )
    arrangement = json.loads((output_root / "CH03" / "ARRANGEMENT_INPUT.json").read_text(encoding="utf-8"))
    return {
        "returncode": completed.returncode,
        "plan_review_argument": plan["review_argument"],
        "plan_shared_scope_statement": plan["shared_scope"]["statement"],
        "arrangement_review_argument": arrangement.get("review_argument"),
        "cli_used_shared_scope_as_argument": arrangement.get("review_argument") == plan["shared_scope"]["statement"],
    }


class SyntheticCounter:
    def __call__(self, _encoding, messages):
        content = messages[-1]["content"]
        if "当前任务：chapter_details\n" in content:
            content = content.split("当前任务：chapter_details\n", 1)[1]
            content = content.split("\n\n【本轮交付】", 1)[0]
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            value = {}
        if isinstance(value, dict) and value.get("_weight") is not None:
            return int(value["_weight"])
        rows = value.get("source_materials") or [] if isinstance(value, dict) else []
        candidates = value.get("candidate_materials") or [] if isinstance(value, dict) else []
        return 100_000 + sum(int(row.get("_weight") or 0) for row in rows if isinstance(row, dict)) + sum(int(row.get("_weight") or 0) for row in candidates if isinstance(row, dict))


class EmptyMergePlanner:
    counter = SyntheticCounter()
    output_tokens = 16_000

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, stage, payload):
        self.calls.append((stage, copy.deepcopy(dict(payload))))
        if payload.get("chapter_detail_batches"):
            response = {"chapter_plan": {"thesis": "merge", "reader_objective": "merge", "units": []}}
        else:
            handles = [row["source_handle"] for row in payload.get("source_materials") or []]
            response = {"chapter_plan": {"thesis": "batch", "reader_objective": "batch", "source_handles": handles, "units": [{"substantive_point": "batch", "source_handles": handles}]}}
        return {"_planner_call": True, "response": response, "telemetry": {"finish_reason": "stop"}}


def empty_merge_cache() -> dict:
    root = OUT / "empty_merge"
    payload = {
        "topic_id": "capacity-test", "research_question": "RQ", "chapter": {"chapter_id": "CH01", "title": "Test"},
        "source_materials": [{"source_handle": f"P{i:04d}", "study_summary_A": {"x": "A"}, "_weight": 150_000} for i in range(6)],
        "candidate_materials": [{"source_handle": "P0001", "_weight": 5_000}, {"source_handle": "P9999", "_weight": 7_000}],
    }
    fake = EmptyMergePlanner()
    record, meta = planning._chapter_details_adaptive_record(fake, payload, chapter_id="CH01", cache_root=root / "batches", resume=False)
    merge_path = Path(meta["merge_cache_path"])
    cache = json.loads(merge_path.read_text(encoding="utf-8"))
    units = ((record.get("response") or {}).get("chapter_plan") or {}).get("units") or []
    return {"call_count": len(fake.calls), "batch_count": meta["batch_count"], "merged_units": len(units), "merge_cache_status": cache.get("status"), "empty_units_saved_complete": cache.get("status") == "complete" and not units}


def unmet_feedback_filter() -> dict:
    root = OUT / "feedback_filter"
    captured: list[dict] = []

    def fake(_stage, payload):
        captured.append(copy.deepcopy(dict(payload)))
        return {"_planner_call": True, "response": {"chapter_plan": {"thesis": "T", "reader_objective": "R", "units": []}}, "telemetry": {}}

    planner = planning.ProgressiveReviewPlanner(
        planning.ProgressivePlannerConfig(
            topic_id="feedback-filter", pool_path=root / "pool.jsonl", plan_path=root / "plan.json", output_dir=root,
        ),
        planner=fake,
    )
    unmet = {"status": "partial", "outcome": "unmet", "remaining_gap": "need X", "chapter_ids": ["CH01"]}
    planner._chapter_details(
        chapters=[{"chapter_id": "CH01", "title": "C1", "source_ids": []}], shared_outline=[], topic="RQ", candidates={},
        level1_tools={"supplement_results": [unmet]}, level2_tools={}, chapter_tools={}, resume=False, state={},
    )
    feedback = captured[0].get("relevant_tool_feedback") if captured else None
    return {"input_unmet_group": unmet, "captured_relevant_tool_feedback": feedback, "unmet_without_gap_or_judgment_filtered": feedback == []}


def late_material_and_inventory() -> dict:
    root = OUT / "late_material"
    captured: list[dict] = []

    def fake(stage, payload):
        captured.append({"stage": stage, "payload": copy.deepcopy(dict(payload))})
        return {"_planner_call": True, "response": {}, "telemetry": {}}

    planner = planning.ProgressiveReviewPlanner(
        planning.ProgressivePlannerConfig(
            topic_id="late-material", pool_path=root / "pool.jsonl", plan_path=root / "plan.json", output_dir=root,
        ),
        planner=fake,
    )
    current = {
        "chapter": {"chapter_id": "CH01", "title": "C1", "scope": "S"},
        "chapter_plan": {"units": []},
        "source_materials": [{"source_handle": "S1", "study_summary_A": {"finding": "new"}}],
    }
    baseline = {
        "chapter": {"chapter_id": "CH01", "title": "C1", "scope": "S"},
        "chapter_plan": {"units": []},
        "source_materials": [{"source_handle": "S1", "study_summary_A": {"finding": "old"}}],
    }
    planner._post_case_review(
        root=root, topic="T", harmonized={}, level1_outline={}, detail_records=[current], baseline_detail_records=[baseline],
        case_record={}, level1_tool_result={}, level2_tool_result={}, chapter_tool_result={}, editorial_feedback={},
        original_plan={}, material_theme_inventory=["initial-theme"], pool_rows=[], resume=False, state={},
    )
    whole = next(item["payload"] for item in captured if item["stage"] == "whole_plan_improvement")
    chapter = whole["chapters"][0]
    return {
        "whole_input_late_material_changes": whole.get("late_material_changes"),
        "chapter_late_material_changes": chapter.get("late_material_changes"),
        "whole_input_material_theme_inventory": whole.get("material_theme_inventory"),
        "changed_source_detected": bool(chapter.get("late_material_changes")),
        "inventory_is_forwarded_initial_value": whole.get("material_theme_inventory") == ["initial-theme"],
    }


def main() -> None:
    result = {"stage": stage_without_inputs(), "argument_cli": argument_cli(), "positional_ids": positional_ids(), "empty_merge": empty_merge_cache(), "feedback_filter": unmet_feedback_filter(), "late_material": late_material_and_inventory()}
    dump(OUT / "continuity_verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
