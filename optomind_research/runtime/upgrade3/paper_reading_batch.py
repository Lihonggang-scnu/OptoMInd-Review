"""Resumable, bounded batch driver for standalone paper reading cards.

This module delegates one-paper validation, prompting, budgeting, raw response
retention, and COMMIT matching to paper_reading_card. It only schedules
already-prepared snapshots from a manifest and maintains an atomic checkpoint.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .paper_reading_card import (
    DEFAULT_CONCISE_OUTPUT,
    DEFAULT_INPUT_PROFILE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_THINKING,
    DEFAULT_THINKING_BUDGET,
    MODEL,
    preflight_card,
    run_paper_reading_card,
)
from .local_materials import PreparedSnapshotProvider


SCHEMA_VERSION = "optomind.paper_reading_batch.v1"
DEFAULT_INITIAL_WORKERS = 4
DEFAULT_MAX_WORKERS = 16
DEFAULT_BUDGET_LIMIT_CNY = 25.0
MAX_ATTEMPTS_PER_PAPER = 2
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class PaperReadingBatchError(ValueError):
    """Deterministic batch input/state failure."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PaperReadingBatchError(f"input_file_unreadable:{path}") from exc


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _append_event(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _safe_slug(value: Any) -> str:
    text = str(value or "paper").strip()
    text = _SAFE_RE.sub("-", text).strip(".-")
    return (text or "paper")[:100]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperReadingBatchError(f"invalid_batch_json:{path}") from exc


def _manifest_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    payload = _read_json(path)
    if isinstance(payload, list):
        raw_records, finalized = payload, True
    elif isinstance(payload, Mapping):
        raw_records = payload.get("records") or payload.get("rows")
        if not isinstance(raw_records, list):
            raise PaperReadingBatchError("manifest_records_must_be_array")
        finalized = bool(payload.get("finalized", True))
    else:
        raise PaperReadingBatchError("manifest_object_or_records_array_required")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise PaperReadingBatchError(f"manifest_row_not_object:{index}")
        rows.append(dict(raw))
    return rows, finalized


def _normalise_row(raw: Mapping[str, Any], manifest_path: Path) -> dict[str, Any]:
    paper_id = str(raw.get("paper_id") or raw.get("canonical_paper_id") or raw.get("doi") or raw.get("title") or "").strip()
    if not paper_id:
        raise PaperReadingBatchError("manifest_paper_id_required")
    snapshot_value = raw.get("snapshot_dir") or raw.get("snapshot")
    plan_value = raw.get("plan_path") or raw.get("plan")
    if not snapshot_value or not plan_value:
        raise PaperReadingBatchError(f"manifest_snapshot_and_plan_required:{paper_id}")
    snapshot = Path(str(snapshot_value))
    plan = Path(str(plan_value))
    if not snapshot.is_absolute():
        snapshot = manifest_path.parent / snapshot
    if not plan.is_absolute():
        plan = manifest_path.parent / plan
    snapshot = snapshot.resolve()
    plan = plan.resolve()
    manifest_file = snapshot / "DOCUMENT_MANIFEST.json"
    row = {
        "paper_id": paper_id,
        "snapshot_dir": str(snapshot),
        "plan_path": str(plan),
        "title": str(raw.get("title") or ""),
        "manifest_sha256": str(raw.get("manifest_sha256") or raw.get("snapshot_manifest_sha256") or ""),
        "plan_sha256": str(raw.get("plan_sha256") or ""),
    }
    if manifest_file.is_file():
        actual_manifest_hash = _file_sha(manifest_file)
        if row["manifest_sha256"] and row["manifest_sha256"] != actual_manifest_hash:
            raise PaperReadingBatchError(f"snapshot_manifest_hash_mismatch:{paper_id}")
        row["manifest_sha256"] = actual_manifest_hash
    if plan.is_file():
        actual_plan_hash = _file_sha(plan)
        if row["plan_sha256"] and row["plan_sha256"] != actual_plan_hash:
            raise PaperReadingBatchError(f"plan_hash_mismatch:{paper_id}")
        row["plan_sha256"] = actual_plan_hash
    return row


def _snapshot_material_state(row: Mapping[str, Any]) -> str:
    """Return a local-only readiness classification for a prepared snapshot.

    A valid metadata-only snapshot is deliberately separated from a snapshot
    that is still being published.  The former is terminal for this batch and
    must never reach the model; the latter remains eligible for a later
    manifest refresh.
    """

    snapshot_dir = Path(str(row.get("snapshot_dir") or ""))
    plan_path = Path(str(row.get("plan_path") or ""))
    if not snapshot_dir.is_dir() or not plan_path.is_file():
        return "waiting_material"
    try:
        snapshot = PreparedSnapshotProvider(snapshot_dir).load()
        packet = snapshot.build_reading_packet()
    except Exception:
        # Acquisition publishes files atomically.  Treat an incomplete or
        # currently-invalid snapshot as pending so a later refresh can retry.
        return "waiting_material"
    observations = packet.get("observations") or ()
    if packet.get("material_scope") == "metadata_only" or not any(
        isinstance(item, Mapping) and str(item.get("text") or "").strip()
        for item in observations
    ):
        return "insufficient_material"
    return "ready"


def _work_key(row: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    identity = {
        "paper_id": row.get("paper_id"),
        "snapshot_dir": row.get("snapshot_dir"),
        "manifest_sha256": row.get("manifest_sha256"),
        "plan_path": row.get("plan_path"),
        "plan_sha256": row.get("plan_sha256"),
        "input_profile": config.get("input_profile"),
        "model": config.get("model"),
        "thinking": config.get("thinking"),
        "thinking_budget": config.get("thinking_budget"),
        "concise_output": config.get("concise_output"),
    }
    return _sha(identity)[:32]


def _runtime_config(
    *,
    mode: str,
    input_profile: str,
    thinking: bool,
    thinking_budget: int,
    concise_output: bool,
    max_output_tokens: int,
    timeout_seconds: float,
    initial_workers: int,
    max_workers: int,
    retry_delay_seconds: float,
    budget_limit_cny: float | None,
    budget_ledger_path: str | Path | None,
) -> dict[str, Any]:
    effective_budget = int(thinking_budget) if thinking else 0
    return {
        "mode": mode,
        "model": MODEL,
        "input_profile": input_profile,
        "thinking": bool(thinking),
        "thinking_budget": effective_budget,
        "concise_output": bool(concise_output),
        "max_output_tokens": max(64, int(max_output_tokens)),
        "timeout_seconds": float(timeout_seconds),
        "max_retries": 0,
        "initial_workers": int(initial_workers),
        "max_workers": int(max_workers),
        "retry_delay_seconds": float(retry_delay_seconds),
        "budget_limit_cny": float(budget_limit_cny) if budget_limit_cny is not None else None,
        "budget_ledger_path": str(budget_ledger_path or ""),
    }


def _validate_config(config: Mapping[str, Any], *, client_factory: Callable[..., Any] | None) -> None:
    if config["mode"] not in {"preflight", "run"}:
        raise PaperReadingBatchError("batch_mode_must_be_preflight_or_run")
    if int(config["initial_workers"]) < 1 or int(config["max_workers"]) < int(config["initial_workers"]):
        raise PaperReadingBatchError("invalid_worker_limits")
    # 16 is the conservative default.  An operator may raise it for a live
    # material-ready batch, while the queue remains bounded by this value.
    if int(config["max_workers"]) > 64:
        raise PaperReadingBatchError("max_workers_exceeds_64")
    if float(config["retry_delay_seconds"]) < 0:
        raise PaperReadingBatchError("retry_delay_must_be_nonnegative")
    if config["mode"] == "run" and client_factory is None:
        if not config.get("key_file"):
            raise PaperReadingBatchError("run_key_file_required")
        if not config.get("budget_ledger_path"):
            raise PaperReadingBatchError("run_budget_ledger_required")
        cap = config.get("budget_limit_cny")
        if cap is None or not math.isfinite(float(cap)) or float(cap) <= 0:
            raise PaperReadingBatchError("run_finite_positive_budget_required")


def _transient_failure(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    record = getattr(exc, "record", {})
    if status is None and isinstance(record, Mapping):
        status = record.get("status_code")
    if status in _TRANSIENT_STATUS or bool(getattr(exc, "transient", False)):
        return True
    text = str(exc).casefold()
    return "timeout" in text or "timed out" in text


def _budget_failure(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return "budget" in text and ("exceed" in text or "reserve" in text or "limit" in text)


def _budget_has_room(config: Mapping[str, Any]) -> bool:
    """Check durable spend plus open/uncertain reservations after a drain."""

    path = config.get("budget_ledger_path")
    limit = config.get("budget_limit_cny")
    if not path or limit is None:
        return False
    try:
        from .module4.runtime import GlobalBudgetLedger

        summary = GlobalBudgetLedger(limit_cny=float(limit), path=path).as_dict()
    except Exception:
        return False
    used = float(summary.get("actual_cny") or 0.0) + float(summary.get("reserved_cny") or 0.0)
    return used + 1e-9 < float(summary.get("limit_cny") or limit)


def _initialize_budget_ledger(config: Mapping[str, Any]) -> None:
    """Create/validate the shared ledger once before worker threads start."""

    if config.get("mode") != "run" or not config.get("budget_ledger_path"):
        return
    try:
        from .module4.runtime import GlobalBudgetLedger

        GlobalBudgetLedger(limit_cny=config.get("budget_limit_cny"), path=config["budget_ledger_path"])
    except Exception as exc:
        raise PaperReadingBatchError(f"budget_ledger_initialization_failed:{type(exc).__name__}") from exc


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = _read_json(path)
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        raise PaperReadingBatchError("batch_state_schema_mismatch")
    return dict(payload)


def _attempt_dirs(base: Path) -> list[Path]:
    return sorted((item for item in base.glob("attempt-*") if item.is_dir()), key=lambda item: item.name)


def _next_attempt(base: Path) -> int:
    values = []
    for path in _attempt_dirs(base):
        try:
            values.append(int(path.name.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max(values, default=0) + 1


def _call_one(
    item: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    output_root: Path,
    client_factory: Callable[..., Any] | None,
) -> dict[str, Any]:
    work_key = str(item["work_key"])
    base = output_root / "cards" / _safe_slug(item["paper_id"]) / work_key
    base.mkdir(parents=True, exist_ok=True)
    if config["mode"] == "preflight":
        receipt = preflight_card(
            snapshot_dir=item["snapshot_dir"],
            plan_path=item["plan_path"],
            input_profile=config["input_profile"],
            thinking=bool(config["thinking"]),
            concise_output=bool(config["concise_output"]),
            max_output_tokens=int(config["max_output_tokens"]),
            thinking_budget=int(config["thinking_budget"]),
            timeout_seconds=float(config["timeout_seconds"]),
            max_retries=0,
        )
        return {"ok": True, "preflight": receipt, "attempt": 0, "output_dir": "", "reused": False}

    for attempt_dir in _attempt_dirs(base):
        if not ((attempt_dir / "COMMIT.json").is_file() and (attempt_dir / "PAPER_READING_CARD.json").is_file()):
            continue
        try:
            attempt = int(attempt_dir.name.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        try:
            client = client_factory(item, attempt) if client_factory else None
            result = run_paper_reading_card(
                snapshot_dir=item["snapshot_dir"],
                plan_path=item["plan_path"],
                output_dir=attempt_dir,
                key_file=config.get("key_file") or None,
                budget_ledger_path=config.get("budget_ledger_path") or None,
                budget_limit_cny=config.get("budget_limit_cny"),
                client=client,
                input_profile=config["input_profile"],
                thinking=bool(config["thinking"]),
                concise_output=bool(config["concise_output"]),
                max_output_tokens=int(config["max_output_tokens"]),
                thinking_budget=int(config["thinking_budget"]),
                timeout_seconds=float(config["timeout_seconds"]),
                max_retries=0,
            )
            return {"ok": True, "result": result, "attempt": attempt, "output_dir": str(attempt_dir), "reused": bool(result.get("reused"))}
        except Exception:
            continue

    attempt = _next_attempt(base)
    if attempt > MAX_ATTEMPTS_PER_PAPER:
        return {"ok": False, "attempt": attempt - 1, "output_dir": str(base / f"attempt-{attempt - 1:02d}"), "error_type": "attempt_limit_exhausted", "error": "attempt_limit_exhausted", "retryable": False, "budget_failure": False}
    attempt_dir = base / f"attempt-{attempt:02d}"
    try:
        client = client_factory(item, attempt) if client_factory else None
        result = run_paper_reading_card(
            snapshot_dir=item["snapshot_dir"],
            plan_path=item["plan_path"],
            output_dir=attempt_dir,
            key_file=config.get("key_file") or None,
            budget_ledger_path=config.get("budget_ledger_path") or None,
            budget_limit_cny=config.get("budget_limit_cny"),
            client=client,
            input_profile=config["input_profile"],
            thinking=bool(config["thinking"]),
            concise_output=bool(config["concise_output"]),
            max_output_tokens=int(config["max_output_tokens"]),
            thinking_budget=int(config["thinking_budget"]),
            timeout_seconds=float(config["timeout_seconds"]),
            max_retries=0,
        )
        return {"ok": True, "result": result, "attempt": attempt, "output_dir": str(attempt_dir), "reused": bool(result.get("reused"))}
    except Exception as exc:
        return {"ok": False, "attempt": attempt, "output_dir": str(attempt_dir), "error_type": type(exc).__name__, "error": str(exc), "retryable": _transient_failure(exc), "budget_failure": _budget_failure(exc)}


def _refresh_items(*, manifest_path: Path, state: dict[str, Any], config: Mapping[str, Any]) -> tuple[bool, list[str]]:
    raw_rows, finalized = _manifest_records(manifest_path)
    known = state.setdefault("items", {})
    duplicates = state.setdefault("duplicates", [])
    keys_seen: set[str] = set()
    newly_pending: list[str] = []
    for raw in raw_rows:
        try:
            row = _normalise_row(raw, manifest_path)
            key = _work_key(row, config)
        except PaperReadingBatchError as exc:
            key = _sha({"raw": raw, "error": str(exc)})[:32]
            if key not in known:
                known[key] = {"work_key": key, "paper_id": str(raw.get("paper_id") or ""), "row": dict(raw), "status": "invalid", "error": str(exc), "attempts": []}
            continue
        if key in keys_seen:
            duplicate = {"work_key": key, "paper_id": row["paper_id"], "reason": "duplicate_manifest_row"}
            if duplicate not in duplicates:
                duplicates.append(duplicate)
            continue
        keys_seen.add(key)
        if key not in known:
            known[key] = {"work_key": key, **row, "status": "pending", "attempts": []}
            material_state = _snapshot_material_state(known[key])
            known[key]["material_state"] = material_state
            known[key]["material_check_work_key"] = key
            if material_state == "insufficient_material":
                known[key]["status"] = "insufficient_material"
                known[key]["error"] = "no_abstract_body_or_meaningful_fragment"
            elif material_state == "waiting_material":
                known[key]["status"] = "waiting_material"
            else:
                newly_pending.append(key)
        else:
            item = known[key]
            item.update({name: row[name] for name in ("paper_id", "snapshot_dir", "plan_path", "title", "manifest_sha256", "plan_sha256")})
            if item.get("status") in {"waiting_material", "pending"}:
                cached_ready = item.get("material_state") == "ready" and item.get("material_check_work_key") == key and item.get("status") == "pending"
                material_state = "ready" if cached_ready else _snapshot_material_state(item)
                item["material_state"] = material_state
                item["material_check_work_key"] = key
                if material_state == "insufficient_material":
                    item["status"] = "insufficient_material"
                    item["error"] = "no_abstract_body_or_meaningful_fragment"
                elif material_state == "ready" and item.get("status") == "waiting_material":
                    item["status"] = "pending"
                    newly_pending.append(key)
    if finalized:
        for item in known.values():
            if item.get("status") == "waiting_material":
                item["status"] = "invalid"
                item["error"] = "snapshot_or_plan_not_ready_when_manifest_finalized"
    state["manifest_finalized"] = bool(finalized)
    state["manifest_rows_seen"] = len(raw_rows)
    return bool(finalized), newly_pending


def _summary_counts(items: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items.values():
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _card_from_result(output_dir: str) -> dict[str, Any] | None:
    path = Path(output_dir) / "PAPER_READING_CARD.json"
    if not path.is_file():
        return None
    try:
        value = _read_json(path)
    except PaperReadingBatchError:
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _write_aggregates(output_root: Path, state: Mapping[str, Any]) -> None:
    card_rows: list[dict[str, Any]] = []
    planning_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for item in sorted((state.get("items") or {}).values(), key=lambda row: (str(row.get("paper_id")), str(row.get("work_key")))):
        if item.get("status") not in {"success", "reused"}:
            continue
        output_dir = str(item.get("output_dir") or "")
        card = _card_from_result(output_dir)
        if not card:
            continue
        base = {"work_key": item.get("work_key"), "paper_id": item.get("paper_id"), "output_dir": output_dir, "attempt": item.get("attempt")}
        card_rows.append({**base, "card": card})
        planning_rows.append({**base, "material_scope": (card.get("material") or {}).get("material_scope"), "declared_content_depth": (card.get("material") or {}).get("declared_content_depth"), "paper_identity": card.get("paper_identity"), "planning_view": card.get("planning_view"), "review_planning": card.get("review_planning")})
        summaries.append({**base, "card_id": card.get("card_id"), "paper_identity": card.get("paper_identity"), "material": card.get("material"), "status": item.get("status")})
    _write_jsonl(output_root / "BATCH_CARDS_INDEX.jsonl", card_rows)
    _write_jsonl(output_root / "BATCH_PLANNING_VIEWS.jsonl", planning_rows)
    _atomic_json(output_root / "BATCH_RESULTS.json", {"schema_version": SCHEMA_VERSION, "counts": _summary_counts(state.get("items") or {}), "cards": summaries, "cards_index": "BATCH_CARDS_INDEX.jsonl", "planning_views": "BATCH_PLANNING_VIEWS.jsonl", "manifest_finalized": bool(state.get("manifest_finalized")), "stop_reason": state.get("stop_reason", "")})


def run_paper_reading_batch(
    *,
    manifest_path: str | Path,
    output_root: str | Path,
    mode: str = "preflight",
    input_profile: str = DEFAULT_INPUT_PROFILE,
    thinking: bool = DEFAULT_THINKING,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    concise_output: bool = DEFAULT_CONCISE_OUTPUT,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout_seconds: float = 300.0,
    key_file: str | Path | None = None,
    budget_ledger_path: str | Path | None = None,
    budget_limit_cny: float | None = DEFAULT_BUDGET_LIMIT_CNY,
    initial_workers: int = DEFAULT_INITIAL_WORKERS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    retry_delay_seconds: float = 1.0,
    poll_seconds: float = 5.0,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run or preflight a resumable batch; live calls require mode='run'."""

    manifest = Path(manifest_path).resolve()
    output = Path(output_root).resolve()
    if not manifest.is_file():
        raise PaperReadingBatchError("manifest_file_required")
    config = _runtime_config(mode=mode, input_profile=input_profile, thinking=thinking, thinking_budget=thinking_budget, concise_output=concise_output, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds, initial_workers=initial_workers, max_workers=max_workers, retry_delay_seconds=retry_delay_seconds, budget_limit_cny=budget_limit_cny, budget_ledger_path=budget_ledger_path)
    config["key_file"] = str(key_file or "")
    _validate_config(config, client_factory=client_factory)
    output.mkdir(parents=True, exist_ok=True)
    _initialize_budget_ledger(config)
    state_path = output / "BATCH_STATE.json"
    events_path = output / "BATCH_EVENTS.jsonl"
    state = _load_state(state_path)
    if state is not None:
        if str(state.get("manifest_path") or "") != str(manifest):
            raise PaperReadingBatchError("batch_manifest_mismatch_use_new_output_root")
        previous_config = dict(state.get("config") or {})
        if previous_config != config:
            # A no-network preflight is intentionally promotable to the same
            # live batch once its operator supplies the key and ledger.
            comparable_previous = dict(previous_config)
            comparable_current = dict(config)
            for name in (
                "mode",
                "key_file",
                "budget_limit_cny",
                "budget_ledger_path",
                "initial_workers",
                "max_workers",
                "retry_delay_seconds",
            ):
                comparable_previous.pop(name, None)
                comparable_current.pop(name, None)
            if previous_config.get("mode") != "preflight" or config.get("mode") != "run" or comparable_previous != comparable_current:
                raise PaperReadingBatchError("batch_config_mismatch_use_new_output_root")
            state["config"] = config
            for item in (state.get("items") or {}).values():
                if item.get("status") == "preflight":
                    item["status"] = "pending"
        for item in (state.get("items") or {}).values():
            if item.get("status") == "running":
                item["status"] = "pending"
    else:
        state = {"schema_version": SCHEMA_VERSION, "manifest_path": str(manifest), "config": config, "items": {}, "duplicates": [], "target_workers": int(initial_workers), "success_streak": 0, "max_observed_inflight": 0, "stop_reason": ""}
    finalized, newly_pending = _refresh_items(manifest_path=manifest, state=state, config=config)
    pending = deque(newly_pending)
    pending_set = set(pending)
    for key, item in (state.get("items") or {}).items():
        if item.get("status") == "pending" and key not in pending_set:
            pending.append(key)
            pending_set.add(key)
    _atomic_json(state_path, state)
    _append_event(events_path, {"event": "batch_started", "mode": mode, "manifest": str(manifest), "finalized": finalized, "items": len(state["items"])})

    target_workers = int(state.get("target_workers") or initial_workers)
    success_streak = int(state.get("success_streak") or 0)
    in_flight: dict[concurrent.futures.Future[Any], str] = {}
    halt_dispatch = False

    def enqueue_ready(executor: concurrent.futures.Executor) -> None:
        while not halt_dispatch and pending and len(in_flight) < target_workers:
            key = pending.popleft()
            pending_set.discard(key)
            item = state["items"].get(key)
            if not item or item.get("status") not in {"pending", "waiting_material"}:
                continue
            if not Path(str(item.get("snapshot_dir") or "")).is_dir() or not Path(str(item.get("plan_path") or "")).is_file():
                if finalized:
                    item["status"] = "invalid"
                    item["error"] = "snapshot_or_plan_missing"
                else:
                    item["status"] = "waiting_material"
                continue
            item["status"] = "running"
            future = executor.submit(_call_one, item, config=config, output_root=output, client_factory=client_factory)
            in_flight[future] = key
        state["target_workers"] = target_workers
        state["success_streak"] = success_streak
        state["max_observed_inflight"] = max(int(state.get("max_observed_inflight") or 0), len(in_flight))
        _atomic_json(state_path, state)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            finalized, newly_pending = _refresh_items(manifest_path=manifest, state=state, config=config)
            for key in newly_pending:
                if key not in pending_set and key not in in_flight and state["items"].get(key, {}).get("status") == "pending":
                    pending.append(key)
                    pending_set.add(key)
            enqueue_ready(executor)
            if in_flight:
                done, _ = concurrent.futures.wait(tuple(in_flight), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    key = in_flight.pop(future)
                    item = state["items"][key]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {"ok": False, "attempt": 0, "output_dir": "", "error_type": type(exc).__name__, "error": str(exc), "retryable": _transient_failure(exc), "budget_failure": _budget_failure(exc)}
                    if outcome.get("ok"):
                        item["status"] = "preflight" if mode == "preflight" else ("reused" if outcome.get("reused") else "success")
                        item["output_dir"] = outcome.get("output_dir", "")
                        item["attempt"] = outcome.get("attempt", 0)
                        item["preflight"] = outcome.get("preflight")
                        item["last_error"] = ""
                        success_streak += 1
                        if mode == "run" and success_streak >= 20 and target_workers < max_workers:
                            target_workers = min(max_workers, max(1, target_workers * 2))
                            success_streak = 0
                        _append_event(events_path, {"event": "paper_complete", "work_key": key, "paper_id": item.get("paper_id"), "status": item["status"], "attempt": item.get("attempt", 0), "target_workers": target_workers})
                    else:
                        attempt = int(outcome.get("attempt") or 0)
                        item.setdefault("attempts", []).append({"attempt": attempt, "output_dir": outcome.get("output_dir", ""), "status": "failed", "error_type": outcome.get("error_type", ""), "error": outcome.get("error", ""), "retryable": bool(outcome.get("retryable")), "budget_failure": bool(outcome.get("budget_failure"))})
                        item["output_dir"] = outcome.get("output_dir", "")
                        item["attempt"] = attempt
                        item["last_error"] = outcome.get("error", "")
                        item["status"] = "failed"
                        success_streak = 0
                        if outcome.get("budget_failure"):
                            halt_dispatch = True
                            state["stop_reason"] = "global_budget_exceeded_pending_drain"
                            if attempt < MAX_ATTEMPTS_PER_PAPER and key not in pending_set:
                                item["status"] = "pending"
                                pending.append(key)
                                pending_set.add(key)
                        elif attempt < MAX_ATTEMPTS_PER_PAPER:
                            if outcome.get("retryable"):
                                target_workers = max(1, target_workers // 2)
                                if retry_delay_seconds:
                                    time.sleep(float(retry_delay_seconds))
                            item["status"] = "pending"
                            if key not in pending_set:
                                pending.append(key)
                                pending_set.add(key)
                        _append_event(events_path, {"event": "paper_failed", "work_key": key, "paper_id": item.get("paper_id"), "attempt": attempt, "retry_scheduled": item["status"] == "pending", "error_type": outcome.get("error_type", ""), "target_workers": target_workers})
                state["target_workers"] = target_workers
                state["success_streak"] = success_streak
                state["max_observed_inflight"] = max(int(state.get("max_observed_inflight") or 0), len(in_flight))
                _atomic_json(state_path, state)
                continue
            if halt_dispatch:
                if not in_flight and pending and _budget_has_room(config):
                    # Reservations from completed workers have now settled.
                    # Permit one bounded retry to distinguish a temporary
                    # reservation pressure failure from a truly exhausted cap.
                    halt_dispatch = False
                    target_workers = 1
                    state["stop_reason"] = "budget_drain_retry"
                    _append_event(events_path, {"event": "budget_drain_retry", "target_workers": target_workers})
                    _atomic_json(state_path, state)
                    continue
                for item in state["items"].values():
                    if item.get("status") in {"pending", "waiting_material"}:
                        item["status"] = "budget_blocked"
                state["stop_reason"] = "global_budget_exhausted"
                finalized = True
                state["manifest_finalized"] = True
                _atomic_json(state_path, state)
                break
            if finalized and not pending:
                state["stop_reason"] = state.get("stop_reason") or "finalized_complete"
                state["manifest_finalized"] = True
                _atomic_json(state_path, state)
                break
            if pending:
                continue
            time.sleep(max(0.0, float(poll_seconds)))

    _write_aggregates(output, state)
    _append_event(events_path, {"event": "batch_finished", "finalized": bool(state.get("manifest_finalized")), "counts": _summary_counts(state.get("items") or {}), "stop_reason": state.get("stop_reason", "")})
    return {"state": state, "output_root": str(output), "counts": _summary_counts(state.get("items") or {}), "cards_index": str(output / "BATCH_CARDS_INDEX.jsonl"), "planning_views": str(output / "BATCH_PLANNING_VIEWS.jsonl")}


__all__ = ["SCHEMA_VERSION", "DEFAULT_INITIAL_WORKERS", "DEFAULT_MAX_WORKERS", "DEFAULT_BUDGET_LIMIT_CNY", "MAX_ATTEMPTS_PER_PAPER", "PaperReadingBatchError", "run_paper_reading_batch"]
