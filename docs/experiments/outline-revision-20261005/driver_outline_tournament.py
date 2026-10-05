# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
"""Small, resumable driver for the real two-unit outline tournament.

All imports come from the read-only consolidation source.  Only the output
directory and the already shared ledger are touched.  ``offline`` prepares and
checks the exact material projection; ``r1`` performs one paid strong-planner
call and then leaves the other arms untouched for human review.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


SOURCE_ROOT = Path(r"<LOCAL_SOURCE_BACKUP_PATH>")
OUT = Path(r"<LOCAL_REVIEW2_PATH>")
PLAN_PATH = Path(r"<LOCAL_REVIEW2_PATH>")
PACKET_PATH = Path(r"<LOCAL_REVIEW2_PATH>")
ARR_PATH = Path(r"<LOCAL_REVIEW2_PATH>")
BODY_PATH = Path(r"<LOCAL_REVIEW2_PATH>")
LEDGER_PATH = Path(r"<LOCAL_REVIEW2_PATH>")
KEY_PATH = Path(r"<LOCAL_REVIEW2_PATH>")

R1_MODEL = "qwen3.8-max"
R1_OUTPUT_TOKENS = 12_288
R1_THINKING_BUDGET = 4_096


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_imports() -> tuple[Any, Any, Any]:
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.module4.runtime import (
        GlobalBudgetLedger, QwenDirectClient, estimated_cost_cny, invoke_client,
    )
    return arranging, planning, (GlobalBudgetLedger, QwenDirectClient, estimated_cost_cny, invoke_client)


def plan_chapter(plan: Mapping[str, Any]) -> dict[str, Any]:
    for chapter in plan.get("chapters") or ():
        if isinstance(chapter, Mapping) and str(chapter.get("chapter", {}).get("chapter_id") or chapter.get("chapter_id") or "") == "Ch1":
            return dict(chapter)
    raise RuntimeError("Ch1_missing_in_final_plan")


def cluster_material() -> tuple[list[str], list[str]]:
    """Return stable U1/U2 ids and their exact 30-handle source projection."""
    plan = load(PLAN_PATH)
    chapter = plan_chapter(plan)
    units = chapter["chapter_plan"]["units"]
    if len(units) < 2:
        raise RuntimeError("Ch1_has_fewer_than_two_units")
    selected = units[:2]
    unit_ids = [str(item.get("unit_id") or item.get("id") or "") for item in selected]
    if any(not item for item in unit_ids):
        raise RuntimeError("selected_unit_without_stable_id")
    arrangement = load(ARR_PATH)
    # The packet used by the prior BODY writer round has legacy unit IDs.  Do
    # not match them by position: the final plan is authoritative, so match
    # the old arrangement to it by the exact substantive point and record that
    # bridge in the manifest.
    by_point = {str(item.get("substantive_point") or item.get("focus") or "").strip(): item for item in arrangement.get("units") or ()}
    handles: list[str] = []
    for unit in selected:
        row = by_point.get(str(unit.get("substantive_point") or unit.get("focus") or "").strip())
        if not row:
            raise RuntimeError("arrangement_unit_substantive_point_missing:" + unit_ids[len(handles)])
        for task in row.get("paragraph_tasks") or ():
            for use in task.get("source_uses") or ():
                handle = str(use.get("source_handle") or "")
                if handle and handle not in handles:
                    handles.append(handle)
        for task in row.get("table_tasks") or ():
            for use in task.get("source_uses") or ():
                handle = str(use.get("source_handle") or "")
                if handle and handle not in handles:
                    handles.append(handle)
    return unit_ids, handles


def bridge_old_arrangement(data: Mapping[str, Any], view: Any) -> dict[str, Any]:
    """Carry only old source uses into final-plan identities by substantive point."""
    final_units = {str(unit.unit_id): unit for unit in view.units}
    old_rows = list(data["legacy_arrangement"].get("units") or ())
    old_by_focus = {str(row.get("focus") or "").strip(): row for row in old_rows}
    output: dict[str, Any] = {
        "schema_version": data["arrangement"].get("schema_version"),
        "chapter_id": "Ch1", "chapter_argument": data["arrangement"].get("chapter_argument") or "",
        "unit_id_remap": {}, "units": [], "unused_sources": [], "issues": [],
    }
    for unit_id, final_unit in final_units.items():
        old = old_by_focus.get(str(final_unit.substantive_point or "").strip())
        if old is None:
            raise RuntimeError("old_arrangement_bridge_missing_by_substantive_point:" + unit_id)
        new_unit = {"unit_id": unit_id, "focus": str(final_unit.substantive_point or ""), "unit_notes": str(old.get("unit_notes") or ""), "paragraph_tasks": [], "table_tasks": []}
        old_tasks = list(old.get("paragraph_tasks") or ())
        for ordinal, brief in enumerate(final_unit.paragraph_briefs, start=1):
            exact = [row for row in old_tasks if {str(use.get("source_handle")) for use in row.get("source_uses") or ()} == set(brief.source_handles)]
            candidate = exact[0] if exact else (old_tasks[ordinal - 1] if ordinal <= len(old_tasks) else None)
            if candidate is None:
                raise RuntimeError("old_arrangement_paragraph_bridge_missing:" + unit_id + ":" + brief.paragraph_id)
            task = copy.deepcopy(candidate)
            task["paragraph_id"] = brief.paragraph_id
            task["point"] = brief.point
            task["development"] = brief.development
            task["source_briefs"] = [brief.paragraph_id]
            new_unit["paragraph_tasks"].append(task)
        # This cluster has no selected tables; preserving an accidental old
        # table would import a legacy task identity into the final plan.
        output["units"].append(new_unit)
    return output


def make_subset_inputs() -> dict[str, Any]:
    plan = load(PLAN_PATH)
    packet = load(PACKET_PATH)
    arrangement = load(ARR_PATH)
    chapter = plan_chapter(plan)
    unit_ids, handles = cluster_material()
    handle_set = set(handles)

    subset_plan = copy.deepcopy(chapter["chapter_plan"])
    subset_plan["units"] = [copy.deepcopy(unit) for unit in subset_plan.get("units") or () if str(unit.get("unit_id") or unit.get("id")) in unit_ids]
    # Supporting-study rows are part of the final PLAN, but a few of them have
    # no complete material in the selected real packet.  Keep every available
    # generated-content row and remove only unavailable case references so the
    # arrangement view cannot silently add out-of-cluster identities.
    for unit in subset_plan["units"]:
        unit["supporting_studies"] = [
            row for row in unit.get("supporting_studies") or ()
            if not isinstance(row, Mapping) or str(row.get("source_handle") or "") in handle_set
        ]
    subset_packet = copy.deepcopy(packet)
    subset_packet["chapter_plan"] = subset_plan
    subset_packet["source_materials"] = [
        row for row in packet.get("source_materials") or ()
        if isinstance(row, Mapping) and str(row.get("source_handle") or "") in handle_set
    ]
    subset_packet["source_identity_map"] = {
        key: value for key, value in (packet.get("source_identity_map") or {}).items() if str(key) in handle_set
    }
    for field in ("candidate_navigation", "candidate_materials", "tool_materials"):
        value = packet.get(field)
        if isinstance(value, list):
            subset_packet[field] = [
                row for row in value
                if not isinstance(row, Mapping)
                or not row.get("source_handle")
                or str(row.get("source_handle")) in handle_set
            ]
    subset_packet["chapter"] = copy.deepcopy(packet.get("chapter") or {})
    subset_packet["chapter"]["source_handles"] = [h for h in subset_packet["chapter"].get("source_handles") or () if str(h) in handle_set]
    selected_paper_ids = {
        str(row.get("paper_id") or "") for row in subset_packet["source_materials"]
        if isinstance(row, Mapping) and row.get("paper_id")
    }
    subset_packet["chapter"]["source_ids"] = [
        row for row in subset_packet["chapter"].get("source_ids") or ()
        if (isinstance(row, Mapping) and str(row.get("source_handle") or row.get("handle") or "") in handle_set)
        or (not isinstance(row, Mapping) and str(row) in selected_paper_ids)
    ]

    subset_arrangement = copy.deepcopy(arrangement)
    # Keep old arrangement only as a source-use scaffold.  The final plan's
    # stable units and paragraph briefs are inserted by bridge_old_arrangement
    # after ChapterView creates their canonical IDs.
    subset_arrangement["units"] = []
    subset_arrangement["source_catalog"] = {
        key: copy.deepcopy(value) for key, value in (arrangement.get("source_catalog") or {}).items()
        if str(key) in handle_set
    }
    subset_arrangement["unused_sources"] = [
        row for row in arrangement.get("unused_sources") or ()
        if str(row.get("source_handle") or "") in handle_set
    ]
    subset_arrangement["chapter_tool_materials"] = [
        row for row in arrangement.get("chapter_tool_materials") or ()
        if not isinstance(row, Mapping) or not row.get("source_handles")
        or any(str(handle) in handle_set for handle in row.get("source_handles") or ())
    ]

    root = OUT / "inputs"
    dump(root / "base_subset_plan.json", subset_plan)
    dump(root / "base_subset_packet.json", subset_packet)
    dump(root / "base_subset_arrangement.json", subset_arrangement)
    dump(root / "cluster_manifest.json", {
        "chapter_id": "Ch1",
        "unit_ids": unit_ids,
        "source_handles": handles,
        "source_handle_count": len(handles),
        "source_material_count": len(subset_packet["source_materials"]),
        "direct_handles": ["P0096", "P0146", "P0585", "P0478", "P0081", "P0402"],
        "body_path": str(BODY_PATH),
        "source_root": str(SOURCE_ROOT),
        "source_head": "43c1c940f85d92fe414590db1820d73d25a96084",
    })
    dump(root / "body_reference.json", {"path": str(BODY_PATH), "sha256": hashlib.sha256(BODY_PATH.read_bytes()).hexdigest()})
    return {"plan": plan, "packet": subset_packet, "chapter": chapter, "arrangement": subset_arrangement, "legacy_arrangement": arrangement, "unit_ids": unit_ids, "handles": handles}


def filtered_control(arranging: Any, data: Mapping[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    # Tool-return rows can carry paper identities outside this deliberately
    # bounded 29-row material projection.  They remain in the R1 payload, but
    # the offline seam uses the same source-material contract without letting
    # unresolved tool identities expand the selected source set.
    view_packet = copy.deepcopy(data["packet"])
    view_packet["tool_materials"] = []
    view_packet["chapter_tool_materials"] = []
    for row in view_packet.get("source_materials") or ():
        if isinstance(row, dict):
            # Nested tool returns can introduce unresolved identities during
            # view construction.  Keep study_summary_A/review_planning_B and
            # all ordinary generated material; selected tool returns remain in
            # the production packet for the writer's bounded source lookup.
            row["tool_materials"] = []
            row["tool_supplement_materials"] = []
    packet_path = OUT / "inputs" / "view_subset_packet.json"
    dump(packet_path, view_packet)
    view = arranging.build_chapter_view(
        packet_path,
        shared_outline=data["packet"].get("shared_outline") or (),
        review_argument=str(data["packet"].get("review_argument") or ""),
        shared_scope=data["packet"].get("shared_scope") or {},
        review_argument_status=str(data["packet"].get("review_argument_status") or ""),
        review_argument_source=str(data["packet"].get("review_argument_source") or ""),
        fallback_source_identity_map=data["packet"].get("source_identity_map") or {},
        id_map_path=OUT / "inputs" / "ID_MAP.json",
    )
    arranging.write_view(view, OUT / "inputs" / "ARRANGEMENT_INPUT.json")
    # The control is the unchanged final PLAN copy.  It is intentionally not
    # validated against the legacy arrangement: all three arms will receive a
    # fresh production arrangement from this same ChapterView.
    control = {
        "schema_version": "optomind.outline_revision_real_tournament.control.v1",
        "chapter_id": "Ch1",
        "chapter_argument": str(data["packet"].get("chapter_plan", {}).get("thesis") or ""),
        "units": copy.deepcopy(data["packet"].get("chapter_plan", {}).get("units") or []),
        "source_handles": list(data["handles"]),
        "validation": {"status": "original_plan_pending_fresh_arrangement", "legacy_arrangement_used": False},
    }
    dump(OUT / "inputs" / "control_original_plan.json", control)
    validation = {"status": "view_built_no_legacy_arrangement_validation", "sources": len(view.sources), "units": len(view.units)}
    return view, control, validation


def offline() -> dict[str, Any]:
    arranging, planning, _ = source_imports()
    data = make_subset_inputs()
    view, control, validation = filtered_control(arranging, data)
    messages = planning._messages_for("chapter_details", {
        "planning_revision_mode": True,
        "chapter": data["packet"].get("chapter"),
        "chapter_plan": data["packet"].get("chapter_plan"),
        "source_materials": data["packet"].get("source_materials"),
        "source_identity_map": data["packet"].get("source_identity_map"),
        "revision_request": "保留两个真实单元的稳定ID；允许修改职责、逐段brief、案例用途和衔接，但不得生成综述正文或题目特例答案。",
        "task_cluster": {"unit_ids": data["unit_ids"], "source_handles": data["handles"]},
    })
    projected_chars = sum(len(str(row)) for row in data["packet"].get("source_materials") or ())
    dump(OUT / "offline_seam.json", {
        "status": "passed",
        "unit_ids": data["unit_ids"],
        "source_handle_count": len(data["handles"]),
        "source_material_count": len(data["packet"].get("source_materials") or ()),
        "paragraph_brief_count": sum(len(unit.get("paragraph_briefs") or ()) for unit in data["packet"].get("chapter_plan", {}).get("units") or ()),
        "projected_material_chars": projected_chars,
        "control_units": len(control.get("units") or ()),
        "control_validation": validation,
        "r1_message_chars": sum(len(str(item.get("content") or "")) for item in messages),
        "same_material_fingerprint": hashlib.sha256(json.dumps(data["packet"].get("source_materials"), ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "paid_calls": 0,
    })
    print(json.dumps({"status": "passed", "unit_ids": data["unit_ids"], "handles": len(data["handles"]), "materials": len(data["packet"].get("source_materials") or ()), "projected_material_chars": projected_chars}, ensure_ascii=False))
    return data


def r1_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    packet = data["packet"]
    chapter = copy.deepcopy(packet.get("chapter") or {})
    chapter["source_handles"] = list(data["handles"])
    return {
        "planning_revision_mode": True,
        "topic_id": packet.get("topic_id"),
        "research_question": packet.get("research_question"),
        "review_argument": packet.get("review_argument"),
        "review_argument_status": packet.get("review_argument_status"),
        "shared_scope": packet.get("shared_scope"),
        "shared_outline": packet.get("shared_outline"),
        "chapter": chapter,
        "chapter_plan": packet.get("chapter_plan"),
        "source_materials": packet.get("source_materials"),
        "source_identity_map": packet.get("source_identity_map"),
        "candidate_navigation": packet.get("candidate_navigation"),
        "tool_materials": packet.get("tool_materials"),
        "task_cluster": {
            "unit_ids": data["unit_ids"],
            "source_handles": data["handles"],
            "direct_paragraph_handles": ["P0096", "P0146", "P0585", "P0478", "P0081", "P0402"],
            "constraint": "只在这个两单元局部执行修订；保持稳定unit_id，允许改职责/brief/案例安排/衔接，不能写正文或题目答案。",
        },
        "revision_request": "以现有两单元为控制基线做一次较强模型细纲修订。检查共享研究在两个不同职责中的边界，必要时重排或拆合段落任务；所有科学数字、研究对象、条件和限制只能来自给定材料。返回完整 chapter_plan JSON，不返回论文库存或综述正文。",
    }


def make_r1_client(runtime_parts: tuple[Any, Any, Any, Any], raw_dir: Path) -> tuple[Any, Any]:
    GlobalBudgetLedger, QwenDirectClient, _, _ = runtime_parts
    ledger = GlobalBudgetLedger(path=LEDGER_PATH, limit_cny=10.0)
    if ledger.reserved_cny + ledger.actual_cny >= 10.0:
        raise RuntimeError("shared_budget_unavailable_before_r1")
    from optomind_research.runtime.upgrade3.progressive_review_plan import (
        DEFAULT_TOKENIZER_PATH, TOKEN_FRAMING_MARGIN, TOKEN_MARGIN_MULTIPLIER, qwen_local_token_counter,
    )
    counter = qwen_local_token_counter(DEFAULT_TOKENIZER_PATH)
    client = QwenDirectClient(
        model=R1_MODEL, key_file=KEY_PATH, max_retries=0, max_keys=1,
        timeout_seconds=900.0, max_output_tokens=R1_OUTPUT_TOKENS,
        thinking=True, thinking_budget=R1_THINKING_BUDGET, json_mode=False,
        raw_response_dir=raw_dir, budget_ledger=ledger,
        prompt_token_counter=counter, prompt_token_multiplier=TOKEN_MARGIN_MULTIPLIER,
        prompt_token_framing_margin=TOKEN_FRAMING_MARGIN,
    )
    return client, ledger


def r1() -> dict[str, Any]:
    arranging, planning, runtime_parts = source_imports()
    data = make_subset_inputs()
    view, control, _ = filtered_control(arranging, data)
    payload = r1_payload(data)
    messages = planning._messages_for("chapter_details", payload)
    messages[-1]["content"] += (
        "\n\n【额外返回合同】除了 chapter_plan，还返回 diagnosis（本簇的结构性诊断，引用给定source_handle）和 changes "
        "（逐项说明职责/brief/案例用途/衔接的变化及依据）。不要把诊断写成题目答案，也不要将 reviewer 金标准暗示给写作者。"
    )
    call_dir = OUT / "calls" / "R1_strong_once"
    call_dir.mkdir(parents=True, exist_ok=True)
    dump(call_dir / "request_messages.json", messages)
    dump(call_dir / "request_payload.json", payload)
    raw_dir = call_dir / "raw_responses"
    client, ledger = make_r1_client(runtime_parts, raw_dir)
    before = ledger.as_dict()
    dump(call_dir / "ledger_before.json", before)
    call_id = "outline-tournament:R1:Ch1-U1-U2:" + uuid.uuid4().hex[:12]
    _, _, _, invoke_client = runtime_parts
    raw = invoke_client(client, messages, model=R1_MODEL, call_id=call_id,
                        max_output_tokens=R1_OUTPUT_TOKENS, thinking_budget=R1_THINKING_BUDGET)
    after = ledger.as_dict()
    dump(call_dir / "response_envelope.json", raw)
    dump(call_dir / "ledger_after.json", after)
    parsed, telemetry = planning._parse_planner_response(raw)
    dump(call_dir / "response_parsed.json", parsed)
    chapter_plan = parsed.get("chapter_plan") if isinstance(parsed, Mapping) else None
    if not isinstance(chapter_plan, Mapping):
        raise RuntimeError("r1_response_missing_chapter_plan")
    candidate = copy.deepcopy(dict(chapter_plan))
    candidate_units = candidate.get("units") or []
    if len(candidate_units) != len(data["unit_ids"]):
        raise RuntimeError("r1_candidate_unit_count_mismatch")
    missing_ids: list[int] = []
    for index, unit in enumerate(candidate_units):
        if not isinstance(unit, Mapping):
            raise RuntimeError("r1_candidate_unit_not_object")
        if not str(unit.get("unit_id") or unit.get("id") or "").strip():
            missing_ids.append(index)
            continue
        if str(unit.get("unit_id") or unit.get("id")) not in data["unit_ids"]:
            raise RuntimeError("r1_candidate_unknown_unit_id")
    if missing_ids:
        dump(call_dir / "candidate_contract_failure.json", {
            "error": "r1_candidate_missing_stable_unit_id",
            "indices": missing_ids,
            "expected_unit_ids": data["unit_ids"],
        })
        raise RuntimeError("r1_candidate_missing_stable_unit_id:" + ",".join(map(str, missing_ids)))
    candidate["units"] = candidate_units
    dump(OUT / "candidates" / "R1_candidate_plan.json", candidate)
    original_text = json.dumps(data["packet"].get("chapter_plan"), ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True)
    candidate_text = json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True).splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(original_text, candidate_text, fromfile="original_subset_plan.json", tofile="R1_candidate_plan.json"))
    (OUT / "candidates" / "R1_vs_original.diff").write_text(diff, encoding="utf-8")
    dump(OUT / "R1_RESULT.json", {
        "status": "complete_stop_for_review",
        "call_id": call_id,
        "model": R1_MODEL,
        "thinking": True,
        "thinking_budget": R1_THINKING_BUDGET,
        "visible_output_tokens": R1_OUTPUT_TOKENS,
        "unit_ids": data["unit_ids"],
        "source_handles": data["handles"],
        "source_material_count": len(data["packet"].get("source_materials") or ()),
        "response_telemetry": telemetry,
        "raw_response_sha256": raw.get("raw_response_sha256"),
        "ledger_before": before,
        "ledger_after": after,
        "candidate_path": str(OUT / "candidates" / "R1_candidate_plan.json"),
        "diff_path": str(OUT / "candidates" / "R1_vs_original.diff"),
        "control_arrangement_path": str(OUT / "inputs" / "control_arrangement.json"),
        "message_path": str(call_dir / "request_messages.json"),
    })
    print(json.dumps({"status": "complete_stop_for_review", "call_id": call_id, "model": R1_MODEL, "usage": raw.get("usage"), "candidate": str(OUT / "candidates" / "R1_candidate_plan.json")}, ensure_ascii=False))
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["offline", "r1"])
    args = parser.parse_args()
    if args.stage == "offline":
        offline()
    else:
        r1()


if __name__ == "__main__":
    main()
