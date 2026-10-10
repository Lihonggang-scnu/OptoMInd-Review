"""External recovery wrapper for the legacy-unit Plus run.

This file intentionally lives under the run output, not in the source
worktree.  It provides a user-authorized budget waiver for exactly one known
uncertain reservation, then invokes the unchanged legacy CLI for recovery.
The default action is a no-network audit.  It never edits source files.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any


ROOT = Path(__file__).resolve().parent
WORKTREE = ROOT / "worktree"
INPUT = ROOT / "input" / "6d6ae867ceae5c88f25d36f88087925341af5cf82f1672680157a3588d14e5f7" / "FULL_BODY_INPUT.json"
LEDGER = ROOT / "BUDGET.sqlite"
MARKER = ROOT / "BUDGET.sqlite.legacy-route.json"
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
KEY_FILE = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
EXPECTED_RESERVATION = "res-ac0d4c128b3d4981"
EXPECTED_UNKNOWN_AMOUNT = 1.63964
EXPECTED_SETTLED_ACTUAL = 1.936256
EXPECTED_MISSING = [
    "Ch6:Ch6_U1", "Ch6:Ch6_U2", "Ch6:Ch6_U3", "Ch6:Ch6_U4",
    "Ch7:Ch7_U01", "Ch7:Ch7_U02", "Ch7:Ch7_U03", "Ch7:Ch7_U04",
]
EXPECTED_CALL_ID = "17b84ac97c390c34-attempt_001:key0:attempt0"
AUTHORIZATION_TEXT = (
    "用户最新授权：无需核对费用，旧uncertain1.63964元忽略不计，然后恢复8单元，成功21不能重跑；"
    "只qwen3.5-plus直连，同30元账本，旧实际1.936256元保持，不用Max/指南/旧稿/实际前文，"
    "不重跑上游或删材料。保留旧失败attempt与原费用未知记录，采用可追溯的用户豁免释放占额，"
    "不伪造账单已结算0。"
)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_modules():
    if str(WORKTREE) not in sys.path:
        sys.path.insert(0, str(WORKTREE))
    from optomind_research.runtime.upgrade3 import legacy_unit_route as route
    from scripts.upgrade3 import legacy_unit_writer as cli
    return route, cli


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False, capture_output=True, text=False, timeout=10,
            )
            text = (result.stdout or b"").decode(errors="replace")
            return str(pid) in text and "No tasks" not in text
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def row_dict(columns: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: row[index] for index, name in enumerate(columns)}


def ledger_columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(reservations)").fetchall()]


def read_ledger() -> dict[str, Any]:
    if not LEDGER.is_file() or not MARKER.is_file():
        raise RuntimeError("missing_original_ledger_or_scope_marker")
    with sqlite3.connect(LEDGER) as db:
        columns = ledger_columns(db)
        rows = [row_dict(columns, row) for row in db.execute(
            "SELECT " + ",".join(columns) + " FROM reservations ORDER BY created_at,reservation_id"
        ).fetchall()]
        meta = dict(db.execute("SELECT key,value FROM budget_meta").fetchall())
        audit_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_budget_waivers'"
        ).fetchone() is not None
        waivers = []
        if audit_exists:
            waivers = [dict(zip(
                [row[1] for row in db.execute("PRAGMA table_info(legacy_budget_waivers)").fetchall()], row
            )) for row in db.execute("SELECT * FROM legacy_budget_waivers ORDER BY created_at,waiver_id").fetchall()]
    return {"columns": columns, "rows": rows, "meta": meta, "audit_exists": audit_exists, "waivers": waivers}


def expected_row_checks(snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = snapshot["rows"]
    target = next((row for row in rows if row.get("reservation_id") == EXPECTED_RESERVATION), None)
    settled = [row for row in rows if row.get("status") == "settled"]
    unknown = [row for row in rows if row.get("status") == "uncertain"]
    return {
        "reservation_present": target is not None,
        "target_status": target.get("status") if target else None,
        "target_amount_cny": target.get("amount_cny") if target else None,
        "target_actual_cny": target.get("actual_cny") if target else None,
        "target_call_id": target.get("call_id") if target else None,
        "target_matches_expected": bool(target and target.get("status") == "uncertain"
            and target.get("actual_cny") is None
            and abs(float(target.get("amount_cny") or 0) - EXPECTED_UNKNOWN_AMOUNT) < 1e-9
            and target.get("call_id") == EXPECTED_CALL_ID),
        "settled_count": len(settled),
        "settled_actual_cny": round(sum(float(row.get("actual_cny") or 0) for row in settled), 9),
        "uncertain_count": len(unknown),
        "unknown_amount_cny": round(sum(float(row.get("amount_cny") or 0) for row in unknown), 9),
    }


def validate_cache_and_identity() -> dict[str, Any]:
    route, _cli = source_modules()
    identity = load(ROOT / "RUN_IDENTITY.json")
    report = load(ROOT / "RUN_REPORT.json")
    rows = report.get("units", [])
    complete_rows = [row for row in rows if row.get("status") == "complete"]
    complete_checks: list[dict[str, Any]] = []
    complete_ok = True
    for row in complete_rows:
        stage = Path(row["messages_path"]).parent
        attempts = sorted(stage.glob("attempt_*"))
        result_path = attempts[0] / "UNIT_RESULT.json" if len(attempts) == 1 else None
        seal_path = attempts[0] / "RESULT_SEAL.json" if len(attempts) == 1 else None
        result = load(result_path) if result_path and result_path.is_file() else None
        seal = load(seal_path) if seal_path and seal_path.is_file() else None
        body = Path(result["body_path"]) if isinstance(result, dict) and result.get("body_path") else None
        message_equal = bool(
            len(attempts) == 1
            and (stage / "UNIT_MESSAGES.json").is_file()
            and (attempts[0] / "UNIT_MESSAGES.json").is_file()
            and load(stage / "UNIT_MESSAGES.json") == load(attempts[0] / "UNIT_MESSAGES.json")
        )
        seal_ok = bool(
            isinstance(result, dict) and isinstance(seal, dict)
            and seal.get("sha256") == sha256_bytes(stable_json(result))
            and body is not None and body.is_file()
            and seal.get("body_file_sha256") == sha256_file(body)
            and result.get("complete") is True
            and result.get("finish_reason") == "stop"
        )
        item = {"chapter_id": row["chapter_id"], "unit_id": row["unit_id"],
                "attempt_count": len(attempts), "message_equal": message_equal,
                "result_seal_ok": seal_ok, "attempt_dir": str(attempts[0]) if attempts else ""}
        complete_checks.append(item)
        complete_ok = complete_ok and message_equal and seal_ok

    missing = report.get("missing_units") or []
    missing_checks = []
    for key in EXPECTED_MISSING:
        chapter, unit = key.split(":", 1)
        row = next((item for item in rows if item.get("chapter_id") == chapter and item.get("unit_id") == unit), None)
        stage = Path(row["messages_path"]).parent if row and row.get("messages_path") else None
        attempts = sorted(stage.glob("attempt_*")) if stage and stage.exists() else []
        valid_results = [a for a in attempts if (a / "UNIT_RESULT.json").is_file() and (a / "RESULT_SEAL.json").is_file()]
        missing_checks.append({"unit": key, "report_status": row.get("status") if row else None,
                               "attempts": [a.name for a in attempts], "valid_result_attempts": [a.name for a in valid_results]})

    pid_record = load(ROOT / "LIVE_PID.json") if (ROOT / "LIVE_PID.json").is_file() else {}
    pid = int(pid_record.get("pid", 0) or 0)
    source_head = subprocess.run(["git", "-C", str(WORKTREE), "rev-parse", "HEAD"],
                                 check=False, capture_output=True, text=True).stdout.strip()
    source_status = subprocess.run(["git", "-C", str(WORKTREE), "status", "--short"],
                                   check=False, capture_output=True, text=True).stdout.strip()
    identity_ok = (
        source_head == "054f94a3e9659d5b6c4ffcc9bfafd9ae646d0512"
        and not source_status
        and identity.get("code_sha256") == route._code_hash()
        and identity.get("prompt_sha256") == route._hash(route.writer.load_writer_prompt(planning_revision=True))
        and identity.get("profile", {}).get("model") == "qwen3.5-plus"
        and identity.get("profile", {}).get("max_output_tokens") == 32768
        and identity.get("profile", {}).get("thinking_budget") == 8192
    )
    ledger = read_ledger()
    ledger_checks = expected_row_checks(ledger)
    waiver_state = "not_applied"
    if any(row.get("status") == "user_waived" for row in ledger["rows"]):
        waiver_state = "applied"
        waiver_row = next(row for row in ledger["rows"] if row.get("reservation_id") == EXPECTED_RESERVATION)
        audit_rows = [row for row in ledger["waivers"] if row.get("reservation_id") == EXPECTED_RESERVATION]
        ledger_checks.update({
            "waiver_row_status": waiver_row.get("status"),
            "waiver_row_amount_cny": waiver_row.get("amount_cny"),
            "waiver_row_actual_cny": waiver_row.get("actual_cny"),
            "waiver_audit_count": len(audit_rows),
            "waiver_audit_original_amount_cny": audit_rows[0].get("original_amount_cny") if audit_rows else None,
            "waiver_audit_original_actual_cny": audit_rows[0].get("original_actual_cny") if audit_rows else None,
            "waiver_audit_has_user_text": bool(audit_rows and audit_rows[0].get("user_authorization_text")),
        })

    timeout_record = load(ROOT / "units" / "Ch6" / "Ch6_U1" /
                          "17b84ac97c390c34f79ca965553ff149380db6b6fc430dccdabd78e27d8ed3a0" /
                          "attempt_001" / "ERROR.json")
    timeout = timeout_record.get("record") or {}
    timeout_checks = {
        "error": timeout_record.get("error"),
        "error_type": timeout_record.get("type"),
        "stream_event_count": timeout.get("stream_event_count"),
        "partial_content_bytes": timeout.get("partial_content_bytes"),
        "partial_reasoning_bytes": timeout.get("partial_reasoning_bytes"),
        "stream_done": timeout.get("stream_done"),
        "finish_reason_present": "finish_reason" in timeout,
        "usage_present": "usage" in timeout,
        "inactivity_timeout_seconds": (timeout.get("effective_request") or {}).get("stream_inactivity_timeout_seconds"),
        "overall_timeout_seconds": (timeout.get("effective_request") or {}).get("stream_overall_timeout_seconds"),
    }
    result = {
        "schema_version": "legacy_unit_plus.recovery_wrapper_free_check.v1",
        "network_calls": 0,
        "physical_factory_invoked": False,
        "process_alive": process_alive(pid),
        "pid_checked": pid,
        "source_head": source_head,
        "source_worktree_dirty": bool(source_status),
        "source_identity_ok": identity_ok,
        "identity": identity,
        "report_status": report.get("status"),
        "complete_units": report.get("complete_units"),
        "expected_complete_units": 21,
        "missing_units": missing,
        "expected_missing_units": EXPECTED_MISSING,
        "complete_cache_checks": complete_checks,
        "complete_cache_all_ok": complete_ok and len(complete_rows) == 21,
        "missing_cache_checks": missing_checks,
        "missing_cache_all_clean": all(not item["valid_result_attempts"] for item in missing_checks),
        "ledger": ledger_checks,
        "waiver_state": waiver_state,
        "timeout_evidence": timeout_checks,
    }
    result["free_acceptance"] = {
        "no_process": not result["process_alive"],
        "21_success_seals_and_messages": result["complete_cache_all_ok"],
        "8_missing_from_ch6_u1": missing == EXPECTED_MISSING and result["missing_cache_all_clean"],
        "source_identity_054_unchanged": identity_ok,
        "prewaiver_unknown_preserved_or_postwaiver_audited": (
            waiver_state == "not_applied"
            and ledger_checks["target_matches_expected"]
        ) or (
            waiver_state == "applied"
            and ledger_checks.get("waiver_row_status") == "user_waived"
            and float(ledger_checks.get("waiver_row_amount_cny") or 0) == 0
            and ledger_checks.get("waiver_row_actual_cny") is None
            and ledger_checks.get("waiver_audit_count") == 1
            and abs(float(ledger_checks.get("waiver_audit_original_amount_cny") or 0) - EXPECTED_UNKNOWN_AMOUNT) < 1e-9
            and ledger_checks.get("waiver_audit_original_actual_cny") is None
        ),
        "timeout_is_client_read_timeout_only": timeout_checks["error"] == "qwen_stream_read_timeout"
        and timeout_checks["error_type"] == "QwenTransportError"
        and timeout_checks["stream_event_count"] == 646
        and timeout_checks["partial_content_bytes"] == 0
        and timeout_checks["stream_done"] is False
        and timeout_checks["finish_reason_present"] is False
        and timeout_checks["usage_present"] is False,
    }
    result["free_acceptance"]["all_pass"] = all(result["free_acceptance"].values())
    return result


def write_free_check(result: dict[str, Any]) -> None:
    dump(ROOT / "RECOVERY_FREE_CHECK.json", result)
    lines = [
        "# Legacy Plus recovery wrapper: free check",
        "",
        f"- all_pass: `{result['free_acceptance']['all_pass']}`",
        f"- network_calls: `{result['network_calls']}`; physical_factory_invoked: `{result['physical_factory_invoked']}`",
        f"- process_alive: `{result['process_alive']}` (PID checked: `{result['pid_checked']}`)",
        f"- source HEAD: `{result['source_head']}`; source_identity_ok: `{result['source_identity_ok']}`",
        f"- complete units: `{result['complete_units']}/29`; complete cache/seal checks: `{result['complete_cache_all_ok']}`",
        f"- missing units exact from Ch6_U1: `{result['free_acceptance']['8_missing_from_ch6_u1']}`",
        f"- waiver state: `{result['waiver_state']}`",
        f"- timeout evidence is client read-timeout only: `{result['free_acceptance']['timeout_is_client_read_timeout_only']}`",
        "",
        "No provider call, credential read, waiver mutation, or source edit is performed by `--check`.",
    ]
    (ROOT / "RECOVERY_FREE_CHECK.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def backup_before_waive() -> Path:
    base = ROOT / "recovery_backups"
    base.mkdir(exist_ok=True)
    path = base / f"before_waive_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    path.mkdir()
    shutil.copy2(LEDGER, path / "BUDGET.sqlite")
    shutil.copy2(MARKER, path / MARKER.name)
    shutil.copy2(ROOT / "RUN_REPORT.json", path / "RUN_REPORT.json")
    shutil.copytree(ROOT / "assembled", path / "assembled")
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            files.append({"path": str(item.relative_to(path)), "sha256": sha256_file(item), "bytes": item.stat().st_size})
    dump(path / "BACKUP_MANIFEST.json", {"schema_version": "legacy_plus.before_waive_backup.v1", "reservation_id": EXPECTED_RESERVATION, "files": files})
    return path


def waiver_table_sql() -> str:
    return """CREATE TABLE IF NOT EXISTS legacy_budget_waivers (
        waiver_id TEXT PRIMARY KEY,
        reservation_id TEXT NOT NULL UNIQUE,
        authorized_at TEXT NOT NULL,
        user_authorization_text TEXT NOT NULL,
        reason TEXT NOT NULL,
        original_status TEXT NOT NULL,
        original_amount_cny REAL NOT NULL,
        original_actual_cny REAL,
        original_row_json TEXT NOT NULL,
        backup_dir TEXT NOT NULL,
        release_amount_cny REAL NOT NULL,
        created_at REAL NOT NULL
    )"""


def apply_waiver(authorization_text: str) -> dict[str, Any]:
    if not authorization_text.strip():
        raise RuntimeError("authorization_text_required")
    backup = backup_before_waive()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    waiver_id = "waiver-" + uuid.uuid4().hex[:16]
    with sqlite3.connect(LEDGER, timeout=30.0) as db:
        db.execute("BEGIN IMMEDIATE")
        columns = ledger_columns(db)
        row = db.execute(
            "SELECT " + ",".join(columns) + " FROM reservations WHERE reservation_id=?",
            (EXPECTED_RESERVATION,),
        ).fetchone()
        if row is None:
            raise RuntimeError("target_reservation_missing")
        original = row_dict(columns, row)
        if original.get("status") == "user_waived":
            existing = db.execute(
                "SELECT waiver_id,original_row_json,backup_dir FROM legacy_budget_waivers WHERE reservation_id=?",
                (EXPECTED_RESERVATION,),
            ).fetchone()
            db.rollback()
            return {"status": "already_applied", "waiver_id": existing[0] if existing else None,
                    "backup_dir": existing[2] if existing else "", "original_row": json.loads(existing[1]) if existing else original}
        if original.get("status") != "uncertain" or original.get("actual_cny") is not None:
            raise RuntimeError("target_reservation_not_uncertain_with_null_actual")
        if abs(float(original.get("amount_cny") or 0) - EXPECTED_UNKNOWN_AMOUNT) > 1e-9:
            raise RuntimeError("target_uncertain_amount_changed")
        if original.get("call_id") != EXPECTED_CALL_ID:
            raise RuntimeError("target_call_identity_changed")
        db.execute(waiver_table_sql())
        existing = db.execute(
            "SELECT waiver_id FROM legacy_budget_waivers WHERE reservation_id=?",
            (EXPECTED_RESERVATION,),
        ).fetchone()
        if existing:
            db.rollback()
            raise RuntimeError("waiver_audit_already_exists_without_user_waived_row")
        metadata = json.loads(original.get("request_metadata_json") or "{}") if original.get("request_metadata_json") else {}
        metadata["budget_waiver"] = {
            "waiver_id": waiver_id, "reservation_id": EXPECTED_RESERVATION,
            "authorized_at": now, "release_amount_cny": EXPECTED_UNKNOWN_AMOUNT,
            "actual_cny_preserved_as": None, "status": "user_waived",
        }
        db.execute(
            "INSERT INTO legacy_budget_waivers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (waiver_id, EXPECTED_RESERVATION, now, authorization_text,
             "User-authorized release of one historical uncertain occupancy; actual provider charge remains unknown.",
             original["status"], float(original["amount_cny"]), original["actual_cny"],
             json.dumps(original, ensure_ascii=False, sort_keys=True), str(backup), EXPECTED_UNKNOWN_AMOUNT, time.time()),
        )
        db.execute(
            "UPDATE reservations SET amount_cny=0.0,status='user_waived',actual_cny=NULL,request_metadata_json=? WHERE reservation_id=?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), EXPECTED_RESERVATION),
        )
        db.commit()
    record = {
        "schema_version": "legacy_plus.user_budget_waiver.v1",
        "waiver_id": waiver_id,
        "reservation_id": EXPECTED_RESERVATION,
        "status_transition": "uncertain -> user_waived",
        "original_amount_cny": EXPECTED_UNKNOWN_AMOUNT,
        "released_budget_amount_cny": EXPECTED_UNKNOWN_AMOUNT,
        "actual_cny": None,
        "provider_charge_claim": "unknown_and_preserved_as_unknown",
        "authorized_at": now,
        "user_authorization_text": authorization_text,
        "backup_dir": str(backup),
        "network_calls": 0,
        "source_worktree_changed": False,
    }
    target = ROOT / "BUDGET_WAIVER.json"
    if target.exists():
        raise RuntimeError("BUDGET_WAIVER.json_already_exists_after_ledger_mutation")
    dump(target, record)
    return record


def waived_snapshot(ledger: Path, marker: Path, scope: dict[str, Any]) -> dict[str, Any]:
    """Read the same ledger while excluding only the audited user_waived row.

    The database row remains amount_cny=0 and actual_cny=NULL.  The unchanged
    CLI guard sees only reservations that can still block a call; audited
    waivers are exposed separately with their original amount and authorization.
    """
    if Path(ledger).resolve() != LEDGER.resolve() or Path(marker).resolve() != MARKER.resolve():
        raise RuntimeError("waiver_wrapper_scope_mismatch")
    if load(marker) != scope:
        raise RuntimeError("waiver_wrapper_marker_scope_mismatch")
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
        rows = db.execute("SELECT reservation_id,amount_cny,actual_cny,status FROM reservations ORDER BY created_at,reservation_id").fetchall()
        audit_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_budget_waivers'"
        ).fetchone() is not None
        audit_rows = []
        if audit_exists:
            audit_columns = [item[1] for item in db.execute("PRAGMA table_info(legacy_budget_waivers)").fetchall()]
            audit_rows = [dict(zip(audit_columns, item)) for item in db.execute(
                "SELECT " + ",".join(audit_columns) + " FROM legacy_budget_waivers"
            ).fetchall()]
    if row is None or float(row[0]) != float(scope["budget_limit_cny"]):
        raise RuntimeError("waiver_wrapper_budget_limit_mismatch")
    current_by_reservation = {
        rid: {"amount_cny": amount, "actual_cny": cost, "status": status}
        for rid, amount, cost, status in rows
    }
    audits_by_reservation = {}
    for audit in audit_rows:
        rid = audit.get("reservation_id")
        if not rid or rid in audits_by_reservation:
            raise RuntimeError("waiver_wrapper_duplicate_or_missing_reservation_audit")
        waiver_id = str(audit.get("waiver_id") or "")
        original = json.loads(audit.get("original_row_json") or "{}")
        current = current_by_reservation.get(rid)
        if (not waiver_id.startswith("waiver-")
                or current is None
                or current.get("status") != "user_waived"
                or current.get("actual_cny") is not None
                or float(current.get("amount_cny") or 0) != 0.0
                or original.get("reservation_id") != rid
                or original.get("status") != "uncertain"
                or original.get("actual_cny") is not None
                or abs(float(original.get("amount_cny") or 0) - float(audit.get("original_amount_cny") or 0)) > 1e-9):
            raise RuntimeError("waiver_wrapper_audit_identity_mismatch")
        audits_by_reservation[rid] = audit
    raw_statuses = {status for _rid, _amount, _cost, status in rows}
    if not raw_statuses.issubset({"settled", "reserved", "uncertain", "user_waived"}):
        raise RuntimeError("waiver_wrapper_unknown_ledger_status")
    for rid, _amount, _cost, status in rows:
        if status == "user_waived" and rid not in audits_by_reservation:
            raise RuntimeError("waiver_wrapper_unaudited_user_waived_row")
    actual = sum(float(cost or 0) for _rid, _amount, cost, _status in rows)
    held = sum(float(amount or 0) for _rid, amount, _cost, status in rows if status in {"reserved", "uncertain"})
    used = sum(
        float(cost if cost is not None else amount or 0) if status == "settled"
        else float(amount or 0) if status in {"reserved", "uncertain"}
        else 0.0
        for _rid, amount, cost, status in rows
    )
    reservations = []
    waived_reservations = []
    for rid, amount, cost, status in rows:
        if status == "user_waived":
            audit = audits_by_reservation[rid]
            waived_reservations.append({
                "reservation_id": rid, "amount_cny": 0.0, "actual_cny": None,
                "status": status, "waiver_id": audit["waiver_id"],
                "original_amount_cny": audit["original_amount_cny"],
                "user_authorization_text": audit["user_authorization_text"],
            })
        else:
            reservations.append({"reservation_id": rid, "amount_cny": amount, "actual_cny": cost, "status": status})
    return {"ledger_path": str(ledger), "limit_cny": float(scope["budget_limit_cny"]),
            "actual_cny": actual, "reserved_cny": held,
            "remaining_cny": max(0.0, float(scope["budget_limit_cny"]) - used),
            "reservations": reservations, "waived_reservations": waived_reservations}


def finalize_report() -> None:
    path = ROOT / "RUN_REPORT.json"
    if not path.is_file() or not (ROOT / "BUDGET_WAIVER.json").is_file():
        return
    report = load(path)
    ledger = read_ledger()
    rows = []
    for row in ledger["rows"]:
        if row.get("status") != "user_waived":
            rows.append({"reservation_id": row.get("reservation_id"), "amount_cny": row.get("amount_cny"),
                         "actual_cny": row.get("actual_cny"), "status": row.get("status")})
    waived_rows = []
    for row in ledger["rows"]:
        if row.get("status") == "user_waived":
            audit = next((item for item in ledger["waivers"] if item.get("reservation_id") == row.get("reservation_id")), None)
            if audit is None:
                raise RuntimeError("missing_waiver_audit_during_report_finalize")
            waived_rows.append({
                "reservation_id": row.get("reservation_id"), "amount_cny": 0.0,
                "actual_cny": None, "status": "user_waived", "waiver_id": audit.get("waiver_id"),
                "original_amount_cny": audit.get("original_amount_cny"),
                "user_authorization_text": audit.get("user_authorization_text"),
            })
    settled_actual = sum(float(row["actual_cny"] or 0) for row in rows if row["status"] == "settled")
    reserved = sum(float(row["amount_cny"] or 0) for row in rows if row["status"] in {"reserved", "uncertain"})
    used = sum(
        float(row["actual_cny"] if row["actual_cny"] is not None else row["amount_cny"] or 0) if row["status"] == "settled"
        else float(row["amount_cny"] or 0) if row["status"] in {"reserved", "uncertain"}
        else 0.0 for row in rows
    )
    waiver = load(ROOT / "BUDGET_WAIVER.json")
    report["ledger"] = {
        "ledger_path": str(LEDGER), "limit_cny": 30.0, "actual_cny": settled_actual,
        "reserved_cny": reserved, "remaining_cny": max(0.0, 30.0 - used),
        "reservations": rows, "waived_reservations": waived_rows,
        "budget_waiver": waiver,
        "unknown_cost_reservations": [row for row in rows if row["status"] == "uncertain"],
    }
    report["unsettled_budget_holds"] = sum(row["status"] in {"reserved", "uncertain"} for row in rows)
    report["budget_waiver"] = waiver
    dump(path, report)


def run_live() -> int:
    snapshot = read_ledger()
    checks = expected_row_checks(snapshot)
    if not any(row.get("status") == "user_waived" for row in snapshot["rows"]):
        raise RuntimeError("apply_explicit_waiver_before_live_recovery")
    if process_alive(int((load(ROOT / "LIVE_PID.json") if (ROOT / "LIVE_PID.json").is_file() else {}).get("pid", 0) or 0)):
        raise RuntimeError("existing_live_process_is_still_running")
    route, cli = source_modules()
    original_snapshot = cli._ledger_snapshot
    cli._ledger_snapshot = waived_snapshot
    args = [
        "--input", str(INPUT), "--output-dir", str(ROOT), "--model", "qwen3.5-plus",
        "--budget-limit", "30", "--ledger", str(LEDGER), "--tokenizer", str(TOKENIZER),
        "--key-file", str(KEY_FILE), "--run", "--retry-failed",
    ]
    try:
        return_code = cli.main(args)
    finally:
        cli._ledger_snapshot = original_snapshot
        finalize_report()
    print(json.dumps({"return_code": return_code, "new_calls_are_live": True, "old_success_cache_is_reused": True}, ensure_ascii=False, indent=2))
    return return_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="No-network free verification (default)")
    mode.add_argument("--waive", action="store_true", help="Apply the explicit single-reservation user waiver")
    mode.add_argument("--run", action="store_true", help="Invoke the unchanged legacy CLI with --run --retry-failed")
    parser.add_argument("--authorization-text", default=AUTHORIZATION_TEXT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.waive:
        record = apply_waiver(args.authorization_text)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    if args.run:
        return run_live()
    result = validate_cache_and_identity()
    write_free_check(result)
    print(json.dumps({"all_pass": result["free_acceptance"]["all_pass"],
                      "waiver_state": result["waiver_state"],
                      "network_calls": 0,
                      "complete_units": result["complete_units"],
                      "missing_units": result["missing_units"]}, ensure_ascii=False, indent=2))
    return 0 if result["free_acceptance"]["all_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
