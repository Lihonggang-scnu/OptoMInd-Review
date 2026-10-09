"""No-network recovery guard check for the final uncertain ledger state."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKTREE = ROOT / "worktree"
sys.path.insert(0, str(WORKTREE))
from scripts.upgrade3.legacy_unit_writer import _ledger_snapshot  # noqa: E402


def main() -> int:
    ledger = ROOT / "BUDGET.sqlite"
    marker = ROOT / "BUDGET.sqlite.legacy-route.json"
    scope = json.loads(marker.read_text(encoding="utf-8"))
    snapshot = _ledger_snapshot(ledger, marker, scope)
    unsettled = [r for r in snapshot["reservations"] if r["status"] in {"reserved", "uncertain"} or r["actual_cny"] is None]
    result = {
        "schema_version": "legacy_unit_plus.recovery_guard_audit.v1",
        "network_calls": 0,
        "physical_factory_invoked": False,
        "ledger_snapshot": snapshot,
        "unsettled_or_unknown_rows": unsettled,
        "guard_would_block_next_call": bool(unsettled),
        "expected_guard_error": "legacy_unsettled_budget_requires_receipt_reconciliation" if unsettled else "",
        "action_taken": "read_only_only; no retry, no release, no cache recovery call",
    }
    (ROOT / "RECOVERY_GUARD_AUDIT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
