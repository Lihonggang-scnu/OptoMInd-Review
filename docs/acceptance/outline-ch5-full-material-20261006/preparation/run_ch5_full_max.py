"""Run the root-approved Ch5 full-material Max request once.

The prepared messages and payload are checked before crossing the paid boundary.
The shared ledger is bounded by the existing 40-CNY round budget, and every
response is saved before arrangement projection.  This driver never retries.
"""
from __future__ import annotations

import hashlib
import json
import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_full_strengthening_20261006_40cny")
FULL = OUT / "ch5_full_material"
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
BASELINE = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006\LIVE_BUDGET_BASELINE.json")
ROUND_LIMIT = 40.0
SHARED_LIMIT = 85.0


def copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def hash_value(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


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
    import sqlite3
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,usage_json,request_metadata_json from reservations order by rowid"
        ).fetchall()
    names = ("reservation_id", "call_id", "reserved_cny", "actual_cny", "status", "returned_model", "finish_reason", "request_id", "raw_response_sha256", "usage_json", "request_metadata_json")
    return [dict(zip(names, row)) for row in rows]


def round_spend(baseline_ids: set[str]) -> dict[str, Any]:
    rows = [row for row in ledger_rows() if str(row.get("reservation_id")) not in baseline_ids]
    actual = sum(float(row.get("actual_cny") or 0) for row in rows)
    held = sum(float(row.get("reserved_cny") or 0) for row in rows if row.get("status") in {"reserved", "uncertain"})
    return {"rows": rows, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    result_path = OUT / "ch5_full_material" / "RESULT.json"
    if result_path.is_file():
        raise SystemExit("result_already_exists_no_recall")
    if not KEY.is_file():
        raise SystemExit("key_file_missing")
    if not BASELINE.is_file():
        raise SystemExit("shared_budget_baseline_missing")

    payload = load(FULL / "INPUT_PAYLOAD.json")
    prepared = load(FULL / "FULL_MAX_MESSAGES.json")
    report = load(FULL / "PREPARE_REPORT.json")
    messages = prepared.get("messages") or []
    expected_messages_sha = str(prepared.get("sha256") or "")
    if hash_value(messages) != expected_messages_sha:
        raise SystemExit("prepared_messages_hash_mismatch")
    if hash_value(payload) != str(report.get("input_payload_sha256") or ""):
        raise SystemExit("prepared_payload_hash_mismatch")

    if str(report.get("full_messages_sha256") or "") != expected_messages_sha:
        raise SystemExit("prepare_report_messages_hash_mismatch")
    if git_head() != str(report.get("provenance", {}).get("source_head_at_prepare") or ""):
        raise SystemExit("source_head_changed_since_prepare")

    if str(prepared.get("profile", {}).get("model")) != "qwen3.8-max":
        raise SystemExit("prepared_model_changed")
    if int(prepared.get("profile", {}).get("thinking_budget", 0)) != 32768:
        raise SystemExit("prepared_thinking_changed")
    if int(prepared.get("profile", {}).get("max_output_tokens", 0)) != 65536:
        raise SystemExit("prepared_answer_changed")

    if str(LEDGER) == "":
        raise SystemExit("ledger_missing")
    from optomind_research.runtime.upgrade3 import chapter_arrangement as arranging
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
    from optomind_research.runtime.upgrade3.module4 import runtime

    profile = load_quality_profile("strong_outline_chapter")
    for key in ("model", "thinking_budget", "max_output_tokens"):
        if profile.get(key) != prepared.get("profile", {}).get(key):
            raise SystemExit(f"effective_profile_changed:{key}")
    current_messages = strengthening.strengthening_messages(payload)
    if hash_value(current_messages) != expected_messages_sha:
        raise SystemExit("current_messages_changed_since_prepare")

    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    estimate = strengthening.estimate_strengthening_request(messages, profile=profile, token_counter=counter)
    baseline = load(BASELINE)
    baseline_ids = set(str(item) for item in baseline.get("reservation_ids_before") or [])
    spend = round_spend(baseline_ids)
    projected = spend["occupied_cny"] + float(estimate.get("estimated_cost_cny") or 0)
    if projected > ROUND_LIMIT + 1e-9:
        raise SystemExit(f"round_budget_exceeded:{projected}")

    import sqlite3
    with sqlite3.connect(LEDGER) as con:
        metadata = {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}
    if float(metadata.get("limit_cny", "nan")) != SHARED_LIMIT:
        raise SystemExit(f"shared_limit_changed:{metadata.get('limit_cny')}")

    stage = FULL
    wire = {
        "stage": "ch5_full_material_max",
        "source_head": git_head(),
        "source_diff_sha256": git_diff_sha256(),
        "input_payload_sha256": hash_value(payload),
        "request_sha256": expected_messages_sha,
        "messages": messages,
        "profile": profile,
        "estimate": estimate,
        "budget_before": {"metadata": metadata, "round_spend": spend, "projected_occupied_cny": projected, "round_limit_cny": ROUND_LIMIT},
        "baseline_path": str(BASELINE),
        "prepared_input_path": str(FULL / "INPUT_PAYLOAD.json"),
        "prepared_messages_path": str(FULL / "FULL_MAX_MESSAGES.json"),
        "created_at_epoch": time.time(),
        "max_retries": 0,
    }
    dump(stage / "WIRE_REQUEST.json", wire)
    dump(stage / "CALL_STATE.json", {"state": "about_to_call", "request_sha256": expected_messages_sha, "source_head": git_head(), "max_retries": 0})
    if args.prepare_only:
        print(json.dumps({"status": "prepared_no_paid_calls", "request_sha256": expected_messages_sha, "estimate": estimate, "budget": wire["budget_before"]}, ensure_ascii=False), flush=True)
        return 0

    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT)
    client = strengthening.make_strengthening_client(
        role="strong_outline_chapter", key_file=KEY, budget_ledger=ledger,
        raw_response_dir=stage / "raw_responses", prompt_token_counter=counter,
    )
    call_id = str(payload.get("call_id") or "outline-full-strengthening-20261006:Ch5") + ":full-max"
    try:
        result = strengthening.run_strengthening(
            payload, client=client, model=profile["model"],
            thinking_budget=int(profile["thinking_budget"]), max_output_tokens=int(profile["max_output_tokens"]),
            call_id=call_id,
        )
    except Exception as exc:
        dump(stage / "CALL_STATE.json", {"state": "call_exception", "request_sha256": expected_messages_sha, "source_head": git_head(), "error_type": type(exc).__name__})
        dump(stage / "EXCEPTION.json", {"error_type": type(exc).__name__, "source_head": git_head(), "request_sha256": expected_messages_sha})
        raise

    dump(stage / "RESULT.json", result)
    dump(stage / "RAW_RESPONSE.json", result.get("raw_response") or {})
    dump(stage / "EFFECTIVE_REQUEST.json", (result.get("raw_response") or {}).get("effective_request") or {})
    dump(stage / "CALL_STATE.json", {"state": "response_saved", "request_sha256": expected_messages_sha, "source_head": git_head(), "status": result.get("status")})

    arrangement_path = None
    if result.get("status") in {"updated", "no_change"}:
        packet = strengthening.project_plan_for_arrangement(payload, result)
        dump(stage / "ARRANGEMENT_PACKET.json", packet)
        view = arranging.build_chapter_view(stage / "ARRANGEMENT_PACKET.json", id_map_path=stage / "ID_MAP.json")
        arrangement_path = arranging.write_view(view, stage / "ARRANGEMENT_INPUT.json")

    after = round_spend(baseline_ids)
    raw = result.get("raw_response") or {}
    run_report = {
        "status": result.get("status"), "result_path": str(stage / "RESULT.json"),
        "raw_response_path": str(stage / "RAW_RESPONSE.json"), "effective_request_path": str(stage / "EFFECTIVE_REQUEST.json"),
        "arrangement_input": str(arrangement_path) if arrangement_path else None,
        "request_sha256": expected_messages_sha, "source_head": git_head(), "source_diff_sha256": git_diff_sha256(),
        "returned_model": raw.get("returned_model"), "finish_reason": raw.get("finish_reason"),
        "usage": raw.get("usage"), "effective_request": raw.get("effective_request"),
        "estimate": estimate, "budget_before": spend, "budget_after": after,
        "new_round_actual_cny": after["actual_cny"] - spend["actual_cny"],
        "new_round_held_cny": after["held_cny"], "new_round_occupied_cny": after["occupied_cny"] - spend["occupied_cny"],
        "round_limit_cny": ROUND_LIMIT, "shared_limit_cny": SHARED_LIMIT, "paid_call_count": 1,
        "max_retries": 0,
    }
    dump(stage / "RUN_REPORT.json", run_report)
    print(json.dumps(run_report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    raise SystemExit(main())
