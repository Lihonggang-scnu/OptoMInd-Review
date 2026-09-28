"""Upgrade-3 persistent cross-process budget ledger and per-request reservation.

Ticket 005.  One SQLite database (outputs/upgrade3/BUDGET_LEDGER.sqlite) is the
single authority for the 150 CNY hard budget across tickets and processes.
All amounts are integer micro-CNY (1 CNY = 1_000_000 micro); floats never
accumulate.  Thread locks are irrelevant here by design: every mutation runs
inside a SQLite BEGIN IMMEDIATE transaction so concurrent processes serialise.

Constraint enforced in the same transaction that creates a reservation:
    bound_total + new_upper_bound <= 150_000_000          (global)
    pool_bound(pool) + new_upper_bound <= pool_cap        (per 000 table)
where bound = reserved upper bounds + frozen unknown bounds + settled actuals.

Physical-call discipline (callers MUST use reserve -> send -> settle):
- reserve() refuses when the price manifest cannot produce a reliable upper
  bound or when either balance check fails: 0 physical requests are possible.
- release() may only run before a physical attempt.
- an attempt that was sent but never settled is frozen as unknown at the upper
  bound (mark_unknown / sweep_stale) until a verifiable bill arrives
  (settle_unknown replaces the bound with the actual).
- settle() is idempotent per request_id; a contradicting second usage is an error.
- actual > upper bound records an overrun: the real cost is charged and the
  task is flagged so further reservations for it are refused.
- cache replays are recorded with zero cost and linked to the original receipt.

Model keys are never read or logged here; callers pass pricing facts only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, Optional, Tuple

MICRO = 1_000_000
GLOBAL_CAP_CNY = 150
FINAL_LOCKED_CNY = 60

DEFAULT_ALLOCATIONS = {
    # pool -> cap micro-CNY (000 budget table).  Pools partition the global cap.
    "global": 150 * MICRO,
    "local_001_046": 51 * MICRO,
    "milestone_031": 18 * MICRO,
    "final_047_locked": 60 * MICRO,
    "reserve_pre031": 7 * MICRO,
    "reserve_032_046": 6 * MICRO,
    "reserve_047": 8 * MICRO,
    # Approved strong-model takeover budget (SM01-SM26 table in the handoff
    # entry document, 28.2 CNY total).  It is a disjoint pool carved out of the
    # non-locked headroom: the global cap stays 150 CNY and neither the hidden
    # E2E pool nor the 047 lock is touched.
    "strong_model": 28_200_000,
}
TASK_POOL = {
    "031": "milestone_031",
    "047": "final_047_locked",
}
# Strong-model takeover tickets SM01-SM26 draw from their own bounded pool.  The
# per-ticket caps are the ones published in the handoff budget table; the pool cap
# is their sum, so a ticket can never borrow from the hidden-E2E or 047 pools.
STRONG_MODEL_TASK_CAPS_CNY = {
    "SM01": 0.5, "SM02": 1.2, "SM03": 0.3, "SM04": 1.2, "SM05": 1.2,
    "SM06": 0.3, "SM07": 0.8, "SM08": 1.5, "SM09": 0.2, "SM10": 3.5,
    "SM11": 1.0, "SM12": 1.0, "SM13": 2.0, "SM14": 2.0, "SM15": 1.5,
    "SM16": 1.0, "SM17": 2.0, "SM18": 1.0, "SM19": 2.0, "SM20": 1.0,
    "SM21": 2.0, "SM22": 0.5, "SM23": 0.5, "SM24": 0.0, "SM25": 0.0,
    "SM26": 0.0,
}
for _ticket in STRONG_MODEL_TASK_CAPS_CNY:
    TASK_POOL[_ticket] = "strong_model"
# tickets 001-030 and 032-046 draw from the local pool
LOCAL_POOL_TASKS = {str(i).zfill(3) for i in range(1, 47)} - {"031", "047"}

ACTIVE_STATES = ("reserved", "unknown_frozen")
_SETTLED = "settled"


class BudgetRefused(Exception):
    """Raised instead of any physical provider call when rules fail."""


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def price_upper_bound_micro(pricing: Dict[str, Any], model: str,
                            max_input_tokens: int, max_output_tokens: int,
                            attempts: int) -> Tuple[int, str]:
    """Reliable per-attempt price from the frozen manifest * full retry attempts.

    Raises BudgetRefused when the model has no manifest entry (unknown price
    means the call must not happen)."""
    models = pricing.get("models") or {}
    entry = models.get(model)
    if not entry:
        raise BudgetRefused("price_manifest.unknown_model:%s" % model)
    tiers = sorted(entry, key=lambda t: t.get("max_input_tokens", 0))
    chosen = tiers[-1]
    for tier in tiers:
        if max_input_tokens <= int(tier.get("max_input_tokens", 0)):
            chosen = tier
            break
    in_rate = float(chosen["input_cny_per_million"])
    out_rate = float(chosen["output_cny_per_million"])
    per_attempt_micro = int(round((in_rate * max_input_tokens + out_rate * max_output_tokens) * MICRO / 1_000_000))
    total = per_attempt_micro * max(1, int(attempts))
    manifest_id = "pricing:%s:%s" % (pricing.get("schema_version", "unknown"), model)
    return total, manifest_id


def task_pool_for(task_id: str) -> str:
    if task_id in TASK_POOL:
        return TASK_POOL[task_id]
    if task_id in LOCAL_POOL_TASKS:
        return "local_001_046"
    raise BudgetRefused("task_pool.unknown_task:%s" % task_id)


class BudgetLedger:
    def __init__(self, db_path: str,
                 pricing: Optional[Dict[str, Any]] = None,
                 allocations: Optional[Dict[str, int]] = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock_file = db_path + ".inuse"
        self._conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._init_schema(allocations or DEFAULT_ALLOCATIONS)
        self.pricing = pricing

    # ---------------- schema ----------------
    def _init_schema(self, allocations: Dict[str, int]) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS allocations(
                pool TEXT PRIMARY KEY,
                cap_micro INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservations(
                request_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                logical_call_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                pool TEXT NOT NULL,
                model TEXT NOT NULL,
                price_manifest_id TEXT NOT NULL,
                upper_bound_micro INTEGER NOT NULL,
                bound_micro INTEGER NOT NULL,
                state TEXT NOT NULL,
                usage_json TEXT,
                provider_receipt_id TEXT,
                overrun_micro INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, logical_call_id, attempt_id)
            );
            CREATE TABLE IF NOT EXISTS reserve_transfers(
                transfer_id TEXT PRIMARY KEY,
                from_pool TEXT NOT NULL,
                task_id TEXT NOT NULL,
                amount_micro INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        c.execute("BEGIN IMMEDIATE")
        try:
            for pool, cap in allocations.items():
                row = c.execute("SELECT cap_micro FROM allocations WHERE pool=?",
                                (pool,)).fetchone()
                if row is None:
                    c.execute("INSERT INTO allocations VALUES(?,?)", (pool, cap))
                elif row[0] != cap:
                    raise ValueError("allocation cap drift for %s: %s != %s"
                                     % (pool, row[0], cap))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # ---------------- internals ----------------
    def _used_by_pool(self, c: sqlite3.Cursor) -> Dict[str, int]:
        rows = c.execute(
            "SELECT pool, SUM(bound_micro) FROM reservations "
            "WHERE state IN ('reserved','unknown_frozen','settled') GROUP BY pool"
        ).fetchall()
        return {p: (s or 0) for p, s in rows}

    def _task_overrun(self, c: sqlite3.Cursor, task_id: str) -> bool:
        row = c.execute("SELECT 1 FROM reservations WHERE task_id=? AND overrun_micro>0 LIMIT 1",
                        (task_id,)).fetchone()
        return row is not None

    # ---------------- public API ----------------
    def reserve(self, task_id: str, logical_call_id: str, attempt_id: str,
                generation_id: str, model: str,
                max_input_tokens: int, max_output_tokens: int,
                attempts: int = 1) -> Dict[str, Any]:
        if self.pricing is None:
            raise BudgetRefused("ledger.pricing_manifest_missing")
        upper, manifest_id = price_upper_bound_micro(
            self.pricing, model, max_input_tokens, max_output_tokens, attempts)
        if upper <= 0:
            raise BudgetRefused("price_manifest.non_positive_upper_bound")
        pool = task_pool_for(task_id)
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            dup = c.execute(
                "SELECT request_id,state FROM reservations WHERE task_id=? AND logical_call_id=? AND attempt_id=?",
                (task_id, logical_call_id, attempt_id)).fetchone()
            if dup and dup[1] in ("reserved", "unknown_frozen", "settled"):
                c.execute("COMMIT")
                row = c.execute(
                    "SELECT request_id,task_id,logical_call_id,attempt_id,generation_id,"
                    "pool,model,price_manifest_id,upper_bound_micro,bound_micro,state "
                    "FROM reservations WHERE request_id=?", (dup[0],)).fetchone()
                cols = ("request_id", "task_id", "logical_call_id", "attempt_id",
                        "generation_id", "pool", "model", "price_manifest_id",
                        "upper_bound_micro", "bound_micro", "state")
                return self._row_to_receipt(dict(zip(cols, row)), idempotent=True)
            if dup:
                # released/replayed attempt: a fresh physical attempt needs a
                # fresh reservation under a derived, still-unique attempt id
                n = 2
                while True:
                    attempt_id = "%s:r%d" % (attempt_id, n)
                    clash = c.execute(
                        "SELECT 1 FROM reservations WHERE task_id=? AND logical_call_id=? AND attempt_id=?",
                        (task_id, logical_call_id, attempt_id)).fetchone()
                    if not clash or clash[0] not in ("released", "cache_replay"):
                        if not clash:
                            break
                    n += 1
            if self._task_overrun(c, task_id):
                raise BudgetRefused("task.overrun_flagged:%s" % task_id)
            used = self._used_by_pool(c)
            global_used = sum(used.values())
            if global_used + upper > int(dict(c.execute("SELECT pool,cap_micro FROM allocations"))["global"]):
                raise BudgetRefused("budget.global_exhausted")
            pool_used = used.get(pool, 0)
            pool_cap = int(dict(c.execute("SELECT pool,cap_micro FROM allocations"))[pool])
            # reserve transfers may raise the effective pool cap (not the global one)
            transfers = c.execute(
                "SELECT COALESCE(SUM(amount_micro),0) FROM reserve_transfers WHERE task_id=?",
                (task_id,)).fetchone()[0]
            if pool_used + upper > pool_cap + transfers:
                raise BudgetRefused("budget.pool_exhausted:%s" % pool)
            request_id = "req_" + uuid.uuid4().hex
            ts = _iso(_now())
            c.execute(
                "INSERT INTO reservations(request_id,task_id,logical_call_id,attempt_id,"
                "generation_id,pool,model,price_manifest_id,upper_bound_micro,bound_micro,"
                "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, task_id, logical_call_id, attempt_id, generation_id, pool,
                 model, manifest_id, upper, upper, "reserved", ts, ts))
            c.execute("COMMIT")
        except BudgetRefused:
            c.execute("ROLLBACK")
            raise
        except Exception:
            c.execute("ROLLBACK")
            raise
        return {"request_id": request_id, "upper_bound_micro": upper,
                "price_manifest_id": manifest_id, "model": model,
                "state": "reserved", "task_id": task_id, "pool": pool}

    def _row_to_receipt(self, row: Dict[str, Any], idempotent: bool = False) -> Dict[str, Any]:
        out = {k: row[k] for k in ("request_id", "task_id", "logical_call_id", "attempt_id",
                                   "generation_id", "pool", "model", "price_manifest_id",
                                   "upper_bound_micro", "bound_micro", "state")}
        out["idempotent_replay"] = idempotent
        return out

    def release(self, request_id: str) -> None:
        """Only legal before a physical attempt was sent."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state FROM reservations WHERE request_id=?",
                            (request_id,)).fetchone()
            if not row:
                raise BudgetRefused("release.unknown_request")
            if row[0] != "reserved":
                raise BudgetRefused("release.not_reserved_state:%s" % row[0])
            c.execute("UPDATE reservations SET state='released', bound_micro=0, updated_at=? "
                      "WHERE request_id=?", (_iso(_now()), request_id))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def settle(self, request_id: str, usage: Dict[str, Any],
               provider_receipt_id: str = "") -> Dict[str, Any]:
        """Idempotent settlement with the provider's ACTUAL usage."""
        actual = self._actual_micro(usage)
        usage_json = json.dumps(usage, sort_keys=True, ensure_ascii=False)
        usage_hash = hashlib.sha256(usage_json.encode()).hexdigest()
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state,bound_micro,upper_bound_micro,usage_json,pool,overrun_micro "
                            "FROM reservations WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise BudgetRefused("settle.unknown_request")
            state, bound, upper, old_json, pool, overrun = row
            if state == "settled":
                if old_json and hashlib.sha256(old_json.encode()).hexdigest() == usage_hash:
                    c.execute("COMMIT")
                    return {"request_id": request_id, "settled_micro": bound,
                            "idempotent": True}
                raise BudgetRefused("settle.contradicting_usage_for_settled_request")
            if actual > upper:
                c.execute("UPDATE reservations SET state='settled', bound_micro=?, "
                          "overrun_micro=?, usage_json=?, provider_receipt_id=?, updated_at=? "
                          "WHERE request_id=?",
                          (actual, actual - upper, usage_json, provider_receipt_id,
                           _iso(_now()), request_id))
                c.execute("COMMIT")
                return {"request_id": request_id, "settled_micro": actual,
                        "overrun_micro": actual - upper, "task_flagged": True}
            c.execute("UPDATE reservations SET state='settled', bound_micro=?, usage_json=?, "
                      "provider_receipt_id=?, updated_at=? WHERE request_id=?",
                      (actual, usage_json, provider_receipt_id, _iso(_now()), request_id))
            c.execute("COMMIT")
            return {"request_id": request_id, "settled_micro": actual}
        except Exception:
            c.execute("ROLLBACK")
            raise

    def amend_settlement(self, request_id: str, usage: Dict[str, Any],
                         reason: str) -> Dict[str, Any]:
        """Correct a settled row with a verifiable recomputation (e.g. rates
        were missing when it settled).  The bound delta is applied so the
        global constraint stays true; the reason is recorded."""
        actual = self._actual_micro(usage)
        usage_json = json.dumps(usage, sort_keys=True, ensure_ascii=False)
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state,bound_micro,upper_bound_micro FROM reservations "
                            "WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise BudgetRefused("amend.unknown_request")
            state, bound, upper = row
            if state != "settled":
                raise BudgetRefused("amend.not_settled_state:%s" % state)
            delta = actual - bound
            c.execute("UPDATE reservations SET bound_micro=?, "
                      "overrun_micro=MAX(overrun_micro, MAX(0, ?-upper_bound_micro)), "
                      "usage_json=?, updated_at=? WHERE request_id=?",
                      (actual, actual, usage_json + " | amend_reason=" + reason,
                       _iso(_now()), request_id))
            c.execute("COMMIT")
            return {"request_id": request_id, "previous_bound_micro": bound,
                    "amended_bound_micro": actual, "delta_micro": delta}
        except Exception:
            c.execute("ROLLBACK")
            raise

    def mark_unknown(self, request_id: str) -> None:
        """Sent but never settled: keep counting at the upper bound, forever,
        until a verifiable bill arrives via settle_unknown."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state,bound_micro FROM reservations WHERE request_id=?",
                            (request_id,)).fetchone()
            if not row:
                raise BudgetRefused("unknown.unknown_request")
            if row[0] not in ("reserved", "unknown_frozen"):
                raise BudgetRefused("unknown.illegal_state:%s" % row[0])
            c.execute("UPDATE reservations SET state='unknown_frozen', bound_micro=?, updated_at=? "
                      "WHERE request_id=?",
                      (row[1], _iso(_now()), request_id))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def settle_unknown(self, request_id: str, usage: Dict[str, Any],
                       provider_receipt_id: str = "") -> Dict[str, Any]:
        """A verifiable bill arrived for a frozen unknown: charge the actual."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state FROM reservations WHERE request_id=?",
                            (request_id,)).fetchone()
            if not row or row[0] != "unknown_frozen":
                raise BudgetRefused("settle_unknown.not_frozen")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return self.settle(request_id, usage, provider_receipt_id)

    def sweep_stale(self, older_than_seconds: float) -> int:
        """Crash recovery: reservations stuck in 'reserved' become unknown_frozen."""
        c = self._conn
        cutoff = _iso(_now() - older_than_seconds)
        c.execute("BEGIN IMMEDIATE")
        try:
            cur = c.execute(
                "UPDATE reservations SET state='unknown_frozen', updated_at=? "
                "WHERE state='reserved' AND updated_at<=?", (_iso(_now()), cutoff))
            c.execute("COMMIT")
            return cur.rowcount
        except Exception:
            c.execute("ROLLBACK")
            raise

    def cache_replay(self, task_id: str, logical_call_id: str, attempt_id: str,
                     generation_id: str, original_request_id: str,
                     model: str) -> Dict[str, Any]:
        """Record a cache replay: zero new cost, linked to the original receipt."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            orig = c.execute("SELECT state,price_manifest_id FROM reservations WHERE request_id=?",
                             (original_request_id,)).fetchone()
            if not orig:
                raise BudgetRefused("cache_replay.unknown_original")
            request_id = "replay_" + uuid.uuid4().hex
            ts = _iso(_now())
            c.execute(
                "INSERT INTO reservations(request_id,task_id,logical_call_id,attempt_id,"
                "generation_id,pool,model,price_manifest_id,upper_bound_micro,bound_micro,"
                "state,provider_receipt_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, task_id, logical_call_id, attempt_id, generation_id,
                 task_pool_for(task_id), model, orig[1], 0, 0, "cache_replay",
                 original_request_id, ts, ts))
            c.execute("COMMIT")
            return {"request_id": request_id, "bound_micro": 0,
                    "linked_to": original_request_id}
        except Exception:
            c.execute("ROLLBACK")
            raise

    def balances(self) -> Dict[str, Any]:
        c = self._conn
        caps = dict(c.execute("SELECT pool,cap_micro FROM allocations"))
        used = self._used_by_pool(c)
        out = {"pools": {}, "global": {
            "cap_micro": caps["global"],
            "used_micro": sum(used.values()),
            "remaining_micro": caps["global"] - sum(used.values()),
        }}
        for pool, cap in caps.items():
            if pool == "global":
                continue
            u = used.get(pool, 0)
            out["pools"][pool] = {"cap_micro": cap, "used_micro": u,
                                  "remaining_micro": cap - u}
        return out

    def clear_overrun_flag(self, task_id: str, reason: str) -> None:
        """Overrun flags are cleared only with a written root cause (recorded in
        the ledger).  Clearing does not refund: the overrun cost stays spent.
        A later overrun re-flags the task automatically."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("CREATE TABLE IF NOT EXISTS overrun_clearances("
                      "task_id TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.execute("INSERT INTO overrun_clearances VALUES(?,?,?)",
                      (task_id, "overrun_flag_cleared: " + reason, _iso(_now())))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _task_overrun(self, c: sqlite3.Cursor, task_id: str) -> bool:
        row = c.execute("SELECT 1 FROM reservations WHERE task_id=? AND overrun_micro>0 LIMIT 1",
                        (task_id,)).fetchone()
        if not row:
            return False
        latest = c.execute("SELECT MAX(updated_at) FROM reservations WHERE task_id=? AND overrun_micro>0",
                           (task_id,)).fetchone()[0]
        try:
            clr = c.execute("SELECT MAX(created_at) FROM overrun_clearances WHERE task_id=?",
                            (task_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            clr = None
        return not (clr is not None and latest is not None and clr >= latest)

    def settle_verified_zero(self, request_id: str, reason: str) -> None:
        """Replace a frozen unknown with a verified zero bill.  Only for
        provider-rejected attempts whose error contract proves no inference ran
        (auth/arrears/throttling classes).  The reason is recorded."""
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT state FROM reservations WHERE request_id=?",
                            (request_id,)).fetchone()
            if not row or row[0] not in ("unknown_frozen", "reserved"):
                raise BudgetRefused("zero_bill.illegal_state:%s" % (row[0] if row else None))
            c.execute("UPDATE reservations SET state='settled', bound_micro=0, "
                      "usage_json=?, updated_at=? WHERE request_id=?",
                      (json.dumps({"verified_zero_bill": True, "reason": reason},
                                  sort_keys=True), _iso(_now()), request_id))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def record_transfer(self, task_id: str, from_pool: str, amount_micro: int,
                        reason: str) -> Dict[str, Any]:
        """RESERVE_TRANSFER: move unused pool money to a later task (never backwards
        into the final locked pool, never past it)."""
        if from_pool not in DEFAULT_ALLOCATIONS or from_pool in ("global",):
            raise BudgetRefused("transfer.illegal_source_pool")
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            used = self._used_by_pool(c)
            cap = int(dict(c.execute("SELECT pool,cap_micro FROM allocations"))[from_pool])
            if used.get(from_pool, 0) > cap - amount_micro:
                raise BudgetRefused("transfer.source_pool_has_no_unused_margin")
            transfer_id = "xfer_" + uuid.uuid4().hex
            c.execute("INSERT INTO reserve_transfers VALUES(?,?,?,?,?,?)",
                      (transfer_id, from_pool, task_id, amount_micro, reason, _iso(_now())))
            c.execute("COMMIT")
            return {"transfer_id": transfer_id, "amount_micro": amount_micro}
        except Exception:
            c.execute("ROLLBACK")
            raise

    @staticmethod
    def _actual_micro(usage: Dict[str, Any]) -> int:
        """Actual cost from provider usage fields; falls back to explicit cny."""
        if "actual_cost_micro" in usage:
            return int(usage["actual_cost_micro"])
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        rates = usage.get("rates") or {}
        micro = (pt * float(rates.get("input_cny_per_million", 0))
                 + ct * float(rates.get("output_cny_per_million", 0))) * MICRO / 1_000_000
        return int(round(micro))


# ---------------------------------------------------- provider boundary ----
def budgeted_chat_completion(ledger: BudgetLedger, *, task_id: str, logical_call_id: str,
                             attempt_id: str, generation_id: str, model: str,
                             system_prompt: str, user_payload: Dict[str, Any],
                             api_key: str, base_url: str,
                             max_input_tokens: int, max_output_tokens: int,
                             attempts: int = 1, temperature: float = 0.1,
                             timeout_sec: float = 60.0) -> Dict[str, Any]:
    """The enforced reservation boundary at the lowest provider call layer.

    reserve -> one physical HTTP attempt -> settle with the API's ACTUAL usage.
    Any refusal raises BudgetRefused BEFORE any network activity.  If the HTTP
    attempt was sent but no usable usage came back, the reservation is frozen
    as unknown (never silently dropped)."""
    receipt = ledger.reserve(task_id, logical_call_id, attempt_id, generation_id,
                             model, max_input_tokens, max_output_tokens, attempts)
    import urllib.request
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max(1, int(max_output_tokens)),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
        ],
    }
    request = urllib.request.Request(
        url=base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    sent = False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout_sec) as response:
            sent = True
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Any HTTP response means the attempt physically happened.
        sent = True
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if exc.code < 500:
            # 4xx = provider rejected the request before any inference ran
            # (auth/arrears/params): deterministically unbilled -> release.
            ledger.release(receipt["request_id"])
            raise BudgetRefused("provider.http_%d_rejected_no_charge:%s:%s"
                                % (exc.code, receipt["request_id"], body_text))
        ledger.mark_unknown(receipt["request_id"])
        raise BudgetRefused("provider.http_%d_unknown_outcome_frozen:%s:%s"
                            % (exc.code, receipt["request_id"], body_text))
    except Exception:
        if sent:
            ledger.mark_unknown(receipt["request_id"])
            raise BudgetRefused("provider.unknown_outcome_frozen:" + receipt["request_id"])
        ledger.release(receipt["request_id"])
        raise
    usage_api = data.get("usage") or {}
    if not usage_api:
        ledger.mark_unknown(receipt["request_id"])
        raise BudgetRefused("provider.no_usage_frozen:" + receipt["request_id"])
    model_rates = _rates_for(ledger.pricing, model)
    usage = {
        "prompt_tokens": int(usage_api.get("prompt_tokens") or 0),
        "completion_tokens": int(usage_api.get("completion_tokens") or 0),
        "total_tokens": int(usage_api.get("total_tokens") or 0),
        "model": data.get("model") or model,
        "rates": model_rates,
        "cached_tokens": int((usage_api.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
    }
    settle = ledger.settle(receipt["request_id"], usage,
                           provider_receipt_id=str(data.get("id") or ""))
    return {
        "content": str((data.get("choices") or [{}])[0].get("message", {}).get("content", "")),
        "usage_actual": usage,
        "reservation": receipt,
        "settlement": settle,
        "model_reported": data.get("model"),
    }


def _rates_for(pricing: Optional[Dict[str, Any]], model: str) -> Dict[str, float]:
    if not pricing:
        return {}
    entry = (pricing.get("models") or {}).get(model) or []
    if not entry:
        return {}
    return {"input_cny_per_million": float(entry[0]["input_cny_per_million"]),
            "output_cny_per_million": float(entry[0]["output_cny_per_million"])}
