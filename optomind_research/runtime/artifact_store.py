"""Artifact store — manages the per-task working directory with atomic writes."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "outputs" / "research_harness_runs"


def _replace_with_retry(
    temporary_path: str,
    destination: Path,
    *,
    attempts: int = 8,
) -> None:
    """Replace an artifact despite short-lived Windows reader/AV locks.

    Dashboards and progress monitors may briefly hold ``COST.json`` or a state
    file open.  A transient sharing violation must not trigger model/key
    recovery or repeat paid scientific work.
    """
    target = str(destination.resolve())
    for attempt in range(attempts):
        try:
            os.replace(temporary_path, target)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.025 * (2 ** attempt))


def task_work_dir(run_id: str, task_id: str, runs_root: Path | None = None) -> Path:
    root = runs_root or DEFAULT_RUNS_ROOT
    return root / run_id / "tasks" / task_id


def ensure_work_dir(run_id: str, task_id: str, runs_root: Path | None = None) -> Path:
    d = task_work_dir(run_id, task_id, runs_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to a temp file then rename atomically."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to a temp file then rename atomically."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, record: Any) -> None:
    """Append one JSON record as a line to a .jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
