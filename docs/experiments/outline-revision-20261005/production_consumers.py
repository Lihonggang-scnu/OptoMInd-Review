# PUBLIC ARCHIVE COPY: local paths and credentials are intentionally absent.
"""Production consumers for the three-arm two-unit outline tournament.

This file deliberately starts after planning.  It imports only the current
consolidation source and writes every packet, request, response and export
under the new experiment directory.  ``prepare`` makes all inputs and exact
token/cost estimates; ``arrange`` and ``write`` are paid stages and are
idempotent when their saved request hash matches.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

SOURCE_ROOT = Path(r"<LOCAL_SOURCE_BACKUP_PATH>")
OUT = Path(r"<LOCAL_REVIEW2_PATH>")
LEDGER = Path(r"<LOCAL_REVIEW2_PATH>")
KEY_FILE = Path(r"<LOCAL_REVIEW2_PATH>")
TOKENIZER = Path(r"<LOCAL_REVIEW2_PATH>")
BASE_PACKET = OUT / "inputs" / "base_subset_packet.json"
BASE_PLAN = OUT / "inputs" / "base_subset_plan.json"
R1_PLAN = OUT / "candidates" / "R1_candidate_plan.json"
R2_PLAN = OUT / "candidates" / "R2_candidate_plan.json"

ARR_MODEL = "qwen3.5-plus"
ARR_OUTPUT = 5_000
ARR_THINKING = 1_024
WRITER_MODEL = "qwen3.7-flash"
WRITER_OUTPUT = 4_000
WRITER_THINKING = 0


def _imports() -> tuple[Any, Any, Any, Any]:
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
    from optomind_research.runtime.upgrade3 import review_unit_writer as writing
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.module4.runtime import (
        GlobalBudgetLedger, QwenDirectClient, invoke_client,
    )
    return arranging, writing, planning, (GlobalBudgetLedger, QwenDirectClient, invoke_client)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _request_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _saved_writer_reuse(unit_dir: Path, request_sha: str) -> dict[str, Any] | None:
    """Return a saved writer result only when its prior request is complete and identical.

    Read the previous request before the current request is written.  This small
    guard prevents a changed input from looking identical merely because the
    driver overwrote ``WRITER_REQUEST.json`` first.
    """
    result_path = unit_dir / "UNIT_RESULT.json"
    request_path = unit_dir / "WRITER_REQUEST.json"
    if not result_path.is_file() or not request_path.is_file():
        return None
    prior_result = _load(result_path)
    prior_request = _load(request_path)
    body_path = Path(str(prior_result.get("body_path") or ""))
    if (
        prior_result.get("mode") == "run"
        and prior_result.get("complete") is True
        and prior_request.get("request_sha256") == request_sha
        and body_path.is_file()
    ):
        return prior_result
    return None


def _ledger_snapshot(GlobalBudgetLedger: Any) -> dict[str, Any]:
    return GlobalBudgetLedger(path=LEDGER, limit_cny=10.0).as_dict()


def _arm_plan_paths() -> dict[str, Path]:
    return {"control_original": BASE_PLAN, "R1_candidate": R1_PLAN, "R2_candidate": R2_PLAN}


def _clean_view_packet(packet: Mapping[str, Any], plan: Mapping[str, Any], path: Path) -> None:
    """Build the arrangement view packet without identity-expanding tool rows."""
    view_packet = copy.deepcopy(dict(packet))
    view_packet["chapter_plan"] = copy.deepcopy(dict(plan))
    view_packet["tool_materials"] = []
    view_packet["chapter_tool_materials"] = []
    for row in view_packet.get("source_materials") or ():
        if isinstance(row, dict):
            row["tool_materials"] = []
            row["tool_supplement_materials"] = []
    _dump(path, view_packet)


def _make_arm_packet(arm: str, plan_path: Path, packet: Mapping[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    arm_dir = OUT / "production" / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    plan = _load(plan_path)
    if not isinstance(plan, Mapping) or not isinstance(plan.get("units"), list):
        raise RuntimeError(f"{arm}:candidate_plan_without_units:{plan_path}")
    arm_packet = copy.deepcopy(dict(packet))
    arm_packet["chapter_plan"] = copy.deepcopy(dict(plan))
    packet_path = arm_dir / "ARM_WRITER_PACKET.json"
    view_path = arm_dir / "VIEW_PACKET.json"
    _dump(packet_path, arm_packet)
    _clean_view_packet(arm_packet, plan, view_path)
    return packet_path, view_path, dict(plan)


def _build_view(arranging: Any, packet: Mapping[str, Any], view_path: Path, arm_dir: Path) -> Any:
    return arranging.build_chapter_view(
        view_path,
        shared_outline=packet.get("shared_outline") or (),
        review_argument=str(packet.get("review_argument") or ""),
        shared_scope=packet.get("shared_scope") or {},
        review_argument_status=str(packet.get("review_argument_status") or ""),
        review_argument_source=str(packet.get("review_argument_source") or ""),
        fallback_source_identity_map=packet.get("source_identity_map") or {},
        id_map_path=arm_dir / "ID_MAP.json",
    )


def _material_inventory(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    # Same full 29-row A/B material inventory is visible to every arrangement
    # arm; no support row is synthesized into a candidate plan.
    return [copy.deepcopy(dict(row)) for row in packet.get("source_materials") or () if isinstance(row, Mapping)]


def prepare() -> dict[str, Any]:
    arranging, writing, planning, runtime = _imports()
    packet = _load(BASE_PACKET)
    if not isinstance(packet, Mapping):
        raise RuntimeError("base_subset_packet_not_object")
    arm_records: dict[str, Any] = {}
    counter = planning.qwen_local_token_counter(TOKENIZER)
    for arm, plan_path in _arm_plan_paths().items():
        if not plan_path.is_file():
            arm_records[arm] = {"status": "waiting_for_candidate", "plan_path": str(plan_path)}
            continue
        packet_path, view_path, plan = _make_arm_packet(arm, plan_path, packet)
        arm_dir = packet_path.parent
        view = _build_view(arranging, packet, view_path, arm_dir)
        arranging.write_view(view, arm_dir / "ARRANGEMENT_INPUT.json")
        payload = view.arrangement_payload(max_source_chars=1200)
        payload["planning_revision_mode"] = True
        payload["available_material_inventory"] = _material_inventory(packet)
        messages = arranging.arrangement_messages(
            payload, prompt=arranging.load_editor_prompt(planning_revision=True), planning_revision=True,
        )
        estimate = arranging.estimate_arrangement_cost(
            messages, model=ARR_MODEL, output_tokens=ARR_OUTPUT, thinking_budget=ARR_THINKING,
        )
        _dump(arm_dir / "ARRANGEMENT_REQUEST.json", {
            "arm": arm, "model": ARR_MODEL, "output_tokens": ARR_OUTPUT,
            "thinking_budget": ARR_THINKING, "request_sha256": _request_hash(messages),
            "messages": messages, "estimate": estimate,
        })
        unit_ids = [str(row.get("unit_id") or "") for row in plan.get("units") or () if isinstance(row, Mapping)]
        arm_records[arm] = {
            "status": "prepared", "plan_path": str(plan_path), "packet_path": str(packet_path),
            "view_packet_path": str(view_path), "view_path": str(arm_dir / "ARRANGEMENT_INPUT.json"),
            "unit_ids": unit_ids, "view_units": [str(row.unit_id) for row in view.units],
            "view_sources": len(view.sources), "material_rows": len(packet.get("source_materials") or ()),
            "arrangement_estimate": estimate,
        }
    total_arrangement = sum(float(row.get("arrangement_estimate", {}).get("estimated_cost_cny") or 0.0) for row in arm_records.values())
    _dump(OUT / "production" / "PREPARE_REPORT.json", {
        "status": "prepared_no_paid_calls", "source_head": "43c1c940f85d92fe414590db1820d73d25a96084",
        "ledger": _ledger_snapshot(runtime[0]), "arms": arm_records,
        "total_arrangement_estimate_cny": total_arrangement,
        "writer_params": {"model": WRITER_MODEL, "output_tokens": WRITER_OUTPUT, "thinking_budget": WRITER_THINKING},
    })
    print(json.dumps({"status": "prepared_no_paid_calls", "total_arrangement_estimate_cny": total_arrangement, "arms": arm_records}, ensure_ascii=False))
    return arm_records


def _client(runtime: tuple[Any, Any, Any], planning: Any, model: str, output: int, thinking: int, raw_dir: Path) -> tuple[Any, Any]:
    GlobalBudgetLedger, QwenDirectClient, _invoke = runtime
    ledger = GlobalBudgetLedger(path=LEDGER, limit_cny=10.0)
    counter = planning.qwen_local_token_counter(TOKENIZER)
    client = QwenDirectClient(
        model=model, key_file=KEY_FILE, max_retries=0, max_keys=1, timeout_seconds=900,
        max_output_tokens=output, thinking=bool(thinking), thinking_budget=thinking,
        json_mode=False, raw_response_dir=raw_dir, budget_ledger=ledger,
        prompt_token_counter=counter, prompt_token_multiplier=planning.TOKEN_MARGIN_MULTIPLIER,
        prompt_token_framing_margin=planning.TOKEN_FRAMING_MARGIN,
    )
    return client, ledger


def arrange() -> dict[str, Any]:
    arranging, writing, planning, runtime = _imports()
    packet = _load(BASE_PACKET)
    report = _load(OUT / "production" / "PREPARE_REPORT.json") if (OUT / "production" / "PREPARE_REPORT.json").is_file() else prepare()
    results: dict[str, Any] = {}
    for arm, record in report.get("arms", {}).items():
        if record.get("status") != "prepared":
            results[arm] = record
            continue
        arm_dir = Path(record["packet_path"]).parent
        req = _load(arm_dir / "ARRANGEMENT_REQUEST.json")
        messages = req["messages"]
        output_path = arm_dir / "ARRANGEMENT_RESULT.json"
        if output_path.is_file():
            old = _load(output_path)
            if old.get("request_sha256") == req.get("request_sha256"):
                results[arm] = {"status": "reused_saved_arrangement", "path": str(output_path)}
                continue
        view = _build_view(arranging, packet, Path(record["view_packet_path"]), arm_dir)
        client, ledger = _client(runtime, planning, ARR_MODEL, ARR_OUTPUT, ARR_THINKING, arm_dir / "raw_responses" / "arrangement")
        before = ledger.as_dict()
        _dump(arm_dir / "ledger_before_arrangement.json", before)
        call_id = f"outline-tournament:consumer-arrangement:{arm}:Ch1:{uuid.uuid4().hex[:10]}"
        raw = runtime[2](client, messages, model=ARR_MODEL, call_id=call_id)
        parsed = arranging.parse_arrangement_response(raw)
        validated = arranging.validate_arrangement(parsed, view, planning_revision=True)
        exported = dict(validated)
        exported["source_catalog"] = arranging.build_source_catalog(view, validated)
        for entry in exported["source_catalog"].values():
            entry.setdefault("locator", {})["writer_packet"] = record["packet_path"]
        exported["chapter_tool_materials"] = []
        exported["provenance"] = {"model_call": call_id, "model": ARR_MODEL, "arm": arm, "same_material_inventory": True}
        after = ledger.as_dict()
        _dump(arm_dir / "ARRANGEMENT_RESPONSE_ENVELOPE.json", raw)
        _dump(arm_dir / "ARRANGEMENT_PARSED.json", parsed)
        _dump(arm_dir / "ARRANGEMENT_VALIDATION.json", validated)
        _dump(arm_dir / "ledger_after_arrangement.json", after)
        _dump(output_path, {"request_sha256": req["request_sha256"], "call_id": call_id, "usage": raw.get("usage"), "arrangement": exported})
        _dump(arm_dir / "ARRANGEMENT_FOR_WRITER.json", exported)
        results[arm] = {"status": "arranged", "call_id": call_id, "validation": validated.get("validation"), "usage": raw.get("usage"), "path": str(output_path)}
        # Stop on a contract failure; never launch writers against invalid tasks.
        if not validated.get("validation", {}).get("ok"):
            raise RuntimeError(f"{arm}:arrangement_validation_failed:{json.dumps(validated.get('validation'), ensure_ascii=False)}")
    _dump(OUT / "production" / "ARRANGEMENT_REPORT.json", {"status": "complete", "ledger": _ledger_snapshot(runtime[0]), "arms": results})
    print(json.dumps(results, ensure_ascii=False))
    return results


def write() -> dict[str, Any]:
    arranging, writing, planning, runtime = _imports()
    report = _load(OUT / "production" / "ARRANGEMENT_REPORT.json")
    packet = _load(BASE_PACKET)
    results: dict[str, Any] = {}
    counter = planning.qwen_local_token_counter(TOKENIZER)
    for arm, row in report.get("arms", {}).items():
        if row.get("status") not in {"arranged", "reused_saved_arrangement"}:
            results[arm] = {"status": "not_arranged"}
            continue
        arm_dir = OUT / "production" / arm
        arrangement_path = arm_dir / "ARRANGEMENT_FOR_WRITER.json"
        if not arrangement_path.is_file():
            results[arm] = {"status": "arrangement_file_missing"}
            continue
        arrangement = _load(arrangement_path)
        unit_results: dict[str, Any] = {}
        for unit in arrangement.get("units") or ():
            unit_id = str(unit.get("unit_id") or "")
            if not unit_id:
                continue
            unit_dir = arm_dir / "writer" / unit_id
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit_view = writing.build_unit_view(
                arrangement_path, unit_id, view_path=arm_dir / "ARRANGEMENT_INPUT.json",
                max_material_chars_per_source=writing.DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE,
            )
            payload = writing.unit_payload(unit_view, language="zh")
            payload["planning_revision_mode"] = True
            messages = writing.unit_messages(unit_view, prompt=writing.load_writer_prompt(planning_revision=True), language="zh", payload=payload, planning_revision=True)
            estimate = writing.estimate_unit_cost(messages, model=WRITER_MODEL, output_tokens=WRITER_OUTPUT, thinking_budget=WRITER_THINKING, token_counter=counter)
            request_sha = _request_hash(messages)
            prior_result = _saved_writer_reuse(unit_dir, request_sha)
            request_record = {"model": WRITER_MODEL, "output_tokens": WRITER_OUTPUT, "thinking_budget": WRITER_THINKING, "request_sha256": request_sha, "messages": messages, "estimate": estimate}
            _dump(unit_dir / "WRITER_REQUEST.json", request_record)
            if prior_result is not None:
                unit_results[unit_id] = {"status": "reused_saved_writer", "body_path": prior_result.get("body_path"), "usage": prior_result.get("usage"), "completion_status": prior_result.get("completion_status"), "request_sha256": request_sha}
                continue
            client, ledger = _client(runtime, planning, WRITER_MODEL, WRITER_OUTPUT, WRITER_THINKING, unit_dir / "raw_responses")
            before = ledger.as_dict()
            _dump(unit_dir / "ledger_before.json", before)
            raw = writing.run_unit_writing(unit_view, client=client, model=WRITER_MODEL, prompt=writing.load_writer_prompt(planning_revision=True), language="zh", payload=payload, raw_response_dir=unit_dir / "raw_responses", planning_revision=True)
            after = ledger.as_dict()
            written = writing.write_unit_output(unit_view, raw["body_markdown"], unit_dir, model=WRITER_MODEL, language="zh", mode="run", used_messages=raw.get("messages") or messages, estimate=estimate, usage=raw.get("usage") or {}, response_path=raw.get("raw_response") or "", finish_reason=raw.get("finish_reason") or "", complete=raw.get("complete", True), partial_error=raw.get("partial_error") or "")
            _dump(unit_dir / "ledger_after.json", after)
            unit_results[unit_id] = {"status": "written", "body_path": written.get("body_path") if isinstance(written, Mapping) else str(unit_dir / "UNIT_BODY.md"), "usage": raw.get("usage"), "estimate": estimate, "completion_status": written.get("completion_status") if isinstance(written, Mapping) else None, "output_diagnostics": written.get("output_diagnostics") if isinstance(written, Mapping) else None, "request_sha256": request_sha}
        results[arm] = unit_results
    _dump(OUT / "production" / "WRITER_REPORT.json", {"status": "complete", "ledger": _ledger_snapshot(runtime[0]), "arms": results})
    print(json.dumps(results, ensure_ascii=False))
    return results


def aligned_prepare() -> dict[str, Any]:
    """Prepare only the fourth aligned-owner arm; never touch prior reports."""
    arranging, writing, planning, runtime = _imports()
    packet = _load(BASE_PACKET)
    plan_path = OUT / "candidates" / "R1_aligned_owner_plan.json"
    if not plan_path.is_file():
        raise RuntimeError("aligned_owner_candidate_missing")
    arm = "R1_aligned_owner"
    packet_path, view_path, plan = _make_arm_packet(arm, plan_path, packet)
    arm_dir = packet_path.parent
    view = _build_view(arranging, packet, view_path, arm_dir)
    arranging.write_view(view, arm_dir / "ARRANGEMENT_INPUT.json")
    payload = view.arrangement_payload(max_source_chars=1200)
    payload["planning_revision_mode"] = True
    payload["available_material_inventory"] = _material_inventory(packet)
    messages = arranging.arrangement_messages(payload, prompt=arranging.load_editor_prompt(planning_revision=True), planning_revision=True)
    estimate = arranging.estimate_arrangement_cost(messages, model=ARR_MODEL, output_tokens=ARR_OUTPUT, thinking_budget=ARR_THINKING)
    snapshot = _ledger_snapshot(runtime[0])
    available = float(snapshot["limit_cny"]) - float(snapshot["actual_cny"]) - float(snapshot["reserved_cny"])
    if float(estimate.get("estimated_cost_cny") or 0.0) > available:
        raise RuntimeError(f"aligned_owner_arrangement_conservative_budget_exceeded:estimate={estimate.get('estimated_cost_cny')}:available={available}")
    _dump(arm_dir / "ARRANGEMENT_REQUEST.json", {"arm": arm, "model": ARR_MODEL, "output_tokens": ARR_OUTPUT, "thinking_budget": ARR_THINKING, "request_sha256": _request_hash(messages), "messages": messages, "estimate": estimate})
    result = {"status": "prepared", "plan_path": str(plan_path), "packet_path": str(packet_path), "view_packet_path": str(view_path), "view_path": str(arm_dir / "ARRANGEMENT_INPUT.json"), "unit_ids": [str(row.get("unit_id") or "") for row in plan.get("units") or () if isinstance(row, Mapping)], "view_units": [str(row.unit_id) for row in view.units], "view_sources": len(view.sources), "material_rows": len(packet.get("source_materials") or ()), "arrangement_estimate": estimate, "ledger_before": snapshot, "available_before_cny": available}
    _dump(OUT / "production" / "ALIGNED_PREPARE_REPORT.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return result


def aligned_arrange() -> dict[str, Any]:
    arranging, writing, planning, runtime = _imports()
    prep = _load(OUT / "production" / "ALIGNED_PREPARE_REPORT.json")
    arm_dir = Path(prep["packet_path"]).parent
    req = _load(arm_dir / "ARRANGEMENT_REQUEST.json")
    output_path = arm_dir / "ARRANGEMENT_RESULT.json"
    if output_path.is_file():
        old = _load(output_path)
        if old.get("request_sha256") == req.get("request_sha256"):
            return old
    packet = _load(BASE_PACKET)
    view = _build_view(arranging, packet, Path(prep["view_packet_path"]), arm_dir)
    client, ledger = _client(runtime, planning, ARR_MODEL, ARR_OUTPUT, ARR_THINKING, arm_dir / "raw_responses" / "arrangement")
    before = ledger.as_dict()
    _dump(arm_dir / "ledger_before_arrangement.json", before)
    call_id = f"outline-tournament:consumer-arrangement:R1_aligned_owner:Ch1:{uuid.uuid4().hex[:10]}"
    raw = runtime[2](client, req["messages"], model=ARR_MODEL, call_id=call_id)
    parsed = arranging.parse_arrangement_response(raw)
    validated = arranging.validate_arrangement(parsed, view, planning_revision=True)
    exported = dict(validated)
    exported["source_catalog"] = arranging.build_source_catalog(view, validated)
    for entry in exported["source_catalog"].values():
        entry.setdefault("locator", {})["writer_packet"] = prep["packet_path"]
    exported["chapter_tool_materials"] = []
    exported["provenance"] = {"model_call": call_id, "model": ARR_MODEL, "arm": "R1_aligned_owner", "same_material_inventory": True}
    after = ledger.as_dict()
    _dump(arm_dir / "ARRANGEMENT_RESPONSE_ENVELOPE.json", raw)
    _dump(arm_dir / "ARRANGEMENT_PARSED.json", parsed)
    _dump(arm_dir / "ARRANGEMENT_VALIDATION.json", validated)
    _dump(arm_dir / "ledger_after_arrangement.json", after)
    _dump(output_path, {"request_sha256": req["request_sha256"], "call_id": call_id, "usage": raw.get("usage"), "arrangement": exported})
    _dump(arm_dir / "ARRANGEMENT_FOR_WRITER.json", exported)
    result = {"status": "arranged", "call_id": call_id, "usage": raw.get("usage"), "validation": validated.get("validation"), "path": str(output_path), "ledger_after": after}
    _dump(OUT / "production" / "ALIGNED_ARRANGEMENT_REPORT.json", result)
    if not validated.get("validation", {}).get("ok"):
        raise RuntimeError("aligned_owner_arrangement_validation_failed:" + json.dumps(validated.get("validation"), ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))
    return result


def aligned_write() -> dict[str, Any]:
    """Write exactly the two aligned-owner units with the established params."""
    arranging, writing, planning, runtime = _imports()
    prep = _load(OUT / "production" / "ALIGNED_PREPARE_REPORT.json")
    arrangement_report = _load(OUT / "production" / "ALIGNED_ARRANGEMENT_REPORT.json")
    if arrangement_report.get("validation", {}).get("ok") is not True:
        raise RuntimeError("aligned_owner_arrangement_not_approved_for_writer")
    arm_dir = Path(prep["packet_path"]).parent
    arrangement_path = arm_dir / "ARRANGEMENT_FOR_WRITER.json"
    arrangement = _load(arrangement_path)
    counter = planning.qwen_local_token_counter(TOKENIZER)
    results: dict[str, Any] = {}
    for unit in arrangement.get("units") or ():
        unit_id = str(unit.get("unit_id") or "")
        if not unit_id:
            continue
        unit_dir = arm_dir / "writer" / unit_id
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_view = writing.build_unit_view(arrangement_path, unit_id, view_path=arm_dir / "ARRANGEMENT_INPUT.json", max_material_chars_per_source=writing.DEFAULT_MAX_MATERIAL_CHARS_PER_SOURCE)
        payload = writing.unit_payload(unit_view, language="zh")
        payload["planning_revision_mode"] = True
        messages = writing.unit_messages(unit_view, prompt=writing.load_writer_prompt(planning_revision=True), language="zh", payload=payload, planning_revision=True)
        estimate = writing.estimate_unit_cost(messages, model=WRITER_MODEL, output_tokens=WRITER_OUTPUT, thinking_budget=WRITER_THINKING, token_counter=counter)
        request_sha = _request_hash(messages)
        prior_result = _saved_writer_reuse(unit_dir, request_sha)
        _dump(unit_dir / "WRITER_REQUEST.json", {"model": WRITER_MODEL, "output_tokens": WRITER_OUTPUT, "thinking_budget": WRITER_THINKING, "request_sha256": request_sha, "messages": messages, "estimate": estimate})
        if prior_result is not None:
            results[unit_id] = {"status": "reused_saved_writer", "body_path": prior_result.get("body_path"), "usage": prior_result.get("usage"), "completion_status": prior_result.get("completion_status"), "request_sha256": request_sha}
            continue
        snapshot = _ledger_snapshot(runtime[0])
        available = float(snapshot["limit_cny"]) - float(snapshot["actual_cny"]) - float(snapshot["reserved_cny"])
        if float(estimate.get("estimated_cost_cny") or 0.0) > available:
            raise RuntimeError(f"aligned_owner_writer_conservative_budget_exceeded:{unit_id}:estimate={estimate.get('estimated_cost_cny')}:available={available}")
        _dump(unit_dir / "ledger_before.json", snapshot)
        client, ledger = _client(runtime, planning, WRITER_MODEL, WRITER_OUTPUT, WRITER_THINKING, unit_dir / "raw_responses")
        raw = writing.run_unit_writing(unit_view, client=client, model=WRITER_MODEL, prompt=writing.load_writer_prompt(planning_revision=True), language="zh", payload=payload, raw_response_dir=unit_dir / "raw_responses", planning_revision=True)
        after = ledger.as_dict()
        written = writing.write_unit_output(unit_view, raw["body_markdown"], unit_dir, model=WRITER_MODEL, language="zh", mode="run", used_messages=raw.get("messages") or messages, estimate=estimate, usage=raw.get("usage") or {}, response_path=raw.get("raw_response") or "", finish_reason=raw.get("finish_reason") or "", complete=raw.get("complete", True), partial_error=raw.get("partial_error") or "")
        _dump(unit_dir / "ledger_after.json", after)
        results[unit_id] = {"status": "written", "body_path": written.get("body_path") if isinstance(written, Mapping) else str(unit_dir / "UNIT_BODY.md"), "usage": raw.get("usage"), "estimate": estimate, "completion_status": written.get("completion_status") if isinstance(written, Mapping) else None, "request_sha256": request_sha}
    final = {"status": "complete", "ledger": _ledger_snapshot(runtime[0]), "arms": {"R1_aligned_owner": results}}
    _dump(OUT / "production" / "ALIGNED_WRITER_REPORT.json", final)
    print(json.dumps(final, ensure_ascii=False))
    return final


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if stage == "prepare":
        prepare()
    elif stage == "arrange":
        arrange()
    elif stage == "write":
        write()
    elif stage == "aligned_prepare":
        aligned_prepare()
    elif stage == "aligned_arrange":
        aligned_arrange()
    elif stage == "aligned_write":
        aligned_write()
    else:
        raise SystemExit("usage: production_consumers.py [prepare|arrange|write]")
