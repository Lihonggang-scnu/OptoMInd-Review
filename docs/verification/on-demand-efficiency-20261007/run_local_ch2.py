"""Opt-in Ch2 efficiency runner; default only prepares and verifies requests.

This experiment driver deliberately does not use the historical all-material
upper gate.  It guards each concrete access/owner request against the current
round cap immediately before the shared client reserves it, and leaves the
production payload/response contract unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

SOURCE = Path(r"F:\OptoMind-branch-backups\20261005-final-consolidation-204632\source")
ROOT = Path(r"F:\OptoMind-Review-2\outputs\on_demand_efficiency_20261007")
INPUT = ROOT / "CH2_INPUT_PAYLOAD.json"
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005\live\budget.sqlite")
KEY = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
ROUND_LIMIT = 25.0
BUDGET_RAISE_RECORD = Path(r"F:\OptoMind-Review-2\outputs\selector_local_depth_acceptance_20261007\BUDGET_RAISE_RECORD.json")
EXEMPT_UNCERTAIN = {
    "res-e50dd5e3f2d44902": 2.496072,
    "res-9cde6a09cf714811": 6.037356,
}

sys.path.insert(0, str(SOURCE))
from optomind_research.runtime.upgrade3 import outline_on_demand as on_demand
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.quality_capacity import load_quality_profile


def clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ledger_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(LEDGER) as con:
        rows = con.execute(
            "select reservation_id,call_id,amount_cny,actual_cny,status,returned_model,finish_reason,request_id,raw_response_sha256,usage_json from reservations order by rowid"
        ).fetchall()
    names = ("reservation_id", "call_id", "reserved_cny", "actual_cny", "status", "returned_model", "finish_reason", "request_id", "raw_response_sha256", "usage_json")
    return [dict(zip(names, row)) for row in rows]


def baseline_ids() -> set[str]:
    record = load(BUDGET_RAISE_RECORD)
    ids = {str(value) for value in record.get("historical_reservation_ids") or []}
    if not ids:
        raise RuntimeError("budget_raise_record_has_no_historical_ids")
    return ids


def round_spend() -> dict[str, Any]:
    rows = []
    for row in ledger_rows():
        reservation_id = str(row.get("reservation_id") or "")
        if reservation_id in baseline_ids():
            continue
        if reservation_id in EXEMPT_UNCERTAIN and str(row.get("status") or "").casefold() == "uncertain":
            continue
        rows.append(row)
    actual = sum(float(row.get("actual_cny") or 0.0) for row in rows)
    held = sum(float(row.get("reserved_cny") or 0.0) for row in rows if row.get("status") in {"reserved", "uncertain"})
    return {"rows": rows, "actual_cny": actual, "held_cny": held, "occupied_cny": actual + held,
            "exempt_uncertain": clone(EXEMPT_UNCERTAIN), "round_limit_cny": ROUND_LIMIT,
            "budget_raise_record": str(BUDGET_RAISE_RECORD),
            "exemption_rule": "only_matching_reservation_rows_with_status_uncertain_are_excluded"}


def source_signature() -> dict[str, Any]:
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
        diff = subprocess.check_output(["git", "diff", "--no-ext-diff", "--binary"], cwd=SOURCE)
        diff_sha = hashlib.sha256(diff).hexdigest()
    except Exception:
        head, diff_sha = "unknown", "unknown"
    return {"head": head, "diff_sha256": diff_sha}


class RoundGuardClient:
    """Guard and save each concrete request; no combined upper-bound gate."""

    def __init__(self, inner: Any, *, role: str, profile: Mapping[str, Any], counter: Any, out: Path):
        self.inner = inner
        self.role = role
        self.profile = dict(profile)
        self.counter = counter
        self.out = out
        self.sequence = 0

    def complete(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        # This experiment opts into the existing long-request streaming policy
        # without changing quality profiles or normal non-stream callers.
        if kwargs.get("stream"):
            kwargs.setdefault("timeout_seconds", 1800)
            kwargs.setdefault("stream_overall_timeout_seconds", 3600)
        estimate = strengthening.estimate_strengthening_request(
            messages, profile=self.profile, token_counter=self.counter,
            stream=bool(kwargs.get("stream", False)),
            json_mode=self.profile.get("json_mode"),
        )
        before = round_spend()
        projected = float(before["occupied_cny"]) + float(estimate["estimated_cost_cny"])
        self.sequence += 1
        stem = f"{self.sequence:02d}_{self.role}"
        guard = self.out / "CALL_GUARDS"
        precall = {
            "state": "precall",
            "role": self.role,
            "messages": clone(messages),
            "messages_sha256": digest(messages),
            "profile": clone(self.profile),
            "invoke_kwargs": {str(k): v for k, v in kwargs.items() if k not in {"client", "key_file"}},
            "estimate": estimate,
            "round_before": before,
            "projected_occupied_cny": projected,
            "source": source_signature(),
            "created_at_epoch": time.time(),
        }
        # Persist the exact preflight even when the guard refuses the call.
        dump(guard / f"{stem}_PRECHECK.json", precall)
        if projected > ROUND_LIMIT + 1e-9:
            raise RuntimeError(f"round_budget_guard:{self.role}:{projected:.6f}>{ROUND_LIMIT:.6f}")
        dump(guard / f"{stem}_PRECALL.json", precall)
        try:
            result = runtime.invoke_client(self.inner, messages, **kwargs)
        except Exception as exc:
            dump(guard / f"{stem}_EXCEPTION.json", {
                "state": "exception", "role": self.role, "error_type": type(exc).__name__,
                "error": str(exc), "precall_sha256": digest(precall),
            })
            raise
        dump(guard / f"{stem}_POSTCALL.json", {
            "state": "postcall", "role": self.role, "messages_sha256": digest(messages),
            "estimate": estimate, "round_after": round_spend(),
            "returned_model": result.get("returned_model") if isinstance(result, Mapping) else None,
            "finish_reason": result.get("finish_reason") if isinstance(result, Mapping) else None,
            "usage": result.get("usage") if isinstance(result, Mapping) else None,
            "created_at_epoch": time.time(),
        })
        return result


def transport_observer(out: Path, role: str):
    """Persist bounded safe transport phases; runtime already filters headers."""
    path = out / "TRANSPORT_EVENTS" / f"{role}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    def observe(event: Mapping[str, Any]) -> None:
        safe = {str(k): clone(v) for k, v in event.items() if str(k) not in {"headers", "authorization", "api_key"}}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")

    return observe


def prepare(out: Path) -> dict[str, Any]:
    payload = load(INPUT)
    catalog = on_demand.build_material_catalog(payload)
    access_profile = load_quality_profile("autonomous_outline")
    owner_profile = load_quality_profile("owner_revision")
    counter = planning.qwen_local_token_counter(TOKENIZER) if TOKENIZER.is_file() else None
    access_messages = on_demand.access_messages(payload, catalog)
    initial_trace = {
        "status": "initial_no_materials", "material_requests": [], "selected_materials": [],
        "trace": [], "resolved_count": 0, "access_ids": [], "catalog_sha256": catalog.get("catalog_sha256"),
    }
    owner_messages = on_demand.owner_messages(payload, catalog, initial_trace)
    access_estimate = strengthening.estimate_strengthening_request(access_messages, profile=access_profile, token_counter=counter, stream=True)
    owner_estimate = strengthening.estimate_strengthening_request(owner_messages, profile=owner_profile, token_counter=counter, stream=True)
    request = {
        "mode": "efficiency_on_demand", "payload": payload,
        "access_messages": access_messages, "owner_initial_messages": owner_messages,
        "access_request_sha256": digest(access_messages), "owner_initial_request_sha256": digest(owner_messages),
        "access_profile": access_profile, "owner_profile": owner_profile,
        "access_estimate": access_estimate, "owner_initial_estimate": owner_estimate,
        "round_policy": {"limit_cny": ROUND_LIMIT, "ledger_path": str(LEDGER), "budget_raise_record": str(BUDGET_RAISE_RECORD), "historical_reservation_ids_source": "BUDGET_RAISE_RECORD.historical_reservation_ids", "exempt_uncertain": clone(EXEMPT_UNCERTAIN), "exempt_only_when_status_uncertain": True},
        "source": source_signature(), "combined_uppergate_removed": True,
    }
    dump(out / "EFFICIENCY_REQUEST.json", request)
    dump(out / "ACCESS_REQUEST.json", {"messages": access_messages, "sha256": request["access_request_sha256"], "profile": access_profile})
    dump(out / "OWNER_INITIAL_REQUEST.json", {"messages": owner_messages, "sha256": request["owner_initial_request_sha256"], "profile": owner_profile, "trace": initial_trace})
    report = {
        "status": "prepared_no_paid_calls", "request_path": str(out / "EFFICIENCY_REQUEST.json"),
        "request_sha256": digest(request), "access_request_sha256": request["access_request_sha256"],
        "owner_initial_request_sha256": request["owner_initial_request_sha256"],
        "access_estimate": access_estimate, "owner_initial_estimate": owner_estimate,
        "round_before": round_spend(), "source": request["source"], "no_paid_calls": True,
    }
    dump(out / "EFFICIENCY_PREPARE_REPORT.json", report)
    return report


def run(out: Path, *, key_file: Path, ledger_path: Path) -> dict[str, Any]:
    if Path(ledger_path).resolve() != LEDGER.resolve():
        raise SystemExit("efficiency_driver_requires_shared_ledger")
    request = load(out / "EFFICIENCY_REQUEST.json")
    payload = request["payload"]
    catalog = on_demand.build_material_catalog(payload)
    if digest(on_demand.access_messages(payload, catalog)) != request["access_request_sha256"]:
        raise SystemExit("prepared_access_messages_changed:rerun_prepare")
    empty_trace = {
        "status": "initial_no_materials", "material_requests": [], "selected_materials": [],
        "trace": [], "resolved_count": 0, "access_ids": [], "catalog_sha256": catalog.get("catalog_sha256"),
    }
    current_owner_initial = on_demand.owner_messages(payload, catalog, empty_trace)
    if digest(current_owner_initial) != request["owner_initial_request_sha256"]:
        raise SystemExit("prepared_owner_messages_changed:rerun_prepare")
    access_profile = load_quality_profile("autonomous_outline")
    owner_profile = load_quality_profile("owner_revision")
    if request["access_profile"] != access_profile or request["owner_profile"] != owner_profile:
        raise SystemExit("prepared_profile_changed:rerun_prepare")
    if request.get("source") != source_signature():
        raise SystemExit("prepared_source_changed:rerun_prepare")
    counter = planning.qwen_local_token_counter(TOKENIZER) if TOKENIZER.is_file() else None
    ledger = runtime.GlobalBudgetLedger(path=LEDGER, limit_cny=125.0)
    access_inner = strengthening.make_strengthening_client(role="autonomous_outline", key_file=key_file, budget_ledger=ledger, raw_response_dir=out / "access_raw_responses", prompt_token_counter=counter)
    owner_inner = strengthening.make_strengthening_client(role="owner_revision", key_file=key_file, budget_ledger=ledger, raw_response_dir=out / "owner_raw_responses", prompt_token_counter=counter)
    access = RoundGuardClient(access_inner, role="autonomous_outline", profile=access_profile, counter=counter, out=out)
    owner = RoundGuardClient(owner_inner, role="owner_revision", profile=owner_profile, counter=counter, out=out)
    access_observer = transport_observer(out, "access")
    owner_observer = transport_observer(out, "owner")
    result = on_demand.run_on_demand_strengthening(
        payload, access_client=access, access_model=access_profile["model"], access_thinking_budget=access_profile["thinking_budget"], access_max_output_tokens=access_profile["max_output_tokens"],
        owner_client=owner, owner_model=owner_profile["model"], owner_thinking_budget=owner_profile["thinking_budget"], owner_max_output_tokens=owner_profile["max_output_tokens"],
        access_call_id=str(payload.get("call_id") or "efficiency-ch2") + ":efficiency:access", owner_call_id=str(payload.get("call_id") or "efficiency-ch2") + ":efficiency:owner",
        checkpoint_dir=out / "stages", resume=True, access_profile=access_profile, owner_profile=owner_profile,
        access_stream=True, access_stream_overall_timeout_seconds=3600, access_transport_observer=access_observer,
        owner_stream=True, owner_stream_overall_timeout_seconds=3600, owner_transport_observer=owner_observer,
    )
    dump(out / "RESULT.json", result)
    report = {"status": result.get("status"), "result_path": str(out / "RESULT.json"), "round_after": round_spend(), "no_retry": True}
    dump(out / "RUN_REPORT.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "live_ch2"))
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--key-file", default=str(KEY))
    parser.add_argument("--budget-ledger", default=str(LEDGER))
    args = parser.parse_args(argv)
    out = Path(args.output)
    report = run(out, key_file=Path(args.key_file), ledger_path=Path(args.budget_ledger)) if args.run else prepare(out)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
