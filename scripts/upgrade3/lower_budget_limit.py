"""Preview or explicitly lower an existing experiment ledger, without erasing spend.

Stop old running providers before lowering: already dispatched calls cannot be
cancelled by a database limit. New-runtime clients also recheck the durable cap.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sqlite3
import time


def lower_limit(path: str | Path, target: float, *, expected_limit: float | None = None,
                apply: bool = False) -> dict:
    if not math.isfinite(target) or target <= 0:
        raise ValueError("target_must_be_finite_positive")
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("existing_ledger_required:no_new_ledger_created")
    # mode=rw prevents racing deletion from silently creating an empty ledger.
    with sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        if row is None:
            raise ValueError("existing_finite_budget_limit_required")
        old = float(row[0])
        if not math.isfinite(old) or old <= 0:
            raise ValueError("existing_finite_budget_limit_required")
        if expected_limit is not None and (not math.isfinite(expected_limit) or abs(expected_limit-old)>1e-9):
            raise ValueError("expected_limit_mismatch")
        if target > old + 1e-9:
            raise ValueError("this_tool_only_lowers_limits")
        count = db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
        used = float(db.execute("SELECT COALESCE(SUM(CASE WHEN status='settled' THEN COALESCE(actual_cny,amount_cny) ELSE amount_cny END),0) FROM reservations").fetchone()[0])
        held = float(db.execute("SELECT COALESCE(SUM(amount_cny),0) FROM reservations WHERE status IN ('reserved','uncertain')").fetchone()[0])
        result = {"ledger_path":str(path), "old_limit_cny":old, "new_limit_cny":target,
                  "exposure_cny":used, "reserved_and_uncertain_cny":held,
                  "reservation_rows_preserved":count, "remaining_cny":max(0.0,target-used),
                  "already_over_new_limit":used>target+1e-9,
                  "status":"preview", "paid_calls":0,
                  "note":"Existing spend and all holds remain. Stop old running processes; in-flight calls are not cancelled."}
        if apply:
            if target < old - 1e-9:
                db.execute("CREATE TABLE IF NOT EXISTS budget_limit_changes (changed_at REAL NOT NULL, old_limit_cny REAL NOT NULL, new_limit_cny REAL NOT NULL, exposure_cny REAL NOT NULL, reason TEXT NOT NULL)")
                db.execute("INSERT INTO budget_limit_changes VALUES (?,?,?,?,?)",(time.time(),old,target,used,"explicit_user_lowering"))
                db.execute("UPDATE budget_meta SET value=? WHERE key='limit_cny'",(str(float(target)),))
                result["status"]="lowered"
            else:
                result["status"]="no_change"
            db.commit()
        else:
            db.rollback()
        return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ledger", required=True)
    p.add_argument("--lower-to", type=float, required=True)
    p.add_argument("--expected-limit", type=float)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--report")
    args=p.parse_args(argv)
    try:
        result=lower_limit(args.ledger,args.lower_to,expected_limit=args.expected_limit,apply=args.apply)
    except (ValueError,OSError,sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False)); return 2
    text=json.dumps(result,ensure_ascii=False,indent=2)
    print(text)
    if args.report:
        try:
            target=Path(args.report); target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text+"\n",encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"report_write_error":str(exc), "ledger_change_status":result["status"],
                              "new_limit_cny":result["new_limit_cny"]},ensure_ascii=False))
            return 3
    return 0

if __name__=="__main__":
    raise SystemExit(main())
