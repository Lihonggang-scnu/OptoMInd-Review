"""OptoMind local read-only run viewer + decision answering (P2-2).

Safety contract (ticket section 1, binding):
  - the server binds 127.0.0.1 only and has no authentication BECAUSE
    loopback-only means zero network exposure.  Never rebind it.
  - the ONLY write path is POST /api/runs/{run_id}/decisions/{id},
    which delegates to human_decision_gate.resolve_decision; every GET
    handler is read-only over run directories.
  - PENDING_DECISIONS/*.json and DECISION_LEDGER.jsonl are never parsed
    or written here: all of that goes through the gate module functions
    (ticket revision 2), which also own the ledger format via
    decision_history().
No model calls happen anywhere in this file.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from fastapi.staticfiles import StaticFiles

from optomind_research.runtime.human_decision_gate import (
    decision_history,
    decision_state,
    expire_due_decisions,
    list_pending,
    resolve_decision,
)
from optomind_ui import intent_router
from optomind_ui.narrator import build as build_narrative, project_line
from optomind_ui.preflight import (
    _blocking_failures as preflight_blocking_failures,
    check_all as preflight_check_all,
)
from optomind_ui.stage_registry import (
    all_stages,
    stage_explain,
    stage_label,
    status_label,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Ticket revision 3: same root as DEFAULT_OUTPUT_ROOT in
# run_review_harness.py (:77); every direct subdirectory is one run.
DEFAULT_RUN_ROOT = PROJECT_ROOT / "outputs" / "research_harness_e2e"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CHUNK_SIZE = 1 << 20
_RUN_ID_RE = re.compile(r"^rhr_[a-z0-9]{8,32}$")
_PROCESS_LOCK = threading.Lock()
_TASK_PROCESSES: Dict[str, subprocess.Popen] = {}
# F1: human-facing stage/status labels live in optomind_ui.stage_registry,
# the single source of truth built on ReviewHarnessOrchestrator.STAGES.
_COMPLETED_STATUSES = {
    "completed", "completed_with_limits", "completed_with_warnings",
    "partial", "compiled", "compiled_awaiting_metadata",
    "submission_ready",
}

# (mtime_ns, size) keyed line-offset index for big JSONL streams.
# Rebuilt only when either component changes; guarded by a lock because
# uvicorn may serve requests from several threads.
_LINE_INDEX_CACHE: Dict[str, Dict[str, Any]] = {}
_LINE_INDEX_LOCK = threading.Lock()


def _jsonl_line_offsets(path: Path) -> List[int]:
    """Byte offsets where each JSONL line starts; cached by mtime+size."""

    try:
        stat_result = path.stat()
    except OSError:
        return []
    stamp = (stat_result.st_mtime_ns, stat_result.st_size)
    key = str(path)
    with _LINE_INDEX_LOCK:
        entry = _LINE_INDEX_CACHE.get(key)
        if entry is not None and entry["stamp"] == stamp:
            return entry["offsets"]
    offsets: List[int] = []
    position = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            newline_at = chunk.find(b"\n")
            while newline_at != -1:
                offsets.append(position + newline_at + 1)
                newline_at = chunk.find(b"\n", newline_at + 1)
            position += len(chunk)
    total_size = stamp[1]
    if offsets and offsets[-1] >= total_size:
        # A trailing final newline is not an extra empty line.
        offsets.pop()
    if stamp[1] > 0:
        # Line zero starts at byte 0; the scan above only records lines
        # that FOLLOW a newline character.
        offsets.insert(0, 0)
    with _LINE_INDEX_LOCK:
        _LINE_INDEX_CACHE[key] = {"stamp": stamp, "offsets": offsets}
    return offsets


def _read_jsonl_page(
    path: Path,
    offset: int,
    limit: int,
) -> Dict[str, Any]:
    offsets = _jsonl_line_offsets(path)
    total = len(offsets)
    page = offsets[offset : offset + limit]
    events: List[Any] = []
    if page:
        total_size = path.stat().st_size
        boundary = set(offsets)
        with path.open("rb") as handle:
            for index, line_start in enumerate(page):
                line_end = (
                    offsets[offset + index + 1]
                    if offset + index + 1 < total
                    else total_size
                )
                handle.seek(line_start)
                raw = handle.read(max(0, line_end - line_start))
                try:
                    events.append(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    events.append(
                        {"_unparsable": True,
                         "_raw_head": raw[:200].decode("utf-8", "replace")}
                    )
    return {
        "total_lines": total,
        "offset": offset,
        "returned": len(events),
        "events": events,
    }


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _safe_run_id(value: str) -> str:
    run_id = str(value or "").strip().lower()
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    return run_id


def _task_command(run_root: Path, run_id: str, question: str) -> List[str]:
    harness = PROJECT_ROOT / "run_review_harness.py"
    return [
        sys.executable, str(harness),
        "--question", question,
        "--output-root", str(run_root),
        "--run-dir", str(run_root / run_id),
        "--auto-confirm-query-plan",
    ]


# ---- F2 process tracking & concurrency guards -------------------------------
# Ticket note: this dot-file lives in outputs/ ROOT by explicit F2 instruction
# (never inside a run_dir -- those stay read-only). It is written lazily, only
# when a UI-launched task spawns/stops; harness-started runs never touch it.
_UI_TASK_REGISTRY_NAME = ".ui_task_registry.json"
_IDEMPOTENCY_WINDOW_SECONDS = 120.0
_STOP_GRACE_SECONDS = 10.0


def _pid_alive(pid: Any) -> bool:
    """Windows-safe liveness probe. NEVER os.kill(pid, 0): on Windows that
    can TERMINATE the target instead of signalling."""

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False

        class _ExitCode(ctypes.Structure):
            _fields_ = [("value", ctypes.c_ulong)]

        code = _ExitCode()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:
        return False


# ---- F2 / GAP-6: background expiry of due human decisions ------------------
# P3-3 made every GET a pure read, so NOTHING expires decisions anymore;
# expire_due_decisions had zero production callers. This loop is the single
# sanctioned writer-side scheduler. Boundary (P3-5): the per-run lock is an
# in-process threading.RLock -- multi-process deployments are out of scope;
# this scheduler only acts inside the UI service process.
_EXPIRY_LOGGER_NAME = "optomind_ui.decision_expiry"
_EXPIRY_ENV_VAR = "OPTOMIND_UI_EXPIRE_INTERVAL_SECONDS"
_ACTIVE_DECISION_STATUSES = {"running", "starting", "awaiting_human_review"}


def _expire_interval_seconds() -> float:
    try:
        value = float(os.environ.get(_EXPIRY_ENV_VAR, "60"))
    except ValueError:
        value = 60.0
    return value


async def _expire_cycle_once(run_root_str: str) -> Dict[str, int]:
    """One sweep over ACTIVE run dirs only (never the whole outputs tree)."""

    from optomind_research.runtime.human_decision_gate import (
        expire_due_decisions as _expire,
        list_pending as _pending,
    )

    root = Path(run_root_str)
    scanned = 0
    expired = 0
    if not root.is_dir():
        return {"scanned": 0, "expired": 0}
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        state, _stale = _read_state_cached(Path(entry.path))
        status = str((state or {}).get("status") or "")
        if status not in _ACTIVE_DECISION_STATUSES:
            continue
        run_dir = Path(entry.path)
        scanned += 1
        try:
            before = len(list_pending(run_dir))
            # expire_due_decisions itself holds the P3-5 per-run RLock.
            _expire(run_dir)
            after = len(list_pending(run_dir))
            expired += max(0, before - after)
        except Exception as exc:  # one bad run must not kill the sweep
            logging.getLogger(_EXPIRY_LOGGER_NAME).warning(
                "expiry sweep skipped %s: %s", entry.name, exc
            )
    result = {"scanned": scanned, "expired": expired}
    logging.getLogger(_EXPIRY_LOGGER_NAME).info(
        "decision expiry cycle: scanned=%(scanned)d expired=%(expired)d", result
    )
    return result


async def _expiry_loop(run_root_str: str, interval: float) -> None:
    logger = logging.getLogger(_EXPIRY_LOGGER_NAME)
    while True:
        await asyncio.sleep(interval)
        try:
            await _expire_cycle_once(run_root_str)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("expiry loop error: %s", exc)


def _terminate_foreign_pid(pid: Any) -> None:
    """Last-resort stop for a pid whose Popen handle was lost to a restart."""

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, int(pid))  # TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
    except Exception:
        pass


# ---- F2 incremental readers -------------------------------------------------
# The harness WRITES these files while the UI reads them. Re-reading the whole
# event stream (5.5 MB) plus HARNESS_STATE.json (9 MB) every two seconds per
# open tab raced with the writer: partial reads returned None/{} and the UI
# showed "准备启动 / ¥0" for a running task. Both readers below are
# incremental, cached, and NEVER silently degrade to empty -- a failed read
# surfaces as stale=True with the last good value retained.

_STATE_TTL_SECONDS = 2.0
_STATE_CACHE: Dict[str, Dict[str, Any]] = {}
_EVENTS_AGG_CACHE: Dict[str, Dict[str, Any]] = {}
_INCREMENTAL_LOCK = threading.Lock()
_LAST_EVENTS_BYTES_READ = {"bytes": 0}
_GIANT_LINE_BYTES = 262144  # aggregate head-fields only; never json.loads these
_EVENT_HEAD_RE = re.compile(r'"(?P<key>event|stage|status)"\s*:\s*"(?P<val>[^"]*)"')


def _read_state_cached(run_dir: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Read HARNESS_STATE.json behind a short TTL.

    Returns (state_or_last_good_value, stale). On a read failure (the
    harness holding the file mid-write) the previous value is kept and
    stale=True -- never a silent {}.
    """

    path = run_dir / "HARNESS_STATE.json"
    key = str(path)
    now = time.monotonic()
    entry: Optional[Dict[str, Any]] = None
    with _INCREMENTAL_LOCK:
        entry = _STATE_CACHE.get(key)
        if entry is not None and now < entry["expires"]:
            return entry["value"], False
    value = _read_json(path)
    with _INCREMENTAL_LOCK:
        if value is not None:
            _STATE_CACHE[key] = {"value": value, "expires": time.monotonic() + _STATE_TTL_SECONDS}
            return value, False
        if entry is not None:
            return entry["value"], True
    return None, True


def _new_event_agg() -> Dict[str, Any]:
    return {
        "count": 0,
        "last_started": "",
        "finished": {},
        "last_event": "",
        "last_stage": "",
    }


def _agg_absorb_line(agg: Dict[str, Any], raw: bytes) -> None:
    """Fold one JSONL line into the aggregate; giant lines parse head-only."""

    if not raw.strip():
        return
    if len(raw) >= _GIANT_LINE_BYTES:
        heads = {
            match.group("key"): match.group("val")
            for match in _EVENT_HEAD_RE.finditer(raw[:4096].decode("utf-8", "replace"))
        }
        event = heads.get("event", "")
        stage = heads.get("stage", "")
        status = heads.get("status", "")
        if not event:
            return
    else:
        try:
            row = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return
        if not isinstance(row, dict):
            return
        event = str(row.get("event") or "")
        stage = str(row.get("stage") or "")
        status = str(row.get("status") or "")
    agg["count"] += 1
    agg["last_event"] = event
    agg["last_stage"] = stage
    if event == "stage_started" and stage:
        agg["last_started"] = stage
    elif event == "stage_finished" and stage:
        agg["finished"][stage] = status or "completed"


def _aggregate_events(path: Path) -> Dict[str, Any]:
    """Byte-offset incremental aggregation of the events JSONL.

    First call backfills once; every later call reads only bytes appended
    since the previous call (O(new bytes), never O(file)).
    """

    key = str(path)
    try:
        stat_result = path.stat()
    except OSError:
        return _new_event_agg()
    size = stat_result.st_size
    with _INCREMENTAL_LOCK:
        entry = _EVENTS_AGG_CACHE.get(key)
        if entry is None or stat_result.st_size < entry["offset"]:
            entry = {"offset": 0, "partial": b"", "agg": _new_event_agg()}
        agg = entry["agg"]
        bytes_read = 0
        if size > entry["offset"]:
            try:
                with path.open("rb") as handle:
                    handle.seek(entry["offset"])
                    chunk = handle.read(size - entry["offset"])
                bytes_read = len(chunk)
                blob = entry["partial"] + chunk if entry["partial"] else chunk
                lines = blob.split(b"\n")
                tail = lines.pop()  # possibly-incomplete final line stays buffered
                entry["partial"] = tail
                for line in lines:
                    _agg_absorb_line(agg, line)
                entry["offset"] += bytes_read - len(tail)
            except OSError:
                pass  # keep last good aggregate on transient failures
        _LAST_EVENTS_BYTES_READ["bytes"] = bytes_read
        _EVENTS_AGG_CACHE[key] = entry
        # Persistent aggregates fold COMPLETE lines only; the buffered tail
        # (a writer mid-line, or a file without a trailing newline) is folded
        # into the RETURNED VIEW idempotently so it is never lost yet never
        # double-counted.
        result = {
            "count": agg["count"],
            "last_started": agg["last_started"],
            "finished": dict(agg["finished"]),
            "last_event": agg["last_event"],
            "last_stage": agg["last_stage"],
        }
        if entry["partial"]:
            _agg_absorb_line(result, entry["partial"])
        return result


def _progress_snapshot(run_dir: Path) -> Dict[str, Any]:
    state, stale_state = _read_state_cached(run_dir)
    stale_sources: List[str] = ["HARNESS_STATE.json"] if stale_state else []
    state = state or {}
    current = str(state.get("current_stage") or "")
    stages = dict(state.get("stages") or {})
    agg = _aggregate_events(run_dir / "HARNESS_EVENTS.jsonl")
    if not current:
        current = str(agg.get("last_started") or "")
    if not state.get("status"):
        state = dict(state)
        state["status"] = "running" if agg["count"] else "starting"
    for finished_stage, finished_status in agg["finished"].items():
        stages[finished_stage] = {"status": finished_status}
    # F1 ticket fix B: never blank the current stage. The last stage_started
    # wins; when it has just finished and nothing newer started, keep it as
    # the current stage and flag the hand-off instead ("已完成，正在衔接").
    bridging = (
        bool(current)
        and agg["count"] > 0
        and agg["last_event"] == "stage_finished"
        and agg["last_stage"] == current
    )
    completed: set[str] = set()
    for name, value in stages.items():
        if isinstance(value, dict) and str(value.get("status") or "").lower() in _COMPLETED_STATUSES:
            completed.add(str(name))
    items: List[Dict[str, Any]] = []
    current_seen = False
    for index, record in enumerate(all_stages()):
        stage = record["key"]
        status = "pending"
        if stage in completed:
            status = "completed"
        elif stage == current and not current_seen:
            status = "completed" if bridging else "running"
            current_seen = True
        elif any(item.get("status") == "running" for item in items):
            status = "pending"
        # "detail" kept for pre-F1 consumers; "explain" is the canonical name.
        items.append(
            {
                "index": index,
                "stage": stage,
                "label": record["label"],
                "detail": record["explain"],
                "explain": stage_explain(stage),
                "status": status,
            }
        )
    base_label = (stage_label(current) or "准备启动") if current else "准备启动"
    if bridging:
        base_label = f"{base_label}（已完成，正在衔接）"
    return {
        "run_id": run_dir.name,
        "status": state.get("status", "starting"),
        "status_label": status_label(str(state.get("status") or "starting")),
        "current_stage": current,
        "bridging": bridging,
        "current_label": base_label,
        "steps": items,
        "error_count": state.get("error_count", 0),
        "cost_cny": (_read_json(run_dir / "HARNESS_COST.json") or {}).get("cost_cny", 0.0),
        "event_count": agg["count"],
        "stale": bool(stale_sources),
        "stale_sources": stale_sources,
    }


def _user_question(run_dir: Path) -> str:
    original = _read_json(run_dir / "query_planner" / "ORIGINAL_USER_QUESTION.json") or {}
    question = str(original.get("user_question") or "").strip()
    if question:
        return question
    plan = _read_json(run_dir / "query_planner" / "query_plan.json") or {}
    value = plan.get("input") if isinstance(plan.get("input"), dict) else {}
    question = str(value.get("user_query") or "").strip()
    if question.startswith("Research question requiring"):
        return ""
    return question


def create_app(run_root: Optional[Path] = None) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # GAP-6 scheduler: default on, 60 s period, configurable via
        # OPTOMIND_UI_EXPIRE_INTERVAL_SECONDS (<= 0 disables). Cancelled
        # cleanly on shutdown so no task dangles past server exit.
        interval = _expire_interval_seconds()
        expiry_task = None
        if interval > 0:
            expiry_task = asyncio.create_task(
                _expiry_loop(str(Path(app.state.run_root)), interval)
            )
        try:
            yield
        finally:
            if expiry_task is not None:
                expiry_task.cancel()
                try:
                    await expiry_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="OptoMind local run viewer",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.run_root = Path(run_root or DEFAULT_RUN_ROOT)
    app.state.project_root = PROJECT_ROOT
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    def run_dir(run_id: str) -> Path:
        if (
            not run_id
            or "/" in run_id
            or "\\" in run_id
            or ".." in run_id
            or run_id.startswith(".")
        ):
            raise HTTPException(status_code=400, detail="invalid run id")
        root = Path(app.state.run_root).resolve()
        candidate = (root / run_id).resolve()
        if root != candidate and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="invalid run id")
        if not candidate.is_dir():
            raise HTTPException(status_code=404, detail="unknown run")
        return candidate

    def _registry_path() -> Path:
        return Path(app.state.run_root) / _UI_TASK_REGISTRY_NAME

    def _load_task_registry() -> Dict[str, Any]:
        value = _read_json(_registry_path())
        if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict):
            return {"version": 1, "tasks": {}}
        return value

    def _save_task_registry(registry: Dict[str, Any]) -> None:
        path = _registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)  # atomic on same volume

    # ---- F3 static assembly: the React SPA (static/dist) becomes the UI --
    # This is routing glue only (ticket F3 allows "静态资源装配"); no F1/F2
    # endpoint logic is touched. Legacy templates/app.js stay on disk but are
    # no longer referenced once the dist build exists.
    _DIST_DIR = Path(__file__).parent / "static" / "dist"

    def _spa_shell() -> FileResponse:
        return FileResponse(_DIST_DIR / "index.html")

    if _DIST_DIR.is_dir():

        @app.get("/")
        def index() -> FileResponse:
            return _spa_shell()

        # F6: the desktop sidecar must serve the built SPA fully (hashed
        # JS/CSS live under dist/assets); additive mount after all API routes.
        if (_DIST_DIR / "assets").is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(_DIST_DIR / "assets")),
                name="spa_assets",
            )

        @app.get("/task/{run_id}")
        @app.get("/run/{run_id}")
        @app.get("/run/{run_id}/{section}")
        def spa_deep_link(run_id: str, section: str = "") -> FileResponse:
            # Deep links hand the path to the SPA; state is recovered from
            # the URL plus the read-only API. run_id is validated by the
            # same rule as every other consumer when data is requested.
            del run_id
            return _spa_shell()

    @app.get("/api/runs")
    def list_runs() -> List[Dict[str, Any]]:
        root = Path(app.state.run_root)
        runs: List[Dict[str, Any]] = []
        if not root.is_dir():
            return runs
        for entry in os.scandir(root):
            # os.scandir per ticket revision 3; each subdir is one run.
            if not entry.is_dir():
                continue
            state = _read_json(Path(entry.path) / "HARNESS_STATE.json") or {}
            status = str(state.get("status") or "unknown")
            current_stage = str(state.get("current_stage") or "")
            runs.append(
                {
                    "run_id": entry.name,
                    "question": _user_question(Path(entry.path)),
                    "status": status,
                    "status_label": status_label(status) or "历史任务",
                    "current_stage": current_stage,
                    "current_label": stage_label(current_stage) or "准备中",
                    "error_count": state.get("error_count", 0),
                }
            )
        runs.sort(key=lambda item: item["run_id"], reverse=True)
        return runs

    @app.post("/api/tasks")
    def create_task(body: Dict[str, Any]) -> Dict[str, Any]:
        question = str(body.get("question") or "").strip()
        if len(question) < 4:
            raise HTTPException(status_code=400, detail="请先输入一个完整的科研问题")
        if len(question) > 4000:
            raise HTTPException(status_code=400, detail="问题长度不能超过 4000 个字符")
        # F2: spawning a PAID harness run requires a fresh credential signed by
        # the intent router for THIS exact question. A bare ">=4 chars" string
        # no longer starts anything.
        ok, reason = intent_router.verify_credential(
            str(body.get("intent_token") or ""), question
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="请先通过意图确认再开始研究（" + reason + "）",
            )
        # ---- F2 concurrency guards: single-flight + double-click protection
        topic_hash = hashlib.sha256(
            intent_router._normalized_topic(question).encode("utf-8")
        ).hexdigest()[:24]
        registry = _load_task_registry()
        active_run_id = None
        duplicate_run_id = None
        now_ts = time.time()
        for task_id, info in list(registry["tasks"].items()):
            if info.get("status") != "running":
                continue
            proc = _TASK_PROCESSES.get(task_id)
            alive = (
                proc is not None and proc.poll() is None
            ) or _pid_alive(info.get("pid"))
            if not alive:
                info["status"] = "orphan"  # server restarted; pid is gone
                continue
            active_run_id = active_run_id or task_id
            if (
                info.get("topic_hash") == topic_hash
                and now_ts - float(info.get("created_ts") or 0)
                <= _IDEMPOTENCY_WINDOW_SECONDS
            ):
                duplicate_run_id = task_id
        if duplicate_run_id is not None:
            # double-click: same topic inside the window rejoins the same run
            return JSONResponse(
                status_code=200,
                content={
                    "run_id": duplicate_run_id,
                    "status": "starting",
                    "duplicate": True,
                    "question": question,
                },
            )
        if active_run_id is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "已有正在运行的研究任务：请等它结束，或先停止它。",
                    "existing_run_id": active_run_id,
                },
            )
        # ---- F5/F6 hard preflight gate ----
        # The onboarding page's check was a FRONTEND-only speed bump: deep
        # links (/run/<id>) and a direct POST both bypassed it, so a judge
        # could start a paid run with no API key and hit an opaque traceback
        # 20 minutes in. Blocking items are re-verified HERE, at the last
        # point before the irreversible spawn. Read-only: key contents are
        # never read (see preflight._check_api_key).
        blocking = preflight_blocking_failures(preflight_check_all())
        if blocking:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "环境自检未通过，研究任务未启动（未产生任何费用）。",
                    "preflight_failed": [item.to_dict() for item in blocking],
                },
            )
        run_id = "rhr_" + uuid.uuid4().hex[:8]
        root = Path(app.state.run_root).resolve()
        run_dir_path = root / run_id
        run_dir_path.mkdir(parents=True, exist_ok=False)
        command = _task_command(root, run_id, question)
        log_path = run_dir_path / "UI_TASK_STDOUT.log"
        log_handle = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
        except Exception:
            log_handle.close()
            try:
                log_path.unlink()
            except OSError:
                pass
            raise HTTPException(status_code=500, detail="无法启动研究任务")
        with _PROCESS_LOCK:
            _TASK_PROCESSES[run_id] = process
        registry["tasks"][run_id] = {
            "pid": process.pid,
            "status": "running",
            "topic_hash": topic_hash,
            "created_ts": now_ts,
            "cmdline_fingerprint": hashlib.sha256(
                " ".join(command).encode("utf-8")
            ).hexdigest()[:16],
        }
        _save_task_registry(registry)
        log_handle.close()
        return {"run_id": run_id, "status": "starting", "question": question, "log_path": str(log_path)}

    @app.post("/api/tasks/{run_id}/stop")
    def stop_task(run_id: str) -> Dict[str, Any]:
        """Graceful stop for a UI-launched harness task (F2).

        terminate() first, 10 s grace, then kill(). The terminal state is
        recorded in the on-disk task registry as stopped_by_user; the run
        directory itself is never written.
        """

        run_id = _safe_run_id(run_id)
        with _PROCESS_LOCK:
            proc = _TASK_PROCESSES.pop(run_id, None)
        terminated_live = False
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_STOP_GRACE_SECONDS)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            terminated_live = True
        registry = _load_task_registry()
        info = registry["tasks"].get(run_id)
        if info is None and proc is None:
            raise HTTPException(status_code=404, detail="unknown or finished task")
        if info is None:
            # Tracked in memory only (pre-restart launch): record it now so
            # the stop intent survives on disk too.
            info = {
                "pid": getattr(proc, "pid", 0) if proc is not None else 0,
                "status": "running",
                "created_ts": time.time(),
            }
            registry["tasks"][run_id] = info
        if not terminated_live and _pid_alive(info.get("pid")):
            _terminate_foreign_pid(info.get("pid"))
        info["status"] = "stopped_by_user"
        info["stopped_at"] = time.time()
        _save_task_registry(registry)
        return {
            "ok": True,
            "run_id": run_id,
            "status": "stopped_by_user",
            "terminated_live_process": terminated_live,
        }

    @app.get("/api/tasks/{run_id}/progress")
    def task_progress(run_id: str) -> Dict[str, Any]:
        run_id = _safe_run_id(run_id)
        directory = run_dir(run_id)
        progress = _progress_snapshot(directory)
        progress["question"] = _user_question(directory)
        # F1: bounded narrative projection alongside the raw snapshot.
        narrative = build_narrative(directory)
        progress["headline"] = narrative["headline"]
        progress["detail"] = narrative["detail"]
        progress["metrics"] = narrative["metrics"]
        progress["lines"] = narrative["lines"]
        with _PROCESS_LOCK:
            process = _TASK_PROCESSES.get(run_id)
        if process is not None and process.poll() is not None:
            progress["process_exit_code"] = process.returncode
        return progress

    _TERMINAL_STREAM_STATUSES = {"completed", "failed", "degraded"}
    _STREAM_REPLAY_LINES = 50
    _STREAM_POLL_SECONDS = 0.5
    _STREAM_HEARTBEAT_SECONDS = 15.0
    _MAX_PUSH_BYTES = 64 * 1024

    @app.get("/api/tasks/{run_id}/stream")
    async def task_stream(run_id: str, request: Request) -> EventSourceResponse:
        """SSE tail of the run's events JSONL (F2).

        Replays the newest complete lines once, then pushes only newly
        appended bytes, every row passing the narrator whitelist +
        truncation first. Sends a comment heartbeat while idle and a
        terminal event when the run reaches a terminal state, then ends
        the stream instead of hanging forever.
        """

        run_id = _safe_run_id(run_id)
        directory = run_dir(run_id)
        primary = directory / "HARNESS_EVENTS.jsonl"
        legacy = directory / "EVENTS.jsonl"  # legacy runs fallback, P3-1 order
        if primary.is_file() or not legacy.is_file():
            events_path = primary
        else:
            events_path = legacy

        async def event_gen():
            # NOTE: client disconnection is handled by sse-starlette's
            # cancellation propagation -- an explicit
            # "await request.is_disconnected()" here would deadlock the loop
            # whenever the transport has no pending receive event.
            # --- one-time replay of the newest complete lines ---
            replay_rows: List[Dict[str, Any]] = []
            try:
                if events_path.is_file():
                    total_lines = len(_jsonl_line_offsets(events_path))
                    start = max(0, total_lines - _STREAM_REPLAY_LINES)
                    page = _read_jsonl_page(events_path, start, _STREAM_REPLAY_LINES)
                    replay_rows = [
                        row for row in page.get("events", []) if isinstance(row, dict)
                    ]
            except OSError:
                replay_rows = []
            for row in replay_rows:
                payload = json.dumps(project_line(row), ensure_ascii=False)
                yield {"event": "log", "data": payload}
            offset = 0
            try:
                offset = events_path.stat().st_size
            except OSError:
                offset = 0
            last_activity = time.monotonic()
            while True:
                sent_something = False
                try:
                    stat_result = events_path.stat()
                except OSError:
                    stat_result = None
                if stat_result is not None and stat_result.st_size > offset:
                    try:
                        with events_path.open("rb") as handle:
                            handle.seek(offset)
                            chunk = handle.read(stat_result.st_size - offset)
                    except OSError:
                        chunk = b""
                    offset += len(chunk)
                    lines = chunk.split(b"\n")
                    if chunk and not chunk.endswith(b"\n"):
                        tail = lines.pop() if lines else b""
                        offset -= len(tail)  # partial line: retry after more bytes
                    for raw in lines:
                        if not raw.strip():
                            continue
                        try:
                            row = json.loads(raw.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if not isinstance(row, dict):
                            continue
                        payload = json.dumps(project_line(row), ensure_ascii=False)
                        if len(payload.encode("utf-8")) >= _MAX_PUSH_BYTES:
                            continue  # belt-and-braces; project_line already clips
                        sent_something = True
                        yield {"event": "log", "data": payload}
                state, _stale = _read_state_cached(directory)
                status = str((state or {}).get("status") or "")
                tracked_exited = False
                with _PROCESS_LOCK:
                    proc = _TASK_PROCESSES.get(run_id)
                    if proc is not None and proc.poll() is not None:
                        tracked_exited = True
                terminal = status in _TERMINAL_STREAM_STATUSES or (
                    proc is not None and tracked_exited and not status
                )
                if terminal:
                    done = json.dumps(
                        {"terminal": True, "status": status or "process_exited"},
                        ensure_ascii=False,
                    )
                    yield {"event": "done", "data": done}
                    break
                now = time.monotonic()
                if now - last_activity >= _STREAM_HEARTBEAT_SECONDS:
                    last_activity = now
                    yield {"comment": "ping"}
                if sent_something:
                    last_activity = now
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return EventSourceResponse(event_gen())

    @app.api_route("/api/intent", methods=["GET", "POST"])
    async def api_intent(request: Request) -> Dict[str, Any]:
        """HTTP forwarding ONLY -- every model call lives in intent_router."""

        question = ""
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                question = str(body.get("question") or "")
        else:
            question = str(request.query_params.get("question") or "")
        return await intent_router.classify(question)

    @app.get("/api/tasks/{run_id}/log")
    def task_log(run_id: str, tail: int = 120) -> Dict[str, Any]:
        run_id = _safe_run_id(run_id)
        directory = run_dir(run_id)
        # F1: the narrative projection over HARNESS_EVENTS.jsonl is the
        # PRIMARY log feed now (works for every run, UI-launched or not);
        # UI_TASK_STDOUT.log stays available as a secondary raw outlet.
        projection = build_narrative(directory)
        entries = list(projection.get("lines") or [])
        if int(tail) > 0:
            entries = entries[-max(20, min(int(tail), 300)):]
        lines: List[str] = [
            (f"{entry.get('ts', '')} {entry.get('text', '')}".strip())
            for entry in entries
        ]
        stdout_tail: List[str] = []
        stdout_path = directory / "UI_TASK_STDOUT.log"
        if stdout_path.is_file():
            try:
                stdout_tail = stdout_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-max(20, min(int(tail), 300)):]
            except OSError:
                stdout_tail = []
        return {
            "run_id": run_id,
            "source": "narrative",
            "lines": lines,
            "entries": entries,
            "headline": projection["headline"],
            "detail": projection["detail"],
            "stdout_tail": stdout_tail,
        }

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> Dict[str, Any]:
        directory = run_dir(run_id)
        state = _read_json(directory / "HARNESS_STATE.json") or {}
        cost = _read_json(directory / "HARNESS_COST.json") or {}
        gate_report = _read_json(directory / "DELIVERY_GATE.json") or {}
        stages = state.get("stages") or {}
        timeline = [
            {
                "stage": stage_name,
                "label": stage_label(str(stage_name)),
                "wall_time_seconds": (stage_value or {}).get(
                    "wall_time_seconds"
                ),
            }
            for stage_name, stage_value in sorted(stages.items())
        ]
        canonical = cost.get("canonical_totals") or {}
        narrative = build_narrative(directory)
        run_status = str(state.get("status") or "unknown")
        run_current_stage = str(state.get("current_stage") or "")
        return {
            "run_id": run_id,
            "status": state.get("status", "unknown"),
            "status_label": status_label(run_status),
            "current_stage": state.get("current_stage", ""),
            "current_stage_label": stage_label(run_current_stage),
            "error_count": state.get("error_count", 0),
            "timeline": timeline,
            "cost_cny": cost.get("cost_cny"),
            "model_call_count": cost.get("model_call_count"),
            "cost_by_stage": canonical.get("stages") or {},
            "headline": narrative["headline"],
            "detail": narrative["detail"],
            "metrics": narrative["metrics"],
            "lines": narrative["lines"],
            "delivery_gate": {
                "status": gate_report.get("status", "missing"),
                "passed": bool(gate_report.get("passed")),
                "blocking_checks": gate_report.get("blocking_checks") or [],
                "awaiting_human_checks": (
                    gate_report.get("awaiting_human_checks") or []
                ),
            },
        }


    @app.get("/api/preflight")
    def api_preflight() -> Dict[str, Any]:
        """F5 doctor endpoint for the F6 preflight panel (read-only)."""

        results = [item.to_dict() for item in preflight_check_all()]
        blocking_missing = [
            item for item in results if item["blocking"] and item["status"] != "ok"
        ]
        return {
            "checks": results,
            "ready": not blocking_missing,
            "blocking_missing": [item["key"] for item in blocking_missing],
        }

    @app.get("/api/runs/{run_id}/narrative")
    def run_narrative(run_id: str) -> Dict[str, Any]:
        """F4 addition: expose F1 narrator.build() verbatim.

        Pure read-only disk aggregation, zero harness interaction; added so
        the SPA metric cards consume the same single source of truth as the
        SSE lines without touching any F1/F2 endpoint behaviour.
        """

        directory = run_dir(_safe_run_id(run_id))
        payload = build_narrative(directory)
        payload["run_id"] = _safe_run_id(run_id)
        return payload

    @app.get("/api/runs/{run_id}/events")
    def run_events(
        run_id: str,
        offset: int = 0,
        limit: int = 200,
    ) -> Dict[str, Any]:
        directory = run_dir(run_id)
        events_path = directory / "HARNESS_EVENTS.jsonl"
        if not events_path.is_file():
            events_path = directory / "EVENTS.jsonl"   # legacy runs fallback
        if not events_path.is_file():
            raise HTTPException(
                status_code=404,
                detail="no events file (expected HARNESS_EVENTS.jsonl)",
            )
        offset = max(0, int(offset))
        limit = min(max(1, int(limit)), 1000)
        page = _read_jsonl_page(events_path, offset, limit)
        page["run_id"] = run_id
        # F4 addition: annotate oversized rows so the SPA can offer the lazy
        # "expand raw JSON (N bytes)" affordance without loading them by
        # default. Pure metadata on top of the existing read behaviour.
        try:
            offsets = _jsonl_line_offsets(events_path)
            file_size = events_path.stat().st_size
            for index, row in enumerate(page.get("events") or []):
                line_no = offset + index
                if line_no >= len(offsets):
                    continue
                end = (
                    offsets[line_no + 1]
                    if line_no + 1 < len(offsets)
                    else file_size
                )
                size = max(0, end - offsets[line_no])
                if size > _GIANT_LINE_BYTES:
                    row["truncated"] = True
                    row["raw_bytes"] = size
        except OSError:
            pass
        return page

    @app.get("/api/runs/{run_id}/cost")
    def run_cost(run_id: str) -> Dict[str, Any]:
        directory = run_dir(run_id)
        cost = _read_json(directory / "HARNESS_COST.json")
        if cost is None:
            raise HTTPException(status_code=404, detail="no HARNESS_COST.json")
        cost.setdefault("run_id", run_id)
        return cost

    @app.get("/api/runs/{run_id}/deliverables")
    def run_deliverables(run_id: str) -> Dict[str, Any]:
        directory = run_dir(run_id)
        gate_report = _read_json(directory / "DELIVERY_GATE.json")
        if gate_report is None:
            raise HTTPException(
                status_code=404, detail="no DELIVERY_GATE.json"
            )
        checks = gate_report.get("checks") or {}
        deliverables = [
            {
                "name": name,
                "ok": bool((value or {}).get("ok", False)),
                "status": (value or {}).get("status", ""),
                "path": (value or {}).get("path", ""),
                "awaiting_human": bool(
                    (value or {}).get("awaiting_human", False)
                ),
            }
            for name, value in sorted(checks.items())
        ]
        return {
            "run_id": run_id,
            "gate_status": gate_report.get("status", "missing"),
            "passed": bool(gate_report.get("passed")),
            "deliverables": deliverables,
        }

    @app.get("/api/runs/{run_id}/visuals")
    def run_visuals(run_id: str) -> Dict[str, Any]:
        directory = run_dir(run_id)
        package_path = (
            directory / "visual_editor" / "final"
            / "FINAL_VISUAL_PACKAGE.json"
        )
        package = _read_json(package_path)
        if package is None:
            # Run never reached the visual stage: empty list is correct.
            return {
                "run_id": run_id,
                "available": False,
                "delivered_figures": [],
                "pending_review_figures": [],
                "blocked_opportunities": [],
                "deliberate_no_figure": [],
            }
        delivered: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        for figure in package.get("figures") or []:
            row = {
                "figure_id": figure.get("figure_id", ""),
                "section_id": figure.get("section_id", ""),
                "figure_type": figure.get("figure_type", ""),
                "caption_en": figure.get("caption_en", ""),
                "local_path": figure.get("local_path", ""),
            }
            if str(figure.get("generation_status") or "") == (
                "model_approved_human_pending"
            ):
                pending.append(row)
            else:
                delivered.append(row)
        blocked: List[Dict[str, Any]] = []
        deliberate: List[Dict[str, Any]] = []
        for opportunity in package.get(
            "unfilled_visual_opportunities"
        ) or []:
            reason = str(opportunity.get("reason") or "")
            target = (
                blocked if reason.startswith("generation_") else deliberate
            )
            target.append(
                {
                    "section_id": opportunity.get("section_id", ""),
                    "reason": reason,
                }
            )
        return {
            "run_id": run_id,
            "available": True,
            "delivered_figures": delivered,
            "pending_review_figures": pending,
            "blocked_opportunities": blocked,
            "deliberate_no_figure": deliberate,
        }


    @app.get("/api/runs/{run_id}/decisions")
    def run_decisions(run_id: str) -> Dict[str, Any]:
        directory = run_dir(run_id)
        pending_view: List[Dict[str, Any]] = []
        for payload in list_pending(directory):
            seconds = payload.get("auto_accept_after_seconds")
            created = payload.get("created_ts")
            due = (
                created + seconds
                if seconds is not None and created is not None
                else None
            )
            pending_view.append(
                {
                    "decision_id": payload.get("decision_id"),
                    "kind": payload.get("kind"),
                    "subject_id": payload.get("subject_id"),
                    "context": payload.get("context") or {},
                    "options": payload.get("options") or [],
                    "default_option": payload.get(
                        "requested_default_option"
                    ),
                    "created_ts": created,
                    "due_ts": due,
                }
            )
        history = decision_history(directory)
        return {
            "run_id": run_id,
            "pending": pending_view,
            "history": history,
        }

    @app.post("/api/runs/{run_id}/decisions/{decision_id}")
    def answer_decision(
        run_id: str,
        decision_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        directory = run_dir(run_id)
        chosen = str(body.get("chosen") or "").strip()
        actor = str(body.get("actor") or "").strip() or "human:local-ui"
        note = str(body.get("note") or "")
        if not chosen:
            raise HTTPException(
                status_code=400, detail="chosen must be a non-empty option"
            )
        try:
            state = decision_state(directory, decision_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail="unknown or already resolved decision",
            )
        if state.get("state") != "pending":
            raise HTTPException(
                status_code=409, detail="decision is already resolved"
            )
        try:
            resolve_decision(
                directory, decision_id, chosen, actor=actor, note=note,
            )
        except KeyError:
            raise HTTPException(
                status_code=409, detail="decision is already resolved"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "ok": True,
            "decision_id": decision_id,
            "chosen": chosen,
            "actor": actor,
        }

    return app


app = create_app()


if __name__ == "__main__":
    import argparse

    import uvicorn

    arg_parser = argparse.ArgumentParser(
        description="OptoMind local read-only UI (loopback only)"
    )
    arg_parser.add_argument("--port", type=int, default=8765)
    arg_parser.add_argument(
        "--run-root",
        type=Path,
        default=DEFAULT_RUN_ROOT,
    )
    args = arg_parser.parse_args()
    local_app = create_app(run_root=args.run_root)
    # Safety contract: loopback bind only.  Never change this host.
    uvicorn.run(local_app, host="127.0.0.1", port=args.port)
