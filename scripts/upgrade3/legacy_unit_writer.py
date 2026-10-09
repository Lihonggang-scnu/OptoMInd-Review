"""Current complete BODY materials -> legacy unit writer -> existing assembly.

Default preview is offline and saves every exact request. --run explicitly
uses a dedicated persistent CNY ledger. --retry-failed authorizes a new charged
attempt; otherwise interrupted, uncertain and incomplete attempts stay pending.
No GUIDE conversion, historic manuscript import, automatic model switch, or
previous-body prefix is used by this route.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from optomind_research.runtime.upgrade3.legacy_unit_route import run_legacy_units, _hash, _read, _write
from scripts.upgrade3.writer_candidates import make_live_factory, tokenizer_counter


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="Current lossless FULL_BODY_INPUT.json")
    p.add_argument("--output-dir", required=True, help="Dedicated preview/run/resume directory")
    p.add_argument("--model", choices=("qwen3.5-plus",), default="qwen3.5-plus")
    p.add_argument("--output-tokens", type=int, default=32768)
    p.add_argument("--thinking-budget", type=int, default=8192)
    p.add_argument("--budget-limit", type=float, default=30.0,
                   help="Absolute lifetime CNY cap of this separate experiment, never an increment")
    p.add_argument("--ledger", help="Explicit dedicated SQLite ledger, required with --run; reuse for all resumes")
    p.add_argument("--run", action="store_true", help="Explicitly allow live provider calls")
    p.add_argument("--retry-failed", action="store_true", help="Permit a new charged attempt for pending units")
    p.add_argument("--key-file", help="Local credential file; not read in preview, never copied")
    p.add_argument("--tokenizer", help="Existing local tokenizer.json, no downloads")
    return p


def _ledger_snapshot(ledger: Path, marker: Path, scope: dict) -> dict:
    """Read-only receipt validation: missing/corrupt state is never a new cap."""
    if not ledger.is_file():
        raise ValueError("legacy_ledger_missing:no_budget_reset")
    if not marker.is_file() or _read(marker) != scope:
        raise ValueError("legacy_ledger_scope_conflict:retain_existing_budget_and_reconcile")
    try:
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if row is None or not math.isfinite(float(row[0])) or float(row[0]) != scope["budget_limit_cny"]:
                raise ValueError("legacy_stored_budget_limit_changed")
            rows = db.execute("SELECT reservation_id, amount_cny, actual_cny, status FROM reservations").fetchall()
    except sqlite3.Error as exc:
        raise ValueError("legacy_ledger_invalid_sqlite:no_budget_reset") from exc
    actual = held = used = 0.0
    reservations = []
    for reservation_id, amount, cost, status in rows:
        if status not in {"settled", "reserved", "uncertain"}:
            raise ValueError("legacy_invalid_reservation_status")
        if amount is None or not math.isfinite(float(amount)) or float(amount) < 0 or (cost is not None and
                (not math.isfinite(float(cost)) or float(cost) < 0)):
            raise ValueError("legacy_invalid_reservation_amount")
        actual += float(cost or 0)
        held += float(amount) if status in {"reserved", "uncertain"} else 0
        used += float(cost if cost is not None else amount) if status == "settled" else float(amount)
        reservations.append({"reservation_id": reservation_id, "amount_cny": amount,
                             "actual_cny": cost, "status": status})
    if not all(math.isfinite(value) for value in (actual, held, used)):
        raise ValueError("legacy_invalid_budget_total")
    return {"ledger_path": str(ledger), "limit_cny": scope["budget_limit_cny"],
            "actual_cny": actual, "reserved_cny": held, "remaining_cny": max(0, scope["budget_limit_cny"] - used),
            "reservations": reservations}


def _dedicated_factory(args, book, counter):
    if not math.isfinite(args.budget_limit) or args.budget_limit <= 0 or args.budget_limit > 30:
        raise ValueError("legacy_budget_must_be_finite_positive_and_at_most_30_cny")
    if not args.ledger:
        raise ValueError("legacy_run_requires_explicit_dedicated_ledger:reuse_for_every_resume")
    ledger = Path(args.ledger).expanduser().resolve()
    marker = ledger.with_name(ledger.name + ".legacy-route.json")
    scope = {"route": "legacy_unit_writer", "ledger_path": str(ledger),
             "book_sha256": _hash(book), "budget_limit_cny": args.budget_limit}
    # The same ledger remains valid when code fixes require a new output root.
    # Never adopt, reset, migrate or raise an earlier experiment's budget.
    if ledger.exists() and not marker.exists():
        raise ValueError("legacy_existing_ledger_not_owned_by_this_run:retain_existing_budget_and_reconcile")
    if marker.exists() and _read(marker) != scope:
        raise ValueError("legacy_ledger_scope_conflict:retain_existing_budget_and_reconcile")
    if marker.exists() and not ledger.is_file():
        raise ValueError("legacy_ledger_missing:no_budget_reset")
    if not ledger.exists():
        from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.touch(exist_ok=False)  # Concurrent first creators fail closed.
        GlobalBudgetLedger(limit_cny=args.budget_limit, path=ledger)
        _write(marker, scope)
    _ledger_snapshot(ledger, marker, scope)
    args.budget_ledger = str(ledger)
    args.responses = None
    args.allow_max = False
    base_factory = make_live_factory(args, token_counter=counter)

    def snapshot():
        return _ledger_snapshot(ledger, marker, scope)

    def guarded_factory(role, attempt, profile):
        prior = snapshot()
        if any(row["status"] in {"reserved", "uncertain"} or row["actual_cny"] is None
               for row in prior["reservations"]):
            from optomind_research.runtime.upgrade3.module4.runtime import QwenTransportError
            raise QwenTransportError("legacy_unsettled_budget_requires_receipt_reconciliation",
                                     record={"actual_cny": prior["actual_cny"],
                                             "reserved_cny": prior["reserved_cny"]})
        return base_factory(role, attempt, profile)

    guarded_factory.execution_mode = "live"
    guarded_factory.ledger_snapshot = snapshot
    return guarded_factory


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    book = _read(Path(args.input).expanduser().resolve())
    counter, tokenizer = tokenizer_counter(args.tokenizer)
    # make_live_factory is lazy. Even --run does not read credentials until a
    # non-cached, capacity-safe request actually needs to be dispatched.
    factory = _dedicated_factory(args, book, counter) if args.run else None
    report = run_legacy_units(
        book, output_dir=args.output_dir, model=args.model,
        output_tokens=args.output_tokens, thinking_budget=args.thinking_budget,
        budget_limit=args.budget_limit, run=args.run, retry_failed=args.retry_failed,
        client_factory=factory, token_counter=counter)
    _write(Path(args.output_dir).expanduser().resolve() / "TOKENIZER.json", tokenizer)
    print(json.dumps({key: report[key] for key in
          ("status", "model", "original_units", "complete_units", "model_calls",
           "estimated_all_units_cny", "estimated_all_units_within_budget", "capacity_blocked_units")},
          ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"preview", "written_pending_review"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
