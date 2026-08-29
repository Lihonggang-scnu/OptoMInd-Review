#!/usr/bin/env python3
"""Canonical OptoMind Research Harness entry point.

The command supports two safe entry modes:

1. A human-confirmed Query Planner JSON.  A paper-free task database is
   created automatically unless the operator explicitly supplies one.
2. A natural-language question.  Query Planner writes an English downstream
   plan first; execution pauses for human confirmation unless
   ``--auto-confirm-query-plan`` is explicitly supplied.

Historical test corpora are never selected implicitly.  The fixed review
mentor library teaches writing and planning patterns only; current scientific
evidence is collected separately through the legal open-access pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# On Windows, Anaconda's ``_sqlite3.pyd`` depends on the SQLite DLL shipped
# beside the interpreter.  A conflicting DLL earlier on PATH can otherwise
# make the harness fail before its own imports run.  Keep the directory
# handles alive for the process and scope the repair to the active Python.
_WINDOWS_DLL_HANDLES = []
if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    _python_root = Path(sys.executable).resolve().parent
    for _dll_dir in (
        _python_root / "DLLs",
        _python_root / "Library" / "bin",
    ):
        if _dll_dir.is_dir():
            try:
                _WINDOWS_DLL_HANDLES.append(
                    os.add_dll_directory(str(_dll_dir))
                )
            except OSError:
                # PATH remains the operator-controlled fallback if the
                # interpreter rejects a directory handle.
                pass

from config.qwen_config import (
    ECONOMY_TEXT_CEILING_ENV,
    set_economy_text_ceiling_enabled,
    validate_qwen_config,
)
from optomind_research.runtime.artifact_store import atomic_write_json
from optomind_research.runtime.cost_ledger import estimate_call_cost_cny
from optomind_research.runtime.central_material_cache import (
    DEFAULT_CACHE_ROOT as DEFAULT_LONG_TERM_MATERIAL_CACHE_ROOT,
    initialize_empty_cache,
    project_to_review_kb,
    resolve_current_snapshot,
)
from optomind_research.runtime.harness_observability import (
    HarnessObservability,
)
from optomind_research.runtime.topic_identity import (
    build_topic_identity_contract,
)
from optomind_research.runtime.review_harness_orchestrator import (
    ReviewHarnessConfig,
    ReviewHarnessOrchestrator,
)

HISTORICAL_CORE58_KB = (
    PROJECT_ROOT
    / "outputs"
    / "review_knowledge_base"
    / "core58-rkb-hqvisual-v1-20260703"
    / "review_knowledge_base.sqlite"
)
DEFAULT_BASE_KB = None
# M1 mentor library is optional.  The historical outputs/ copy has been
# archived; loading it silently returned {} and degraded blueprint quality
# without any signal.  Every consumer (review lead tool provider, phase-3
# orchestrator, full-review/global-audit configs) already treats None as
# "run without mentor guidance", so the default is now honestly None.
# Pass --m1-library explicitly to enable mentor guidance for a run.
DEFAULT_M1 = None
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "research_harness_e2e"
EMPTY_TASK_SEED_RELATIVE = Path("task_material") / "EMPTY_TASK_SEED.sqlite"
SOURCE_BASE_SNAPSHOT_SCHEMA_VERSION = "optomind.run_source_base_snapshot.v1"
SOURCE_BASE_SNAPSHOT_FILENAME = "SOURCE_BASE_SNAPSHOT.json"
SOURCE_BASE_SNAPSHOT_DIRNAME = "source_base_snapshot"
RUN_DIR_LOCK_FILENAME = "RUN_DIR_LOCK.json"
RUN_DIR_LOCK_SCHEMA_VERSION = "optomind.run_dir_lock.v1"
_LOCK_ACQUIRE_RETRIES = 8
_LOCK_RETRY_DELAY_SECONDS = 0.05
_MALFORMED_LOCK_RECOVERY_DELAY_SECONDS = 2.0

# The CLI exposes one global budget by default.  These values remain as
# compatibility fallbacks for callers that explicitly select one or more of
# the legacy per-stage switches; they are not automatically reserved in the
# normal global-only run.
_LEGACY_STAGE_BUDGET_DEFAULTS = {
    "review_lead_budget_cny": 4.0,
    "coverage_budget_cny": 14.0,
    "portfolio_coverage_budget_cny": 4.0,
    "feedback_coverage_budget_cny": 3.0,
    "authoring_budget_cny": 28.0,
    "article_completion_budget_cny": 18.0,
    "visual_budget_cny": 5.0,
    "chapter_style_governance_budget_cny": 0.75,
    "translation_cost_budget_cny": 3.0,
    "research_plan_budget_cny": 4.0,
    "research_plan_translation_cost_budget_cny": 0.5,
}


def _normalize_budget_arguments(args: argparse.Namespace) -> None:
    """Select global-only budgeting unless a legacy stage cap is explicit."""

    explicit_stage_override = any(
        getattr(args, name, None) is not None
        for name in _LEGACY_STAGE_BUDGET_DEFAULTS
    )
    for name, default in _LEGACY_STAGE_BUDGET_DEFAULTS.items():
        if getattr(args, name, None) is None:
            setattr(args, name, default)
    # This marker is intentionally derived from the original ``None`` values,
    # before fallback literals are populated.  A command containing only
    # ``--global-budget-cny`` therefore uses one shared pool.
    args.global_budget_only = not explicit_stage_override


def _resolve_m1_library_path(requested: Path | None) -> tuple[Path | None, str]:
    """Resolve an explicit M1 file or canonical-library directory.

    M1 is optional guidance, so an omitted or invalid path remains disabled;
    this helper never falls back to an archived default implicitly.
    """

    if requested is None:
        return None, "not_requested"
    path = Path(requested).expanduser()
    if path.is_file():
        return path.resolve(), "explicit_file"
    if path.is_dir():
        for name in (
            "intellectual_moves_active_by_category.json",
            "intellectual_moves_enriched_by_category.json",
            "intellectual_moves_library_by_category.json",
        ):
            candidate = path / name
            if candidate.is_file():
                return candidate.resolve(), "resolved_from_canonical_directory"
        return None, "directory_has_no_canonical_library"
    return None, "path_not_found"


def _pid_is_alive(pid: int) -> bool:
    """Return True when a PID plausibly belongs to a live process.

    On POSIX, ``os.kill(pid, 0)`` probes without delivering a signal. On
    Windows that call terminates the process instead, so the equivalent
    ``OpenProcess`` / ``GetExitCodeProcess`` probe is used there.
    """

    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists, but the current user may not signal it.
            return True
        except OSError:
            return False
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            ctypes.c_ulong(pid),
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    # On an unknown platform, treat a positive PID as alive so that a second
    # process cannot silently share corruptible state.
    return True


class RunDirectoryLockError(RuntimeError):
    """Raised when another live process owns the run-directory lock."""

    def __init__(
        self,
        lock_path: Path,
        owner_pid: int | None,
        detail: str = "",
    ) -> None:
        self.lock_path = Path(lock_path)
        self.owner_pid = owner_pid
        if owner_pid is None:
            message = (
                f"Run directory is locked by another harness process whose "
                f"PID could not be read from {self.lock_path}."
            )
        else:
            message = (
                f"Run directory is locked by live harness process "
                f"PID {owner_pid} (lock: {self.lock_path})."
            )
        if detail:
            message += f" {detail}"
        super().__init__(message)


class RunDirectoryLock:
    """Single-instance guard for one harness run directory.

    The lock is a JSON file created atomically with ``O_CREAT|O_EXCL``. The
    winning process records its PID, an instance token, and minimal audit
    metadata. A second live process reports the owning PID and exits before
    any observability or stage artifact is written. A lock left behind by a
    crashed process is recovered by verifying that its recorded PID is dead;
    release deletes only a lock whose token still belongs to this instance.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.lock_path = self.run_dir / RUN_DIR_LOCK_FILENAME
        self._token = uuid.uuid4().hex
        self._pid = int(os.getpid())
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        """Acquire the lock or raise :class:`RunDirectoryLockError`."""

        if self._owned:
            return
        for attempt in range(_LOCK_ACQUIRE_RETRIES + 1):
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                if self._recover_existing_lock():
                    continue
                if attempt < _LOCK_ACQUIRE_RETRIES:
                    time.sleep(_LOCK_RETRY_DELAY_SECONDS)
                    continue
                payload = self._read_lock_payload()
                owner_pid = self._payload_pid(payload)
                if owner_pid is not None and _pid_is_alive(owner_pid):
                    raise RunDirectoryLockError(
                        self.lock_path,
                        owner_pid,
                        self._payload_summary(payload),
                    )
                raise RunDirectoryLockError(
                    self.lock_path,
                    None,
                    "the stale lock could not be removed",
                )
            else:
                payload = self._payload()
                payload_bytes = json.dumps(payload).encode("utf-8")
                try:
                    os.write(fd, payload_bytes)
                    os.fsync(fd)
                except BaseException:
                    os.close(fd)
                    # This file was just created exclusively by us, so it is
                    # safe to remove even if the payload write was incomplete.
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                os.close(fd)
                self._owned = True
                return
        raise RunDirectoryLockError(
            self.lock_path,
            None,
            "lock acquisition retry limit reached",
        )

    def release(self) -> None:
        """Release the lock, removing only this instance's lock file."""

        if not self._owned:
            return
        try:
            payload, raw = self._read_lock_raw()
            if (
                isinstance(payload, dict)
                and payload.get("token") == self._token
                and payload.get("pid") == self._pid
            ):
                self._remove_if_unchanged(raw)
        except OSError:
            # Cleanup must not turn a completed run into a failure. A lock
            # that cannot be removed now remains stale-recoverable later.
            pass
        finally:
            self._owned = False

    def __enter__(self) -> "RunDirectoryLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_DIR_LOCK_SCHEMA_VERSION,
            "pid": self._pid,
            "token": self._token,
            "run_dir": str(self.run_dir),
            "hostname": platform.node(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _payload_pid(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        pid = payload.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None

    @staticmethod
    def _payload_summary(payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return ""
        parts = []
        acquired_at = payload.get("acquired_at")
        if acquired_at:
            parts.append(f"acquired at {acquired_at}")
        hostname = payload.get("hostname")
        if hostname:
            parts.append(f"on {hostname}")
        return "; ".join(parts)

    def _read_lock_raw(self) -> tuple[dict[str, Any] | None, bytes]:
        try:
            raw = self.lock_path.read_bytes()
        except FileNotFoundError:
            return None, b""
        except OSError:
            # Treat an unreadable lock entry as malformed rather than leaking
            # a raw filesystem error from a concurrency guard.
            return None, b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, raw
        if not isinstance(payload, dict):
            return None, raw
        return payload, raw

    def _read_lock_payload(self) -> dict[str, Any] | None:
        payload, _raw = self._read_lock_raw()
        return payload

    def _lock_age_seconds(self) -> float | None:
        try:
            return max(0.0, time.time() - self.lock_path.stat().st_mtime)
        except (FileNotFoundError, OSError):
            return None

    def _remove_if_unchanged(self, expected_raw: bytes) -> bool:
        """Remove the lock only when its bytes still match what was read."""

        try:
            current_raw = self.lock_path.read_bytes()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if current_raw != expected_raw:
            return False
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _recover_existing_lock(self) -> bool:
        """Handle an already-present lock, raising for a live owner."""

        payload, raw = self._read_lock_raw()
        if payload is None:
            deadline = (
                time.monotonic() + _MALFORMED_LOCK_RECOVERY_DELAY_SECONDS
            )
            while payload is None:
                age = self._lock_age_seconds()
                if age is None:
                    # The file disappeared while we were waiting.
                    return True
                if age >= _MALFORMED_LOCK_RECOVERY_DELAY_SECONDS:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_RETRY_DELAY_SECONDS)
                payload, raw = self._read_lock_raw()
        owner_pid = self._payload_pid(payload)
        if owner_pid is not None and _pid_is_alive(owner_pid):
            raise RunDirectoryLockError(
                self.lock_path,
                owner_pid,
                self._payload_summary(payload),
            )
        return self._remove_if_unchanged(raw)


def _is_historical_core58(path: Path) -> bool:
    if any("core58" in part.casefold() for part in path.parts):
        return True
    try:
        return path.resolve() == HISTORICAL_CORE58_KB.resolve()
    except (OSError, RuntimeError):
        return any("core58" in part.casefold() for part in path.parts)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_base_snapshot_metadata_path(run_dir: Path) -> Path:
    return run_dir / "task_material" / SOURCE_BASE_SNAPSHOT_FILENAME


def _read_source_base_snapshot(run_dir: Path) -> dict[str, Any] | None:
    metadata_path = _source_base_snapshot_metadata_path(run_dir)
    if not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != SOURCE_BASE_SNAPSHOT_SCHEMA_VERSION
    ):
        return None
    raw_path = str(metadata.get("source_base_kb") or "")
    if not raw_path:
        return None
    snapshot_path = Path(raw_path)
    expected_hash = str(metadata.get("source_base_sha256") or "")
    if not snapshot_path.is_file() or not expected_hash:
        return None
    if _sha256_file(snapshot_path) != expected_hash:
        return None
    return metadata


def _persist_source_base_snapshot(
    run_dir: Path,
    projection_path: Path,
    *,
    cache_root: Path,
) -> Path:
    digest = _sha256_file(projection_path)
    snapshot_dir = (
        run_dir / "task_material" / SOURCE_BASE_SNAPSHOT_DIRNAME
    )
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{digest}.sqlite"
    if not snapshot_path.is_file() or _sha256_file(snapshot_path) != digest:
        staging = snapshot_path.with_name(
            snapshot_path.name + f".tmp-{uuid.uuid4().hex[:8]}"
        )
        shutil.copy2(str(projection_path), str(staging))
        if _sha256_file(staging) != digest:
            try:
                staging.unlink()
            except OSError:
                pass
            raise OSError("source-base snapshot copy failed its integrity hash")
        os.replace(staging, snapshot_path)
    metadata_path = _source_base_snapshot_metadata_path(run_dir)
    atomic_write_json(
        metadata_path,
        {
            "schema_version": SOURCE_BASE_SNAPSHOT_SCHEMA_VERSION,
            "source_base_asset_role": (
                "central_long_term_material_cache_projection"
            ),
            "source_base_kb": str(snapshot_path),
            "source_base_sha256": digest,
            "original_projection_path": str(projection_path),
            "central_cache_root": str(cache_root.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provenance": {
                "materialized_from": "central_long_term_material_cache",
                "immutable": True,
            },
        },
    )
    return snapshot_path


def _base_kb_for_run(
    requested: Path | None,
    *,
    run_dir: Path,
    allow_historical_test_assets: bool,
    materialize_empty_seed: bool,
    query_plan_path: Path | None = None,
    long_term_material_cache_root: Path = DEFAULT_LONG_TERM_MATERIAL_CACHE_ROOT,
) -> tuple[Path, str]:
    """Resolve the run's paper source without silently importing old tests."""

    if requested is not None:
        candidate = Path(requested)
        if _is_historical_core58(candidate):
            if not allow_historical_test_assets:
                raise ValueError(
                    "core58 is a historical first-test paper set, not a "
                    "default source for new research. Use a fresh run without "
                    "--base-kb, or add --allow-historical-test-assets only "
                    "for an explicit historical test."
                )
            return candidate, "historical_test_asset"
        return candidate, "user_supplied_research_material"

    if materialize_empty_seed and query_plan_path is not None:
        pointer = Path(long_term_material_cache_root) / "CURRENT.json"
        if not pointer.is_file():
            initialize_empty_cache(long_term_material_cache_root)
        existing_snapshot = _read_source_base_snapshot(run_dir)
        if existing_snapshot is not None:
            return (
                Path(str(existing_snapshot["source_base_kb"])),
                "central_long_term_material_cache_projection",
            )
        projection_path = (
            run_dir
            / "task_material"
            / "LONG_TERM_MATERIAL_PROJECTION.sqlite"
        )
        report_path = (
            run_dir
            / "task_material"
            / "LONG_TERM_MATERIAL_PROJECTION.json"
        )
        if projection_path.is_file() and report_path.is_file():
            try:
                prior_report = json.loads(
                    report_path.read_text(encoding="utf-8")
                )
                prior_snapshot = str(prior_report.get("snapshot") or "")
            except (OSError, json.JSONDecodeError):
                prior_snapshot = ""
            if prior_snapshot:
                current_snapshot = str(
                    resolve_current_snapshot(long_term_material_cache_root)
                )
                if prior_snapshot != current_snapshot:
                    raise ValueError(
                        "legacy run has no source-base snapshot and the "
                        "central material cache CURRENT snapshot advanced; "
                        "refusing to overwrite the original projection. "
                        "Restore the original central snapshot or provide an "
                        "explicit matching --base-kb."
                    )
        project_to_review_kb(
            query_plan_path=query_plan_path,
            output_kb_path=projection_path,
            cache_root=long_term_material_cache_root,
            report_path=report_path,
        )
        snapshot_path = _persist_source_base_snapshot(
            run_dir,
            projection_path,
            cache_root=Path(long_term_material_cache_root),
        )
        return snapshot_path, "central_long_term_material_cache_projection"

    seed_path = run_dir / EMPTY_TASK_SEED_RELATIVE
    if materialize_empty_seed:
        from optomind_research.runtime.topic_scoped_kb_stage import (
            create_empty_review_kb,
        )

        create_empty_review_kb(seed_path)
    return seed_path, "empty_task_seed"


def _project_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _write_asset_roles(
    *,
    run_dir: Path,
    base_kb: Path,
    base_role: str,
    mentor_library: Path | None,
    mentor_requested_path: Path | None = None,
    mentor_resolution_reason: str = "not_requested",
) -> None:
    """Record the four asset roles in plain, auditable terms."""

    atomic_write_json(
        run_dir / "ASSET_ROLES.json",
        {
            "schema_version": "optomind.run_asset_roles.v1",
            "historical_test_assets": [
                {
                    "logical_name": "core58_first_pipeline_test",
                    "path": _project_relative(HISTORICAL_CORE58_KB),
                    "used_in_this_run": base_role == "historical_test_asset",
                    "default_for_new_research": False,
                }
            ],
            "stable_guidance": [
                {
                    "logical_name": "review_intellectual_mentor_library",
                    "path": (
                        _project_relative(mentor_library)
                        if mentor_library is not None
                        else ""
                    ),
                    "used_in_this_run": bool(
                        mentor_library is not None and mentor_library.is_file()
                    ),
                    "requested_path": (
                        _project_relative(mentor_requested_path)
                        if mentor_requested_path is not None
                        else ""
                    ),
                    "resolution_reason": mentor_resolution_reason,
                    "sha256": (
                        hashlib.sha256(
                            mentor_library.read_bytes()
                        ).hexdigest()
                        if mentor_library is not None and mentor_library.is_file()
                        else ""
                    ),
                    "m1_enabled": bool(
                        mentor_library is not None and mentor_library.is_file()
                    ),
                    "may_supply_scientific_facts": False,
                    "purpose": "teach review organization and research planning",
                }
            ],
            "current_task_material": [
                {
                    "logical_name": "run_starting_paper_material",
                    "path": _project_relative(base_kb),
                    "role": base_role,
                },
                {
                    "logical_name": "s2_and_open_access_results",
                    "path": "s2_literature_intelligence",
                    "role": "current_task_material",
                },
            ],
            "mutable_run_cache": [
                {
                    "logical_name": "search_fulltext_and_resume_cache",
                    "path": ".",
                    "role": "mutable_run_cache",
                    "reuse_requires_same_research_question": True,
                }
            ],
        },
    )


def _usage_cost(usage: Dict[str, Any]) -> tuple[float, int, int]:
    model = str(usage.get("model_name") or "qwen3.7-max")
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("estimated_input_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("estimated_output_tokens")
        or 0
    )
    return (
        estimate_call_cost_cny(model, input_tokens, output_tokens),
        input_tokens,
        output_tokens,
    )


def _prepare_query_plan(
    *,
    question: str,
    query_plan_path: Path | None,
    work_dir: Path,
    mock: bool,
    auto_confirm: bool,
    observability: HarnessObservability,
) -> tuple[Path | None, Dict[str, Any]]:
    observability.start_stage(
        "query_planner",
        source="provided_plan" if query_plan_path is not None else "question",
    )
    if query_plan_path is not None:
        if not query_plan_path.exists():
            raise FileNotFoundError(query_plan_path)
        from optomind_research.query_planner import QueryPlannerAgent

        provided = json.loads(query_plan_path.read_text(encoding="utf-8"))
        validation = QueryPlannerAgent(real_llm=False).validate_payload(
            provided,
            raw_text=json.dumps(provided, ensure_ascii=False),
        )
        topic_identity = build_topic_identity_contract(provided)
        if not validation.ok or not topic_identity.get("valid"):
            raise ValueError(
                "Provided query plan is not execution-ready: "
                + "; ".join(validation.errors[:5])
            )
        atomic_write_json(work_dir / "TOPIC_IDENTITY.json", topic_identity)
        wall_time = observability.finish_stage(
            "query_planner",
            "completed",
            reused=True,
        )
        return query_plan_path, {
            "status": "provided_confirmed_plan",
            "planner_generation_status": "provided_confirmed_plan",
            "execution_ready": True,
            "terminal_status": "confirmed_query_plan",
            "topic_fingerprint": topic_identity["fingerprint"],
            "cost_cny": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "wall_time_seconds": round(wall_time, 3),
        }

    from optomind_research.query_planner import QueryPlannerAgent

    query_dir = work_dir / "query_planner"
    query_dir.mkdir(parents=True, exist_ok=True)
    cached_question_path = query_dir / "ORIGINAL_USER_QUESTION.json"
    cached_plan_path = query_dir / "query_plan.json"
    cached_gate_path = work_dir / "QUERY_PLAN_ENTRY_GATE.json"
    # A confirmed plan is a durable human-in-the-loop artifact.  Resuming the
    # same run with the exact same natural-language question must not call the
    # model again merely to regenerate an equivalent plan.  The current
    # ``--auto-confirm-query-plan`` flag is itself the operator's confirmation;
    # the historical gate's ``auto_confirmation_requested`` field is an audit
    # record only and is not a prerequisite for reuse.
    if (
        auto_confirm
        and cached_question_path.is_file()
        and cached_plan_path.is_file()
        and cached_gate_path.is_file()
    ):
        try:
            cached_question = json.loads(
                cached_question_path.read_text(encoding="utf-8")
            )
            cached_plan = json.loads(
                cached_plan_path.read_text(encoding="utf-8")
            )
            cached_gate = json.loads(
                cached_gate_path.read_text(encoding="utf-8")
            )
            validation = QueryPlannerAgent(
                real_llm=False
            ).validate_payload(
                cached_plan,
                raw_text=json.dumps(cached_plan, ensure_ascii=False),
            )
            topic_identity = build_topic_identity_contract(cached_plan)
            same_question = (
                str(cached_question.get("user_question") or "").strip()
                == str(question or "").strip()
            )
            confirmed_gate = bool(
                cached_gate.get("status") == "passed"
                and cached_gate.get("execution_ready") is True
            )
            if (
                same_question
                and confirmed_gate
                and validation.ok
                and topic_identity.get("valid")
            ):
                atomic_write_json(
                    work_dir / "TOPIC_IDENTITY.json",
                    topic_identity,
                )
                wall_time = observability.finish_stage(
                    "query_planner",
                    "completed",
                    reused=True,
                    reuse_reason="same_confirmed_question",
                )
                return cached_plan_path, {
                    "status": "reused_confirmed_query_plan",
                    "planner_generation_status": (
                        "reused_confirmed_query_plan"
                    ),
                    "execution_ready": True,
                    "terminal_status": "auto_confirmed_query_plan",
                    "topic_fingerprint": topic_identity["fingerprint"],
                    "cost_cny": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "wall_time_seconds": round(wall_time, 3),
                    "reused": True,
                }
        except Exception:
            # A malformed or stale cache is ignored and rebuilt below.  The
            # normal model/validation path remains fail-closed.
            pass
    atomic_write_json(
        query_dir / "ORIGINAL_USER_QUESTION.json",
        {
            "schema_version": "research_harness.original_question.v1",
            "user_question": question,
        },
    )
    agent = QueryPlannerAgent(real_llm=not mock)
    review_package = agent.plan_review_dict(question)
    atomic_write_json(
        query_dir / "QUERY_PLAN_REVIEW_PACKAGE.json",
        review_package,
    )
    compact_plan = review_package.get("result")
    if not isinstance(compact_plan, dict):
        raise RuntimeError("Query Planner did not return a compact plan")
    compact_path = query_dir / "query_plan.json"
    atomic_write_json(compact_path, compact_plan)

    primary_cost, primary_in, primary_out = _usage_cost(agent.last_usage)
    repair_cost, repair_in, repair_out = _usage_cost(
        agent.last_repair_usage
    )
    planner_generation_status = str(review_package.get("status") or "")
    metrics = {
        # ``status`` remains as a compatibility alias in the stage-local
        # receipt.  Run-level callers must expose the unambiguous field below.
        "status": planner_generation_status,
        "planner_generation_status": planner_generation_status,
        "cost_cny": round(primary_cost + repair_cost, 6),
        "input_tokens": primary_in + repair_in,
        "output_tokens": primary_out + repair_out,
        "needs_human_confirmation": bool(
            review_package.get("needs_human_confirmation", True)
        ),
        "compact_query_plan": str(compact_path),
    }
    final_validation = review_package.get("final_validation", {})
    safe_generation_statuses = {
        "primary_valid",
        "repaired_by_format_model",
        "primary_valid_optional_notes_dropped",
        "repaired_optional_notes_dropped",
    }
    topic_identity = build_topic_identity_contract(compact_plan)
    execution_ready = bool(
        planner_generation_status in safe_generation_statuses
        and isinstance(final_validation, dict)
        and final_validation.get("ok") is True
        and topic_identity.get("valid") is True
    )
    metrics.update(
        {
            "execution_ready": execution_ready,
            "topic_fingerprint": (
                str(topic_identity.get("fingerprint") or "")
                if execution_ready
                else ""
            ),
            "terminal_status": (
                "query_plan_ready_for_confirmation"
                if execution_ready
                else "needs_model_recovery"
            ),
        }
    )
    metrics["wall_time_seconds"] = round(
        observability.finish_stage(
            "query_planner",
            "completed",
            estimated_cost_cny=metrics["cost_cny"],
            input_tokens=metrics["input_tokens"],
            output_tokens=metrics["output_tokens"],
            needs_human_confirmation=metrics[
                "needs_human_confirmation"
            ],
        ),
        3,
    )
    primary_usage = dict(agent.last_usage or {})
    repair_usage = dict(agent.last_repair_usage or {})
    observability.emit(
        "query_planner_diagnostic",
        stage="query_planner",
        status=planner_generation_status,
        primary_success=bool(primary_usage.get("success")),
        primary_error_type=str(primary_usage.get("error_type") or ""),
        primary_attempted_models=list(
            primary_usage.get("attempted_models") or []
        ),
        repair_success=bool(repair_usage.get("success")),
        repair_error_type=str(repair_usage.get("error_type") or ""),
        repair_attempted_models=list(
            repair_usage.get("attempted_models") or []
        ),
        deterministic_fallback=(
            planner_generation_status
            == "deterministic_fallback_after_repair_failed"
        ),
    )
    atomic_write_json(query_dir / "QUERY_PLANNER_COST.json", metrics)
    gate_payload = {
        "schema_version": "research_harness.query_entry_gate.v1",
        "status": "passed" if execution_ready else "failed",
        "execution_ready": execution_ready,
        "generation_status": planner_generation_status,
        "auto_confirmation_requested": bool(auto_confirm),
        "reason": (
            "validated_model_query_plan"
            if execution_ready
            else "primary_and_repair_models_did_not_produce_an_execution_ready_plan"
        ),
        "query_plan_path": str(compact_path),
        "instruction": (
            "Review and edit the English query plan, then rerun with "
            "--query-plan pointing to the edited file."
            if execution_ready
            else "Restore a working Qwen key/model and rerun Query Planner. "
            "The deterministic fallback is diagnostic only and must never "
            "enter literature retrieval or writing."
        ),
    }
    atomic_write_json(work_dir / "QUERY_PLAN_ENTRY_GATE.json", gate_payload)
    if not execution_ready:
        observability.emit(
            "query_plan_entry_gate_failed",
            stage="query_planner",
            status=planner_generation_status,
            downstream_calls_blocked=True,
        )
        atomic_write_json(
            work_dir / "REVIEW_CONTENT_PACKAGE.json",
            {
                "schema_version": "research_harness.content_package.v1",
                "status": "needs_model_recovery",
                "query_plan_path": str(compact_path),
                "entry_gate_path": str(
                    work_dir / "QUERY_PLAN_ENTRY_GATE.json"
                ),
                "instruction": gate_payload["instruction"],
                "query_planner_metrics": metrics,
            },
        )
        return None, metrics

    atomic_write_json(work_dir / "TOPIC_IDENTITY.json", topic_identity)
    if not auto_confirm:
        atomic_write_json(
            work_dir / "REVIEW_CONTENT_PACKAGE.json",
            {
                "schema_version": "research_harness.content_package.v1",
                "status": "awaiting_query_plan_confirmation",
                "query_plan_path": str(compact_path),
                "instruction": (
                    "Review and edit the English query plan, then rerun with "
                    "--query-plan pointing to this file."
                ),
                "query_planner_metrics": metrics,
            },
        )
        return None, metrics
    metrics["terminal_status"] = "auto_confirmed_query_plan"
    return compact_path, metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the reusable, budgeted OptoMind Research Harness."
    )
    entry = parser.add_mutually_exclusive_group(required=True)
    entry.add_argument("--question", default="")
    entry.add_argument(
        "--question-file",
        type=Path,
        help="Read one UTF-8 natural-language question from a file.",
    )
    entry.add_argument("--query-plan", type=Path)
    parser.add_argument("--execution-profile", choices=("library_offline", "private_study", "submission"), default="private_study")
    parser.add_argument(
        "--base-kb",
        type=Path,
        default=DEFAULT_BASE_KB,
        help=(
            "Explicit test/recovery paper database override. Normal runs "
            "omit this option and project relevant material from the central "
            "long-term cache before external retrieval."
        ),
    )
    parser.add_argument(
        "--long-term-material-cache-root",
        type=Path,
        default=DEFAULT_LONG_TERM_MATERIAL_CACHE_ROOT,
        help=(
            "Stable central MaterialUnit cache. Normal runs search its "
            "CURRENT snapshot before any external retrieval."
        ),
    )
    parser.add_argument(
        "--no-long-term-material-cache-writeback",
        action="store_true",
        help=(
            "Diagnostic/test-only: do not merge newly materialized text "
            "back into the central long-term cache."
        ),
    )
    parser.add_argument(
        "--allow-historical-test-assets",
        action="store_true",
        help=(
            "Permit an explicitly supplied core58 database for historical "
            "pipeline tests. It remains disabled for normal research."
        ),
    )
    parser.add_argument("--m1-library", type=Path, default=DEFAULT_M1)
    parser.add_argument(
        "--phase3-artifacts-root",
        type=Path,
        help="Phase-3 run directory whose audited material drives R4 authoring.",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--qwen-key-file",
        type=Path,
        help=(
            "Use only keys from this file for the run. Historical project, "
            "desktop, and environment key pools are excluded."
        ),
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Resume or continue an existing harness run directory.",
    )
    parser.add_argument(
        "--rebuild-scoped-kb",
        action="store_true",
        help=(
            "Explicit recovery only: archive validated topic-scoped KB/S2 "
            "artifacts and rebuild them. Production resume reuses validated "
            "manifests by default."
        ),
    )
    parser.add_argument(
        "--rebuild-phase3-handoff",
        action="store_true",
        help=(
            "Explicit recovery only: archive an existing Phase-3 directory "
            "and regenerate the canonical R3 handoff."
        ),
    )
    parser.add_argument(
        "--auto-confirm-query-plan",
        action="store_true",
        help="Continue without the normal human review of a newly generated plan.",
    )
    parser.add_argument(
        "--mock-query-planner",
        action="store_true",
        help="Use the deterministic Query Planner fallback; for offline checks only.",
    )
    parser.add_argument("--global-budget-cny", type=float, default=120.0)
    parser.add_argument("--review-lead-budget-cny", type=float, default=None)
    parser.add_argument("--coverage-budget-cny", type=float, default=None)
    parser.add_argument(
        "--portfolio-coverage-budget-cny", type=float, default=None
    )
    parser.add_argument(
        "--feedback-coverage-budget-cny", type=float, default=None
    )
    parser.add_argument("--authoring-budget-cny", type=float, default=None)
    parser.add_argument(
        "--article-completion-budget-cny",
        type=float,
        default=None,
        help=(
            "CNY cap for the publication mainline's enhancement admission; "
            "the legacy one-call article completion path is disabled by "
            "default and can be restored with --no-publication-mainline."
        ),
    )
    parser.add_argument(
        "--no-publication-mainline",
        action="store_true",
        help=(
            "Use the legacy article-completion path instead of the default "
            "per-section traceable-draft publication mainline."
        ),
    )
    parser.add_argument(
        "--publication-mainline-commander-model-tier",
        default="c2_model",
        help="Model tier for the five-role global manuscript commander.",
    )
    parser.add_argument(
        "--publication-mainline-staged-model-tier",
        default="c_model",
        help="Model tier for staged conclusion/introduction/abstract and bounded edits.",
    )
    parser.add_argument(
        "--publication-mainline-staged-reviewer-tier",
        default="c2_model",
        help="Model tier for the whole-manuscript multi-reviewer stage.",
    )
    parser.add_argument(
        "--publication-mainline-staged-editorial-verifier-tier",
        default="c2_model",
        help="Independent verifier tier for bounded editorial revisions.",
    )
    parser.add_argument(
        "--publication-mainline-local-metadata-db",
        type=Path,
        default=None,
        help=(
            "Optional long-term abstract/material SQLite for local-first "
            "explanatory citation search. When omitted, the current base KB "
            "is used read-only."
        ),
    )
    parser.add_argument(
        "--no-publication-mainline-representative-applications",
        action="store_true",
        help=(
            "Disable the enhancer's representative-application garnish in "
            "the publication mainline (default: enabled)."
        ),
    )
    parser.add_argument(
        "--publication-mainline-application-max-targets",
        type=int,
        default=5,
        help="Max representative-application targets per chapter (default: 5).",
    )
    parser.add_argument(
        "--publication-mainline-application-soft-min-targets",
        type=int,
        default=4,
        help=(
            "Advisory soft minimum of representative-application targets; "
            "one bounded supplemental planner call may close a shortfall "
            "(default: 4)."
        ),
    )
    parser.add_argument(
        "--publication-mainline-application-per-target-cap",
        type=int,
        default=6,
        help="Max S2 results per representative-application target (default: 6).",
    )
    parser.add_argument(
        "--publication-mainline-application-local-max-results",
        type=int,
        default=6,
        help="Max local metadata results per target (clamped to <=6).",
    )
    parser.add_argument(
        "--publication-mainline-application-writer-tier",
        default="c2_model",
        help="Cheap Qwen tier for the batched application writer.",
    )
    parser.add_argument(
        "--no-publication-mainline-s2-metadata-fallback",
        action="store_true",
        help=(
            "Disable the lazy S2 metadata/abstract fallback for "
            "representative applications (default: enabled)."
        ),
    )
    parser.add_argument(
        "--publication-mainline-enhancement-workers",
        type=int,
        default=3,
        help="Bounded parallel chapter-enhancement workers (default: 3).",
    )
    parser.add_argument(
        "--publication-mainline-staged-editorial-workers",
        type=int,
        default=3,
        help=(
            "Bounded editorial work-item workers; same-section items stay "
            "serial (default: 3)."
        ),
    )
    parser.add_argument(
        "--visual-workers",
        type=int,
        default=2,
        help="Bounded parallel visual-generation workers (default: 2).",
    )
    parser.add_argument(
        "--visual-budget-cny",
        type=float,
        default=None,
        help=(
            "Shared CNY cap for visual planning, selected-source audit, "
            "and bounded image generation."
        ),
    )
    parser.add_argument(
        "--no-real-visual-audit",
        action="store_true",
        help="Skip real Qwen-VL audit of selected pending source figures.",
    )
    parser.add_argument(
        "--no-real-image-generation",
        action="store_true",
        help="Do not materialize requested explanatory visuals with Qwen Image.",
    )
    parser.add_argument(
        "--no-publication-metadata-online",
        action="store_true",
        help=(
            "Disable online provider calls (OpenAlex, Crossref, Semantic Scholar) "
            "during publication metadata resolution.  Useful when the network is "
            "unavailable or during offline test runs without toggling visual_test_mode."
        ),
    )
    parser.add_argument(
        "--visual-image-model",
        default="qwen-image-2.0-pro",
        help="Preferred Qwen image generation model with internal fallback.",
    )
    parser.add_argument(
        "--visual-max-generated-images",
        type=int,
        default=None,
    )
    parser.add_argument("--visual-fulltext-processing", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--oa-fulltext-paper-cap", type=int, default=None)
    parser.add_argument("--llm-style-pipeline-enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--chapter-style-governance-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Enable the chapter-scoped Qwen reviewer/author pass. When omitted, "
            "it follows the live LLM style profile."
        ),
    )
    parser.add_argument(
        "--chapter-style-governance-budget-cny",
        type=float,
        default=None,
        help="Hard CNY cap for the chapter-scoped reviewer/author pass.",
    )
    parser.add_argument(
        "--chapter-style-governance-workers",
        type=int,
        default=6,
        help="Maximum parallel chapter workers (default: 6).",
    )
    parser.add_argument(
        "--chapter-style-reviewer-model-tier",
        default="c_model",
        help="Reviewer tier; production default resolves to qwen3.5-plus.",
    )
    parser.add_argument(
        "--chapter-style-reviser-model-tier",
        default="c2_model",
        help="Reviser tier; production default resolves to qwen3.7-flash.",
    )
    parser.add_argument(
        "--visual-review-auto-accept-seconds",
        type=int,
        default=None,
        help=(
            "Auto-accept approved-pending figures after N seconds via the "
            "human decision gate (P2-1). Omit to wait indefinitely outside "
            "pytest; under pytest visual reviews never block." 
        ),
    )
    parser.add_argument("--research-plan-budget-cny", type=float, default=None)
    parser.add_argument(
        "--no-research-plan",
        action="store_true",
        help="Produce only the review and visual content package.",
    )
    parser.add_argument(
        "--no-research-plan-publication",
        action="store_true",
        help="Skip English/Chinese PDF packaging for the research-plan branch.",
    )
    parser.add_argument(
        "--no-latex-publication",
        action="store_true",
        help="Skip deterministic English and Chinese LaTeX/PDF packaging.",
    )
    parser.add_argument(
        "--no-chinese-publication",
        action="store_true",
        help="Skip scientific Chinese translation and the Chinese PDF.",
    )
    parser.add_argument(
        "--publication-metadata",
        type=Path,
        help=(
            "Optional JSON containing title, authors, abstract, keywords, "
            "date, and acknowledgements."
        ),
    )
    parser.add_argument(
        "--no-crossref-bibliography-enrichment",
        action="store_true",
        help="Do not fill missing bibliographic fields through Crossref.",
    )
    parser.add_argument(
        "--no-latex-previews",
        action="store_true",
        help="Compile the PDF without rendering first/middle/last page previews.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help=(
            "Skip PDF compilation entirely; still produce .tex/.md/.bib. "
            "Missing LaTeX toolchain then never interrupts the run."
        ),
    )
    parser.add_argument(
        "--require-pdf",
        action="store_true",
        help=(
            "Strict mode: fail fast when the LaTeX toolchain is missing "
            "instead of degrading to a no-PDF run."
        ),
    )
    parser.add_argument("--translation-model-tier", default="c2_model")
    parser.add_argument(
        "--translation-fallback-model-tier",
        default="c_model",
    )
    parser.add_argument("--translation-workers", type=int, default=3)
    parser.add_argument(
        "--translation-cost-budget-cny",
        type=float,
        default=None,
        help="Hard CNY budget for English-to-Chinese translation and audit.",
    )
    parser.add_argument(
        "--translation-fail-open",
        action="store_true",
        help=(
            "Deprecated and now the default: validated units are always kept. "
            "Retained so existing commands keep working."
        ),
    )
    parser.add_argument(
        "--translation-strict",
        action="store_true",
        help=(
            "Fail the Chinese stage when any translation unit fails instead "
            "of keeping the validated units as a degraded deliverable. "
            "(Config default is likewise fail-open; use this flag only "
            "when strict failure is required.)"
        ),
    )
    parser.add_argument(
        "--research-plan-translation-cost-budget-cny",
        type=float,
        default=None,
        help="Hard CNY budget for bilingual research-plan translation and audit.",
    )
    parser.add_argument("--review-lead-model-tier", default="premium_model")
    parser.add_argument("--coverage-model-tier", default="advanced_model")
    parser.add_argument("--author-model-tier", default="advanced_model")
    parser.add_argument(
        "--article-completion-model-tier",
        default="premium_model",
    )
    parser.add_argument("--managing-editor-model-tier", default="premium_model")
    parser.add_argument("--visual-model-tier", default="advanced_model")
    parser.add_argument("--no-llm-global-audit", action="store_true")
    parser.add_argument(
        "--allow-premium-text-models",
        action="store_true",
        help=(
            "Exceptional opt-in: do not enforce the default run-scoped "
            "economy ceiling that downgrades premium text models."
        ),
    )
    parser.add_argument(
        "--phase3-execute-coverage",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow Phase 3 gap requests to invoke the bounded Phase 2 OA "
            "coverage loop. The default enables it for live runs and disables "
            "it for offline/test runs."
        ),
    )
    phase3_dag_group = parser.add_mutually_exclusive_group()
    phase3_dag_group.add_argument(
        "--phase3-llm-dag",
        dest="phase3_llm_dag",
        action="store_true",
        default=None,
        help=(
            "Opt in to the optional cross-section LLM relation/DAG pass. "
            "Fast mode defaults this OFF; enabling it may expand Phase 3 "
            "time to multi-hour/near-ten-hour levels for large reviews."
        ),
    )
    phase3_dag_group.add_argument(
        "--no-phase3-llm-dag",
        dest="phase3_llm_dag",
        action="store_false",
        help=(
            "Explicitly disable the optional cross-section LLM relation/DAG "
            "pass while keeping the central-cache claim pool and chapter "
            "evidence pipeline active. This is the fast default."
        ),
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Write and print a cost admission plan without calling any model.",
    )
    return parser


def _execution_profile(args: argparse.Namespace) -> dict[str, object]:
    profiles = {
        "library_offline": {"visual_fulltext_processing": False, "oa_fulltext_paper_cap": 0, "visual_max_generated_images": 4, "llm_style_pipeline_enabled": False, "chapter_style_governance_enabled": False},
        "private_study": {"visual_fulltext_processing": True, "oa_fulltext_paper_cap": 6, "visual_max_generated_images": 4, "llm_style_pipeline_enabled": True, "chapter_style_governance_enabled": True},
        "submission": {"visual_fulltext_processing": True, "oa_fulltext_paper_cap": 6, "visual_max_generated_images": 4, "llm_style_pipeline_enabled": True, "chapter_style_governance_enabled": True},
    }
    resolved = dict(profiles[str(args.execution_profile)])
    for name in ("visual_fulltext_processing", "oa_fulltext_paper_cap", "visual_max_generated_images", "llm_style_pipeline_enabled", "chapter_style_governance_enabled"):
        value = getattr(args, name, None)
        if value is not None:
            resolved[name] = value
    resolved["oa_fulltext_paper_cap"] = min(10, max(0, int(resolved["oa_fulltext_paper_cap"])))
    resolved["visual_max_generated_images"] = max(0, int(resolved["visual_max_generated_images"]))
    resolved["execution_profile"] = str(args.execution_profile)
    return resolved


def main() -> int:
    args = build_parser().parse_args()
    previous_ceiling = os.environ.get(ECONOMY_TEXT_CEILING_ENV)
    set_economy_text_ceiling_enabled(not args.allow_premium_text_models)
    try:
        return _main_with_args(args)
    finally:
        if previous_ceiling is None:
            os.environ.pop(ECONOMY_TEXT_CEILING_ENV, None)
        else:
            os.environ[ECONOMY_TEXT_CEILING_ENV] = previous_ceiling


def _main_with_args(args: argparse.Namespace) -> int:
    _normalize_budget_arguments(args)
    if args.qwen_key_file is not None:
        key_file = args.qwen_key_file.resolve()
        if not key_file.is_file():
            raise FileNotFoundError(f"--qwen-key-file not found: {key_file}")
        os.environ["QWEN_API_KEY_FILE"] = str(key_file)
    question = str(args.question or "")
    if args.question_file is not None:
        question = args.question_file.read_text(encoding="utf-8").strip()
        if not question:
            raise ValueError("--question-file is empty")
    run_dir = args.run_dir or (
        args.output_root / ("rhr_" + uuid.uuid4().hex[:8])
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = RunDirectoryLock(run_dir)
    try:
        lock.acquire()
    except RunDirectoryLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        return _run_harness(args, question, run_dir)
    finally:
        lock.release()


def _run_harness(
    args: argparse.Namespace,
    question: str,
    run_dir: Path,
) -> int:
    profile = _execution_profile(args)
    observability = HarnessObservability(run_dir, run_dir.name)
    m1_library_path, m1_resolution_reason = _resolve_m1_library_path(
        args.m1_library
    )
    base_kb_path, base_kb_asset_role = _base_kb_for_run(
        args.base_kb,
        run_dir=run_dir,
        allow_historical_test_assets=bool(
            args.allow_historical_test_assets
        ),
        materialize_empty_seed=False,
        long_term_material_cache_root=args.long_term_material_cache_root,
    )

    if args.preflight_only:
        # Preflight is read-only with respect to the canonical run state and
        # event stream.  Reusing a confirmed natural-language entry also needs
        # no new Query Planner allowance.
        planned_upstream = 1.0 if question else 0.0
        cached_question = run_dir / "query_planner" / "ORIGINAL_USER_QUESTION.json"
        cached_plan = run_dir / "query_planner" / "query_plan.json"
        cached_gate = run_dir / "QUERY_PLAN_ENTRY_GATE.json"
        if question and cached_question.is_file() and cached_plan.is_file() and cached_gate.is_file():
            try:
                saved_question = json.loads(
                    cached_question.read_text(encoding="utf-8")
                )
                saved_gate = json.loads(cached_gate.read_text(encoding="utf-8"))
                if (
                    str(saved_question.get("user_question") or "").strip()
                    == question.strip()
                    and saved_gate.get("status") == "passed"
                    and saved_gate.get("execution_ready") is True
                ):
                    planned_upstream = 0.0
            except Exception:
                pass
        config = ReviewHarnessConfig(
            query_plan_path=args.query_plan or (run_dir / "query_plan.pending"),
            base_kb_sqlite=base_kb_path,
            output_root=args.output_root,
            base_kb_asset_role=base_kb_asset_role,
            long_term_material_cache_root=args.long_term_material_cache_root,
            long_term_material_cache_writeback=(
                not args.no_long_term_material_cache_writeback
            ),
            m1_library_path=m1_library_path,
            rebuild_scoped_kb=bool(args.rebuild_scoped_kb),
            rebuild_phase3_handoff=bool(args.rebuild_phase3_handoff),
            global_cost_budget_cny=args.global_budget_cny,
            global_budget_only=bool(args.global_budget_only),
            review_lead_budget_cny=args.review_lead_budget_cny,
            section_coverage_budget_cny=args.coverage_budget_cny,
            portfolio_coverage_budget_cny=(
                args.portfolio_coverage_budget_cny
            ),
            feedback_coverage_budget_cny=args.feedback_coverage_budget_cny,
            authoring_budget_cny=args.authoring_budget_cny,
            article_completion_budget_cny=(
                args.article_completion_budget_cny
            ),
            publication_mainline_enabled=(
                not args.no_publication_mainline
            ),
            publication_mainline_commander_model_tier=(
                args.publication_mainline_commander_model_tier
            ),
            publication_mainline_staged_model_tier=(
                args.publication_mainline_staged_model_tier
            ),
            publication_mainline_staged_reviewer_tier=(
                args.publication_mainline_staged_reviewer_tier
            ),
            publication_mainline_staged_editorial_verifier_tier=(
                args.publication_mainline_staged_editorial_verifier_tier
            ),
            publication_mainline_local_metadata_db_path=(
                args.publication_mainline_local_metadata_db
            ),
            publication_mainline_representative_applications_enabled=(
                not args.no_publication_mainline_representative_applications
            ),
            publication_mainline_application_max_targets=max(
                1, args.publication_mainline_application_max_targets
            ),
            publication_mainline_application_soft_min_targets=max(
                0, args.publication_mainline_application_soft_min_targets
            ),
            publication_mainline_application_per_target_cap=max(
                1, args.publication_mainline_application_per_target_cap
            ),
            publication_mainline_application_local_max_results=min(
                6,
                max(
                    1,
                    args.publication_mainline_application_local_max_results,
                ),
            ),
            publication_mainline_application_writer_tier=(
                args.publication_mainline_application_writer_tier
            ),
            publication_mainline_s2_metadata_fallback=(
                not args.no_publication_mainline_s2_metadata_fallback
            ),
            publication_mainline_enhancement_workers=max(
                1, args.publication_mainline_enhancement_workers
            ),
            publication_mainline_staged_editorial_workers=max(
                1, args.publication_mainline_staged_editorial_workers
            ),
            visual_editor_budget_cny=args.visual_budget_cny,
            visual_real_audit=False,
            visual_real_generation=False,
            visual_test_mode=True,
            publication_metadata_online=False,
            visual_image_model=args.visual_image_model,
            visual_max_generated_images=max(
                0,
                int(profile["visual_max_generated_images"]),
            ),
            execution_profile=str(profile["execution_profile"]),
            visual_fulltext_processing=bool(profile["visual_fulltext_processing"]),
            oa_fulltext_paper_cap=int(profile["oa_fulltext_paper_cap"]),
            llm_style_pipeline_enabled=bool(profile["llm_style_pipeline_enabled"]),
            chapter_style_governance_enabled=(
                bool(profile["chapter_style_governance_enabled"])
                if args.chapter_style_governance_enabled is None
                else bool(args.chapter_style_governance_enabled)
            ),
            chapter_style_governance_budget_cny=max(
                0.0, args.chapter_style_governance_budget_cny
            ),
            chapter_style_governance_workers=max(
                1, args.chapter_style_governance_workers
            ),
            chapter_style_reviewer_model_tier=(
                args.chapter_style_reviewer_model_tier
            ),
            chapter_style_reviser_model_tier=(
                args.chapter_style_reviser_model_tier
            ),
            visual_review_auto_accept_seconds=(
                args.visual_review_auto_accept_seconds
            ),
            visual_workers=max(
                1, args.visual_workers
            ),
            research_plan_budget_cny=args.research_plan_budget_cny,
            produce_research_plan=not args.no_research_plan,
            produce_research_plan_publication=(
                not args.no_research_plan
                and not args.no_research_plan_publication
                and not args.no_latex_publication
                and not args.no_chinese_publication
            ),
            research_plan_translation_cost_budget_cny=max(
                0.01, args.research_plan_translation_cost_budget_cny
            ),
            produce_latex_publication=not args.no_latex_publication,
            produce_chinese_publication=(
                not args.no_latex_publication
                and not args.no_chinese_publication
            ),
            publication_metadata_path=args.publication_metadata,
            latex_enrich_crossref=(
                not args.no_crossref_bibliography_enrichment
            ),
            latex_render_previews=not args.no_latex_previews,
            compile_pdf=(not args.no_pdf),
            pdf_strict=bool(args.require_pdf),
            translation_model_tier=args.translation_model_tier,
            translation_fallback_model_tier=(
                args.translation_fallback_model_tier
            ),
            translation_workers=max(1, args.translation_workers),
            translation_cost_budget_cny=max(
                0.01,
                args.translation_cost_budget_cny,
            ),
            translation_fail_open=not bool(args.translation_strict),
            upstream_cost_cny=planned_upstream,
            phase3_execute_coverage=args.phase3_execute_coverage,
            phase3_real_llm_dag=args.phase3_llm_dag,
        )
        orchestrator = ReviewHarnessOrchestrator(
            config,
            run_dir=run_dir,
            observability=observability,
        )
        report = orchestrator.preflight()
        atomic_write_json(run_dir / "COST_PREFLIGHT.json", report)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["within_budget"] else 2

    observability.start_run(
        entry_mode="natural_language_question" if question else "query_plan",
        resumed=(run_dir / "HARNESS_EVENTS.jsonl").exists(),
    )

    config_status = validate_qwen_config()
    atomic_write_json(
        run_dir / "QWEN_CAPABILITY_STATUS.json",
        {
            "has_api_key": config_status.get("has_api_key"),
            "api_key_candidate_count": config_status.get(
                "api_key_candidate_count", 0
            ),
            "mock_llm": config_status.get("mock_llm"),
            "model_aliases": config_status.get("model_aliases", {}),
            "model_fallbacks": config_status.get("model_fallbacks", {}),
        },
    )

    try:
        query_plan, query_metrics = _prepare_query_plan(
            question=question,
            query_plan_path=args.query_plan,
            work_dir=run_dir,
            mock=bool(args.mock_query_planner),
            auto_confirm=bool(args.auto_confirm_query_plan),
            observability=observability,
        )
    except Exception as exc:
        observability.fail(
            stage="query_planner",
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        observability.finish_run(
            status="failed",
            current_stage="query_planner",
            stage_costs={},
            harness_state={"stages": {}},
        )
        raise
    if query_plan is None:
        terminal_status = str(
            query_metrics.get("terminal_status")
            or "awaiting_query_plan_confirmation"
        )
        awaiting_confirmation = (
            terminal_status == "query_plan_ready_for_confirmation"
        )
        run_status = (
            "awaiting_query_plan_confirmation"
            if awaiting_confirmation
            else terminal_status
        )
        terminal_query_path = Path(
            str(
                query_metrics.get("compact_query_plan")
                or run_dir / "query_planner" / "query_plan.json"
            )
        )
        terminal_harness = ReviewHarnessOrchestrator(
            ReviewHarnessConfig(
                query_plan_path=terminal_query_path,
                base_kb_sqlite=base_kb_path,
                output_root=args.output_root,
                base_kb_asset_role=base_kb_asset_role,
                m1_library_path=m1_library_path,
                global_cost_budget_cny=args.global_budget_cny,
                upstream_cost_cny=float(
                    query_metrics.get("cost_cny", 0.0) or 0.0
                ),
                upstream_input_tokens=int(
                    query_metrics.get("input_tokens", 0) or 0
                ),
                upstream_output_tokens=int(
                    query_metrics.get("output_tokens", 0) or 0
                ),
            ),
            run_dir=run_dir,
            observability=observability,
        )
        terminal_harness.finalize_upstream_stop(
            status=run_status,
            stage="query_planner",
            stage_status=(
                "awaiting_human_confirmation"
                if awaiting_confirmation
                else "failed_closed"
            ),
            stage_metrics=query_metrics,
        )
        output_metrics = {
            key: value
            for key, value in query_metrics.items()
            if key != "status"
        }
        print(
            json.dumps(
                {
                    "status": run_status,
                    "run_dir": str(run_dir),
                    **output_metrics,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 3

    base_kb_path, base_kb_asset_role = _base_kb_for_run(
        args.base_kb,
        run_dir=run_dir,
        allow_historical_test_assets=bool(
            args.allow_historical_test_assets
        ),
        materialize_empty_seed=True,
        query_plan_path=query_plan,
        long_term_material_cache_root=args.long_term_material_cache_root,
    )
    _write_asset_roles(
        run_dir=run_dir,
        base_kb=base_kb_path,
        base_role=base_kb_asset_role,
        mentor_library=m1_library_path,
        mentor_requested_path=args.m1_library,
        mentor_resolution_reason=m1_resolution_reason,
    )

    config = ReviewHarnessConfig(
        query_plan_path=query_plan,
        base_kb_sqlite=base_kb_path,
        output_root=args.output_root,
        base_kb_asset_role=base_kb_asset_role,
        long_term_material_cache_root=args.long_term_material_cache_root,
        long_term_material_cache_writeback=(
            not args.no_long_term_material_cache_writeback
        ),
        m1_library_path=m1_library_path,
        phase3_artifacts_root=args.phase3_artifacts_root,
        rebuild_scoped_kb=bool(args.rebuild_scoped_kb),
        rebuild_phase3_handoff=bool(args.rebuild_phase3_handoff),
        global_cost_budget_cny=args.global_budget_cny,
        global_budget_only=bool(args.global_budget_only),
        review_lead_budget_cny=args.review_lead_budget_cny,
        section_coverage_budget_cny=args.coverage_budget_cny,
        portfolio_coverage_budget_cny=(
            args.portfolio_coverage_budget_cny
        ),
        feedback_coverage_budget_cny=args.feedback_coverage_budget_cny,
        authoring_budget_cny=args.authoring_budget_cny,
        article_completion_budget_cny=(
            args.article_completion_budget_cny
        ),
        publication_mainline_enabled=(
            not args.no_publication_mainline
        ),
        publication_mainline_commander_model_tier=(
            args.publication_mainline_commander_model_tier
        ),
        publication_mainline_staged_model_tier=(
            args.publication_mainline_staged_model_tier
        ),
        publication_mainline_staged_reviewer_tier=(
            args.publication_mainline_staged_reviewer_tier
        ),
        publication_mainline_staged_editorial_verifier_tier=(
            args.publication_mainline_staged_editorial_verifier_tier
        ),
        publication_mainline_local_metadata_db_path=(
            args.publication_mainline_local_metadata_db
        ),
        publication_mainline_representative_applications_enabled=(
            not args.no_publication_mainline_representative_applications
        ),
        publication_mainline_application_max_targets=max(
            1, args.publication_mainline_application_max_targets
        ),
        publication_mainline_application_soft_min_targets=max(
            0, args.publication_mainline_application_soft_min_targets
        ),
        publication_mainline_application_per_target_cap=max(
            1, args.publication_mainline_application_per_target_cap
        ),
        publication_mainline_application_local_max_results=min(
            6,
            max(
                1,
                args.publication_mainline_application_local_max_results,
            ),
        ),
        publication_mainline_application_writer_tier=(
            args.publication_mainline_application_writer_tier
        ),
        publication_mainline_s2_metadata_fallback=(
            not args.no_publication_mainline_s2_metadata_fallback
        ),
        publication_mainline_enhancement_workers=max(
            1, args.publication_mainline_enhancement_workers
        ),
        publication_mainline_staged_editorial_workers=max(
            1, args.publication_mainline_staged_editorial_workers
        ),
        visual_editor_budget_cny=args.visual_budget_cny,
        visual_real_audit=not args.no_real_visual_audit,
        visual_real_generation=not args.no_real_image_generation,
        visual_test_mode=False,
        publication_metadata_online=not args.no_publication_metadata_online,
        visual_image_model=args.visual_image_model,
        visual_max_generated_images=max(
            0,
            int(profile["visual_max_generated_images"]),
        ),
        execution_profile=str(profile["execution_profile"]),
        visual_fulltext_processing=bool(profile["visual_fulltext_processing"]),
        oa_fulltext_paper_cap=int(profile["oa_fulltext_paper_cap"]),
        llm_style_pipeline_enabled=bool(profile["llm_style_pipeline_enabled"]),
        chapter_style_governance_enabled=(
            bool(profile["chapter_style_governance_enabled"])
            if args.chapter_style_governance_enabled is None
            else bool(args.chapter_style_governance_enabled)
        ),
        chapter_style_governance_budget_cny=max(
            0.0, args.chapter_style_governance_budget_cny
        ),
        chapter_style_governance_workers=max(
            1, args.chapter_style_governance_workers
        ),
        chapter_style_reviewer_model_tier=(
            args.chapter_style_reviewer_model_tier
        ),
        chapter_style_reviser_model_tier=(
            args.chapter_style_reviser_model_tier
        ),
        visual_review_auto_accept_seconds=(
            args.visual_review_auto_accept_seconds
        ),
        visual_workers=max(
            1, args.visual_workers
        ),
        research_plan_budget_cny=args.research_plan_budget_cny,
        produce_research_plan=not args.no_research_plan,
        produce_research_plan_publication=(
            not args.no_research_plan
            and not args.no_research_plan_publication
            and not args.no_latex_publication
            and not args.no_chinese_publication
        ),
        research_plan_translation_cost_budget_cny=max(
            0.01, args.research_plan_translation_cost_budget_cny
        ),
        produce_latex_publication=not args.no_latex_publication,
        produce_chinese_publication=(
            not args.no_latex_publication
            and not args.no_chinese_publication
        ),
        publication_metadata_path=args.publication_metadata,
        latex_enrich_crossref=(
            not args.no_crossref_bibliography_enrichment
        ),
        latex_render_previews=not args.no_latex_previews,
        compile_pdf=(not args.no_pdf),
        pdf_strict=bool(args.require_pdf),
        translation_model_tier=args.translation_model_tier,
        translation_fallback_model_tier=(
            args.translation_fallback_model_tier
        ),
        translation_workers=max(1, args.translation_workers),
        translation_cost_budget_cny=max(
            0.01,
            args.translation_cost_budget_cny,
        ),
        translation_fail_open=not bool(args.translation_strict),
        upstream_cost_cny=float(query_metrics.get("cost_cny", 0.0)),
        upstream_input_tokens=int(query_metrics.get("input_tokens", 0)),
        upstream_output_tokens=int(query_metrics.get("output_tokens", 0)),
        review_lead_model_tier=args.review_lead_model_tier,
        coverage_model_tier=args.coverage_model_tier,
        author_model_tier=args.author_model_tier,
        article_completion_model_tier=(
            args.article_completion_model_tier
        ),
        managing_editor_model_tier=args.managing_editor_model_tier,
        visual_editor_model_tier=args.visual_model_tier,
        use_llm_global_audit=not args.no_llm_global_audit,
        phase3_execute_coverage=args.phase3_execute_coverage,
        phase3_real_llm_dag=args.phase3_llm_dag,
    )
    harness = ReviewHarnessOrchestrator(
        config,
        run_dir=run_dir,
        observability=observability,
    )
    try:
        result = harness.run()
    except Exception as exc:
        observability.fail(
            stage="orchestrator",
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        observability.finish_run(
            status="failed",
            current_stage="orchestrator",
            stage_costs=harness.stage_costs,
            harness_state=harness.state,
        )
        raise
    payload = {
        "status": result.status,
        "completed_stage": result.completed_stage,
        "run_dir": str(result.work_dir),
        "content_package": str(result.package_path),
        "final_review": (
            str(result.final_review_path) if result.final_review_path else ""
        ),
        "visual_plan": (
            str(result.visual_plan_path) if result.visual_plan_path else ""
        ),
        "final_visual_package": (
            str(result.final_visual_package_path)
            if result.final_visual_package_path
            else ""
        ),
        "research_plan": (
            str(result.research_plan_path)
            if result.research_plan_path
            else ""
        ),
        "latex_pdf": (
            str(result.latex_pdf_path) if result.latex_pdf_path else ""
        ),
        "latex_source_archive": (
            str(result.latex_source_archive_path)
            if result.latex_source_archive_path
            else ""
        ),
        "chinese_review": (
            str(result.chinese_review_path)
            if result.chinese_review_path
            else ""
        ),
        "chinese_latex_pdf": (
            str(result.chinese_latex_pdf_path)
            if result.chinese_latex_pdf_path
            else ""
        ),
        "chinese_latex_source_archive": (
            str(result.chinese_latex_source_archive_path)
            if result.chinese_latex_source_archive_path
            else ""
        ),
        "input_tokens": result.total_input_tokens,
        "output_tokens": result.total_output_tokens,
        "estimated_cost_cny": result.total_cost_cny,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    if result.status == "completed":
        return 0
    if result.status in {
        "awaiting_human_review",
        "partial",
        "needs_more_literature",
    }:
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
