from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(r"F:\OptoMind-Review-2\outputs\lihonggang_full_acceptance_20261001_50cny")
PACKET_ROOT = BASE / "case_chain_restore_20261002"
ARR_ROOT = BASE / "body_arrangement_case_restore_20261002"
OUT_ROOT = BASE / "body_writing_case_restore_20261002"
SCRIPT = BASE / "worktree" / "scripts" / "upgrade3" / "review_unit_writer.py"
PYTHON = r"C:\Anaconda\python.exe"
KEY_FILE = BASE.parent.parent / "api_keys" / "qwen-api-key.txt"
LEDGER = BASE / "budget.sqlite"
MAX_WORKERS = 3
MODEL = "qwen3.5-plus"
OUTPUT_TOKENS = 12000
THINKING_BUDGET = 4000
TIMEOUT_SECONDS = 900

STATE_PATH = OUT_ROOT / "WRITING_EXECUTION_STATE.json"
STATE_LOCK = threading.Lock()


def atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def load_units() -> list[tuple[str, str]]:
    result = []
    for p in sorted(ARR_ROOT.glob("CH*/CHAPTER_ARRANGEMENT.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        for unit in d.get("units") or []:
            uid = str(unit.get("unit_id") or "").strip()
            if uid:
                result.append((p.parent.name, uid))
    if len(result) != 20:
        raise RuntimeError(f"expected 20 arranged units, found {len(result)}")
    return result


def initial_state(units: list[tuple[str, str]]) -> dict:
    return {
        "phase": "unit_writing",
        "status": "running",
        "started_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "max_workers": MAX_WORKERS,
        "model": MODEL,
        "output_tokens": OUTPUT_TOKENS,
        "thinking_budget": THINKING_BUDGET,
        "timeout_seconds": TIMEOUT_SECONDS,
        "ledger": str(LEDGER),
        "arrangement_root": str(ARR_ROOT),
        "output_root": str(OUT_ROOT),
        "units_total": len(units),
        "units": {
            uid: {"chapter": ch, "status": "pending", "output_root": str(OUT_ROOT / uid)}
            for ch, uid in units
        },
        "completed": 0,
        "failed": 0,
        "model_calls": 0,
    }


def update_unit(uid: str, patch: dict) -> None:
    with STATE_LOCK:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state["units"][uid].update(patch)
        state["completed"] = sum(1 for x in state["units"].values() if x.get("status") == "complete")
        state["failed"] = sum(1 for x in state["units"].values() if x.get("status") == "failed")
        state["model_calls"] = sum(int(x.get("model_calls") or 0) for x in state["units"].values())
        state["updated_at_local"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        atomic_write(STATE_PATH, state)


def run_one(chapter: str, uid: str) -> dict:
    unit_out = OUT_ROOT / uid
    unit_out.mkdir(parents=True, exist_ok=True)
    stdout_path = unit_out / "WRITER.stdout.log"
    stderr_path = unit_out / "WRITER.stderr.log"
    cmd = [
        PYTHON, "-X", "utf8", str(SCRIPT),
        "--arrangement", str(ARR_ROOT / chapter / "CHAPTER_ARRANGEMENT.json"),
        "--view", str(ARR_ROOT / chapter / "ARRANGEMENT_INPUT.json"),
        "--output-root", str(unit_out),
        "--model", MODEL,
        "--max-material-chars-per-source", "0",
        "--output-tokens", str(OUTPUT_TOKENS),
        "--thinking-budget", str(THINKING_BUDGET),
        "--timeout-seconds", str(TIMEOUT_SECONDS),
        "--key-file", str(KEY_FILE),
        "--budget-ledger", str(LEDGER),
        "--planning-revision", "--run", "--unit", uid,
    ]
    started = time.time()
    update_unit(uid, {"status": "running", "started_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "command": cmd, "stdout": str(stdout_path), "stderr": str(stderr_path)})
    with stdout_path.open("w", encoding="utf-8", errors="replace") as so, stderr_path.open("w", encoding="utf-8", errors="replace") as se:
        try:
            proc = subprocess.run(cmd, cwd=str(BASE / "worktree"), stdout=so, stderr=se, timeout=TIMEOUT_SECONDS + 120)
            rc = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            rc = 124
            timed_out = True
    result_path = unit_out / "UNIT_WRITING_RUN.json"
    report = None
    if result_path.exists():
        try:
            report = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            report = {"parse_error": type(exc).__name__ + ":" + str(exc)}
    entry = {
        "status": "complete" if rc == 0 and isinstance(report, dict) and report.get("model_calls") == 1 else "failed",
        "returncode": rc,
        "timed_out": timed_out,
        "finished_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(time.time() - started, 2),
        "model_calls": int(report.get("model_calls") or 0) if isinstance(report, dict) else 0,
        "result_path": str(result_path) if result_path.exists() else "",
        "body_paths": [str(x) for x in unit_out.rglob("*.md")],
        "report_status": ([x.get("status") for x in report.get("units", [])] if isinstance(report, dict) else []),
        "error": report.get("error") if isinstance(report, dict) and report.get("error") else ("process_exit:" + str(rc) if rc else "missing_result"),
    }
    update_unit(uid, entry)
    return {"unit": uid, **entry}


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    units = load_units()
    atomic_write(STATE_PATH, initial_state(units))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(run_one, ch, uid) for ch, uid in units]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"unit": "unknown", "status": "failed", "error": type(exc).__name__ + ":" + str(exc)})
    with STATE_LOCK:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state["status"] = "completed" if all(x.get("status") == "complete" for x in state["units"].values()) else "partial"
        state["finished_at_local"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        state["results"] = results
        atomic_write(STATE_PATH, state)
    return 0 if state["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

