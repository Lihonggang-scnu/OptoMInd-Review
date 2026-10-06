"""Prepare, and optionally run, the first selector-derived on-demand group.

The default action is offline preparation.  ``--run`` is an explicit paid
boundary and is intentionally limited to group 1 (Ch3 U3_2/U3_5); the caller
reviews its result before preparing any later logical group.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import shutil
from pathlib import Path
from typing import Any


SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
SELECTOR_OUT = Path(r"F:\OptoMind-Review-2\outputs\outline_selection_20261006")
OUT = SELECTOR_OUT / "on_demand_group1_ch3"
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
ROUND_BASELINE = Path(r"F:\OptoMind-Review-2\outputs\outline_directory_repair_comparison_20261006\LIVE_BUDGET_BASELINE.json")
GROUP_ID = "selection-66bb7ee2a906d701"
CHAPTER_ID = "Ch3"
ROUND_LIMIT = 40.0
SHARED_LIMIT = 85.0


def copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()


def source_diff_sha() -> str:
    return hashlib.sha256(subprocess.check_output(["git", "diff", "--no-ext-diff", "--binary"], cwd=SOURCE)).hexdigest()


def ledger_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,usage_json,request_metadata_json from reservations order by rowid"
        ).fetchall()
    names = ("reservation_id", "call_id", "reserved_cny", "actual_cny", "status", "returned_model", "finish_reason", "request_id", "raw_response_sha256", "usage_json", "request_metadata_json")
    return [dict(zip(names, row)) for row in rows]


def spend(rows: list[dict[str, Any]], baseline_ids: set[str]) -> dict[str, Any]:
    selected = [row for row in rows if str(row.get("reservation_id")) not in baseline_ids]
    actual = sum(float(row.get("actual_cny") or 0) for row in selected)
    held = sum(float(row.get("reserved_cny") or 0) for row in selected if row.get("status") in {"reserved", "uncertain"})
    return {"rows": selected, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held}


class AuditedClient:
    """Persist each real invoke's exact request and round guard before dispatch."""

    def __init__(self, inner: Any, *, role: str, profile: dict[str, Any], counter: Any, round_ids: set[str]):
        self.inner = inner
        self.role = role
        self.profile = profile
        self.counter = counter
        self.round_ids = round_ids
        self.sequence = 0

    def complete(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening

        estimate = strengthening.estimate_strengthening_request(
            messages, profile=self.profile, token_counter=self.counter,
        )
        before = spend(ledger_rows(), self.round_ids)
        if before["occupied_cny"] + float(estimate.get("estimated_cost_cny") or 0) > ROUND_LIMIT + 1e-9:
            raise RuntimeError(f"round_budget_guard:{self.role}:{before['occupied_cny']}+{estimate.get('estimated_cost_cny')}")
        self.sequence += 1
        stem = f"{self.sequence:02d}_{self.role}"
        precall = {
            "state": "precall",
            "role": self.role,
            "messages": copy(messages),
            "messages_sha256": digest(messages),
            "profile": copy(self.profile),
            "invoke_kwargs": {str(key): value for key, value in kwargs.items() if key not in {"client", "key_file"}},
            "estimate": estimate,
            "round_before": before,
            "round_limit_cny": ROUND_LIMIT,
            "source_head": source_head(),
            "source_diff_sha256": source_diff_sha(),
            "created_at_epoch": time.time(),
        }
        dump(OUT / "CALL_GUARDS" / f"{stem}_PRECALL.json", precall)
        try:
            result = self.inner(messages, **kwargs)
        except Exception as exc:
            dump(OUT / "CALL_GUARDS" / f"{stem}_EXCEPTION.json", {
                "role": self.role, "error_type": type(exc).__name__, "error": str(exc),
                "precall_path": str(OUT / "CALL_GUARDS" / f"{stem}_PRECALL.json"),
            })
            raise
        after = spend(ledger_rows(), self.round_ids)
        dump(OUT / "CALL_GUARDS" / f"{stem}_POSTCALL.json", {
            "state": "postcall", "role": self.role, "messages_sha256": digest(messages),
            "estimate": estimate, "round_after": after,
            "returned_model": result.get("returned_model") if isinstance(result, dict) else None,
            "finish_reason": result.get("finish_reason") if isinstance(result, dict) else None,
            "usage": result.get("usage") if isinstance(result, dict) else None,
            "created_at_epoch": time.time(),
        })
        return result


def snapshot_source_allowlist() -> dict[str, Any]:
    """Copy only the runtime files used by this experiment, including new files."""

    allowlist = [
        "optomind_research/runtime/upgrade3/outline_selection.py",
        "optomind_research/runtime/upgrade3/outline_on_demand.py",
        "optomind_research/runtime/upgrade3/outline_strengthening.py",
        "scripts/upgrade3/outline_strengthening.py",
        "config/outline_revision/quality.json",
        "tests/upgrade3/test_outline_selection.py",
        "tests/upgrade3/test_outline_strengthening.py",
    ]
    root = OUT / "source_snapshot"
    entries = []
    for relative in allowlist:
        source = SOURCE / relative
        if not source.is_file():
            raise SystemExit(f"source_snapshot_file_missing:{relative}")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append({
            "path": relative,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "bytes": source.stat().st_size,
        })
    manifest = {
        "source_head": source_head(),
        "source_diff_sha256": source_diff_sha(),
        "allowlist": allowlist,
        "files": entries,
        "includes_untracked_selection_module": True,
        "no_credentials_or_budget_db": True,
    }
    dump(OUT / "SOURCE_SNAPSHOT_MANIFEST.json", manifest)
    return manifest


def selection_payload() -> tuple[dict[str, Any], dict[str, Any]]:
    from optomind_research.runtime.upgrade3 import outline_selection as selection

    full = load(SELECTOR_OUT / "FULL_CHAPTER_PAYLOADS.json")
    result = load(SELECTOR_OUT / "live_attempt1" / "SELECTION_RESULT.json")
    projected = selection.selection_to_on_demand_payloads(full, result)
    matches = [row for row in projected if row.get("group_id") == GROUP_ID and row.get("chapter_id") == CHAPTER_ID]
    if len(matches) != 1:
        raise SystemExit(f"group1_projection_count:{len(matches)}")
    return copy(matches[0]["payload"]), result


def prepare() -> dict[str, Any]:
    if source_head() != "a0b645e096a148382196b5aadd3dcf36c43c69d6":
        raise SystemExit("source_head_changed_since_selection")
    for path in (KEY, LEDGER, ROUND_BASELINE, SELECTOR_OUT / "FULL_CHAPTER_PAYLOADS.json", SELECTOR_OUT / "live_attempt1" / "SELECTION_RESULT.json"):
        if not path.is_file():
            raise SystemExit(f"required_file_missing:{path}")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile

    payload, selector_result = selection_payload()
    expected_ids = {"U3_2", "U3_5"}
    if set(payload.get("modifiable_unit_ids") or []) != expected_ids:
        raise SystemExit("group1_editable_scope_changed")
    if payload.get("selection_context", {}).get("group_id") != GROUP_ID:
        raise SystemExit("group1_selection_context_changed")
    catalog = on_demand.build_material_catalog(payload)
    access_messages = on_demand.access_messages(payload, catalog)
    upper_trace = on_demand.full_catalog_trace(catalog)
    owner_messages = on_demand.owner_messages(payload, catalog, upper_trace)
    access_profile = load_quality_profile("autonomous_outline")
    owner_profile = load_quality_profile("owner_revision")
    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter)
    owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=owner_profile, token_counter=counter)
    report = {
        "status": "prepared_no_paid_calls",
        "group_id": GROUP_ID,
        "chapter_id": CHAPTER_ID,
        "logical_group_count": 4,
        "projected_request_count_total": 7,
        "modifiable_unit_ids": sorted(payload.get("modifiable_unit_ids") or []),
        "read_only_unit_count": len(payload.get("read_only_unit_ids") or []),
        "payload_sha256": digest(payload),
        "selector_result_sha256": digest(selector_result),
        "access_messages_sha256": digest(access_messages),
        "owner_upper_bound_messages_sha256": digest(owner_messages),
        "access_profile": access_profile,
        "owner_profile": owner_profile,
        "access_estimate": access_estimate,
        "owner_upper_bound_estimate": owner_estimate,
        "owner_upper_bound_trace_status": upper_trace.get("status"),
        "owner_upper_bound_record_count": len(upper_trace.get("selected_materials") or []),
        "source_head": source_head(),
        "source_diff_sha256": source_diff_sha(),
        "round_baseline_path": str(ROUND_BASELINE),
        "round_limit_cny": ROUND_LIMIT,
        "shared_limit_cny": SHARED_LIMIT,
        "no_root_quality_review_in_input": True,
        "no_prior_owner_answer_in_input": True,
        "downstream_owner_calls": 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    dump(OUT / "INPUT_PAYLOAD.json", payload)
    dump(OUT / "CATALOG.json", on_demand.public_catalog(catalog))
    dump(OUT / "ACCESS_MESSAGES.json", {"messages": access_messages, "sha256": digest(access_messages), "profile": access_profile})
    dump(OUT / "OWNER_UPPER_BOUND_MESSAGES.json", {"messages": owner_messages, "sha256": digest(owner_messages), "profile": owner_profile, "trace": upper_trace})
    dump(OUT / "PREPARE_REPORT.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


def run() -> dict[str, Any]:
    if not (OUT / "PREPARE_REPORT.json").is_file():
        prepare()
    if (OUT / "RESULT.json").is_file():
        raise SystemExit("result_already_exists_no_recall")
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
    from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
    from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
    from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile
    from optomind_research.runtime.upgrade3.module4 import runtime

    payload = load(OUT / "INPUT_PAYLOAD.json")
    full_chapter_payloads = load(SELECTOR_OUT / "FULL_CHAPTER_PAYLOADS.json")
    prepared_access = load(OUT / "ACCESS_MESSAGES.json")
    prepared_owner = load(OUT / "OWNER_UPPER_BOUND_MESSAGES.json")
    access_profile = load_quality_profile("autonomous_outline")
    owner_profile = load_quality_profile("owner_revision")
    catalog = on_demand.build_material_catalog(payload)
    access_messages = on_demand.access_messages(payload, catalog)
    upper_trace = on_demand.full_catalog_trace(catalog)
    owner_messages = on_demand.owner_messages(payload, catalog, upper_trace)
    if digest(access_messages) != prepared_access.get("sha256") or digest(owner_messages) != prepared_owner.get("sha256"):
        raise SystemExit("prepared_messages_changed_rerun_prepare")
    tokenizer = TOKENIZER if TOKENIZER.is_file() else None
    counter = planning.qwen_local_token_counter(tokenizer) if tokenizer else None
    access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter)
    owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=owner_profile, token_counter=counter)
    old_baseline = load(ROUND_BASELINE)
    baseline_ids = {str(value) for value in old_baseline.get("reservation_ids_before") or []}
    before = spend(ledger_rows(), baseline_ids)
    worst = before["occupied_cny"] + float(access_estimate.get("estimated_cost_cny") or 0) + 2.0 * float(owner_estimate.get("estimated_cost_cny") or 0)
    if worst > ROUND_LIMIT + 1e-9:
        raise SystemExit(f"round_budget_worst_case_exceeded:{worst}")
    with sqlite3.connect(LEDGER) as con:
        metadata = {str(row[0]): str(row[1]) for row in con.execute("select key,value from budget_meta")}
    if float(metadata.get("limit_cny", "nan")) != SHARED_LIMIT:
        raise SystemExit("shared_budget_limit_changed")
    snapshot_source_allowlist()
    dump(OUT / "LEDGER_BASELINE.json", {"reservation_ids_before": sorted({str(row.get("reservation_id")) for row in ledger_rows()}), "round_baseline_ids": sorted(baseline_ids), "round_before": before, "metadata": metadata})
    dump(OUT / "WIRE_REQUEST.json", {
        "group_id": GROUP_ID, "chapter_id": CHAPTER_ID, "access_messages": access_messages,
        "owner_upper_bound_messages": owner_messages, "access_profile": access_profile, "owner_profile": owner_profile,
        "access_estimate": access_estimate, "owner_upper_bound_estimate": owner_estimate,
        "round_before": before, "worst_case_round_occupied_cny": worst,
        "max_retries": 0, "source_head": source_head(), "source_diff_sha256": source_diff_sha(),
    })
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=SHARED_LIMIT)
    access_client = AuditedClient(
        strengthening.make_strengthening_client(role="autonomous_outline", key_file=KEY, budget_ledger=ledger, raw_response_dir=OUT / "access_raw_responses", prompt_token_counter=counter),
        role="autonomous_outline", profile=access_profile, counter=counter, round_ids=baseline_ids,
    )
    owner_client = AuditedClient(
        strengthening.make_strengthening_client(role="owner_revision", key_file=KEY, budget_ledger=ledger, raw_response_dir=OUT / "owner_raw_responses", prompt_token_counter=counter),
        role="owner_revision", profile=owner_profile, counter=counter, round_ids=baseline_ids,
    )
    result = on_demand.run_on_demand_strengthening(
        payload,
        access_client=access_client, access_model=access_profile["model"], access_thinking_budget=access_profile["thinking_budget"], access_max_output_tokens=access_profile["max_output_tokens"],
        owner_client=owner_client, owner_model=owner_profile["model"], owner_thinking_budget=owner_profile["thinking_budget"], owner_max_output_tokens=owner_profile["max_output_tokens"],
        access_call_id=f"outline-selection:{GROUP_ID}:{CHAPTER_ID}:access",
        owner_call_id=f"outline-selection:{GROUP_ID}:{CHAPTER_ID}:owner",
        checkpoint_dir=OUT / "stages", resume=True, access_profile=access_profile, owner_profile=owner_profile,
    )
    dump(OUT / "RESULT.json", result)
    if isinstance(result.get("on_demand_strengthening"), dict):
        detail = result["on_demand_strengthening"]
        dump(OUT / "EFFECTIVE_REQUESTS.json", {
            "access": detail.get("access"), "owner_initial_messages": (detail.get("owner_initial") or {}).get("messages"),
            "owner_continuation_messages": (detail.get("owner_continuation") or {}).get("messages") if detail.get("owner_continuation") else None,
        })
    if result.get("status") in {"updated", "no_change"}:
        latest = strengthening.merge_owner_result_into_chapter_payloads(full_chapter_payloads, payload, result)
        all_units = [
            row.get("unit_id") or row.get("id")
            for chapter in latest.values()
            for row in (chapter.get("chapter_plan") or {}).get("units") or []
            if isinstance(row, dict)
        ]
        original_units = [
            row.get("unit_id") or row.get("id")
            for chapter in full_chapter_payloads.values()
            for row in (chapter.get("chapter_plan") or {}).get("units") or []
            if isinstance(row, dict)
        ]
        if len(all_units) != 29 or set(all_units) - set(original_units):
            raise SystemExit("merged_total_plan_unit_integrity_failed")
        unchanged_ids = set(original_units) - set(payload.get("modifiable_unit_ids") or [])
        original_by_id = {row.get("unit_id") or row.get("id"): row for chapter in full_chapter_payloads.values() for row in (chapter.get("chapter_plan") or {}).get("units") or [] if isinstance(row, dict)}
        merged_by_id = {row.get("unit_id") or row.get("id"): row for chapter in latest.values() for row in (chapter.get("chapter_plan") or {}).get("units") or [] if isinstance(row, dict)}
        if any(original_by_id.get(unit_id) != merged_by_id.get(unit_id) for unit_id in unchanged_ids):
            raise SystemExit("unselected_total_plan_changed")
        dump(OUT / "CURRENT_CHAPTER_PAYLOADS.json", latest)
        dump(OUT / "MERGE_INTEGRITY.json", {
            "status": "verified", "chapter_count": len(latest), "unit_count": len(all_units),
            "original_unit_count": len(original_units), "unchanged_unit_count": len(unchanged_ids),
            "base_payload_path": str(SELECTOR_OUT / "FULL_CHAPTER_PAYLOADS.json"),
            "complete_total_plan_preserved": True,
        })
    after = spend(ledger_rows(), baseline_ids)
    report = {"status": result.get("status"), "result_path": str(OUT / "RESULT.json"), "before_round": before, "after_round": after, "new_round_actual_cny": after["actual_cny"] - before["actual_cny"], "new_round_held_cny": after["held_cny"], "new_round_occupied_cny": after["occupied_cny"] - before["occupied_cny"], "round_remaining_cny": ROUND_LIMIT - after["occupied_cny"], "owner_call_count": (result.get("on_demand_strengthening") or {}).get("owner_call_count"), "source_head": source_head(), "source_diff_sha256": source_diff_sha()}
    dump(OUT / "RUN_REPORT.json", report)
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == "__main__":
    if str(SOURCE) not in sys.path:
        sys.path.insert(0, str(SOURCE))
    prepare()
    if "--run" in sys.argv[1:]:
        run()
