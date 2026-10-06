"""Bounded live stages for the directory repair comparison.

Stages are deliberately resumable and one-way:

* ``raise_budget`` snapshots the shared ledger and changes only its explicit
  limit from 45 to 85 CNY;
* ``full_plus`` makes the complete-material Plus call once;
* ``access`` makes the semantic-directory access Plus call once and then stops
  after exporting the Max owner request for review.

Existing successful or failed stage records are never overwritten or retried.
The script never reads a prior candidate answer or quality review.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SOURCE_ROOT = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006")
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY_PATH = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
SHARED_LIMIT_BEFORE = 45.0
SHARED_LIMIT_AFTER = 85.0
ROUND_LIMIT = 40.0
TARGET_SHA = "4d81a772bdecc4da18a84a8bc90298fb1a38f625"


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _imports():
    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
    from optomind_research.runtime.upgrade3.module4 import runtime

    return arranging, on_demand, strengthening, planning, load_quality_profile, runtime


def _rows() -> list[dict[str, Any]]:
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,created_at,returned_model,finish_reason,request_id,raw_response_sha256,usage_json,request_metadata_json from reservations order by rowid"
        ).fetchall()
    names = (
        "reservation_id", "call_id", "amount_cny", "actual_cny", "status", "created_at",
        "returned_model", "finish_reason", "request_id", "raw_response_sha256", "usage_json", "request_metadata_json",
    )
    return [dict(zip(names, row)) for row in rows]


def _meta() -> dict[str, str]:
    with sqlite3.connect(LEDGER) as con:
        return {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}


def _round_spend(baseline_ids: set[str]) -> dict[str, Any]:
    rows = [row for row in _rows() if str(row.get("reservation_id")) not in baseline_ids]
    actual = sum(float(row.get("actual_cny") or 0) for row in rows)
    held = sum(float(row.get("amount_cny") or 0) for row in rows if row.get("status") in {"reserved", "uncertain"})
    return {"rows": rows, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held}


def _git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE_ROOT, text=True).strip()


def raise_budget() -> dict[str, Any]:
    path = OUT / "LIVE_BUDGET_BASELINE.json"
    if path.is_file():
        baseline = _load(path)
    else:
        meta = _meta()
        if float(meta.get("limit_cny", "nan")) != SHARED_LIMIT_BEFORE:
            raise RuntimeError(f"unexpected_shared_limit:{meta.get('limit_cny')}")
        baseline = {
            "captured_at_epoch": time.time(),
            "source_head": _git_head(),
            "source_head_expected": TARGET_SHA,
            "metadata_before": meta,
            "reservation_ids_before": [row["reservation_id"] for row in _rows()],
            "rows_before": _rows(),
            "round_limit_cny": ROUND_LIMIT,
            "shared_limit_before_cny": SHARED_LIMIT_BEFORE,
            "shared_limit_after_cny": SHARED_LIMIT_AFTER,
            "paid_calls_before_this_round": 0,
        }
        _dump(path, baseline)
    with sqlite3.connect(LEDGER) as con:
        meta = {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}
        current = float(meta.get("limit_cny", "nan"))
        if current == SHARED_LIMIT_BEFORE:
            con.execute("update budget_meta set value=? where key='limit_cny'", (str(SHARED_LIMIT_AFTER),))
            con.commit()
        elif current != SHARED_LIMIT_AFTER:
            raise RuntimeError(f"unexpected_shared_limit_after_snapshot:{current}")
    after = _meta()
    if float(after.get("limit_cny", "nan")) != SHARED_LIMIT_AFTER:
        raise RuntimeError("shared_limit_raise_not_persisted")
    if len(_rows()) < len(baseline.get("rows_before") or []):
        raise RuntimeError("historical_ledger_rows_lost")
    result = {
        "status": "shared_limit_raised_preserving_history",
        "baseline_path": str(path),
        "metadata_before": baseline.get("metadata_before"),
        "metadata_after": after,
        "reservation_count_before": len(baseline.get("rows_before") or []),
        "reservation_count_after": len(_rows()),
        "round_limit_cny": ROUND_LIMIT,
        "old_rows_preserved": True,
        "new_calls": 0,
    }
    _dump(OUT / "BUDGET_RAISE_REPORT.json", result)
    return result


def _baseline_ids() -> set[str]:
    path = OUT / "LIVE_BUDGET_BASELINE.json"
    if not path.is_file():
        raise RuntimeError("run_raise_budget_first")
    return set(str(item) for item in (_load(path).get("reservation_ids_before") or []))


def _assert_round_room(estimate: Mapping[str, Any]) -> dict[str, Any]:
    spend = _round_spend(_baseline_ids())
    projected = spend["occupied_cny"] + float(estimate.get("estimated_cost_cny") or 0)
    if projected > ROUND_LIMIT + 1e-9:
        raise RuntimeError(f"round_limit_exceeded:{projected}")
    return {"before": spend, "estimate": _copy(estimate), "projected_occupied_cny": projected, "round_limit_cny": ROUND_LIMIT}


def _read_input() -> dict[str, Any]:
    payload = _load(OUT / "INPUT_PAYLOAD.json")
    if not isinstance(payload, dict):
        raise RuntimeError("input_payload_not_object")
    return payload


def _profile_check(saved: Mapping[str, Any], current: Mapping[str, Any], role: str) -> None:
    if saved.get("role") != role:
        raise RuntimeError(f"prepared_role_changed:{saved.get('role')}:{role}")
    for key in ("model", "thinking", "thinking_budget", "max_output_tokens"):
        if saved.get(key) != current.get(key):
            raise RuntimeError(f"prepared_profile_changed:{key}")


def _write_stage_state(path: Path, state: str, **extra: Any) -> None:
    _dump(path, {"state": state, "updated_at_epoch": time.time(), "source_head": _git_head(), **extra})


def full_plus() -> dict[str, Any]:
    arranging, on_demand, strengthening, planning, load_profile, runtime = _imports()
    request_path = OUT / "full_material_plus" / "REQUEST.json"
    result_path = OUT / "full_material_plus" / "RESULT.json"
    if result_path.is_file():
        return {"status": "already_recorded_no_recall", "result_path": str(result_path)}
    request = _load(request_path)
    payload = _read_input()
    profile = load_profile("autonomous_outline")
    messages = request.get("messages") or []
    if _hash(messages) != request.get("request_sha256"):
        raise RuntimeError("full_plus_request_hash_mismatch")
    current_messages = strengthening.strengthening_messages(payload)
    if _hash(current_messages) != request.get("request_sha256"):
        raise RuntimeError("full_plus_messages_changed_rerun_prepare")
    _profile_check(request, profile, "autonomous_outline")
    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
    guard = _assert_round_room(estimate)
    wire = {
        "stage": "full_plus",
        "request_sha256": request["request_sha256"],
        "messages": messages,
        "model": profile["model"],
        "thinking": profile["thinking"],
        "thinking_budget": profile["thinking_budget"],
        "max_output_tokens": profile["max_output_tokens"],
        "estimate": estimate,
        "budget_guard": guard,
        "source_head": _git_head(),
    }
    _dump(OUT / "full_material_plus" / "WIRE_REQUEST.json", wire)
    _write_stage_state(OUT / "full_material_plus" / "CALL_STATE.json", "about_to_call", wire_request_sha256=wire["request_sha256"])
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT_AFTER)
    client = strengthening.make_strengthening_client(
        role="autonomous_outline", key_file=KEY_PATH, budget_ledger=ledger,
        raw_response_dir=OUT / "full_material_plus" / "raw_responses", prompt_token_counter=counter,
    )
    try:
        result = strengthening.run_strengthening(
            payload, client=client, model=profile["model"],
            thinking_budget=profile["thinking_budget"], max_output_tokens=profile["max_output_tokens"],
            call_id=str(payload.get("call_id") or "outline-directory-repair-20261006") + ":full-plus",
        )
    except Exception as exc:
        _write_stage_state(OUT / "full_material_plus" / "CALL_STATE.json", "call_exception", error_type=type(exc).__name__)
        _dump(OUT / "full_material_plus" / "EXCEPTION.json", {"error_type": type(exc).__name__, "source_head": _git_head()})
        raise
    # Persist the complete returned record before any downstream inspection.
    _dump(result_path, result)
    _dump(OUT / "full_material_plus" / "RAW_RESPONSE.json", result.get("raw_response") or {})
    _write_stage_state(OUT / "full_material_plus" / "CALL_STATE.json", "response_saved", result_status=result.get("status"), request_sha256=request["request_sha256"])
    arrangement_path = None
    if result.get("status") in {"updated", "no_change"}:
        packet = strengthening.project_plan_for_arrangement(payload, result)
        _dump(OUT / "full_material_plus" / "ARRANGEMENT_PACKET.json", packet)
        view = arranging.build_chapter_view(OUT / "full_material_plus" / "ARRANGEMENT_PACKET.json", id_map_path=OUT / "full_material_plus" / "ID_MAP.json")
        arrangement_path = arranging.write_view(view, OUT / "full_material_plus" / "ARRANGEMENT_INPUT.json")
    spend = _round_spend(_baseline_ids())
    report = {
        "status": result.get("status"),
        "result_path": str(result_path),
        "raw_response_path": str(OUT / "full_material_plus" / "RAW_RESPONSE.json"),
        "arrangement_input": str(arrangement_path) if arrangement_path else None,
        "request_sha256": request["request_sha256"],
        "returned_model": (result.get("raw_response") or {}).get("returned_model"),
        "finish_reason": (result.get("raw_response") or {}).get("finish_reason"),
        "usage": (result.get("raw_response") or {}).get("usage"),
        "effective_request": (result.get("raw_response") or {}).get("effective_request"),
        "round_spend": spend,
        "budget_guard": guard,
        "paid_call_count": 1,
    }
    _dump(OUT / "full_material_plus" / "RUN_REPORT.json", report)
    return report


def access() -> dict[str, Any]:
    arranging, on_demand, strengthening, planning, load_profile, runtime = _imports()
    stage = OUT / "semantic_catalog_max"
    result_path = stage / "ACCESS_RESULT.json"
    if result_path.is_file():
        return {"status": "already_recorded_no_recall", "result_path": str(result_path)}
    request = _load(stage / "ACCESS_REQUEST.json")
    payload = _read_input()
    catalog = on_demand.build_material_catalog(payload)
    messages = request.get("messages") or []
    profile = load_profile("autonomous_outline")
    if _hash(messages) != request.get("request_sha256"):
        raise RuntimeError("access_request_hash_mismatch")
    if _hash(on_demand.access_messages(payload, catalog)) != request.get("request_sha256"):
        raise RuntimeError("access_messages_changed_rerun_prepare")
    _profile_check(request, profile, "autonomous_outline")
    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
    guard = _assert_round_room(estimate)
    wire = {
        "stage": "semantic_catalog_access_plus",
        "request_sha256": request["request_sha256"],
        "messages": messages,
        "model": profile["model"],
        "thinking": profile["thinking"],
        "thinking_budget": profile["thinking_budget"],
        "max_output_tokens": profile["max_output_tokens"],
        "estimate": estimate,
        "budget_guard": guard,
        "source_head": _git_head(),
    }
    _dump(stage / "ACCESS_WIRE_REQUEST.json", wire)
    _write_stage_state(stage / "ACCESS_CALL_STATE.json", "about_to_call", wire_request_sha256=wire["request_sha256"])
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT_AFTER)
    client = strengthening.make_strengthening_client(
        role="autonomous_outline", key_file=KEY_PATH, budget_ledger=ledger,
        raw_response_dir=stage / "access_raw_responses", prompt_token_counter=counter,
    )
    try:
        raw = runtime.invoke_client(
            client, messages, model=profile["model"],
            max_output_tokens=profile["max_output_tokens"], thinking=True,
            thinking_budget=profile["thinking_budget"],
            call_id=str(payload.get("call_id") or "outline-directory-repair-20261006") + ":access-plus",
        )
    except Exception as exc:
        _write_stage_state(stage / "ACCESS_CALL_STATE.json", "call_exception", error_type=type(exc).__name__)
        _dump(stage / "ACCESS_EXCEPTION.json", {"error_type": type(exc).__name__, "source_head": _git_head()})
        raise
    # Save the transport response immediately; parsing and owner preparation
    # happen only after this durable response record exists.
    _dump(stage / "ACCESS_RAW_RESPONSE.json", raw)
    parsed, telemetry = planning._parse_planner_response(raw)
    validation_errors: list[str] = []
    try:
        validated = on_demand.validate_access_response(parsed, catalog)
    except Exception as exc:
        validated = None
        validation_errors.append(f"{type(exc).__name__}:{exc}")
    access_record = {
        "status": "access_response_saved",
        "request_sha256": request["request_sha256"],
        "messages": messages,
        "raw_response": raw,
        "parsed_response": parsed,
        "telemetry": telemetry,
        "validated": validated,
        "validation_errors": validation_errors,
    }
    _dump(result_path, access_record)
    _write_stage_state(stage / "ACCESS_CALL_STATE.json", "response_saved", parsed_status=parsed.get("status") if isinstance(parsed, Mapping) else None, validation_errors=validation_errors)
    owner_request_path = None
    trace = None
    if validated is not None:
        trace = on_demand.resolve_material_requests(payload, catalog, validated)
        _dump(stage / "RESOLVED_TRACE.json", trace)
        owner_messages = on_demand.owner_messages(payload, catalog, trace)
        max_profile = load_profile("strong_outline")
        owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=max_profile, token_counter=counter)
        owner_request = {
            "status": "prepared_for_root_review_no_paid_owner_call",
            "request_sha256": _hash(owner_messages),
            "messages": owner_messages,
            "model": max_profile["model"],
            "thinking": max_profile["thinking"],
            "thinking_budget": max_profile["thinking_budget"],
            "max_output_tokens": max_profile["max_output_tokens"],
            "wire_completion_capacity": int(max_profile["thinking_budget"]) + int(max_profile["max_output_tokens"]),
            "estimate": owner_estimate,
            "material_access_trace": trace,
            "no_root_scientific_feedback_input": True,
            "no_prior_candidate_answer_input": True,
        }
        owner_request_path = stage / "OWNER_REQUEST_FROM_ACCESS.json"
        _dump(owner_request_path, owner_request)
        _dump(stage / "MODEL_VISIBLE_PAYLOAD_FROM_ACCESS.json", on_demand._model_visible_payload(on_demand._owner_payload(payload, catalog, trace), catalog, trace))
    spend = _round_spend(_baseline_ids())
    report = {
        "status": "access_saved_owner_waiting_root_review" if validated is not None else "access_saved_validation_failed",
        "access_result_path": str(result_path),
        "access_raw_response_path": str(stage / "ACCESS_RAW_RESPONSE.json"),
        "owner_request_path": str(owner_request_path) if owner_request_path else None,
        "request_sha256": request["request_sha256"],
        "parsed_status": parsed.get("status") if isinstance(parsed, Mapping) else None,
        "validation_errors": validation_errors,
        "returned_model": (raw or {}).get("returned_model"),
        "finish_reason": (raw or {}).get("finish_reason"),
        "usage": (raw or {}).get("usage"),
        "effective_request": (raw or {}).get("effective_request"),
        "resolved_material_count": trace.get("resolved_count") if trace else 0,
        "resolved_access_ids": trace.get("access_ids") if trace else [],
        "round_spend": spend,
        "budget_guard": guard,
        "max_owner_called": False,
    }
    _dump(stage / "ACCESS_RUN_REPORT.json", report)
    return report


def _combine_trace(first: Mapping[str, Any], second: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
    selected = [*(first.get("selected_materials") or []), *(second.get("selected_materials") or [])]
    trace = [*(first.get("trace") or []), *(second.get("trace") or [])]
    return {
        "status": "partial",
        "material_requests": [*(first.get("material_requests") or []), *(second.get("material_requests") or [])],
        "selected_materials": selected,
        "trace": trace,
        "resolved_count": len(selected),
        "access_ids": list(dict.fromkeys([*(first.get("access_ids") or []), *(second.get("access_ids") or [])])),
        "catalog_sha256": catalog.get("catalog_sha256"),
    }


def max_owner() -> dict[str, Any]:
    arranging, on_demand, strengthening, planning, load_profile, runtime = _imports()
    stage = OUT / "semantic_catalog_max"
    result_path = stage / "OWNER_RESULT.json"
    if result_path.is_file():
        return {"status": "already_recorded_no_recall", "result_path": str(result_path)}
    payload = _read_input()
    access_record = _load(stage / "ACCESS_RESULT.json")
    trace = _load(stage / "RESOLVED_TRACE.json")
    request = _load(stage / "OWNER_REQUEST_FROM_ACCESS.json")
    catalog = on_demand.build_material_catalog(payload)
    profile = load_profile("strong_outline")
    messages = on_demand.owner_messages(payload, catalog, trace)
    if _hash(messages) != request.get("request_sha256"):
        raise RuntimeError("owner_request_changed_rerun_prepare")
    if request.get("model") != profile.get("model"):
        raise RuntimeError("owner_profile_changed_rerun_prepare")
    for key in ("thinking", "thinking_budget", "max_output_tokens"):
        if request.get(key) != profile.get(key):
            raise RuntimeError(f"owner_profile_changed:{key}")
    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
    guard = _assert_round_room(estimate)
    wire = {
        "stage": "semantic_catalog_owner_max",
        "request_sha256": _hash(messages),
        "messages": messages,
        "model": profile["model"],
        "thinking": profile["thinking"],
        "thinking_budget": profile["thinking_budget"],
        "max_output_tokens": profile["max_output_tokens"],
        "wire_completion_capacity": int(profile["thinking_budget"]) + int(profile["max_output_tokens"]),
        "estimate": estimate,
        "material_access_trace": trace,
        "budget_guard": guard,
        "source_head": _git_head(),
    }
    _dump(stage / "OWNER_WIRE_REQUEST.json", wire)
    _write_stage_state(stage / "OWNER_CALL_STATE.json", "about_to_call", wire_request_sha256=wire["request_sha256"])
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT_AFTER)
    client = strengthening.make_strengthening_client(
        role="strong_outline", key_file=KEY_PATH, budget_ledger=ledger,
        raw_response_dir=stage / "owner_raw_responses", prompt_token_counter=counter,
    )
    try:
        first = strengthening._run_owner_with_messages(
            on_demand._owner_payload(payload, catalog, trace), messages=messages, client=client,
            model=profile["model"], thinking_budget=profile["thinking_budget"],
            max_output_tokens=profile["max_output_tokens"],
            call_id=str(payload.get("call_id") or "outline-directory-repair-20261006") + ":owner-max",
        )
    except Exception as exc:
        _write_stage_state(stage / "OWNER_CALL_STATE.json", "call_exception", error_type=type(exc).__name__)
        _dump(stage / "OWNER_EXCEPTION.json", {"error_type": type(exc).__name__, "source_head": _git_head()})
        raise
    _dump(stage / "OWNER_INITIAL_RESULT.json", first)
    _dump(stage / "OWNER_INITIAL_RAW_RESPONSE.json", first.get("raw_response") or {})
    _write_stage_state(stage / "OWNER_CALL_STATE.json", "initial_response_saved", result_status=first.get("status"))
    final = first
    continuation = None
    continuation_trace = None
    continuation_estimate = None
    parsed = first.get("parsed_response") if isinstance(first.get("parsed_response"), Mapping) else {}
    if str(parsed.get("status") or "").casefold() in {"needs_materials", "needs_more_materials", "needs_material"}:
        requested = parsed.get("material_requests") or parsed.get("requests") or parsed.get("needs_materials")
        validated = on_demand.validate_access_response({"status": "partial", "material_requests": requested}, catalog)
        continuation_trace = on_demand.resolve_material_requests(payload, catalog, validated, prior_access_ids=trace.get("access_ids") or [])
        combined = _combine_trace(trace, continuation_trace, catalog)
        continuation_messages = on_demand.owner_messages(payload, catalog, combined, continuation=True)
        continuation_estimate = strengthening.estimate_strengthening_request(continuation_messages, profile=profile, token_counter=counter)
        continuation_guard = _assert_round_room(continuation_estimate)
        _dump(stage / "CONTINUATION_TRACE.json", continuation_trace)
        _dump(stage / "OWNER_CONTINUATION_WIRE_REQUEST.json", {
            "stage": "semantic_catalog_owner_max_continuation",
            "request_sha256": _hash(continuation_messages),
            "messages": continuation_messages,
            "model": profile["model"],
            "thinking": profile["thinking"],
            "thinking_budget": profile["thinking_budget"],
            "max_output_tokens": profile["max_output_tokens"],
            "wire_completion_capacity": int(profile["thinking_budget"]) + int(profile["max_output_tokens"]),
            "estimate": continuation_estimate,
            "budget_guard": continuation_guard,
            "material_access_trace": combined,
        })
        continuation = strengthening._run_owner_with_messages(
            on_demand._owner_payload(payload, catalog, combined, continuation=True), messages=continuation_messages,
            client=client, model=profile["model"], thinking_budget=profile["thinking_budget"],
            max_output_tokens=profile["max_output_tokens"],
            call_id=str(payload.get("call_id") or "outline-directory-repair-20261006") + ":owner-max:continuation",
        )
        _dump(stage / "OWNER_CONTINUATION_RESULT.json", continuation)
        _dump(stage / "OWNER_CONTINUATION_RAW_RESPONSE.json", continuation.get("raw_response") or {})
        final = continuation
        trace = combined
    _dump(result_path, final)
    _dump(stage / "OWNER_RAW_RESPONSE.json", final.get("raw_response") or {})
    arrangement_path = None
    if final.get("status") in {"updated", "no_change"}:
        packet = strengthening.project_plan_for_arrangement(payload, final)
        _dump(stage / "ARRANGEMENT_PACKET.json", packet)
        view = arranging.build_chapter_view(stage / "ARRANGEMENT_PACKET.json", id_map_path=stage / "ID_MAP.json")
        arrangement_path = arranging.write_view(view, stage / "ARRANGEMENT_INPUT.json")
    spend = _round_spend(_baseline_ids())
    report = {
        "status": final.get("status"),
        "initial_owner_result_path": str(stage / "OWNER_INITIAL_RESULT.json"),
        "result_path": str(result_path),
        "raw_response_path": str(stage / "OWNER_RAW_RESPONSE.json"),
        "arrangement_input": str(arrangement_path) if arrangement_path else None,
        "initial_request_sha256": wire["request_sha256"],
        "returned_model": (final.get("raw_response") or {}).get("returned_model"),
        "finish_reason": (final.get("raw_response") or {}).get("finish_reason"),
        "usage": (final.get("raw_response") or {}).get("usage"),
        "effective_request": (final.get("raw_response") or {}).get("effective_request"),
        "initial_estimate": estimate,
        "continuation_called": continuation is not None,
        "continuation_estimate": continuation_estimate,
        "final_material_access_count": trace.get("resolved_count"),
        "final_material_access_ids": trace.get("access_ids"),
        "round_spend": spend,
        "max_call_count": 2 if continuation is not None else 1,
        "access_reused_without_recall": True,
    }
    _write_stage_state(stage / "OWNER_CALL_STATE.json", "final_response_saved", result_status=final.get("status"), continuation_called=continuation is not None)
    _dump(stage / "OWNER_RUN_REPORT.json", report)
    return report


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"raise_budget", "full_plus", "access", "max_owner"}:
        raise SystemExit("usage: run_live_stages.py raise_budget|full_plus|access|max_owner")
    stage = sys.argv[1]
    if stage == "raise_budget":
        result = raise_budget()
    elif stage == "full_plus":
        result = full_plus()
    elif stage == "access":
        result = access()
    else:
        result = max_owner()
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
