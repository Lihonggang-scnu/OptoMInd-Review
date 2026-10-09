"""Guarded, offline-first launcher for the repaired guide-B trial.

This wrapper keeps the production guided-body CLI unchanged.  It freezes the
round-two ledger's starting spend in a campaign file and narrows every
GlobalBudgetLedger instance to that campaign ceiling.  The underlying ledger
reserve remains the production BEGIN IMMEDIATE operation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


RUN_ROOT = Path(__file__).resolve().parent
WORKTREE = RUN_ROOT / "worktree"
CAMPAIGN_PATH = RUN_ROOT / "CAMPAIGN_BUDGET_START.json"
CAMPAIGN_LOCK = RUN_ROOT / "CAMPAIGN_BUDGET_START.lock"
LEASE_PATH = RUN_ROOT / "CAMPAIGN_RUN_LEASE.json"
RECORDS = RUN_ROOT / "records"
EXPECTED_COMMIT = "145d68788037b4aa31295253d0b0d06f7de88685"
EXPECTED_LEDGER = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\new_round2_budget.sqlite")
EXPECTED_MARKER = Path(str(EXPECTED_LEDGER) + ".evidence_round2.json")
EXPECTED_MANIFEST = Path(r"F:\OptoMind-Review-2\outputs\evidence_body_round2_20261008_60cny\PREPARED_MANIFEST.json")
EXPECTED_GUIDE = WORKTREE / "docs" / "guided_body_writer" / "guides" / "guide_B.json"
EXPECTED_CONFIG = WORKTREE / "config" / "guided_body_writer" / "plus_first.json"
EXPECTED_TOKENIZER = Path(r"F:\OptoMind-Review-2\data\tokenizers\qwen3_5_9b\tokenizer.json")
EXPECTED_KEY = Path(r"[REDACTED_LOCAL_SECRET_PATH]")
MARKER_VALUE = {"schema_version": "optomind.evidence_round2_budget.v1",
                "ledger_path": str(EXPECTED_LEDGER.resolve()), "limit_cny": 60}
PLUS_MODEL = "qwen3.5-plus"
EXPECTED_EFFECTIVE_PROFILE = {
    "model": PLUS_MODEL, "thinking_budget": 16384, "answer_tokens": 49152,
    "max_completion_tokens": 65536, "stream": True,
    "stream_inactivity_timeout_seconds": 1800.0,
    "stream_overall_timeout_seconds": 3600.0,
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _option(argv: list[str], name: str, default: str | None = None) -> str | None:
    for index, item in enumerate(argv):
        if item == name:
            if index + 1 >= len(argv):
                raise ValueError(f"missing_value:{name}")
            return argv[index + 1]
    return default


def _has(argv: list[str], name: str) -> bool:
    return name in argv


def _resolved(value: str | None, *, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=WORKTREE,
                                   text=True, stderr=subprocess.STDOUT).strip()


def _read_ledger(path: Path) -> dict[str, Any]:
    if path.resolve() != EXPECTED_LEDGER.resolve():
        raise ValueError("campaign_ledger_identity_changed")
    if not path.is_file() or not EXPECTED_MARKER.is_file():
        raise ValueError("original_round_two_ledger_and_marker_must_exist")
    marker = _json(EXPECTED_MARKER)
    if marker != MARKER_VALUE:
        raise ValueError("original_round_two_marker_identity_changed")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()
            if row is None or float(row[0]) != 60.0:
                raise ValueError("original_round_two_stored_limit_must_equal_60")
            rows = [dict(item) for item in db.execute(
                "SELECT reservation_id,call_id,amount_cny,actual_cny,status FROM reservations ORDER BY rowid")]
    except sqlite3.Error as exc:
        raise ValueError("campaign_ledger_invalid_sqlite") from exc
    if any(row["status"] not in {"settled", "reserved", "uncertain"} for row in rows):
        raise ValueError("campaign_ledger_invalid_reservation_status")
    settled = sum(float(row["actual_cny"] or 0.0) for row in rows if row["status"] == "settled")
    held = sum(float(row["amount_cny"] or 0.0) for row in rows if row["status"] in {"reserved", "uncertain"})
    return {
        "row_count": len(rows), "rows": rows, "settled_cny": settled,
        "reserved_or_uncertain_cny": held, "stored_limit_cny": 60.0,
        "ledger_sha256": _sha(path), "marker_sha256": _sha(EXPECTED_MARKER),
    }


def _identity(manifest: Path, guide: Path, config: Path, tokenizer: Path) -> dict[str, Any]:
    return {
        "source_commit": _commit(), "manifest_path": str(manifest), "manifest_sha256": _sha(manifest),
        "guide_path": str(guide), "guide_sha256": _sha(guide),
        "config_path": str(config), "config_sha256": _sha(config),
        "tokenizer_path": str(tokenizer), "tokenizer_sha256": _sha(tokenizer),
        "wrapper_path": str(Path(__file__).resolve()), "wrapper_sha256": _sha(Path(__file__).resolve()),
    }


def _baseline_projection(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: row.get(key) for key in ("reservation_id", "call_id", "amount_cny", "actual_cny", "status")}
            for row in rows]


def ensure_campaign(manifest: Path, guide: Path, config: Path, tokenizer: Path) -> dict[str, Any]:
    for actual_raw, expected, label in ((manifest, EXPECTED_MANIFEST.resolve(), "manifest"),
                                        (guide, EXPECTED_GUIDE.resolve(), "guide"),
                                        (config, EXPECTED_CONFIG.resolve(), "config"),
                                        (tokenizer, EXPECTED_TOKENIZER.resolve(), "tokenizer")):
        actual = actual_raw.resolve()
        if actual != expected:
            raise ValueError(f"campaign_{label}_identity_changed")
        if not actual.is_file():
            raise ValueError(f"campaign_{label}_missing")
    if _commit() != EXPECTED_COMMIT:
        raise ValueError("campaign_source_commit_changed")
    state = _read_ledger(EXPECTED_LEDGER)
    if state["reserved_or_uncertain_cny"] > 1e-9:
        raise ValueError("campaign_open_reservation_requires_manual_review")
    identity = _identity(manifest, guide, config, tokenizer)
    effective = min(60.0, state["settled_cny"] + state["reserved_or_uncertain_cny"] + 40.0)
    if CAMPAIGN_PATH.exists():
        campaign = _json(CAMPAIGN_PATH)
        for field, value in (("schema_version", "guided_body_b_repair_budget.v1"),
                             ("ledger_path", str(EXPECTED_LEDGER.resolve())),
                             ("marker_path", str(EXPECTED_MARKER.resolve())),
                             ("stored_limit_cny", 60.0),
                             ("source_commit", EXPECTED_COMMIT)):
            if campaign.get(field) != value:
                raise ValueError(f"campaign_snapshot_{field}_changed")
        expected_effective = min(60.0, float(campaign["s0_settled_cny"])
                                + float(campaign["s0_reserved_or_uncertain_cny"]) + 40.0)
        if campaign.get("effective_cap_cny") != expected_effective:
            raise ValueError("campaign_effective_cap_snapshot_invalid")
        current_by_id = {row["reservation_id"]: row for row in state["rows"]}
        baseline_by_id = {row["reservation_id"]: row for row in campaign["baseline_rows"]}
        if any(current_by_id.get(key) != value for key, value in baseline_by_id.items()):
            raise ValueError("campaign_baseline_row_changed")
        if state["settled_cny"] + state["reserved_or_uncertain_cny"] > float(campaign["effective_cap_cny"]) + 1e-9:
            raise ValueError("campaign_effective_cap_exceeded")
        if campaign.get("identity") != identity:
            raise ValueError("campaign_source_identity_changed_use_new_campaign")
        current_ids = [row["reservation_id"] for row in state["rows"]]
        if not set(campaign["baseline_reservation_ids"]).issubset(current_ids):
            raise ValueError("campaign_baseline_reservation_missing")
        return campaign
    lock_fd = None
    try:
        lock_fd = os.open(CAMPAIGN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        if CAMPAIGN_PATH.exists():
            return ensure_campaign(manifest, guide, config, tokenizer)
        campaign = {
            "schema_version": "guided_body_b_repair_budget.v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "ledger_path": str(EXPECTED_LEDGER.resolve()),
            "marker_path": str(EXPECTED_MARKER.resolve()),
            "source_commit": EXPECTED_COMMIT,
            "marker_sha256": state["marker_sha256"],
            "stored_limit_cny": 60.0,
            "s0_settled_cny": state["settled_cny"],
            "s0_reserved_or_uncertain_cny": state["reserved_or_uncertain_cny"],
            "effective_cap_cny": effective,
            "baseline_row_count": state["row_count"],
            "baseline_reservation_ids": [row["reservation_id"] for row in state["rows"]],
            "baseline_call_ids": [row["call_id"] for row in state["rows"]],
            "baseline_rows": _baseline_projection(state["rows"]),
            "identity": identity,
            "policy": {"new_allowance_cny": 40.0, "max_model": PLUS_MODEL,
                       "max_retries": 0, "max_keys": 1, "atomic_reserve": True,
                       "stored_limit_unchanged": True},
        }
        _write_json(CAMPAIGN_PATH, campaign)
        return campaign
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                CAMPAIGN_LOCK.unlink()
            except FileNotFoundError:
                pass


def _validate_plus_config(config: Path) -> dict[str, Any]:
    value = _json(config)
    writer = value.get("writer") if isinstance(value, dict) else None
    if not isinstance(writer, dict) or writer.get("model") != PLUS_MODEL:
        raise ValueError("campaign_only_qwen3.5-plus_is_allowed")
    expected = {"thinking_budget": 16384, "max_output_tokens": 49152,
                "timeout_seconds": 1800, "stream": True,
                "stream_overall_timeout_seconds": 3600}
    for key, expected_value in expected.items():
        if writer.get(key) != expected_value:
            raise ValueError(f"campaign_writer_profile_changed:{key}")
    return value


def _validate_effective_profile(profile: dict[str, Any]) -> None:
    """Reject a changed model/profile before the provider object is created."""
    expected = {"model": PLUS_MODEL, "thinking": True, "thinking_budget": 16384,
                "max_output_tokens": 49152, "timeout_seconds": 1800,
                "stream": True, "stream_overall_timeout_seconds": 3600}
    for key, expected_value in expected.items():
        if profile.get(key) != expected_value:
            raise ValueError(f"campaign_effective_profile_changed:{key}")


def _install_factory_guard():
    """Wrap the shared factory without changing the production module."""
    from scripts.upgrade3 import writer_candidates as shared
    original = shared.make_live_factory

    def guarded_make_live_factory(args, *, token_counter=None):
        lazy_factory = original(args, token_counter=token_counter)

        def guarded_factory(role: str, stage_dir: Path, profile: dict[str, Any]):
            _validate_effective_profile(dict(profile))
            return lazy_factory(role, stage_dir, profile)

        guarded_factory.execution_mode = getattr(lazy_factory, "execution_mode", "live")
        guarded_factory.ledger_snapshot = lambda: lazy_factory.ledger_snapshot()
        return guarded_factory

    shared.make_live_factory = guarded_make_live_factory
    return shared, original


def _acquire_lease(campaign: dict[str, Any]) -> None:
    if LEASE_PATH.exists():
        raise ValueError("campaign_run_lease_exists_manual_review_required")
    try:
        fd = os.open(LEASE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("campaign_run_lease_exists_manual_review_required") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at_utc": datetime.now(timezone.utc).isoformat(),
                   "campaign_path": str(CAMPAIGN_PATH), "ledger_path": campaign["ledger_path"]}, handle, indent=2)


def _release_lease() -> None:
    try:
        LEASE_PATH.unlink()
    except FileNotFoundError:
        pass


def _write_invocation(argv: list[str], mode: str, campaign: dict[str, Any], output: Path | None) -> Path:
    RECORDS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RECORDS / f"WRAPPER_INVOCATION_{stamp}_{os.getpid()}.json"
    _write_json(path, {"schema_version": "guided_body_b_repair_invocation.v1",
                       "mode": mode, "argv": argv, "output": str(output) if output else None,
                       "campaign_path": str(CAMPAIGN_PATH), "campaign_cap_cny": campaign["effective_cap_cny"],
                       "ledger_path": campaign["ledger_path"], "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
    return path


def _patch_ledger(campaign: dict[str, Any]):
    sys.path.insert(0, str(WORKTREE))
    from optomind_research.runtime.upgrade3.module4 import runtime
    original = runtime.GlobalBudgetLedger
    cap = float(campaign["effective_cap_cny"])

    class CampaignBudgetLedger(original):
        campaign_cap_cny = cap

        def __post_init__(self):
            super().__post_init__()
            current = self.limit_cny
            self.limit_cny = min(float(current) if current is not None else 60.0, self.campaign_cap_cny)

    runtime.GlobalBudgetLedger = CampaignBudgetLedger
    return runtime, original, CampaignBudgetLedger


def _selftest() -> dict[str, Any]:
    sys.path.insert(0, str(WORKTREE))
    from optomind_research.runtime.upgrade3.module4 import runtime
    from optomind_research.runtime.upgrade3.module4.runtime import QwenTransportError

    with tempfile.TemporaryDirectory(prefix="guided_body_budget_selftest_",
                                      ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        class CampaignBudgetLedger(runtime.GlobalBudgetLedger):
            campaign_cap_cny = 10.0

            def __post_init__(self):
                super().__post_init__()
                current = self.limit_cny
                self.limit_cny = min(float(current) if current is not None else 60.0,
                                     self.campaign_cap_cny)
        original_limit = 60.0
        ledger_path = root / "ledger.sqlite"
        first = CampaignBudgetLedger(limit_cny=original_limit, path=ledger_path)
        results: list[str] = []

        def attempt(index: int):
            instance = CampaignBudgetLedger(limit_cny=original_limit, path=ledger_path)
            try:
                row = instance.reserve(6.0, f"selftest-{index}")
                return ("ok", row)
            except QwenTransportError as exc:
                return ("blocked", str(exc))

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (1, 2)))
        if sorted(item[0] for item in outcomes) != ["blocked", "ok"]:
            raise AssertionError(f"atomic_over_cap_failed:{outcomes}")
        db = sqlite3.connect(ledger_path)
        try:
            count = db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            stored = float(db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()[0])
        finally:
            db.close()
        if count != 1 or stored != 60.0:
            raise AssertionError(f"atomic_reserve_state_failed:{count}:{stored}")
        ledger2 = root / "resume.sqlite"
        base = CampaignBudgetLedger(limit_cny=original_limit, path=ledger2)
        prior = base.reserve(3.0, "prior")
        base.settle(prior["reservation_id"], 2.0)
        resumed = CampaignBudgetLedger(limit_cny=original_limit, path=ledger2)
        resumed.limit_cny = 7.0
        next_row = resumed.reserve(4.0, "after-restart")
        try:
            resumed.reserve(2.0, "over-cap")
        except QwenTransportError:
            pass
        else:
            raise AssertionError("restart_cap_not_enforced")
        db = sqlite3.connect(ledger2)
        try:
            stored2 = float(db.execute("SELECT value FROM budget_meta WHERE key='limit_cny'").fetchone()[0])
            rows2 = db.execute("SELECT call_id FROM reservations ORDER BY rowid").fetchall()
        finally:
            db.close()
        if stored2 != 60.0 or [row[0] for row in rows2] != ["prior", "after-restart"]:
            raise AssertionError("restart_did_not_preserve_ledger")
        try:
            _validate_plus_config(root / "missing.json")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        bad = root / "bad.json"
        _write_json(bad, {"writer": {"model": "qwen3.8-max", "thinking_budget": 16384,
                                      "max_output_tokens": 49152, "timeout_seconds": 1800,
                                      "stream": True, "stream_overall_timeout_seconds": 3600}})
        try:
            _validate_plus_config(bad)
        except ValueError as exc:
            if "qwen3.5-plus" not in str(exc):
                raise
        else:
            raise AssertionError("non_plus_was_not_rejected")
        factory_shared, factory_original = _install_factory_guard()
        try:
            from argparse import Namespace
            factory_args = Namespace(run=True, responses=None, budget_ledger=str(ledger2),
                                     budget_limit=60.0, allow_max=False, key_file=[REDACTED_SECRET_PATH]
            guarded = factory_shared.make_live_factory(factory_args)
            bad_profile = {"model": "qwen3.8-max", "thinking": True,
                           "thinking_budget": 16384, "max_output_tokens": 49152,
                           "timeout_seconds": 1800, "stream": True,
                           "stream_overall_timeout_seconds": 3600}
            try:
                guarded("writer", root, bad_profile)
            except ValueError as exc:
                if "effective_profile_changed:model" not in str(exc):
                    raise
            else:
                raise AssertionError("factory_non_plus_was_not_rejected_before_provider")
        finally:
            factory_shared.make_live_factory = factory_original

        # Exercise the real persistent campaign snapshot across a changed
        # ledger, using an isolated temporary ledger and marker.  The saved S0
        # and cap must remain fixed while a new settled row is allowed.
        global CAMPAIGN_PATH, CAMPAIGN_LOCK, EXPECTED_LEDGER, EXPECTED_MARKER
        global EXPECTED_MANIFEST, EXPECTED_GUIDE, EXPECTED_CONFIG, EXPECTED_TOKENIZER, MARKER_VALUE
        saved_globals = {name: globals()[name] for name in (
            "CAMPAIGN_PATH", "CAMPAIGN_LOCK", "EXPECTED_LEDGER", "EXPECTED_MARKER",
            "EXPECTED_MANIFEST", "EXPECTED_GUIDE", "EXPECTED_CONFIG", "EXPECTED_TOKENIZER",
            "MARKER_VALUE")}
        isolated = root / "campaign-recovery"
        isolated.mkdir()
        isolated_ledger = isolated / "ledger.sqlite"
        isolated_marker = Path(str(isolated_ledger) + ".evidence_round2.json")
        isolated_manifest = isolated / "manifest.json"
        isolated_guide = isolated / "guide_B.json"
        isolated_config = isolated / "plus_first.json"
        isolated_tokenizer = isolated / "tokenizer.json"
        isolated_manifest.write_text("{}", encoding="utf-8")
        isolated_guide.write_text("{}", encoding="utf-8")
        isolated_config.write_text(json.dumps({"writer": {"model": PLUS_MODEL,
            "thinking_budget": 16384, "max_output_tokens": 49152,
            "timeout_seconds": 1800, "stream": True,
            "stream_overall_timeout_seconds": 3600}}), encoding="utf-8")
        isolated_tokenizer.write_text("temporary-tokenizer", encoding="utf-8")
        base = runtime.GlobalBudgetLedger(limit_cny=60.0, path=isolated_ledger)
        prior = base.reserve(3.0, "campaign-prior")
        base.settle(prior["reservation_id"], 2.0)
        isolated_marker.write_text(json.dumps({"schema_version": "optomind.evidence_round2_budget.v1",
            "ledger_path": str(isolated_ledger.resolve()), "limit_cny": 60}), encoding="utf-8")
        try:
            CAMPAIGN_PATH, CAMPAIGN_LOCK = isolated / "CAMPAIGN.json", isolated / "CAMPAIGN.lock"
            EXPECTED_LEDGER, EXPECTED_MARKER = isolated_ledger, isolated_marker
            EXPECTED_MANIFEST, EXPECTED_GUIDE = isolated_manifest, isolated_guide
            EXPECTED_CONFIG, EXPECTED_TOKENIZER = isolated_config, isolated_tokenizer
            MARKER_VALUE = {"schema_version": "optomind.evidence_round2_budget.v1",
                            "ledger_path": str(isolated_ledger.resolve()), "limit_cny": 60}
            first_campaign = ensure_campaign(isolated_manifest, isolated_guide,
                                              isolated_config, isolated_tokenizer)
            if first_campaign["s0_settled_cny"] != 2.0 or first_campaign["effective_cap_cny"] != 42.0:
                raise AssertionError("campaign_s0_or_cap_initialization_failed")
            after = runtime.GlobalBudgetLedger(limit_cny=60.0, path=isolated_ledger)
            added = after.reserve(1.0, "campaign-new-settled")
            after.settle(added["reservation_id"], 1.0)
            resumed_campaign = ensure_campaign(isolated_manifest, isolated_guide,
                                                isolated_config, isolated_tokenizer)
            if resumed_campaign["s0_settled_cny"] != 2.0 or resumed_campaign["effective_cap_cny"] != 42.0:
                raise AssertionError("campaign_s0_or_cap_recomputed_on_resume")
            held = runtime.GlobalBudgetLedger(limit_cny=60.0, path=isolated_ledger)
            held.reserve(1.0, "campaign-open-held")
            try:
                ensure_campaign(isolated_manifest, isolated_guide, isolated_config, isolated_tokenizer)
            except ValueError as exc:
                if "open_reservation" not in str(exc):
                    raise
            else:
                raise AssertionError("campaign_open_reservation_not_blocked")
        finally:
            for name, value in saved_globals.items():
                globals()[name] = value
    return {"schema_version": "guided_body_b_repair_selftest.v1", "passed": True,
            "tests": ["atomic_over_cap_reserve_block", "restart_preserves_s0_and_stored_60",
                       "non_plus_rejected_before_provider", "factory_non_plus_rejected_before_provider",
                       "persistent_campaign_s0_survives_resume"], "network_calls": 0}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        argv.remove("--self-test")
        if argv:
            raise SystemExit("--self-test_cannot_be_combined_with_cli_args")
        result = _selftest()
        _write_json(RECORDS / "SELFTEST.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if "--help" in argv:
        sys.path.insert(0, str(WORKTREE))
        from scripts.upgrade3 import guided_body_writer as official
        return official.main(argv)
    manifest = _resolved(_option(argv, "--manifest"), base=Path.cwd())
    guide = _resolved(_option(argv, "--guide"), base=Path.cwd())
    config = _resolved(_option(argv, "--config", str(EXPECTED_CONFIG)), base=Path.cwd())
    tokenizer = _resolved(_option(argv, "--tokenizer"), base=Path.cwd())
    if manifest is None or guide is None or tokenizer is None:
        raise SystemExit("campaign_requires_manifest_guide_and_tokenizer")
    _validate_plus_config(config)
    if _has(argv, "--allow-max"):
        raise SystemExit("campaign_disallows_allow_max")
    campaign = ensure_campaign(manifest, guide, config, tokenizer)
    output = _resolved(_option(argv, "--output"), base=Path.cwd())
    mode = "run" if _has(argv, "--run") else "responses" if _option(argv, "--responses") else "preview"
    if _has(argv, "--run"):
        ledger_arg = _resolved(_option(argv, "--budget-ledger"), base=Path.cwd())
        if ledger_arg != EXPECTED_LEDGER.resolve() or _option(argv, "--budget-limit") != "60":
            raise SystemExit("campaign_run_requires_original_ledger_and_limit_60")
        if _resolved(_option(argv, "--key-file"), base=Path.cwd()) != EXPECTED_KEY.resolve():
            raise SystemExit("campaign_run_requires_existing_key_path")
        _acquire_lease(campaign)
    invocation = _write_invocation(argv, mode, campaign, output)
    runtime = original = patched = None
    shared = shared_original = None
    try:
        if mode == "run":
            runtime, original, patched = _patch_ledger(campaign)
            shared, shared_original = _install_factory_guard()
        sys.path.insert(0, str(WORKTREE))
        from scripts.upgrade3 import guided_body_writer as official
        return official.main(argv)
    finally:
        if shared is not None and shared_original is not None:
            shared.make_live_factory = shared_original
        if runtime is not None and original is not None:
            runtime.GlobalBudgetLedger = original
        if mode == "run":
            _release_lease()
        _write_json(RECORDS / "LAST_RESULT_POINTER.json", {"invocation": str(invocation),
                                                            "mode": mode, "output": str(output) if output else None,
                                                            "finished_at_utc": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    raise SystemExit(main())
