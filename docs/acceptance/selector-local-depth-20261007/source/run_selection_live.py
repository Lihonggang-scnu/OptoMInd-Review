"""Run the approved autonomous unit-selection request exactly once.

This driver consumes the already reviewed SELECTION_MESSAGES.json.  It does
not rebuild or edit the prepared request before the paid boundary, and it
does not run the downstream outline owner.
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


SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
OUT = Path(r"F:\OptoMind-Review-2\outputs\selector_local_depth_acceptance_20261007")
ATTEMPT = OUT / "live_attempt1"
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
EXPECTED_REQUEST_SHA = "4ccb01e614453058aa3c32aae29fb6c582d11ca795d9d856a388ba7dae6b8ee9"
EXPECTED_MESSAGES_SIGNATURE_SHA = "35e6c803e9bd18e91d4a4f7835828953ca9bd3d3c7784ea118f2da4fb8fc4dab"
EXPECTED_SOURCE_HEAD = "e7a1d835ccee9430558596d76f77343f663a3942"
ROUND_LIMIT = 40.0
SHARED_LIMIT = 125.0
CALL_ID = "outline-selector-local-depth-20261007:key0:attempt0"


def copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def hash_value(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()


def git_diff_sha256() -> str:
    diff = subprocess.check_output(["git", "diff", "--no-ext-diff", "--binary"], cwd=SOURCE)
    return hashlib.sha256(diff).hexdigest()


def ledger_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,usage_json,request_metadata_json from reservations order by rowid"
        ).fetchall()
    names = (
        "reservation_id", "call_id", "reserved_cny", "actual_cny", "status",
        "returned_model", "finish_reason", "request_id", "raw_response_sha256",
        "usage_json", "request_metadata_json",
    )
    return [dict(zip(names, row)) for row in rows]


def spend_since(rows: list[dict[str, Any]], baseline_ids: set[str]) -> dict[str, Any]:
    new_rows = [row for row in rows if str(row.get("reservation_id")) not in baseline_ids]
    actual = sum(float(row.get("actual_cny") or 0) for row in new_rows)
    held = sum(float(row.get("reserved_cny") or 0) for row in new_rows if row.get("status") in {"reserved", "uncertain"})
    return {"rows": new_rows, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held}


def main() -> int:
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    if (ATTEMPT / "SELECTION_RESULT.json").is_file():
        raise SystemExit("result_already_exists_no_recall")
    source_input = Path(r"F:\OptoMind-Review-2\outputs\outline_selection_20261006\FULL_CHAPTER_PAYLOADS.json")
    for path in (KEY, LEDGER, OUT / "SELECTION_REQUEST.json", source_input, OUT / "PREPARE_REPORT.json"):
        if not path.is_file():
            raise SystemExit(f"required_file_missing:{path}")
    if git_head() != EXPECTED_SOURCE_HEAD:
        raise SystemExit("source_head_changed_since_prepare")

    prepared = load(OUT / "SELECTION_REQUEST.json")
    messages = prepared.get("messages") or []
    stored_sha = str(prepared.get("request_sha256") or "")
    if stored_sha != EXPECTED_REQUEST_SHA or hash_value(messages) != EXPECTED_MESSAGES_SIGNATURE_SHA:
        raise SystemExit("prepared_messages_hash_mismatch")
    payload = prepared.get("payload") or {}
    model_payload = prepared.get("model_payload") or {}
    prepare_report = load(OUT / "PREPARE_REPORT.json")

    from optomind_research.runtime.upgrade3 import outline_selection as selection
    from optomind_research.runtime.upgrade3 import outline_on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
    from optomind_research.runtime.upgrade3.module4 import runtime

    profile = load_quality_profile("outline_selection")
    if str(profile.get("model")) != "qwen3.8-max" or int(profile.get("thinking_budget", 0)) != 32768 or int(profile.get("max_output_tokens", 0)) != 32768:
        raise SystemExit("selection_profile_changed")
    if selection.selection_messages(payload, model_payload=model_payload) != messages:
        raise SystemExit("prepared_messages_content_changed")
    if hash_value(selection.selection_messages(payload, model_payload=model_payload)) != EXPECTED_MESSAGES_SIGNATURE_SHA:
        raise SystemExit("regenerated_messages_hash_mismatch")
    projection_report = selection.verify_model_visible_projection(payload, model_payload)
    if projection_report.get("chapter_count") != 7 or projection_report.get("unit_count") != 29:
        raise SystemExit("selection_projection_scope_changed")
    if str(prepare_report.get("request_sha256") or "") != EXPECTED_REQUEST_SHA:
        raise SystemExit("prepare_report_hash_mismatch")

    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    estimate = selection.estimate_selection_request(messages, profile=profile, token_counter=counter)
    current_rows = ledger_rows()
    baseline_ids = {str(row.get("reservation_id")) for row in current_rows}
    with sqlite3.connect(LEDGER) as con:
        metadata = {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}
    if float(metadata.get("limit_cny", "nan")) != SHARED_LIMIT:
        raise SystemExit(f"shared_limit_changed:{metadata.get('limit_cny')}")
    existing_spend = spend_since(current_rows, set())
    if existing_spend["occupied_cny"] + float(estimate.get("estimated_cost_cny") or 0) > SHARED_LIMIT + 1e-9:
        raise SystemExit("shared_budget_would_be_exceeded")

    ATTEMPT.mkdir(parents=True, exist_ok=True)
    dump(ATTEMPT / "LEDGER_BASELINE.json", {
        "captured_at_epoch": time.time(), "shared_limit_cny": SHARED_LIMIT,
        "round_limit_cny": ROUND_LIMIT, "reservation_ids_before": sorted(baseline_ids),
        "rows_before": current_rows, "metadata_before": metadata,
        "historical_rows_included": True, "new_round_measured_by_ids": True,
    })
    wire = {
        "stage": "autonomous_unit_selection_max",
        "source_head": git_head(), "source_diff_sha256": git_diff_sha256(),
        "input_payload_sha256": hash_value(payload), "model_payload_sha256": hash_value(model_payload),
        "prepared_request_sha256": EXPECTED_REQUEST_SHA, "messages_signature_sha256": EXPECTED_MESSAGES_SIGNATURE_SHA, "messages": messages, "profile": profile,
        "estimate": estimate, "prepared_estimate": prepare_report.get("selection_estimate"),
        "projection_report": projection_report, "call_id": CALL_ID,
        "budget_before": {"metadata": metadata, "baseline_ids": sorted(baseline_ids), "round_limit_cny": ROUND_LIMIT},
        "max_retries": 0, "downstream_owner_called": False, "created_at_epoch": time.time(),
    }
    dump(ATTEMPT / "WIRE_REQUEST.json", wire)
    dump(ATTEMPT / "CALL_STATE.json", {"state": "about_to_call", "prepared_request_sha256": EXPECTED_REQUEST_SHA, "messages_signature_sha256": EXPECTED_MESSAGES_SIGNATURE_SHA, "call_id": CALL_ID, "max_retries": 0})

    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT)
    client = strengthening.make_strengthening_client(
        role="outline_selection", key_file=KEY, budget_ledger=ledger,
        raw_response_dir=ATTEMPT / "raw_responses", prompt_token_counter=counter,
    )
    try:
        result = selection.run_selection(
            payload, client=client, model=profile["model"],
            thinking_budget=int(profile["thinking_budget"]), max_output_tokens=int(profile["max_output_tokens"]),
            call_id=CALL_ID, checkpoint_dir=ATTEMPT / "stages", resume=True,
            profile=profile, model_payload=model_payload,
        )
    except Exception as exc:
        dump(ATTEMPT / "CALL_STATE.json", {"state": "call_exception", "prepared_request_sha256": EXPECTED_REQUEST_SHA, "messages_signature_sha256": EXPECTED_MESSAGES_SIGNATURE_SHA, "call_id": CALL_ID, "error_type": type(exc).__name__})
        dump(ATTEMPT / "EXCEPTION.json", {"error_type": type(exc).__name__, "error": str(exc), "prepared_request_sha256": EXPECTED_REQUEST_SHA})
        raise

    dump(ATTEMPT / "SELECTION_RESULT.json", result)
    raw = result.get("raw_response") or {}
    dump(ATTEMPT / "RAW_RESPONSE.json", raw)
    dump(ATTEMPT / "EFFECTIVE_REQUEST.json", {
        "request_sha256": result.get("request_sha256"), "call_id": CALL_ID,
        "messages": messages, "profile": profile,
        "provider_effective_request": raw.get("effective_request"),
        "returned_model": raw.get("returned_model"), "finish_reason": raw.get("finish_reason"),
    })
    dump(ATTEMPT / "CALL_STATE.json", {"state": "response_saved", "prepared_request_sha256": EXPECTED_REQUEST_SHA, "messages_signature_sha256": EXPECTED_MESSAGES_SIGNATURE_SHA, "call_id": CALL_ID, "status": result.get("status")})

    chapter_payloads = load(source_input)
    projected = selection.selection_to_on_demand_payloads(chapter_payloads, result) if result.get("status") == "selected" else []
    selected_records = []
    for item in projected:
        owner_payload = item["payload"]
        catalog = outline_on_demand.build_material_catalog(owner_payload)
        access = outline_on_demand.access_messages(owner_payload, catalog)
        owner = outline_on_demand.owner_messages(owner_payload, catalog, outline_on_demand.full_catalog_trace(catalog))
        selected_records.append({
            "group_id": item["group_id"], "chapter_id": item["chapter_id"],
            "group_chapter_ids": item.get("group_chapter_ids"),
            "modifiable_unit_ids": item["modifiable_unit_ids"], "read_only_unit_ids": item["read_only_unit_ids"],
            "selection_reason": item.get("selection_reason"), "improvement_focus": item.get("improvement_focus"),
            "selection_context": owner_payload.get("selection_context"),
            "payload": owner_payload, "catalog": outline_on_demand.public_catalog(catalog),
            "access_messages": access, "owner_messages": owner,
            "access_messages_sha256": hash_value(access), "owner_messages_sha256": hash_value(owner),
        })
    dump(ATTEMPT / "SELECTED_ON_DEMAND_PAYLOADS.json", selected_records)
    dump(ATTEMPT / "SELECTED_ON_DEMAND_SUMMARY.json", {
        "status": result.get("status"), "group_count": len(selected_records),
        "groups": [{"group_id": row["group_id"], "chapter_id": row["chapter_id"], "modifiable_unit_ids": row["modifiable_unit_ids"], "read_only_unit_ids": row["read_only_unit_ids"]} for row in selected_records],
        "owner_calls_made": 0, "offline_messages_only": True,
    })

    after_rows = ledger_rows()
    after = spend_since(after_rows, baseline_ids)
    dump(ATTEMPT / "LEDGER_AFTER.json", {"rows_after": after_rows, "metadata_after": metadata, "new_round": after})
    report = {
        "status": result.get("status"), "result_path": str(ATTEMPT / "SELECTION_RESULT.json"),
        "raw_response_path": str(ATTEMPT / "RAW_RESPONSE.json"), "effective_request_path": str(ATTEMPT / "EFFECTIVE_REQUEST.json"),
        "wire_request_path": str(ATTEMPT / "WIRE_REQUEST.json"), "selected_payloads_path": str(ATTEMPT / "SELECTED_ON_DEMAND_PAYLOADS.json"),
        "request_sha256": result.get("request_sha256"), "expected_messages_signature_sha256": EXPECTED_MESSAGES_SIGNATURE_SHA, "prepared_request_sha256": EXPECTED_REQUEST_SHA,
        "source_head": git_head(), "source_diff_sha256": git_diff_sha256(), "returned_model": raw.get("returned_model"),
        "finish_reason": raw.get("finish_reason"), "usage": raw.get("usage"), "telemetry": result.get("telemetry"),
        "estimate": estimate, "budget_after": after, "new_round_actual_cny": after["actual_cny"],
        "new_round_held_cny": after["held_cny"], "new_round_occupied_cny": after["occupied_cny"],
        "round_limit_cny": ROUND_LIMIT, "shared_limit_cny": SHARED_LIMIT, "paid_call_count": 1,
        "max_retries": 0, "downstream_owner_called": False, "selected_group_count": len(selected_records),
    }
    dump(ATTEMPT / "RUN_REPORT.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
