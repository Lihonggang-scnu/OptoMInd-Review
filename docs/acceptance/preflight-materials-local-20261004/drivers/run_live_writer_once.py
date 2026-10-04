"""Run exactly one authorized live writer call through the production CLI.

This wrapper is intentionally single-shot: it refuses to reuse an existing
output directory and never retries a provider failure.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace
from pathlib import Path

if __name__ == "__main__" and (
    "--allow-local-run" not in sys.argv[1:]
    or os.environ.get("OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN") != "1"
):
    raise SystemExit(
        "refusing provider-capable archive driver; set OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN=1 "
        "and pass --allow-local-run for an explicitly authorized local run"
    )

from scripts.upgrade3 import review_unit_writer as writer_cli
from optomind_research.runtime.upgrade3 import review_unit_writer as writing


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "real_checks" / "results"
OUTPUT = RESULTS / "live_writer_qwen37_single_v2"
LEDGER = Path(os.environ.get("OPTOMIND_LOCAL_LEDGER", "<SET_OPTOMIND_LOCAL_LEDGER>"))
ARRANGEMENT = RESULTS / "ARRANGEMENT_FROM_REAL_PACKET.json"
VIEW = RESULTS / "ARRANGEMENT_INPUT_FROM_REAL_PACKET.json"
KEY_FILE = Path(os.environ.get("OPTOMIND_QWEN_KEY_FILE", "<SET_OPTOMIND_QWEN_KEY_FILE>"))


def require_explicit_local_run() -> None:
    """Prevent accidental provider use from this public derivative."""
    if "--allow-local-run" not in sys.argv[1:] or os.environ.get("OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN") != "1":
        raise SystemExit(
            "refusing provider-capable archive driver; set OPTOMIND_ALLOW_LOCAL_ACCEPTANCE_RUN=1 "
            "and pass --allow-local-run for an explicitly authorized local run"
        )


def ledger_state() -> dict[str, object]:
    with sqlite3.connect(LEDGER) as connection:
        rows = connection.execute(
            "select status, count(*), coalesce(sum(actual_cny),0), coalesce(sum(amount_cny),0) "
            "from reservations group by status order by status"
        ).fetchall()
    by_status = [
        {"status": str(row[0]), "count": int(row[1]), "actual_cny": float(row[2]), "amount_cny": float(row[3])}
        for row in rows
    ]
    return {
        "by_status": by_status,
        "settled_actual_cny": sum(row["actual_cny"] for row in by_status if row["status"] == "settled"),
        "reserved_cny": sum(row["amount_cny"] for row in by_status if row["status"] == "reserved"),
        "uncertain": [row for row in by_status if row["status"] not in {"settled", "reserved"}],
    }


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError("refuse_repeat_live_writer_output_exists:" + str(OUTPUT))
    if not ARRANGEMENT.is_file() or not VIEW.is_file():
        raise RuntimeError("corrected_arrangement_or_view_missing")
    if not LEDGER.is_file() or not KEY_FILE.is_file():
        raise RuntimeError("shared_ledger_or_key_file_missing")
    before = ledger_state()
    started = time.time()
    code = -1
    error = ""
    try:
        view = writing.build_unit_view(ARRANGEMENT, "CH02:U3", view_path=VIEW)
        payload = writing.unit_payload(view, language="zh")
        payload["planning_revision_mode"] = True
        prompt = writing.load_writer_prompt(planning_revision=True)
        messages = writing.unit_messages(view, prompt=prompt, language="zh", payload=payload, planning_revision=True)
        estimate = writing.estimate_unit_cost(messages, model="qwen3.7-flash", output_tokens=4000, thinking_budget=0)
        if float(estimate.get("estimated_cost_cny") or 0.0) > 1.0:
            raise RuntimeError("single_call_estimate_exceeds_1_cny:" + str(estimate.get("estimated_cost_cny")))
        unit_dir = OUTPUT / "CH02_U3"
        writing.write_unit_input(view, messages, unit_dir, estimate=estimate, language="zh")
        args = SimpleNamespace(
            budget_ledger=str(LEDGER), global_budget_cny=None, model="qwen3.7-flash",
            key_file=str(KEY_FILE), timeout_seconds=900.0, output_tokens=4000,
            thinking_budget=0,
        )
        client = writer_cli._real_client(args, token_counter=writing._default_qwen_token_counter())
        result = writing.run_unit_writing(
            view, client=client, model="qwen3.7-flash", prompt=prompt,
            language="zh", payload=payload, raw_response_dir=unit_dir / "raw_responses",
            planning_revision=True,
        )
        written = writing.write_unit_output(
            view, result["body_markdown"], unit_dir, model=result.get("model") or "qwen3.7-flash",
            language="zh", mode="run", used_messages=result["messages"], estimate=estimate,
            usage=result.get("usage") or {}, response_path=result.get("raw_response") or "",
            finish_reason=result.get("finish_reason") or "", complete=result.get("complete", True),
            partial_error=result.get("partial_error", ""), issues=result.get("issues") or [],
        )
        report = {"result": result, "written": written}
        (unit_dir / "LIVE_CALL_RESULT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
        code = 0
    except Exception as exc:  # one provider attempt only; preserve diagnostics
        error = type(exc).__name__ + ":" + str(exc)
    elapsed = time.time() - started
    after = ledger_state()
    report = {
        "schema_version": "body_preflight_material_acceptance.live_writer_once.v1",
        "provider": "qwen3.7-flash",
        "call_count_requested": 1,
        "cli_return_code": code,
        "error": error,
        "elapsed_seconds": elapsed,
        "arrangement": str(ARRANGEMENT),
        "view": str(VIEW),
        "output_root": str(OUTPUT),
        "estimate": estimate if "estimate" in locals() else {},
        "ledger": {"path": str(LEDGER), "before": before, "after": after, "changed": before != after},
        "files": sorted(str(path.relative_to(OUTPUT)) for path in OUTPUT.rglob("*") if path.is_file()) if OUTPUT.is_dir() else [],
        "note": "One production CLI writer call; no retry and no full BODY run.",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "LIVE_WRITER_ONCE_META.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps({"meta": str(RESULTS / "LIVE_WRITER_ONCE_META.json"), "return_code": code, "error": error, "elapsed_seconds": elapsed, "ledger_changed": before != after}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    require_explicit_local_run()
    raise SystemExit(main())
