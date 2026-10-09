"""Guarded local preparation for the guide-maker retest.

This wrapper is deliberately kept outside the source worktree.  It owns the
single retest lock, persists one S0 baseline at the retest root, and constrains
every live Qwen client created by the guide-maker factory.  It does not make a
live call unless ``run-live`` is explicitly selected by the operator after
reviewing the free evidence.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Mapping


RETEST_ROOT = Path(__file__).resolve().parent
WORKTREE = Path(r"F:\OptoMind-Review-2\outputs\guide_maker_max_retest_20261009")
PROJECT_ROOT = WORKTREE
LEDGER = Path(r"F:\OptoMind-Review-2\outputs\guide_maker_acceptance_20261008_30cny\guide_budget.sqlite")
MARKER = Path(str(LEDGER) + ".guide_maker.json")
MANIFEST = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json")
TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
KEY_FILE = Path(r"F:\OptoMind-Review-2\api_keys\qwen-api-key.txt")
CONFIG = WORKTREE / "config" / "guide_maker" / "explicit_max.json"
S0_PATH = Path(r"F:\OptoMind-Review-2\outputs\guide_maker_retest_20261009_10cny\S0_BASELINE.json")
LOCK_PATH = Path(str(LEDGER) + ".guide_retest.lock")
FEEDBACK_PATH = RETEST_ROOT / "USER_FEEDBACK.txt"
FEEDBACK_TEXT = "最终正文连同引言应实际引用至少150篇去重文献。当前只生成正文写作指南，请保留这一全文目标并安排有实质用途的来源；不要机械平分章节指标，不堆列未实际讨论的引用，也不要把尚未生成的引言视为已完成引用。允许局部写法仍有缺陷，选择相对更好的可用安排。"
FREE_ROOT = RETEST_ROOT / "free_tests"
PREVIEW_ROOT = RETEST_ROOT / "message_preview"
LIVE_ROOT = RETEST_ROOT / "cold_start_live"
MODEL = "qwen3.8-max"
ORIGINAL_LIMIT = 30.0
MARKER_SCHEMA = "optomind.guide_maker_budget.v1"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_json_bytes(value) + b"\n")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RetestLock:
    """A Windows/POSIX advisory lock beside the real ledger."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("guide_retest_parallel_ledger_entry_blocked") from exc
        return self

    def __exit__(self, *_exc):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _assert_inputs() -> None:
    expected = {
        "manifest": MANIFEST,
        "tokenizer": TOKENIZER,
        "config": CONFIG,
        "ledger": LEDGER,
        "marker": MARKER,
    }
    missing = [name for name, path in expected.items() if not path.is_file()]
    if not WORKTREE.is_dir():
        missing.insert(0, "worktree")
    if missing:
        raise RuntimeError("missing_retest_input:" + ",".join(missing))
    head = os.popen("git -C " + _quote(WORKTREE) + " rev-parse HEAD").read().strip()
    if head != "d834d79461591e1c3dc1ba00f6084314d5aa0d3b":
        raise RuntimeError("fixed_source_sha_mismatch:" + head)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if config.get("maker", {}).get("model") != MODEL:
        raise RuntimeError("config_model_must_be_qwen3.8-max")
    if not S0_PATH.is_file():
        raise RuntimeError("original_fixed_s0_missing")
    s0 = json.loads(S0_PATH.read_text(encoding="utf-8"))
    if s0.get("s0_actual_cny") != 0.517030 or s0.get("cap_cny") != 10.517030:
        raise RuntimeError("original_fixed_s0_mismatch")
    if FEEDBACK_PATH.read_text(encoding="utf-8") != FEEDBACK_TEXT:
        raise RuntimeError("feedback_not_exact_user_requirement")


def _quote(path: Path) -> str:
    """Quote a path for the tiny git probe without ever including secrets."""
    return '"' + str(path).replace('"', '\\"') + '"'


def _marker_object() -> dict[str, Any]:
    marker = json.loads(MARKER.read_text(encoding="utf-8"))
    expected = {
        "schema_version": MARKER_SCHEMA,
        "ledger_path": str(LEDGER),
        "limit_cny": ORIGINAL_LIMIT,
    }
    if marker != expected:
        raise RuntimeError("guide_budget_marker_changed")
    return marker


def _ledger_snapshot() -> dict[str, Any]:
    """Read the original ledger without allowing SQLite initialization/migration."""
    _assert_inputs()
    marker = _marker_object()
    uri = LEDGER.as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=0.0)
        try:
            stored = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if stored is None or float(stored[0]) != ORIGINAL_LIMIT:
                raise RuntimeError("guide_budget_db_limit_changed")
            columns = {row[1] for row in db.execute("PRAGMA table_info(reservations)")}
            required = {"amount_cny", "actual_cny", "status"}
            if not required.issubset(columns):
                raise RuntimeError("guide_budget_reservations_schema_invalid")
            rows = db.execute("SELECT amount_cny,actual_cny,status FROM reservations ORDER BY created_at,reservation_id").fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise RuntimeError("guide_budget_read_only_check_failed") from exc
    actual = held = reserved = uncertain = used = 0.0
    statuses: dict[str, int] = {}
    for amount, cost, status in rows:
        if status not in {"settled", "reserved", "uncertain"}:
            raise RuntimeError("guide_budget_unknown_status:" + str(status))
        amount = float(amount)
        if not math.isfinite(amount) or amount < 0:
            raise RuntimeError("guide_budget_invalid_amount")
        if cost is not None:
            cost = float(cost)
            if not math.isfinite(cost) or cost < 0:
                raise RuntimeError("guide_budget_invalid_actual")
        statuses[status] = statuses.get(status, 0) + 1
        actual += cost or 0.0
        if status == "reserved":
            reserved += amount
        if status == "uncertain":
            uncertain += amount
        held += amount if status in {"reserved", "uncertain"} else 0.0
        used += (cost if cost is not None else amount) if status == "settled" else amount
    return {
        "ledger_path": str(LEDGER),
        "marker_path": str(MARKER),
        "marker_sha256": _sha256(MARKER),
        "limit_cny": ORIGINAL_LIMIT,
        "actual_cny": round(actual, 9),
        "reserved_cny": round(reserved, 9),
        "uncertain_cny": round(uncertain, 9),
        "held_cny": round(held, 9),
        "used_for_cap_cny": round(used, 9),
        "remaining_cny": round(ORIGINAL_LIMIT - used, 9),
        "reservation_count": len(rows),
        "statuses": statuses,
    }


def _ensure_s0(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot["reserved_cny"] or snapshot["uncertain_cny"]:
        raise RuntimeError("guide_budget_has_open_or_uncertain_reservation_stop_before_paid_run")
    if S0_PATH.exists():
        existing = json.loads(S0_PATH.read_text(encoding="utf-8"))
        required = ("schema_version", "ledger_path", "marker_path", "marker_sha256", "original_limit_cny", "s0_actual_cny", "s0_reserved_cny", "s0_uncertain_cny", "cap_cny", "sidecar_path")
        if any(field not in existing for field in required):
            raise RuntimeError("guide_retest_s0_sidecar_incomplete")
        stable = {
            "schema_version": "optomind.guide_maker_retest_s0.v1",
            "ledger_path": str(LEDGER),
            "marker_path": str(MARKER),
            "marker_sha256": snapshot["marker_sha256"],
            "original_limit_cny": ORIGINAL_LIMIT,
            "sidecar_path": str(S0_PATH),
        }
        for field, value in stable.items():
            if existing.get(field) != value:
                raise RuntimeError("guide_retest_s0_sidecar_mismatch:" + field)
        if any(float(existing.get(field, -1)) < 0 for field in ("s0_actual_cny", "s0_reserved_cny", "s0_uncertain_cny")):
            raise RuntimeError("guide_retest_s0_sidecar_invalid_spend")
        expected_cap = min(ORIGINAL_LIMIT, float(existing["s0_actual_cny"]) + 10.0)
        if float(existing.get("cap_cny", -1)) != expected_cap or float(existing["s0_reserved_cny"]) != 0 or float(existing["s0_uncertain_cny"]) != 0:
            raise RuntimeError("guide_retest_s0_sidecar_cap_invalid")
        return existing
    cap = min(ORIGINAL_LIMIT, float(snapshot["actual_cny"]) + 10.0)
    proposed = {
        "schema_version": "optomind.guide_maker_retest_s0.v1",
        "ledger_path": str(LEDGER),
        "marker_path": str(MARKER),
        "marker_sha256": snapshot["marker_sha256"],
        "original_limit_cny": ORIGINAL_LIMIT,
        "s0_actual_cny": float(snapshot["actual_cny"]),
        "s0_reserved_cny": float(snapshot["reserved_cny"]),
        "s0_uncertain_cny": float(snapshot["uncertain_cny"]),
        "cap_cny": cap,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sidecar_path": str(S0_PATH),
    }
    # Exclusive creation is required for the first baseline.  The ledger lock
    # is held by the caller, and O_EXCL protects against a second first writer.
    S0_PATH.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(S0_PATH, flags)
    except FileExistsError:
        return _ensure_s0(snapshot)
    try:
        os.write(fd, _json_bytes(proposed) + b"\n")
    finally:
        os.close(fd)
    return proposed


def _guard_state() -> tuple[dict[str, Any], dict[str, Any]]:
    with RetestLock(LOCK_PATH):
        snapshot = _ledger_snapshot()
        s0 = _ensure_s0(snapshot)
    # Re-read after releasing the short lock so evidence records the state that
    # the next action actually saw.  The live path reacquires the lock around
    # its complete invocation.
    return snapshot, s0


def _inside_retest(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(RETEST_ROOT)
    except ValueError as exc:
        raise RuntimeError("output_must_be_inside_guide_maker_retest_root") from exc
    return resolved


def _run_cli(argv: list[str], output: Path) -> int:
    from scripts.upgrade3 import guide_maker as cli
    output.mkdir(parents=True, exist_ok=True)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    (output / "WRAPPER_CLI_STDOUT.txt").write_text(stdout.getvalue(), encoding="utf-8")
    (output / "WRAPPER_CLI_STDERR.txt").write_text(stderr.getvalue(), encoding="utf-8")
    return int(code)


def _preview_message(output: Path) -> Path:
    plans = sorted(output.glob("runs/*/plans/maker_001/REQUEST_PREVIEW.json"))
    if not plans:
        raise RuntimeError("first_message_preview_missing")
    request = json.loads(plans[-1].read_text(encoding="utf-8"))
    messages_path = Path(request["messages_path"])
    messages = json.loads(messages_path.read_text(encoding="utf-8"))
    payloads = []
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping) and "full_outline" in value:
                payloads.append(value)
    if len(payloads) != 1:
        raise RuntimeError("first_message_payload_not_unique")
    payload = payloads[0]
    if payload.get("prior_guide") is not None or payload.get("materials") not in ([], None):
        raise RuntimeError("first_message_contains_prior_guide_or_materials")
    if payload.get("feedback") != FEEDBACK_TEXT:
        raise RuntimeError("first_message_contains_feedback")
    target = output / "FIRST_MESSAGE_PREVIEW.json"
    _write_json(target, {"request_preview": str(plans[-1]), "messages_path": str(messages_path), "messages": messages, "payload_checks": {"prior_guide": None, "materials": [], "feedback": FEEDBACK_TEXT}})
    return target


def _patch_live_factory(cap: float, *, block_network: bool = False):
    from optomind_research.runtime.upgrade3.module4 import runtime as module4
    real_ledger = module4.GlobalBudgetLedger
    real_client = module4.QwenDirectClient

    class CappedLedger(real_ledger):
        def __post_init__(self):
            super().__post_init__()
            if self.path is None or Path(self.path).resolve() != LEDGER:
                raise RuntimeError("live_ledger_must_be_original_guide_budget")
            self.limit_cny = min(float(self.limit_cny or ORIGINAL_LIMIT), cap)

        def _refresh_from_db(self):
            super()._refresh_from_db()
            self.limit_cny = min(float(self.limit_cny or ORIGINAL_LIMIT), cap)

        def reserve(self, amount_cny: float, call_id: str):
            self.limit_cny = min(float(self.limit_cny or ORIGINAL_LIMIT), cap)
            return super().reserve(amount_cny, call_id)

    class MaxOnlyClient(real_client):
        def __init__(self, *args, **kwargs):
            model = kwargs.get("model", MODEL)
            if model != MODEL:
                raise RuntimeError("live_wire_model_rejected:" + str(model))
            if not isinstance(kwargs.get("budget_ledger"), CappedLedger):
                raise RuntimeError("live_client_missing_capped_ledger")
            if kwargs.get("max_retries", 0) != 0 or kwargs.get("max_keys", 1) != 1:
                raise RuntimeError("live_client_retry_or_key_rotation_not_allowed")
            super().__init__(*args, **kwargs)

        def __call__(self, messages, **kwargs):
            if kwargs.get("model", self.model) != MODEL:
                raise RuntimeError("live_wire_model_rejected:" + str(kwargs.get("model")))
            if block_network:
                raise RuntimeError("free_test_network_boundary")
            return super().__call__(messages, **kwargs)

    # The production entry point imports these names from the authoritative
    # runtime module dynamically.  Replace that module and any already-loaded
    # aliases so a supplemental reader cannot silently create an uncapped
    # client during this retest.
    for loaded in list(sys.modules.values()):
        if loaded is None:
            continue
        if getattr(loaded, "GlobalBudgetLedger", None) is real_ledger:
            setattr(loaded, "GlobalBudgetLedger", CappedLedger)
        if getattr(loaded, "QwenDirectClient", None) is real_client:
            setattr(loaded, "QwenDirectClient", MaxOnlyClient)
    module4.GlobalBudgetLedger = CappedLedger
    module4.QwenDirectClient = MaxOnlyClient
    return CappedLedger, MaxOnlyClient


def _assert_live_evidence(output: Path, before: Mapping[str, Any], s0: Mapping[str, Any]) -> None:
    after = _ledger_snapshot()
    if after["limit_cny"] != ORIGINAL_LIMIT or after["marker_sha256"] != s0["marker_sha256"]:
        raise RuntimeError("live_run_changed_original_limit_or_marker")
    if after["used_for_cap_cny"] > float(s0["cap_cny"]) + 1e-8:
        raise RuntimeError("live_run_exceeded_retest_cap")
    for path in output.glob("stages/**/attempt_*/ACTUAL_REQUEST.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("profile", {}).get("model") != MODEL:
            raise RuntimeError("actual_request_non_max_model:" + str(path))
    for path in output.glob("stages/**/attempt_*/USAGE.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("model") != MODEL:
            raise RuntimeError("usage_non_max_model:" + str(path))
    _write_json(output / "BUDGET_CAP_CHECK.json", {"before": before, "after": after, "s0": s0, "cap_cny": s0["cap_cny"], "db_limit_unchanged": True, "marker_unchanged": True, "all_wire_models": MODEL})


def command_selftest() -> int:
    before = _ledger_snapshot()
    original_db_hash = _sha256(LEDGER)
    _, s0 = _guard_state()
    s0_bytes = S0_PATH.read_bytes()
    simulated_resume = dict(before, actual_cny=float(s0["s0_actual_cny"]) + 1.0, used_for_cap_cny=float(s0["s0_actual_cny"]) + 1.0)
    assert _ensure_s0(simulated_resume) == s0, "S0 recovery rebased after simulated spend"
    assert S0_PATH.read_bytes() == s0_bytes, "S0 recovery rewrote the fixed sidecar"
    FREE_ROOT.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="ledger_guard_", dir=FREE_ROOT))
    try:
        from optomind_research.runtime.upgrade3.module4 import runtime as module4
        real_ledger = module4.GlobalBudgetLedger
        test_db = temp / "synthetic.sqlite"
        real_ledger(limit_cny=30.0, path=test_db)
        real_ledger_cls = real_ledger
        cap = 2.0

        class TestCapped(real_ledger_cls):
            def __post_init__(self):
                super().__post_init__()
                self.limit_cny = cap
            def _refresh_from_db(self):
                super()._refresh_from_db()
                self.limit_cny = cap

        ledger = TestCapped(limit_cny=30.0, path=test_db)
        first = ledger.reserve(1.5, "free-first")
        blocked = False
        try:
            ledger.reserve(0.6, "free-over-cap")
        except module4.QwenTransportError as exc:
            blocked = "global_budget_exceeded" in str(exc)
        assert blocked, "synthetic over-cap reservation was not blocked"
        ledger.settle(first["reservation_id"], 1.4)
        resumed = TestCapped(limit_cny=30.0, path=test_db)
        assert resumed.as_dict()["actual_cny"] == 1.4, "synthetic resume reset actual spend"
        marker_hash = _sha256(MARKER)
        capped_cls, max_client_cls = _patch_live_factory(float(s0["cap_cny"]), block_network=True)
        original_ledger = capped_cls(limit_cny=ORIGINAL_LIMIT, path=LEDGER)
        try:
            max_client_cls(model="qwen3.5-plus", key_file="missing", budget_ledger=original_ledger, max_retries=0, max_keys=1)
        except Exception as exc:
            assert "live_wire_model_rejected" in str(exc), "Max rejection did not happen at wrapper boundary"
        else:
            raise AssertionError("Max model was accepted")
        # Exercise the real shared factory path.  Construction must produce a
        # capped Plus client, while the network boundary remains blocked for
        # this free test.
        import argparse
        from scripts.upgrade3 import writer_candidates as shared
        args = argparse.Namespace(run=True, responses=None, budget_ledger=str(LEDGER), budget_limit=ORIGINAL_LIMIT, key_file=str(KEY_FILE), allow_max=True)
        factory = shared.make_live_factory(args, token_counter=None)
        stage_dir = FREE_ROOT / "factory_stage"
        stage_dir.mkdir(parents=True, exist_ok=True)
        call = factory("maker", stage_dir, json.loads(CONFIG.read_text(encoding="utf-8"))["maker"])
        assert getattr(call, "model", None) == MODEL, "factory selected a non-Plus model"
        providers = [cell.cell_contents for cell in (call.__closure__ or ()) if isinstance(cell.cell_contents, max_client_cls)]
        assert len(providers) == 1 and providers[0].max_retries == 0 and providers[0].max_keys == 1, "factory enabled retries or key rotation"
        try:
            call([{"role": "user", "content": "free network boundary probe"}], model=MODEL)
        except RuntimeError as exc:
            assert "free_test_network_boundary" in str(exc), "factory probe did not stop at network boundary"
        else:
            raise AssertionError("free factory probe reached network")
        after = _ledger_snapshot()
        assert before == after, "free selftest changed original ledger"
        assert marker_hash == _sha256(MARKER), "free selftest changed marker"
        assert original_db_hash == _sha256(LEDGER), "free selftest changed original database bytes"
        _write_json(FREE_ROOT / "SELFTEST_RESULT.json", {"status": "passed", "checks": ["over_cap_blocked", "resume_preserves_spend", "s0_recovery_does_not_rebase", "other_model_rejected", "authoritative_factory_capped", "network_boundary_blocked", "original_db_and_marker_unchanged"], "s0_sidecar": str(S0_PATH), "s0": s0})
    finally:
        temp.resolve().relative_to(FREE_ROOT.resolve())
        shutil.rmtree(temp, ignore_errors=True)
    return 0


def command_preview() -> int:
    with RetestLock(LOCK_PATH):
        before = _ledger_snapshot()
        s0 = _ensure_s0(before)
        output = _inside_retest(PREVIEW_ROOT)
        argv = ["--manifest", str(MANIFEST), "--config", str(CONFIG), "--allow-max", "--feedback", str(FEEDBACK_PATH), "--tokenizer", str(TOKENIZER), "--output", str(output)]
        code = _run_cli(argv, output)
        if code != 0:
            raise RuntimeError("free_message_preview_failed:" + str(code))
        message = _preview_message(output)
        after = _ledger_snapshot()
        if before != after:
            raise RuntimeError("free_preview_changed_original_ledger")
        _write_json(output / "PREVIEW_CHECK.json", {"status": "passed", "message_preview": str(message), "s0_sidecar": str(S0_PATH), "s0": s0, "ledger_unchanged": True, "paid_calls": 0})
    return 0


def command_live() -> int:
    # This command is intentionally available for the later root-approved
    # cold start; this task does not invoke it.
    with RetestLock(LOCK_PATH):
        before = _ledger_snapshot()
        s0 = _ensure_s0(before)
        output = _inside_retest(LIVE_ROOT)
        from optomind_research.runtime.upgrade3.module4 import runtime as module4
        _patch_live_factory(float(s0["cap_cny"]))
        argv = ["--manifest", str(MANIFEST), "--config", str(CONFIG), "--allow-max", "--feedback", str(FEEDBACK_PATH), "--tokenizer", str(TOKENIZER), "--output", str(output), "--run", "--budget-mode", "dedicated", "--budget-ledger", str(LEDGER), "--budget-limit", str(ORIGINAL_LIMIT), "--key-file", str(KEY_FILE)]
        code = _run_cli(argv, output)
        if code not in (0, 3):
            raise RuntimeError("cold_start_live_failed:" + str(code))
        _assert_live_evidence(output, before, s0)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    if len(argv) != 1 or argv[0] not in {"selftest", "preview", "run-live"}:
        print("usage: run_retest.py {selftest|preview|run-live}", file=sys.stderr)
        return 2
    try:
        return {"selftest": command_selftest, "preview": command_preview, "run-live": command_live}[argv[0]]()
    except Exception as exc:
        _write_json(RETEST_ROOT / "WRAPPER_EXCEPTION.json", {"error": type(exc).__name__, "message": str(exc)})
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
