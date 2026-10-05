from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.upgrade3 import post_body_revision as cli
from optomind_research.runtime.upgrade3.post_body_revision import run_revision


ROOT = Path(r"F:\OptoMind-Review-2\outputs\post_body_revision_local_experiment_20261005")
WORKTREE = ROOT / "worktree"
CASE_PATH = ROOT / "ROOT_FIXED_CASE.json"
CONFIG_PATH = WORKTREE / "config" / "post_body_revision" / "B.json"
OLD_CALL_DIR = ROOT / "live" / "B" / "live" / "calls"
LEDGER_PATH = ROOT / "live" / "budget.sqlite"


def load_saved() -> dict[str, dict]:
    result = {}
    for path in sorted(OLD_CALL_DIR.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        result[value["stage_id"]] = value
    return result


def ledger_snapshot() -> list[dict]:
    import sqlite3
    with sqlite3.connect(LEDGER_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM reservations ORDER BY rowid"
        ).fetchall()
    return [dict(row) for row in rows]


def run(mode: str, output: Path) -> dict:
    saved = load_saved()
    config = cli.validate_config(cli.read_json(CONFIG_PATH), "B")
    config["execution_mode"] = (
        "recovery_boundary_proof"
        if mode == "offline"
        else "live_boundary_retry_with_saved_response_replay"
    )
    config["paid_budget"] = {"limit_cny": 10.0, "shared_ledger": str(LEDGER_PATH)}
    case = cli.read_json(CASE_PATH)
    args = SimpleNamespace(run=True, allow_paid=True, recordings=None, budget_cny=10.0,
                           budget_ledger=str(LEDGER_PATH),
                           key_file=r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
    real_client = None if mode == "offline" else cli.make_live_client(args, config)
    seen = []
    mismatches = []
    boundary_calls = 0

    before_ledger = ledger_snapshot()

    def client(stage: str, messages, *, model: str, **kwargs):
        nonlocal boundary_calls
        seen.append(stage)
        saved_call = saved.get(stage)
        if saved_call is None:
            raise RuntimeError("unexpected_stage:" + stage)
        if model != saved_call.get("model") or messages != saved_call.get("messages"):
            mismatches.append(stage)
            raise RuntimeError("saved_model_or_message_mismatch:" + stage)
        if stage != "issue_003_verifier":
            if saved_call.get("status") != "ok" or "response" not in saved_call:
                raise RuntimeError("saved_replay_not_successful:" + stage)
            raw = saved_call.get("response")
            return {"content": json.dumps(raw, ensure_ascii=False), "usage": {}, "cost_cny": 0.0,
                    "cost_provenance": "saved_response_replay_no_provider_call"}
        boundary_calls += 1
        if mode == "offline":
            return {"content": json.dumps({"verdict": "reject", "preservation_ok": False,
                                            "problem_improved": False,
                                            "reason": "offline boundary proof only", "evidence_ids": []}, ensure_ascii=False),
                    "usage": {}, "cost_cny": 0.0, "cost_provenance": "offline_fake_boundary_no_provider_call"}
        return real_client(stage, messages, model=model, **kwargs)

    client.execution_mode = config["execution_mode"]

    report = run_revision(case, config, output, client, resume=False)
    after_ledger = ledger_snapshot()
    before_ids = {row.get("reservation_id") for row in before_ledger}
    new_rows = [row for row in after_ledger if row.get("reservation_id") not in before_ids]
    original_b_settled = sum(
        float(row.get("actual_cny") or 0)
        for row in before_ledger
        if str(row.get("call_id", "")).startswith("post-body-B-") and row.get("status") == "settled"
    )
    original_b_uncertain = sum(
        float(row.get("amount_cny") or 0)
        for row in before_ledger
        if str(row.get("call_id", "")).startswith("post-body-B-") and row.get("status") == "uncertain"
    )
    incremental_settled = sum(float(row.get("actual_cny") or 0) for row in new_rows if row.get("status") == "settled")
    incremental_uncertain = sum(float(row.get("amount_cny") or 0) for row in new_rows if row.get("status") == "uncertain")
    result = {"mode": mode, "output": str(output), "report_status": report.get("status"),
              "run_completed": report.get("run_completed"), "seen_stages": seen,
              "replayed_stages": [s for s in seen if s != "issue_003_verifier"],
              "boundary_stage": "issue_003_verifier", "boundary_calls": boundary_calls,
              "message_mismatches": mismatches, "provider_calls": boundary_calls if mode == "live" else 0,
              "report_errors": report.get("errors"), "applied_patch_issue_ids": [p.get("issue_id") for p in report.get("applied_patches", [])],
              "saved_replay_calls": len([s for s in seen if s != "issue_003_verifier"]),
              "new_provider_calls": boundary_calls if mode == "live" else 0,
              "original_b_known_settled_cny": original_b_settled,
              "original_b_uncertain_cny_held": original_b_uncertain,
              "incremental_retry_settled_cny": incremental_settled,
              "incremental_retry_uncertain_cny_held": incremental_uncertain,
              "new_ledger_rows": new_rows}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run(args.mode, Path(args.output))
    out = ROOT / "records" / f"live_B_retry_{args.mode}_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
