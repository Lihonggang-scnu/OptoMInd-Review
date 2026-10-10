"""Optional project/account caps using existing persistent budget ledgers.

Mappings use the experiment launcher v1 format for receipt-only recovery.
No key, key fingerprint, or provider call is handled by this module.
"""
from __future__ import annotations
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
from .module4.runtime import GlobalBudgetLedger, QwenTransportError

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))



def bind_json(path, value):
    """Publish an immutable binding atomically, including concurrent creators."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_json(path) != value:
                raise ValueError("account_budget_binding_conflict:no_budget_reset")
    finally:
        temporary.unlink(missing_ok=True)


def account_mapping_dir(path):
    path = Path(path).expanduser().resolve()
    return path.with_name(path.name + ".dual-calls")


def open_account_ledger(path, limit=None):
    """Adopt an explicit existing account cap or create one once; never reset."""
    path = Path(path).expanduser().resolve()
    marker = path.with_name(path.name + ".account-route.json")
    if limit is not None and (isinstance(limit, bool) or not math.isfinite(limit) or limit <= 0):
        raise ValueError("account_budget_must_be_finite_positive")
    if marker.exists() and not path.is_file():
        raise ValueError("account_ledger_missing:no_budget_reset")
    if path.exists():
        # Validate before the ledger constructor can initialize missing tables.
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
                reservations = db.execute("SELECT amount_cny, actual_cny, status FROM reservations").fetchall()
        except sqlite3.Error as exc:
            raise ValueError("account_ledger_invalid:no_budget_reset") from exc
        if row is None or not math.isfinite(float(row[0])) or float(row[0]) <= 0:
            raise ValueError("account_ledger_invalid:no_budget_reset")
        for amount, cost, status in reservations:
            if (status not in {"settled", "reserved", "uncertain"} or amount is None
                    or not math.isfinite(float(amount)) or float(amount) < 0
                    or (cost is not None and (not math.isfinite(float(cost)) or float(cost) < 0))):
                raise ValueError("account_ledger_invalid:no_budget_reset")
        stored = float(row[0])
        if limit is not None and limit != stored:
            raise ValueError("account_budget_limit_conflict:no_budget_reset")
        limit = stored
    elif limit is None:
        raise ValueError("new_account_ledger_requires_account_budget_limit")
    scope = {"schema_version": "dual_budget.account.v1", "ledger_path": str(path), "limit_cny": limit}
    bind_json(marker, scope)
    if not path.exists():
        path.touch(exist_ok=False)
    return GlobalBudgetLedger(limit_cny=limit, path=path)

class DualBudgetLedger:
    """Only reserve/settle delegation; the two existing ledgers enforce caps."""

    def __init__(self, project, total, mapping_dir):
        self.project, self.total = project, total
        self.mapping_dir = Path(mapping_dir)
        if not project.path or not total.path or Path(project.path).resolve() == Path(total.path).resolve():
            raise ValueError("dual_budget_requires_distinct_persistent_ledgers")

    def _path(self, pair_id):
        if not str(pair_id).startswith("dual-") or any(c not in "0123456789abcdef-uald" for c in str(pair_id)):
            raise ValueError("invalid_dual_reservation_id")
        return self.mapping_dir / (pair_id + ".json")

    def _check(self, record):
        if (record["project_path"] != str(Path(self.project.path).resolve()) or
                record["total_path"] != str(Path(self.total.path).resolve())):
            raise ValueError("dual_budget_mapping_ledger_changed")

    def reserve(self, amount_cny, call_id):
        pair_id = "dual-" + uuid.uuid4().hex
        path = self._path(pair_id)
        record = {"pair_id": pair_id, "call_id": call_id, "amount_cny": amount_cny,
                  "project_path": str(Path(self.project.path).resolve()),
                  "total_path": str(Path(self.total.path).resolve()),
                  "project_reservation_id": None, "total_reservation_id": None,
                  "state": "reserving"}
        # A durable pre-reservation record plus unique ledger call tags allow
        # an interrupted mapping write to be diagnosed without touching HTTP.
        write_json(path, record)
        try:
            row = self.project.reserve(amount_cny, f"{call_id}:{pair_id}:project")
        except Exception as exc:
            record.update(state="project_reservation_refused", refusal_type=type(exc).__name__,
                          refusal_reason="project_refused_before_http")
            write_json(path, record)
            raise
        record["project_reservation_id"] = row["reservation_id"]
        write_json(path, record)  # Mapping failure leaves the durable hold.
        try:
            row = self.total.reserve(amount_cny, f"{call_id}:{pair_id}:total")
        except Exception as exc:
            record.update(state="total_reservation_refused", refusal_type=type(exc).__name__,
                          refusal_reason="total_refused_before_http",
                          settlement={"actual_cny": 0.0, "uncertain": False,
                                      "telemetry": {"provider_error_code": "dual_total_refused_before_http"}})
            write_json(path, record)
            # Neither reserve has returned to QwenDirectClient; no HTTP was
            # possible. Only this definite pre-dispatch project hold is zeroed.
            self._finish_settlement(record)
            raise
        record.update(total_reservation_id=row["reservation_id"], state="ready_for_http")
        write_json(path, record)  # Both reservations and their map precede HTTP.
        return {"reservation_id": pair_id, "call_id": call_id,
                "amount_cny": amount_cny, "status": "reserved"}

    @staticmethod
    def _settle_one(ledger, reservation_id, receipt):
        row = next((r for r in ledger.as_dict()["reservations"] if r["reservation_id"] == reservation_id), None)
        if row is None:
            raise ValueError("dual_unknown_reservation")
        desired = "uncertain" if receipt["uncertain"] else "settled"
        if row["status"] in {"settled", "uncertain"}:
            actual = receipt["actual_cny"]
            same_amount = (row.get("actual_cny") is None and actual is None) or (
                row.get("actual_cny") is not None and actual is not None and
                abs(float(row["actual_cny"]) - float(actual)) < 1e-9)
            if row["status"] != desired or not same_amount:
                raise ValueError("dual_conflicting_settlement")
            return  # Same recorded settlement is idempotent, never a waiver.
        ledger.settle(reservation_id, receipt["actual_cny"], uncertain=receipt["uncertain"],
                      telemetry=receipt["telemetry"])

    def _finish_settlement(self, record):
        self._check(record)
        errors = []
        for name, ledger in (("project", self.project), ("total", self.total)):
            reservation_id = record.get(name + "_reservation_id")
            if reservation_id:
                try:
                    self._settle_one(ledger, reservation_id, record["settlement"])
                except Exception as exc:
                    errors.append({"ledger": name, "error_type": type(exc).__name__,
                                   "reason": "settlement_failed_hold_retained"})
        record.update(state="settlement_incomplete" if errors else
                      "uncertain" if record["settlement"]["uncertain"] else "settled",
                      settlement_errors=errors)
        write_json(self._path(record["pair_id"]), record)
        if errors:
            raise QwenTransportError("dual_settlement_incomplete_hold_retained", transient=False)

    def settle(self, reservation_id, actual_cny, *, uncertain=False, telemetry=None):
        record = read_json(self._path(reservation_id))
        self._check(record)
        receipt = {"actual_cny": actual_cny, "uncertain": bool(uncertain),
                   "telemetry": dict(telemetry or {})}
        if record.get("settlement") is not None and record["settlement"] != receipt:
            raise QwenTransportError("dual_conflicting_settlement", transient=False)
        record.update(settlement=receipt, state="settling")
        write_json(self._path(reservation_id), record)  # Save evidence before either side settles.
        self._finish_settlement(record)

    def recover_settlements(self):
        """Finish only saved receipts; unknown/reserving holds stay consumed."""
        for path in sorted(self.mapping_dir.glob("dual-*.json")):
            record = read_json(path)
            # Account-level mapping directories can contain other projects.
            if record.get("project_path") != str(Path(self.project.path).resolve()):
                continue
            self._check(record)
            if record.get("settlement") and record["state"] not in {"settled", "uncertain"}:
                self._finish_settlement(record)

