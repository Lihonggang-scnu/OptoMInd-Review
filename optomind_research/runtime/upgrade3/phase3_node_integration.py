"""Phase-3 A/B/C node persistence integration.

This module is intentionally small and orchestration-agnostic.  It owns the
atomic file boundary around the four-node manifest primitives; it does not
compute claims, bindings, coverage, or handoff content.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .phase3_nodes import (
    NODE_IDS,
    commit_node,
    fail_node,
    mark_running,
    node_dir,
    load_and_validate_committed_node,
    stage_node,
)


ROOT_PROJECTIONS_SCHEMA = "optomind.upgrade3.phase3_root_projections.v1"
ROOT_PROJECTIONS_FILENAME = "ROOT_PROJECTIONS.json"
NODE_INPUT_SNAPSHOT_SCHEMA = "optomind.upgrade3.phase3_node_input_snapshot.v1"
ROOT_PROJECTION_LOCK_FILENAME = ".ROOT_PROJECTIONS.lock"
ROOT_PROJECTION_JOURNAL_FILENAME = "ROOT_PROJECTION_TRANSACTION.json"
ROOT_PROJECTION_FAILURE_FILENAME = "ROOT_PROJECTION_FAILURE.json"
ROOT_PROJECTION_RECOVERY_FILENAME = "ROOT_PROJECTION_RECOVERY.json"
ROOT_PROJECTION_ARCHIVE_JOURNAL_FILENAME = "ROOT_PROJECTION_ARCHIVE_TRANSACTION.json"
ROOT_PROJECTION_ARCHIVE_RECEIPT_FILENAME = "ARCHIVE_RECEIPT.json"
ROOT_PROJECTION_ARCHIVE_RECEIPT_ALIAS_FILENAME = "ROOT_PROJECTION_ARCHIVE_RECEIPT.json"
PHASE3_NEW_GENERATION_REQUIRED_FILENAME = "PHASE3_NEW_GENERATION_REQUIRED.json"
NO_LLM_CONTRACT_SCHEMA = "optomind.upgrade3.phase3_no_llm_contract.v1"


class Phase3NodeIntegrationError(RuntimeError):
    """Raised when a node artifact cannot be persisted or projected safely."""


class RootProjectionConflict(Phase3NodeIntegrationError):
    """Raised when a compatibility root file belongs to another manifest."""


class ProjectionError(Phase3NodeIntegrationError):
    """Raised after a committed node cannot be projected to compatibility root."""

    def __init__(
        self,
        message: str,
        *,
        manifest: Mapping[str, Any],
        receipt_path: Path | None = None,
    ) -> None:
        self.manifest = dict(manifest)
        self.receipt_path = receipt_path
        super().__init__(message)


class RootProjectionLockError(Phase3NodeIntegrationError):
    """Raised when the phase3-root projection CAS lock is not available."""


def phase3_root(output_dir: str | os.PathLike[str]) -> Path:
    value = Path(output_dir).expanduser().resolve(strict=False)
    return value if value.name == "phase3_argument_orchestration" else value / "phase3_argument_orchestration"


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Phase3NodeIntegrationError(
            f"node_payload_not_json:{type(exc).__name__}"
        ) from exc


NEWLINE = chr(10)


def atomic_write_json(path: str | os.PathLike[str], payload: Any) -> Path:
    """Write a node payload atomically.

    An object is written as canonical JSON.  A string is written verbatim (JSON
    Lines payloads are already canonical per row), followed by exactly one newline
    when it does not end with one, so the committed file bytes, the recorded
    sha256 and the byte count all describe the same artifact.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (str, bytes)):
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        if text and not text.endswith(NEWLINE):
            text = text + NEWLINE
        data = text.encode("utf-8")
    else:
        data = _canonical_json(payload)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target


def write_phase3_new_generation_required(
    output_dir: str | os.PathLike[str],
    payload: Mapping[str, Any],
) -> Path:
    """Write a scoped-material generation handoff outside the node root.

    A scoped KB revision invalidates the complete A-D generation.  This
    receipt is deliberately placed beside ``phase3_argument_orchestration``
    so recording the refusal cannot mutate any node, input snapshot, or root
    projection artifact from the previous generation.
    """

    root = phase3_root(output_dir)
    body = {
        "schema_version": "research_harness.phase3_new_generation_required.v1",
        "status": "new_generation_required",
        **dict(payload),
    }
    return atomic_write_json(
        root.parent / PHASE3_NEW_GENERATION_REQUIRED_FILENAME,
        body,
    )


write_new_generation_required_receipt = write_phase3_new_generation_required


def _root_lock_path(root: Path) -> Path:
    return root / ROOT_PROJECTION_LOCK_FILENAME


def _read_root_lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not value.get("pid") or not value.get("token") or not value.get("created_at"):
        return None
    return value


def _root_pid_is_alive(pid: Any) -> bool:
    """Return false only when the owner process is provably absent."""

    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return True
    if process_id <= 0 or process_id == os.getpid():
        return True
    if os.name == "nt":
        # ``os.kill(pid, 0)`` only validates the numeric PID on Windows and
        # can report success after a process has exited.  OpenProcess gives us
        # the stronger existence check required for safe stale-lock reclaim;
        # access denied remains ambiguous and therefore alive/fail-closed.
        try:
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                process_id,
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            if ctypes.get_last_error() == 87:
                return False
            return True
        except Exception:
            # Fall through to the portable probe when the native API is
            # unavailable (for example under a restricted test runtime).
            pass
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Windows reports a PID that no longer exists (and an out-of-range
        # PID) as WinError 87 / errno.EINVAL rather than ProcessLookupError.
        # Signal 0 is valid here, so that platform result is still proof that
        # this owner cannot be alive.  Other OS errors remain ambiguous.
        if getattr(exc, "winerror", None) == 87 or exc.errno == errno.ESRCH:
            return False
        # An indeterminate process state must remain fail-closed.
        return True
    return True


def _coerce_stale_timeout(value: Any) -> float | None:
    """Normalize an optional stale timeout without granting implicit reclaim."""

    if value is None:
        return None
    try:
        candidate = float(value)
    except (TypeError, ValueError) as exc:
        raise RootProjectionLockError(
            f"invalid_root_projection_stale_timeout:{value!r}"
        ) from exc
    return candidate if candidate > 0 else None


def _try_reclaim_stale_root_lock(
    root: Path,
    path: Path,
    *,
    existing: Mapping[str, Any] | None,
    stale_timeout: float | None,
) -> bool:
    """Atomically detach a provably stale lock, returning whether it won.

    This helper is intentionally conservative.  A malformed owner, an
    unknown/alive pid, an unexpired lock, or any filesystem ambiguity leaves
    the original lock in place and returns false.
    """

    if stale_timeout is None or existing is None:
        return False
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return False
    if age < stale_timeout or _root_pid_is_alive(existing.get("pid")):
        return False
    # The owner token must still describe the path immediately before the
    # rename.  A competing reclaimer can then win at most once.
    current = _read_root_lock_owner(path)
    if (
        current is None
        or current.get("token") != existing.get("token")
        or _root_pid_is_alive(current.get("pid"))
    ):
        return False
    tombstone = root / f".{path.name}.stale.{uuid.uuid4().hex}"
    try:
        # A rename is the atomic compare-and-detach operation.  Do not unlink
        # the live path first: another waiter must never observe a window in
        # which it can install a new owner over an unverified lock.
        os.replace(str(path), str(tombstone))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RootProjectionLockError(
            f"root_projection_stale_lock_reclaim_failed:{type(exc).__name__}"
        ) from exc
    try:
        tombstone.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # The live lock is already detached, and the tombstone is harmless to
        # lock acquisition; surface the cleanup ambiguity for auditability.
        raise RootProjectionLockError(
            f"root_projection_stale_lock_cleanup_failed:{type(exc).__name__}"
        ) from exc
    return True


def _acquire_root_lock(
    root: Path,
    *,
    timeout: float = 5.0,
    stale_timeout: float | None = None,
) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    path = _root_lock_path(root)
    try:
        wait_timeout = max(0.0, float(timeout))
    except (TypeError, ValueError) as exc:
        raise RootProjectionLockError(
            f"invalid_root_projection_lock_timeout:{timeout!r}"
        ) from exc
    stale_wait = _coerce_stale_timeout(stale_timeout)
    deadline = time.monotonic() + wait_timeout
    while True:
        token = uuid.uuid4().hex
        created_at = time.time()
        owner = {
            "pid": os.getpid(),
            "token": token,
            "created_at": created_at,
            # ``time`` is retained as the concise owner timestamp named by
            # the root projection contract; ``created_at`` matches the node
            # lock receipt vocabulary and remains the validation field.
            "time": created_at,
        }
        try:
            descriptor = os.open(
                str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            break
        except FileExistsError as exc:
            if time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            existing = _read_root_lock_owner(path)
            if _try_reclaim_stale_root_lock(
                root,
                path,
                existing=existing,
                stale_timeout=stale_wait,
            ):
                # Recompute the deadline only for the new acquisition attempt;
                # reclaiming a stale lock is explicit and should not inherit a
                # previous wait that has already elapsed.
                deadline = time.monotonic() + wait_timeout
                continue
            reason = "malformed" if existing is None else f"pid={existing.get('pid')}"
            raise RootProjectionLockError(f"root_projection_lock_busy:{reason}") from exc
        except OSError as exc:
            raise RootProjectionLockError(
                f"root_projection_lock_acquire_failed:{type(exc).__name__}"
            ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(owner))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Leave an ambiguous lock rather than allowing another writer to
        # guess whether this owner survived a partial write.
        raise
    return path, token


def _release_root_lock(path: Path, token: str) -> None:
    owner = _read_root_lock_owner(path)
    if owner is None or owner.get("token") != token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _root_projection_lock(
    root: Path,
    *,
    timeout: float = 5.0,
    stale_timeout: float | None = None,
) -> Iterator[None]:
    path, token = _acquire_root_lock(
        root,
        timeout=timeout,
        stale_timeout=stale_timeout,
    )
    try:
        yield
    finally:
        _release_root_lock(path, token)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        with source.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                target_handle.write(block)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _remove_root_projection_payload(path: Path) -> None:
    """Remove one verified root payload after its history copy is durable."""

    path.unlink()


def input_row(role: str, path: str | os.PathLike[str]) -> dict[str, str]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise Phase3NodeIntegrationError(f"node_input_missing:{role}:{source}")
    return {"role": str(role), "path": str(source), "sha256": sha256_file(source)}


def write_node_input_snapshot(
    output_dir: str | os.PathLike[str],
    *,
    generation_id: str,
    attempt_id: str,
    blueprint: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
    shared_ledger_path: str | os.PathLike[str] | None,
    shared_kb_paths: Iterable[str | os.PathLike[str]],
    overlay_paths: Mapping[str, str | os.PathLike[str]],
    extra_paths: Iterable[str | os.PathLike[str]] = (),
    scoped_runtime_kb: str | os.PathLike[str] | None = None,
    source_base_kb: str | os.PathLike[str] | None = None,
) -> Path:
    root = phase3_root(output_dir)
    path_rows: list[dict[str, Any]] = []

    def resolved_file(raw: Any, role: str) -> Path | None:
        if not raw:
            return None
        candidate = Path(raw).expanduser().resolve(strict=False)
        if not candidate.is_file():
            raise Phase3NodeIntegrationError(f"node_input_missing:{role}:{candidate}")
        return candidate

    scoped_path = resolved_file(scoped_runtime_kb, "scoped_runtime_kb")
    source_path = resolved_file(source_base_kb, "broad_base_kb_excluded")
    if scoped_path is not None and source_path is not None and scoped_path == source_path:
        raise Phase3NodeIntegrationError("node_input_scoped_source_must_differ")

    def add_path(role: str, raw: Any) -> None:
        if not raw:
            return
        candidate = Path(raw).expanduser().resolve(strict=False)
        row: dict[str, Any] = {"role": role, "path": str(candidate)}
        if candidate.is_file():
            row["sha256"] = sha256_file(candidate)
            row["exists"] = True
        else:
            row["sha256"] = ""
            row["exists"] = False
        path_rows.append(row)

    add_path("shared_ledger", shared_ledger_path)
    for index, raw in enumerate(shared_kb_paths):
        candidate = Path(raw).expanduser().resolve(strict=False) if raw else None
        if source_path is not None and candidate == source_path:
            raise Phase3NodeIntegrationError(
                "node_input_source_base_in_shared_kb"
            )
        add_path(f"shared_kb[{index}]", raw)
    for section_id, raw in sorted(overlay_paths.items()):
        add_path(f"overlay[{section_id}]", raw)
    for index, raw in enumerate(extra_paths):
        candidate = Path(raw).expanduser().resolve(strict=False) if raw else None
        if source_path is not None and candidate == source_path:
            raise Phase3NodeIntegrationError(
                "node_input_source_base_in_extra_paths"
            )
        add_path(f"extra_input[{index}]", raw)

    def boundary_row(role: str, path: Path | None) -> dict[str, str] | None:
        if path is None:
            return None
        row = {"role": role, "path": str(path), "sha256": sha256_file(path)}
        path_rows.append({**row, "exists": True})
        return row

    scoped_row = boundary_row("scoped_runtime_kb", scoped_path)
    source_row = boundary_row("broad_base_kb_excluded", source_path)
    payload = {
        "schema_version": NODE_INPUT_SNAPSHOT_SCHEMA,
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "blueprint": dict(blueprint),
        "runtime_options": dict(runtime_options),
        "paths": path_rows,
    }
    if scoped_row is not None:
        payload["scoped_runtime_kb"] = scoped_row
    if source_row is not None:
        payload["broad_base_kb_excluded"] = source_row
    return atomic_write_json(root / "input" / "NODE_INPUT_SNAPSHOT.json", payload)


def validate_node_input_snapshot_for_resume(
    output_dir: str | os.PathLike[str],
    *,
    blueprint: Mapping[str, Any],
    runtime_options: Mapping[str, Any],
    expected_generation_id: str | None = None,
    generation_id: str | None = None,
    scoped_runtime_kb: str | os.PathLike[str] | None = None,
    source_base_kb: str | os.PathLike[str] | None = None,
    phase3_input_fingerprint_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Validate the persisted P3A input boundary without rebuilding it.

    The snapshot's ``attempt_id`` is deliberately ignored: a D-only retry has
    a new attempt while A/B/C retain their original attempts.  Every other
    input identity is strict and content-addressed, including the optional
    PHASE3_INPUT_FINGERPRINT asset.
    """

    root = phase3_root(output_dir)
    snapshot_path = root / "input" / "NODE_INPUT_SNAPSHOT.json"
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase3NodeIntegrationError(
            f"node_input_snapshot_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(value, Mapping):
        raise Phase3NodeIntegrationError("node_input_snapshot_invalid")
    expected_schema = NODE_INPUT_SNAPSHOT_SCHEMA
    if value.get("schema_version") != expected_schema:
        raise Phase3NodeIntegrationError("node_input_snapshot_schema_invalid")
    stored_generation = str(value.get("generation_id") or "").strip()
    if not stored_generation or not str(value.get("attempt_id") or "").strip():
        raise Phase3NodeIntegrationError("node_input_snapshot_identity_invalid")
    expected_generation = expected_generation_id if expected_generation_id is not None else generation_id
    if expected_generation is not None and stored_generation != str(expected_generation):
        raise Phase3NodeIntegrationError(
            "node_input_snapshot_generation_mismatch:"
            f"expected={expected_generation}:actual={value.get('generation_id')}"
        )
    if not isinstance(value.get("blueprint"), Mapping):
        raise Phase3NodeIntegrationError("node_input_snapshot_blueprint_invalid")
    if _canonical_json(value["blueprint"]) != _canonical_json(dict(blueprint)):
        raise Phase3NodeIntegrationError("node_input_snapshot_blueprint_changed")
    stored_options = value.get("runtime_options")
    if not isinstance(stored_options, Mapping):
        raise Phase3NodeIntegrationError("node_input_snapshot_runtime_options_invalid")
    current_options = dict(runtime_options)
    stored_option_keys = {str(key) for key in stored_options}
    current_option_keys = {str(key) for key in current_options}
    if stored_option_keys != current_option_keys:
        missing = sorted(stored_option_keys - current_option_keys)
        unexpected = sorted(current_option_keys - stored_option_keys)
        raise Phase3NodeIntegrationError(
            "node_input_snapshot_runtime_options_key_set_changed:"
            f"missing={missing}:unexpected={unexpected}"
        )
    option_mismatches = {
        str(key): {
            "stored": stored,
            "current": current_options.get(str(key)),
        }
        for key, stored in stored_options.items()
        if current_options.get(str(key)) != stored
    }
    if option_mismatches:
        raise Phase3NodeIntegrationError(
            "node_input_snapshot_runtime_options_changed:"
            + ",".join(sorted(option_mismatches))
        )
    path_rows = value.get("paths")
    if not isinstance(path_rows, list):
        raise Phase3NodeIntegrationError("node_input_snapshot_paths_invalid")
    normalized_paths: list[dict[str, Any]] = []
    for index, row in enumerate(path_rows):
        if not isinstance(row, Mapping):
            raise Phase3NodeIntegrationError(f"node_input_snapshot_path_row_invalid:{index}")
        role = str(row.get("role") or "").strip()
        path_text = str(row.get("path") or "").strip()
        expected_hash = str(row.get("sha256") or "").strip()
        if not role or not path_text or not expected_hash or row.get("exists") is not True:
            raise Phase3NodeIntegrationError(
                f"node_input_snapshot_path_identity_invalid:{role or index}"
            )
        path = Path(path_text).expanduser().resolve(strict=False)
        if not path.is_file():
            raise Phase3NodeIntegrationError(f"node_input_snapshot_path_missing:{role}")
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise Phase3NodeIntegrationError(
                f"node_input_snapshot_path_hash_changed:{role}"
            )
        normalized_paths.append({
            "role": role,
            "path": str(path),
            "sha256": actual_hash,
        })

    def resolve_optional(raw: Any) -> str:
        return str(Path(raw).expanduser().resolve(strict=False)) if raw else ""

    stored_by_role = {
        str(row["role"]): row
        for row in normalized_paths
        if row.get("role")
    }

    def stored_boundary(key: str, role: str) -> dict[str, str] | None:
        raw = value.get(key)
        row = stored_by_role.get(role)
        if raw is not None:
            if not isinstance(raw, Mapping):
                raise Phase3NodeIntegrationError(
                    f"node_input_snapshot_{key}_invalid"
                )
            raw_path = str(raw.get("path") or "").strip()
            raw_hash = str(raw.get("sha256") or "").strip()
            if not raw_path or len(raw_hash) != 64:
                raise Phase3NodeIntegrationError(
                    f"node_input_snapshot_{key}_identity_invalid"
                )
            if row is None or str(row.get("path")) != str(
                Path(raw_path).expanduser().resolve(strict=False)
            ) or str(row.get("sha256")) != raw_hash:
                raise Phase3NodeIntegrationError(
                    f"node_input_snapshot_{key}_row_mismatch"
                )
            return {
                "role": role,
                "path": str(Path(raw_path).expanduser().resolve(strict=False)),
                "sha256": raw_hash,
            }
        if row is None:
            return None
        return {
            "role": role,
            "path": str(row.get("path") or ""),
            "sha256": str(row.get("sha256") or ""),
        }

    stored_scoped_boundary = stored_boundary(
        "scoped_runtime_kb", "scoped_runtime_kb"
    )
    stored_source_boundary = stored_boundary(
        "broad_base_kb_excluded", "broad_base_kb_excluded"
    )
    scoped_path = resolve_optional(scoped_runtime_kb)
    source_base_path = resolve_optional(source_base_kb)
    if stored_scoped_boundary is not None:
        if not scoped_path:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_scoped_runtime_kb_missing_current"
            )
        if not Path(scoped_path).is_file() or sha256_file(scoped_path) != stored_scoped_boundary["sha256"]:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_scoped_runtime_kb_changed"
            )
        if scoped_path != stored_scoped_boundary["path"]:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_scoped_runtime_kb_changed"
            )
    elif scoped_path:
        raise Phase3NodeIntegrationError(
            "node_input_snapshot_scoped_runtime_kb_unrecorded"
        )
    if stored_source_boundary is not None:
        if not source_base_path:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_source_base_kb_missing_current"
            )
        if not Path(source_base_path).is_file() or sha256_file(source_base_path) != stored_source_boundary["sha256"]:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_source_base_kb_changed"
            )
        if source_base_path != stored_source_boundary["path"]:
            raise Phase3NodeIntegrationError(
                "node_input_snapshot_source_base_kb_changed"
            )
    elif source_base_path:
        raise Phase3NodeIntegrationError(
            "node_input_snapshot_source_base_kb_unrecorded"
        )
    if scoped_path and source_base_path and scoped_path == source_base_path:
        raise Phase3NodeIntegrationError("node_input_scoped_source_must_differ")
    shared_rows = [
        row for row in normalized_paths
        if str(row.get("role") or "").startswith("shared_kb[")
    ]
    if source_base_path and any(
        str(row.get("path") or "") == source_base_path for row in shared_rows
    ):
        raise Phase3NodeIntegrationError("node_input_source_base_in_shared_kb")
    if stored_source_boundary is not None and any(
        str(row.get("path") or "") == stored_source_boundary["path"]
        for row in shared_rows
    ):
        raise Phase3NodeIntegrationError("node_input_source_base_in_shared_kb")
    if stored_source_boundary is not None and any(
        str(row.get("path") or "") == stored_source_boundary["path"]
        and str(row.get("role") or "") != "broad_base_kb_excluded"
        for row in normalized_paths
    ):
        raise Phase3NodeIntegrationError("node_input_source_base_duplicated")
    fingerprint_row = stored_by_role.get("phase3_input_fingerprint")
    if fingerprint_row is None:
        fingerprint_row = next(
            (
                row
                for row in normalized_paths
                if str(row.get("role") or "").startswith("extra_input[")
                and Path(str(row.get("path") or "")).name
                == "PHASE3_INPUT_FINGERPRINT.json"
            ),
            None,
        )
    fingerprint_path = (
        Path(str(fingerprint_row["path"]))
        if fingerprint_row is not None
        else Path(phase3_input_fingerprint_path).expanduser().resolve(strict=False)
        if phase3_input_fingerprint_path
        and Path(phase3_input_fingerprint_path).expanduser().resolve(strict=False).is_file()
        else None
    )
    fingerprint_payload: dict[str, Any] = {}
    if fingerprint_path is not None:
        if not fingerprint_path.is_file():
            raise Phase3NodeIntegrationError("node_input_fingerprint_missing")
        fingerprint_hash = sha256_file(fingerprint_path)
        if fingerprint_row is not None and fingerprint_hash != str(fingerprint_row["sha256"]):
            raise Phase3NodeIntegrationError("node_input_fingerprint_changed")
        try:
            raw_fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"node_input_fingerprint_unreadable:{type(exc).__name__}"
            ) from exc
        if not isinstance(raw_fingerprint, Mapping):
            raise Phase3NodeIntegrationError("node_input_fingerprint_invalid")
        fingerprint_files = raw_fingerprint.get("files")
        fingerprint_value = str(raw_fingerprint.get("sha256") or "").strip()
        if not isinstance(fingerprint_files, Mapping) or not fingerprint_value:
            raise Phase3NodeIntegrationError("node_input_fingerprint_structure_invalid")
        if any(
            not str(key).strip()
            or not isinstance(item, str)
            or len(item) != 64
            or any(char not in "0123456789abcdef" for char in item.lower())
            for key, item in fingerprint_files.items()
        ):
            raise Phase3NodeIntegrationError("node_input_fingerprint_files_invalid")
        if hashlib.sha256(_canonical_json(fingerprint_files)).hexdigest() != fingerprint_value:
            raise Phase3NodeIntegrationError("node_input_fingerprint_hash_invalid")
        expected_known_fingerprints: dict[str, str] = {
            "blueprint": hashlib.sha256(_canonical_json(dict(blueprint))).hexdigest(),
            "phase3_runtime_options": hashlib.sha256(
                _canonical_json(current_options)
            ).hexdigest(),
        }
        for row in normalized_paths:
            role = str(row.get("role") or "")
            path_text = str(row.get("path") or "")
            if role == "shared_ledger":
                expected_known_fingerprints["shared_ledger"] = str(row["sha256"])
            elif role.startswith("shared_kb["):
                expected_known_fingerprints[f"kb:{path_text}"] = str(row["sha256"])
            elif role.startswith("overlay[") and role.endswith("]"):
                expected_known_fingerprints[
                    f"overlay:{role[len('overlay['):-1]}"
                ] = str(row["sha256"])
        for key, expected in expected_known_fingerprints.items():
            if key in fingerprint_files and str(fingerprint_files[key]) != expected:
                raise Phase3NodeIntegrationError(
                    f"node_input_fingerprint_dependency_changed:{key}"
                )
        if (
            raw_fingerprint.get("schema_version")
            != "research_harness.phase3_input_fingerprint.v1"
            or not fingerprint_value
        ):
            raise Phase3NodeIntegrationError("node_input_fingerprint_schema_invalid")
        fingerprint_payload = dict(raw_fingerprint)
        stored_scoped = resolve_optional(raw_fingerprint.get("scoped_runtime_kb"))
        if scoped_path and stored_scoped and stored_scoped != scoped_path:
            raise Phase3NodeIntegrationError("node_input_fingerprint_scoped_kb_changed")
        if scoped_path and not stored_scoped and stored_scoped_boundary is not None:
            raise Phase3NodeIntegrationError("node_input_fingerprint_scoped_kb_missing")
        stored_source_base = resolve_optional(raw_fingerprint.get("broad_base_kb_excluded"))
        if source_base_path and stored_source_base and stored_source_base != source_base_path:
            raise Phase3NodeIntegrationError("node_input_fingerprint_source_base_changed")
        if source_base_path and not stored_source_base and stored_source_boundary is not None:
            raise Phase3NodeIntegrationError("node_input_fingerprint_source_base_missing")
    return {
        "valid": True,
        "schema_version": expected_schema,
        "snapshot_path": str(snapshot_path),
        "generation_id": stored_generation,
        "attempt_id": str(value.get("attempt_id") or ""),
        "snapshot_sha256": sha256_file(snapshot_path),
        "paths": normalized_paths,
        "phase3_input_fingerprint_path": str(fingerprint_path) if fingerprint_path else "",
        "phase3_input_fingerprint": fingerprint_payload,
    }


validate_phase3_input_snapshot_for_resume = validate_node_input_snapshot_for_resume


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def canonical_no_llm_contract() -> dict[str, Any]:
    """Return the stable contract used by a node that makes no model calls.

    A no-LLM node still needs a model fingerprint.  A literal value such as
    ``"ok"`` cannot distinguish the contract from an accidentally omitted
    model boundary, and putting run-time flags in this object would make a
    deterministic node drift when an unrelated LLM node is enabled.  Keep
    this payload small, explicit, and free of time/path/secret material.
    """

    return {
        "schema_version": NO_LLM_CONTRACT_SCHEMA,
        "execution_mode": "no_llm",
        "provider": "none",
        "model": "none",
        "prompt": "none",
        "max_calls": 0,
        "determinism": "canonical",
    }


def canonical_no_llm_contract_hash() -> str:
    """Return the SHA-256 of :func:`canonical_no_llm_contract`."""

    return _hash_payload(canonical_no_llm_contract())


CANONICAL_NO_LLM_CONTRACT_HASH = canonical_no_llm_contract_hash()
NO_LLM_CONTRACT_HASH = CANONICAL_NO_LLM_CONTRACT_HASH


# Friendly aliases for callers that use the shorter contract terminology.
build_no_llm_contract = canonical_no_llm_contract
no_llm_contract_hash = canonical_no_llm_contract_hash


def _prompt_source_for_function(function: Any) -> tuple[str, str, str]:
    """Return ``(qualname, source, source_path)`` for a prompt callable.

    The optional private attributes are used by the orchestration boundary to
    retain the source of the production callable when a test injects a
    replacement callable.  Accepting a mapping also keeps this helper useful
    for source-only dependency injection without making a dummy executable
    function.
    """

    if isinstance(function, Mapping):
        qualname = str(function.get("qualname") or function.get("name") or "")
        source = function.get("source")
        source_path = str(function.get("source_path") or "")
        if isinstance(source, str) and source:
            return qualname or "<prompt>", source, source_path
    source = getattr(function, "__phase3_prompt_source__", None)
    source_path = str(getattr(function, "__phase3_prompt_source_path__", "") or "")
    qualname = str(
        getattr(function, "__phase3_prompt_qualname__", "")
        or getattr(function, "__qualname__", function)
    )
    if not isinstance(source, str) or not source:
        try:
            source = inspect.getsource(function)
        except (OSError, TypeError) as exc:
            raise Phase3NodeIntegrationError(
                f"fingerprint_prompt_function_missing:{type(function).__name__}"
            ) from exc
    if not source_path:
        try:
            source_path = str(Path(inspect.getsourcefile(function) or "").resolve())
        except (OSError, TypeError, ValueError):
            source_path = ""
    return qualname, source, source_path


def build_node_fingerprint_details(
    *,
    node_id: str,
    code_paths: Iterable[str | os.PathLike[str]],
    prompt_context: Any,
    model_context: Any,
    policy_context: Any,
    schema_context: Any,
    prompt_paths: Iterable[str | os.PathLike[str]] = (),
    prompt_functions: Iterable[Any] = (),
) -> dict[str, Any]:
    code_rows: list[dict[str, str]] = []
    for raw in code_paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise Phase3NodeIntegrationError(f"fingerprint_code_missing:{node_id}:{path}")
        code_rows.append({"path": str(path), "sha256": sha256_file(path)})
    prompt_rows: list[dict[str, str]] = []
    for raw in prompt_paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise Phase3NodeIntegrationError(
                f"fingerprint_prompt_missing:{node_id}:{path}"
            )
        prompt_rows.append({"path": str(path), "sha256": sha256_file(path)})
    function_rows: list[dict[str, str]] = []
    for function in prompt_functions:
        qualname, source, source_path = _prompt_source_for_function(function)
        row = {
            "qualname": qualname,
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
        if source_path:
            row["path"] = source_path
        function_rows.append(row)
    fingerprints = {
        "code": _hash_payload({"node_id": node_id, "files": code_rows}),
        "prompt": _hash_payload({
            "node_id": node_id,
            "prompt": prompt_context,
            "files": prompt_rows,
            "functions": function_rows,
        }),
        "model": _hash_payload({"node_id": node_id, "model": model_context}),
        "policy": _hash_payload({"node_id": node_id, "policy": policy_context}),
        "schema": _hash_payload({"node_id": node_id, "schema": schema_context}),
    }
    return {
        "fingerprints": fingerprints,
        "sources": {
            "code": code_rows,
            "prompt_files": prompt_rows,
            "prompt_functions": function_rows,
            "prompt_context": prompt_context,
            "model_context": model_context,
            "policy_context": policy_context,
            "schema_context": schema_context,
        },
    }


def build_node_fingerprints(**kwargs: Any) -> dict[str, str]:
    """Return the five manifest fingerprints from real source material."""

    return dict(build_node_fingerprint_details(**kwargs)["fingerprints"])


def dependency_row(manifest: Mapping[str, Any]) -> dict[str, str]:
    node_id = str(manifest.get("node_id") or "")
    generation_id = str(manifest.get("generation_id") or "")
    attempt_id = str(manifest.get("attempt_id") or "")
    manifest_hash = str(manifest.get("manifest_sha256") or "")
    if not node_id or not generation_id or not attempt_id or not manifest_hash:
        raise Phase3NodeIntegrationError("dependency_manifest_identity_missing")
    return {
        "node_id": node_id,
        "manifest_sha256": manifest_hash,
        "generation_id": generation_id,
        "attempt_id": attempt_id,
    }


def input_rows_from_manifest(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in manifest.get("outputs") or []:
        if not isinstance(raw, Mapping):
            raise Phase3NodeIntegrationError("upstream_output_row_invalid")
        rows.append({
            "role": f"upstream:{raw.get('role')}",
            "path": str(raw.get("path") or ""),
            "sha256": str(raw.get("sha256") or ""),
        })
    if not rows:
        raise Phase3NodeIntegrationError(
            f"upstream_outputs_missing:{manifest.get('node_id')}"
        )
    return rows


def _projection_path(root: Path) -> Path:
    return root / ROOT_PROJECTIONS_FILENAME


def _read_projection_index(root: Path) -> dict[str, Any]:
    path = _projection_path(root)
    if not path.exists():
        return {
            "schema_version": ROOT_PROJECTIONS_SCHEMA,
            "projections": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase3NodeIntegrationError(
            f"root_projections_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != ROOT_PROJECTIONS_SCHEMA:
        raise Phase3NodeIntegrationError("root_projections_schema_invalid")
    projections = value.get("projections")
    if not isinstance(projections, dict):
        raise Phase3NodeIntegrationError("root_projections_rows_invalid")
    return value


def _projection_entries(
    root: Path,
    manifest: Mapping[str, Any],
    root_filenames: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Validate a committed manifest and resolve its projection entries.

    The projection layer receives a manifest from the node commit boundary,
    but it may also be called later during recovery.  Rechecking the state,
    node-local output ownership, and content hash here prevents a caller from
    projecting a staged manifest or an output that has drifted since commit.
    """

    node_id = str(manifest.get("node_id") or "").strip()
    if not node_id:
        raise Phase3NodeIntegrationError("projection_manifest_node_id_missing")
    if str(manifest.get("state") or "") != "committed":
        raise Phase3NodeIntegrationError(
            f"projection_node_not_committed:{node_id}:{manifest.get('state')}"
        )
    manifest_hash = str(manifest.get("manifest_sha256") or "").strip()
    if not manifest_hash:
        raise Phase3NodeIntegrationError("projection_manifest_hash_missing")
    calculated_manifest_hash = _hash_payload(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    if calculated_manifest_hash != manifest_hash:
        raise Phase3NodeIntegrationError(
            f"projection_manifest_hash_mismatch:{node_id}"
        )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise Phase3NodeIntegrationError(f"projection_outputs_missing:{node_id}")
    node_path = node_dir(root, node_id).resolve()
    entries: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()
    for output in outputs:
        if not isinstance(output, Mapping):
            raise Phase3NodeIntegrationError("projection_output_row_invalid")
        role = str(output.get("role") or "").strip()
        filename = str(root_filenames.get(role) or role).strip()
        expected_hash = str(output.get("sha256") or "").strip()
        if not role or not filename or not expected_hash:
            raise Phase3NodeIntegrationError(
                f"projection_output_identity_missing:{node_id}:{role}"
            )
        # A compatibility projection is a direct child of the phase3 root.
        # Reject separators and dot names on every platform rather than
        # relying on the platform-specific Path implementation.
        if (
            filename in {".", ".."}
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise Phase3NodeIntegrationError(
                f"root_projection_filename_invalid:{filename}"
            )
        if filename in seen_filenames:
            raise Phase3NodeIntegrationError(
                f"root_projection_filename_duplicate:{filename}"
            )
        seen_filenames.add(filename)
        source = Path(str(output.get("path") or "")).expanduser().resolve(
            strict=False
        )
        if source.parent != node_path or not source.is_file():
            raise Phase3NodeIntegrationError(
                f"node_output_path_invalid:{node_id}:{role}"
            )
        actual_hash = sha256_file(source)
        if actual_hash != expected_hash:
            raise Phase3NodeIntegrationError(
                f"node_output_hash_mismatch:{node_id}:{role}:"
                f"expected={expected_hash}:actual={actual_hash}"
            )
        entries.append({
            "role": role,
            "filename": filename,
            "source": source,
            "target": root / filename,
            "output_sha256": expected_hash,
            "bytes": int(output.get("bytes") or source.stat().st_size),
        })
    return entries


def _check_projection_conflicts(
    root: Path,
    manifest: Mapping[str, Any],
    root_filenames: Mapping[str, str],
    *,
    allow_unindexed_matching_payload: bool = True,
) -> None:
    entries = _projection_entries(root, manifest, root_filenames)
    index = _read_projection_index(root)
    projections = index["projections"]
    manifest_hash = str(manifest.get("manifest_sha256") or "")
    node_id = str(manifest.get("node_id") or "")
    for entry in entries:
        filename = str(entry["filename"])
        target = Path(entry["target"])
        output_hash = str(entry["output_sha256"])
        existing = projections.get(filename)
        if existing is not None and (
            not isinstance(existing, Mapping)
            or str(existing.get("node_id")) != node_id
            or str(existing.get("manifest_sha256")) != manifest_hash
            or str(existing.get("output_sha256")) != output_hash
        ):
            raise RootProjectionConflict(
                f"root_projection_conflict:{filename}:index_mismatch"
            )
        if target.exists():
            if not isinstance(existing, Mapping):
                # An index write can fail after the payload replacement.  A
                # replay of that same committed node may safely adopt the
                # orphan payload when its bytes match the node output.  An
                # unrelated legacy file still fails closed because its hash
                # cannot match this committed output.
                if not allow_unindexed_matching_payload:
                    raise RootProjectionConflict(
                        f"root_projection_conflict:{filename}:missing_index"
                    )
                try:
                    target_hash = sha256_file(target)
                except OSError as exc:
                    raise RootProjectionConflict(
                        f"root_projection_conflict:{filename}:unreadable_payload"
                    ) from exc
                if target_hash != output_hash:
                    raise RootProjectionConflict(
                        f"root_projection_conflict:{filename}:missing_index"
                    )
                continue
            if (
                str(existing.get("node_id")) != node_id
                or str(existing.get("manifest_sha256")) != manifest_hash
                or str(existing.get("output_sha256")) != output_hash
            ):
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:manifest_or_output_mismatch"
                )
            if sha256_file(target) != output_hash:
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:content_mismatch"
                )


def validate_committed_root_projection(
    output_dir: str | os.PathLike[str],
    manifest_or_node_id: Mapping[str, Any] | str,
    *,
    root_filenames: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Strictly validate one committed node and every root projection.

    ``valid`` is true only when the persisted committed manifest, every
    node-local output, each index row, and every root payload agree.  Missing
    or unregistered payloads whose bytes still match the node output are
    reported as repairable so replay can adopt them.  Any conflicting bytes,
    provenance, or unknown index entry raises ``RootProjectionConflict`` and
    is never overwritten.
    """

    root = phase3_root(output_dir)
    if isinstance(manifest_or_node_id, Mapping):
        node_id = str(manifest_or_node_id.get("node_id") or "").strip()
        if not node_id:
            raise Phase3NodeIntegrationError("projection_validation_node_id_missing")
        expected_manifest = dict(manifest_or_node_id)
    else:
        node_id = str(manifest_or_node_id or "").strip()
        if not node_id:
            raise Phase3NodeIntegrationError("projection_validation_node_id_missing")
        expected_manifest = None
    manifest = load_and_validate_committed_node(root, node_id)
    if expected_manifest is not None and str(
        expected_manifest.get("manifest_sha256") or ""
    ) != str(manifest.get("manifest_sha256") or ""):
        raise RootProjectionConflict(
            f"root_projection_conflict:{node_id}:manifest_hash_mismatch"
        )
    mapping = _mapping_from_matching_journal(root, manifest)
    mapping.update(dict(root_filenames or {}))
    entries = _projection_entries(root, manifest, mapping)
    expected_by_name = {
        str(entry["filename"]): entry for entry in entries
    }
    missing: list[str] = []
    repairable: list[str] = []
    with _root_projection_lock(root):
        index = _read_projection_index(root)
        projections = index["projections"]
        for filename, row in projections.items():
            if not isinstance(row, Mapping):
                continue
            if str(row.get("node_id") or "") != node_id:
                continue
            expected = expected_by_name.get(str(filename))
            if expected is None:
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:unknown_node_entry"
                )
            if (
                str(row.get("manifest_sha256") or "")
                != str(manifest.get("manifest_sha256") or "")
                or str(row.get("output_sha256") or "")
                != str(expected["output_sha256"])
            ):
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:index_mismatch"
                )
            target = Path(expected["target"])
            if target.is_symlink():
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:symlink_payload"
                )
            if not target.exists():
                missing.append(str(filename))
                continue
            if not target.is_file():
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:root_payload_not_regular"
                )
            if sha256_file(target) != str(expected["output_sha256"]):
                raise RootProjectionConflict(
                    f"root_projection_conflict:{filename}:content_mismatch"
                )
        for filename, expected in expected_by_name.items():
            if filename not in projections or not isinstance(projections.get(filename), Mapping):
                missing.append(filename)
                target = Path(expected["target"])
                if target.is_symlink():
                    raise RootProjectionConflict(
                        f"root_projection_conflict:{filename}:symlink_payload"
                    )
                if not target.exists():
                    continue
                if target.is_file() and sha256_file(target) == str(expected["output_sha256"]):
                    repairable.append(filename)
                else:
                    raise RootProjectionConflict(
                        f"root_projection_conflict:{filename}:content_mismatch"
                    )
    return {
        "valid": not missing,
        "repairable": bool(repairable) or bool(missing),
        "node_id": node_id,
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "manifest": manifest,
        "missing_projections": sorted(missing),
        "repairable_projections": sorted(repairable),
        "projection_count": len(expected_by_name),
    }


validate_committed_root_projections = validate_committed_root_projection


def _record_projection_conflict(
    root: Path,
    manifest: Mapping[str, Any],
    error: Exception,
) -> None:
    try:
        index = _read_projection_index(root)
        conflicts = index.setdefault("conflicts", [])
        if not isinstance(conflicts, list):
            return
        entry = {
            "node_id": str(manifest.get("node_id") or ""),
            "generation_id": str(manifest.get("generation_id") or ""),
            "attempt_id": str(manifest.get("attempt_id") or ""),
            "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
            "reason": str(error),
        }
        if entry not in conflicts:
            conflicts.append(entry)
        atomic_write_json(_projection_path(root), index)
    except Exception:
        # The original conflict is the authoritative failure; an inability to
        # append its audit record must not cause a root file overwrite.
        return


def _projection_journal_path(root: Path) -> Path:
    return root / ROOT_PROJECTION_JOURNAL_FILENAME


def _write_projection_failure_receipt(
    root: Path,
    manifest: Mapping[str, Any],
    error: Exception,
    *,
    journal_path: Path | None = None,
    journal: Mapping[str, Any] | None = None,
) -> Path:
    journal_value = dict(journal or {})
    payload = {
        "schema_version": "optomind.upgrade3.phase3_root_projection_failure.v1",
        "node_id": str(manifest.get("node_id") or ""),
        "generation_id": str(manifest.get("generation_id") or ""),
        "attempt_id": str(manifest.get("attempt_id") or ""),
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "node_state": str(manifest.get("state") or "committed"),
        "compatibility_projection_state": "failed",
        "error_type": type(error).__name__,
        "reason": str(error)[:1000],
        "journal_path": str(journal_path) if journal_path else "",
        "transaction_id": str(journal_value.get("transaction_id") or ""),
        "journal_state": str(journal_value.get("state") or "failed"),
        "completed_outputs": list(journal_value.get("completed_outputs") or []),
        "index_state": str(journal_value.get("index_state") or "pending"),
        "created_at": time.time(),
    }
    return atomic_write_json(root / ROOT_PROJECTION_FAILURE_FILENAME, payload)


def _new_projection_journal(
    manifest: Mapping[str, Any],
    outputs: list[Mapping[str, Any]],
    root_filenames: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": "optomind.upgrade3.phase3_root_projection_transaction.v1",
        "transaction_id": uuid.uuid4().hex,
        "state": "prepared",
        "node_id": str(manifest.get("node_id") or ""),
        "generation_id": str(manifest.get("generation_id") or ""),
        "attempt_id": str(manifest.get("attempt_id") or ""),
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "outputs": [
            {
                "role": str(output.get("role") or ""),
                "filename": str(
                    root_filenames.get(str(output.get("role") or ""))
                    or output.get("role")
                    or ""
                ),
                "output_sha256": str(output.get("sha256") or ""),
                "state": "pending",
            }
            for output in outputs
        ],
        "completed_outputs": [],
        "index_state": "pending",
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _mapping_from_matching_journal(
    root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Recover a prior role-to-root-name mapping for projection replay."""

    path = _projection_journal_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    if (
        str(value.get("node_id") or "") != str(manifest.get("node_id") or "")
        or str(value.get("manifest_sha256") or "")
        != str(manifest.get("manifest_sha256") or "")
    ):
        return {}
    mapping: dict[str, str] = {}
    for row in value.get("outputs") or []:
        if not isinstance(row, Mapping):
            continue
        role = str(row.get("role") or "").strip()
        filename = str(row.get("filename") or "").strip()
        if role and filename:
            mapping[role] = filename
    return mapping


def _journal_update(
    journal_path: Path,
    journal: dict[str, Any],
    *,
    state: str | None = None,
    failure: Exception | None = None,
) -> None:
    if state is not None:
        journal["state"] = state
    if failure is not None:
        journal["failure"] = {
            "error_type": type(failure).__name__,
            "reason": str(failure)[:1000],
        }
    journal["updated_at"] = time.time()
    atomic_write_json(journal_path, journal)


def _matching_failure_receipt(
    root: Path,
    manifest: Mapping[str, Any],
) -> tuple[Path | None, str, dict[str, Any] | None]:
    """Read the prior projection failure without changing its history."""

    path = root / ROOT_PROJECTION_FAILURE_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            return None, "", None
        if (
            str(value.get("node_id") or "")
            != str(manifest.get("node_id") or "")
            or str(value.get("manifest_sha256") or "")
            != str(manifest.get("manifest_sha256") or "")
        ):
            return None, "", None
        return path, sha256_file(path), dict(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "", None


def _write_projection_recovery_receipt(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    failure_path: Path,
    failure_sha256: str,
    journal: Mapping[str, Any] | None = None,
) -> Path:
    """Record that a prior projection failure was resolved by replay."""

    target = root / ROOT_PROJECTION_RECOVERY_FILENAME
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing = None
    if (
        isinstance(existing, Mapping)
        and existing.get("state") == "resolved"
        and str(existing.get("manifest_sha256") or "")
        == str(manifest.get("manifest_sha256") or "")
        and str(existing.get("failure_receipt_sha256") or "")
        == str(failure_sha256)
    ):
        # Replaying the same committed node again is idempotent, including its
        # resolution audit.  A different failure receipt hash starts a new
        # resolution record below.
        return target
    journal_value = dict(journal or {})
    payload = {
        "schema_version": "optomind.upgrade3.phase3_root_projection_recovery.v1",
        "state": "resolved",
        "resolution": "resolved",
        "current_failure": False,
        "current_projection_state": "committed",
        "node_state": str(manifest.get("state") or "committed"),
        "node_id": str(manifest.get("node_id") or ""),
        "generation_id": str(manifest.get("generation_id") or ""),
        "attempt_id": str(manifest.get("attempt_id") or ""),
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "failure_receipt_path": str(failure_path),
        "failure_receipt_sha256": str(failure_sha256),
        "transaction_id": str(journal_value.get("transaction_id") or ""),
        "resolved_outputs": list(journal_value.get("completed_outputs") or []),
        "resolved_at": time.time(),
    }
    return atomic_write_json(target, payload)


def project_committed_node(
    output_dir: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    *,
    root_filenames: Mapping[str, str] | None = None,
    lock_timeout: float = 5.0,
    stale_lock_timeout: float | None = None,
    stale_timeout: float | None = None,
) -> dict[str, Any]:
    root = phase3_root(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    mapping = _mapping_from_matching_journal(root, manifest)
    mapping.update(dict(root_filenames or {}))
    effective_stale_timeout = (
        stale_lock_timeout if stale_lock_timeout is not None else stale_timeout
    )
    journal_path = _projection_journal_path(root)
    journal: dict[str, Any] | None = None
    try:
        with _root_projection_lock(
            root,
            timeout=lock_timeout,
            stale_timeout=effective_stale_timeout,
        ):
            outputs = list(manifest.get("outputs") or [])
            journal = _new_projection_journal(manifest, outputs, mapping)
            atomic_write_json(journal_path, journal)
            try:
                # Re-read the index after acquiring the O_EXCL lock.  Matching
                # orphan payloads are the one recoverable state after an
                # earlier index replacement failure; all other conflicts are
                # immutable compatibility-root evidence.
                entries = _projection_entries(root, manifest, mapping)
                _check_projection_conflicts(
                    root,
                    manifest,
                    mapping,
                    allow_unindexed_matching_payload=True,
                )
            except Exception as exc:
                if isinstance(exc, RootProjectionConflict):
                    _record_projection_conflict(root, manifest, exc)
                try:
                    _journal_update(journal_path, journal, state="failed", failure=exc)
                except Exception:
                    pass
                raise
            projections = _read_projection_index(root)
            rows = projections["projections"]
            node_id = str(manifest.get("node_id") or "")
            manifest_hash = str(manifest.get("manifest_sha256") or "")
            try:
                for index, entry in enumerate(entries):
                    role = str(entry["role"])
                    filename = str(entry["filename"])
                    source = Path(entry["source"])
                    target = Path(entry["target"])
                    if target.exists():
                        current = rows.get(filename)
                        journal["outputs"][index]["state"] = (
                            "already_present"
                            if isinstance(current, Mapping)
                            else "already_present_unindexed"
                        )
                    else:
                        _atomic_copy(source, target)
                        journal["outputs"][index]["state"] = "committed"
                    if filename not in journal["completed_outputs"]:
                        journal["completed_outputs"].append(filename)
                    _journal_update(journal_path, journal)
                    rows[filename] = {
                        "node_id": node_id,
                        "manifest_sha256": manifest_hash,
                        "output_sha256": str(entry["output_sha256"]),
                    }
            except Exception as exc:
                try:
                    _journal_update(journal_path, journal, state="failed", failure=exc)
                except Exception:
                    pass
                raise
            projections["generation_id"] = str(manifest.get("generation_id") or "")
            projections["attempt_id"] = str(manifest.get("attempt_id") or "")
            projections["projections"] = rows
            try:
                atomic_write_json(_projection_path(root), projections)
            except Exception as exc:
                journal["index_state"] = "failed"
                try:
                    _journal_update(journal_path, journal, state="failed", failure=exc)
                except Exception:
                    pass
                raise
            journal["index_state"] = "committed"
            _journal_update(journal_path, journal, state="committed")
            return projections
    except ProjectionError:
        raise
    except Exception as exc:
        receipt_path: Path | None = None
        try:
            receipt_path = _write_projection_failure_receipt(
                root,
                manifest,
                exc,
                journal_path=journal_path,
                journal=journal,
            )
        except Exception:
            pass
        raise ProjectionError(
            "node_committed_compatibility_projection_failed",
            manifest=manifest,
            receipt_path=receipt_path,
        ) from exc


def replay_committed_node_projection(
    output_dir: str | os.PathLike[str],
    manifest_or_node_id: Mapping[str, Any] | str,
    *,
    root_filenames: Mapping[str, str] | None = None,
    stale_lock_timeout: float | None = None,
    lock_timeout: float | None = None,
    stale_timeout: float | None = None,
) -> dict[str, Any]:
    """Idempotently rebuild compatibility projections from a committed node.

    ``manifest_or_node_id`` may be the manifest returned by
    :func:`commit_node_artifacts` or a node id whose on-disk manifest should be
    loaded.  The node-local outputs remain authoritative: a matching orphan
    payload left behind by a failed index replacement is adopted and indexed;
    any different payload or provenance still raises ``ProjectionError``.
    """

    if isinstance(manifest_or_node_id, Mapping):
        manifest = dict(manifest_or_node_id)
    else:
        node_id = str(manifest_or_node_id or "").strip()
        if not node_id:
            raise Phase3NodeIntegrationError("replay_node_id_missing")
        manifest_path = node_dir(phase3_root(output_dir), node_id) / "NODE_MANIFEST.json"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"replay_manifest_unreadable:{node_id}:{type(exc).__name__}"
            ) from exc
        if not isinstance(value, Mapping):
            raise Phase3NodeIntegrationError(f"replay_manifest_invalid:{node_id}")
        manifest = dict(value)
    if str(manifest.get("state") or "") != "committed":
        raise Phase3NodeIntegrationError(
            f"replay_node_not_committed:{manifest.get('node_id') or '<missing>'}:"
            f"{manifest.get('state')}"
        )
    effective_stale_timeout = (
        stale_lock_timeout if stale_lock_timeout is not None else stale_timeout
    )
    failure_path, failure_sha256, _failure = _matching_failure_receipt(
        phase3_root(output_dir),
        manifest,
    )
    effective_lock_timeout = (
        0.0
        if lock_timeout is None and effective_stale_timeout is not None
        else (5.0 if lock_timeout is None else lock_timeout)
    )
    projection = project_committed_node(
        output_dir,
        manifest,
        root_filenames=root_filenames,
        lock_timeout=effective_lock_timeout,
        stale_lock_timeout=effective_stale_timeout,
    )
    if failure_path is not None and failure_sha256:
        root = phase3_root(output_dir)
        journal_path = _projection_journal_path(root)
        try:
            journal_value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            journal_value = {}
        _write_projection_recovery_receipt(
            root,
            manifest,
            failure_path=failure_path,
            failure_sha256=failure_sha256,
            journal=journal_value if isinstance(journal_value, Mapping) else {},
        )
    return projection


# Explicit aliases keep recovery call sites readable while preserving one
# implementation and one idempotency contract.
replay_committed_node_outputs = replay_committed_node_projection
replay_projection = replay_committed_node_projection


def _archive_node_root_projections_for_rebuild_new(
    output_dir: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    history_path: str | os.PathLike[str],
    *,
    root_filenames: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Archive one node's legacy root projections before a direct rebuild.

    Every matching index row and root byte is checked while holding the
    phase3-root lock.  Root files are first copied atomically into the already
    created node history directory, then the index is atomically rewritten
    without those rows, and only then are the old root files removed.  An
    unknown or mismatched file aborts before any copy and remains untouched.
    """

    root = phase3_root(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    node_id = str(manifest.get("node_id") or "").strip()
    manifest_hash = str(manifest.get("manifest_sha256") or "").strip()
    if not node_id or not manifest_hash:
        raise Phase3NodeIntegrationError("root_projection_archive_manifest_identity_missing")
    if str(manifest.get("state") or "") not in {"committed", "failed"}:
        raise Phase3NodeIntegrationError(
            f"root_projection_archive_manifest_state_invalid:{node_id}"
        )
    calculated_manifest_hash = _hash_payload(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    if calculated_manifest_hash != manifest_hash:
        raise Phase3NodeIntegrationError(
            f"root_projection_archive_manifest_hash_mismatch:{node_id}"
        )
    try:
        history = Path(history_path).expanduser().resolve(strict=False)
        history_root = (node_dir(root, node_id) / "_history").resolve(strict=False)
        history.relative_to(history_root)
        if history == history_root or history.parent != history_root:
            raise ValueError("history_must_be_direct_attempt_child")
    except (OSError, ValueError) as exc:
        raise Phase3NodeIntegrationError(
            f"root_projection_archive_history_path_invalid:{node_id}"
        ) from exc
    root_archive_dir = history / "root_projections"
    journal_path = root / ROOT_PROJECTION_ARCHIVE_JOURNAL_FILENAME

    with _root_projection_lock(root):
        index = _read_projection_index(root)
        projections = index["projections"]
        node_rows: list[tuple[str, Mapping[str, Any]]] = []
        for filename, row in projections.items():
            if isinstance(row, Mapping) and str(row.get("node_id") or "") == node_id:
                node_rows.append((str(filename), row))
        journal_mapping = _mapping_from_matching_journal(root, manifest)
        journal_mapping.update(dict(root_filenames or {}))
        expected_filenames = {
            str(journal_mapping.get(str(row.get("role") or "")) or row.get("role") or "")
            for row in manifest.get("outputs") or []
            if isinstance(row, Mapping)
        }
        if not node_rows:
            # A known P3D payload without its index is an interrupted
            # projection, not an empty projection.  Leave it untouched and
            # force the caller through the explicit projection repair path.
            if any((root / filename).exists() for filename in expected_filenames if filename):
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{node_id}:missing_index"
                )
            return {
                "node_id": node_id,
                "manifest_sha256": manifest_hash,
                "state": "no_root_projections",
                "journal_path": str(journal_path),
                "root_projection_archive_path": str(root_archive_dir),
                "archived_projections": [],
            }

        output_hashes = {
            str(row.get("sha256") or "")
            for row in manifest.get("outputs") or []
            if isinstance(row, Mapping) and str(row.get("sha256") or "")
        }
        indexed_filenames = {filename for filename, _row in node_rows}
        missing_indexed_names = [
            filename
            for filename in expected_filenames
            if filename and (root / filename).exists() and filename not in indexed_filenames
        ]
        if missing_indexed_names:
            raise RootProjectionConflict(
                "root_projection_archive_conflict:missing_index:"
                + ",".join(sorted(missing_indexed_names))
            )
        entries: list[dict[str, Any]] = []
        for filename, row in node_rows:
            if (
                not filename
                or filename in {".", ".."}
                or Path(filename).name != filename
                or "/" in filename
                or "\\" in filename
                or filename not in expected_filenames
            ):
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:filename_invalid"
                )
            if (
                str(row.get("manifest_sha256") or "") != manifest_hash
                or str(row.get("output_sha256") or "") not in output_hashes
            ):
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:manifest_or_output_mismatch"
                )
            source = root / filename
            if source.is_symlink() or not source.is_file():
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:root_payload_missing"
                )
            expected_hash = str(row.get("output_sha256") or "")
            actual_hash = sha256_file(source)
            if actual_hash != expected_hash:
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:content_mismatch"
                )
            destination = root_archive_dir / filename
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:history_target_invalid"
                    )
                if sha256_file(destination) != expected_hash:
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:history_target_mismatch"
                    )
            entries.append({
                "filename": filename,
                "source": str(source),
                "destination": str(destination),
                "output_sha256": expected_hash,
                "state": "pending",
            })

        root_archive_dir.mkdir(parents=True, exist_ok=True)
        journal: dict[str, Any] = {
            "schema_version": "optomind.upgrade3.phase3_root_projection_archive_transaction.v1",
            "transaction_id": uuid.uuid4().hex,
            "state": "prepared",
            "node_id": node_id,
            "manifest_sha256": manifest_hash,
            "source_manifest": dict(manifest),
            "source_manifest_sha256": manifest_hash,
            "source_projection_index": {
                filename: dict(row) for filename, row in node_rows
            },
            "history_path": str(history),
            "root_projection_archive_path": str(root_archive_dir),
            "entries": entries,
            "journal_path": str(journal_path),
            "index_state": "pending",
            "index_plan": {
                "remove_filenames": [filename for filename, _row in node_rows],
                "state": "pending",
            },
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        try:
            atomic_write_json(journal_path, journal)
            _journal_update(journal_path, journal, state="copying")
            for entry in entries:
                source = Path(entry["source"])
                destination = Path(entry["destination"])
                if destination.exists():
                    entry["state"] = "already_archived"
                else:
                    _atomic_copy(source, destination)
                    entry["state"] = "archived"
                _journal_update(journal_path, journal)
            updated_index = dict(index)
            updated_index["projections"] = {
                filename: row
                for filename, row in projections.items()
                if not (
                    isinstance(row, Mapping)
                    and str(row.get("node_id") or "") == node_id
                )
            }
            atomic_write_json(_projection_path(root), updated_index)
            journal["index_state"] = "committed"
            journal["index_plan"] = {
                **dict(journal.get("index_plan") or {}),
                "state": "committed",
            }
            _journal_update(journal_path, journal, state="indexed")
            for entry in entries:
                source = Path(entry["source"])
                try:
                    _remove_root_projection_payload(source)
                except FileNotFoundError:
                    pass
                entry["state"] = "removed"
                _journal_update(journal_path, journal)
            receipt = {
                "schema_version": "optomind.upgrade3.phase3_root_projection_archive_receipt.v1",
                "state": "committed",
                "node_id": node_id,
                "manifest_sha256": manifest_hash,
                "history_path": str(history),
                "root_projection_archive_path": str(root_archive_dir),
                "transaction_id": journal["transaction_id"],
                "archived_projections": [dict(entry) for entry in entries],
                "created_at": time.time(),
            }
            receipt_path = root_archive_dir / ROOT_PROJECTION_ARCHIVE_RECEIPT_FILENAME
            atomic_write_json(receipt_path, receipt)
            alias_receipt_path = root_archive_dir / ROOT_PROJECTION_ARCHIVE_RECEIPT_ALIAS_FILENAME
            if alias_receipt_path != receipt_path:
                atomic_write_json(alias_receipt_path, receipt)
            journal["receipt_path"] = str(receipt_path)
            _journal_update(journal_path, journal, state="committed")
            return {
                "node_id": node_id,
                "manifest_sha256": manifest_hash,
                "state": "archived",
                "history_path": str(history),
                "root_projection_archive_path": str(root_archive_dir),
                "archive_receipt_path": str(receipt_path),
                "journal_path": str(journal_path),
                "archived_projections": [dict(entry) for entry in entries],
            }
        except BaseException as exc:
            try:
                _journal_update(journal_path, journal, state="failed", failure=exc)
            except BaseException:
                pass
            raise


def _root_archive_result_from_journal(
    journal: Mapping[str, Any],
    *,
    receipt_path: Path,
) -> dict[str, Any]:
    return {
        "node_id": str(journal.get("node_id") or ""),
        "manifest_sha256": str(
            journal.get("manifest_sha256")
            or journal.get("source_manifest_sha256")
            or ""
        ),
        "state": "archived",
        "history_path": str(journal.get("history_path") or ""),
        "root_projection_archive_path": str(
            journal.get("root_projection_archive_path") or ""
        ),
        "archive_receipt_path": str(receipt_path),
        "journal_path": str(journal.get("journal_path") or ""),
        "archived_projections": [
            dict(row)
            for row in (journal.get("entries") or [])
            if isinstance(row, Mapping)
        ],
    }


def continue_node_root_projections_archive(
    output_dir: str | os.PathLike[str],
    manifest_or_node_id: Mapping[str, Any] | str,
    history_path: str | os.PathLike[str] | None = None,
    *,
    root_filenames: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Continue an interrupted root projection archive transaction."""

    root = phase3_root(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(manifest_or_node_id, Mapping):
        node_id = str(manifest_or_node_id.get("node_id") or "").strip()
        manifest = dict(manifest_or_node_id)
    else:
        node_id = str(manifest_or_node_id or "").strip()
        manifest = None
    if not node_id:
        raise Phase3NodeIntegrationError("root_projection_archive_node_id_missing")
    journal_path = root / ROOT_PROJECTION_ARCHIVE_JOURNAL_FILENAME
    with _root_projection_lock(root):
        try:
            journal_value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"root_projection_archive_journal_unreadable:{type(exc).__name__}"
            ) from exc
        if not isinstance(journal_value, Mapping):
            raise Phase3NodeIntegrationError("root_projection_archive_journal_invalid")
        journal = dict(journal_value)
        source_manifest = journal.get("source_manifest")
        if not isinstance(source_manifest, Mapping):
            raise RootProjectionConflict(
                f"root_projection_archive_conflict:{node_id}:source_manifest_missing"
            )
        expected_hash = str(
            journal.get("manifest_sha256")
            or journal.get("source_manifest_sha256")
            or ""
        )
        if (
            str(journal.get("node_id") or "") != node_id
            or str(source_manifest.get("node_id") or "") != node_id
            or str(source_manifest.get("manifest_sha256") or "") != expected_hash
            or _hash_payload(
                {key: value for key, value in source_manifest.items() if key != "manifest_sha256"}
            ) != expected_hash
        ):
            raise NodeValidationError(
                f"root_projection_archive_journal_identity_mismatch:{node_id}"
            )
        if manifest is not None and str(manifest.get("manifest_sha256") or "") != expected_hash:
            raise RootProjectionConflict(
                f"root_projection_archive_conflict:{node_id}:manifest_hash_mismatch"
            )
        history = Path(
            history_path or str(journal.get("history_path") or "")
        ).expanduser().resolve(strict=False)
        history_root = (node_dir(root, node_id) / "_history").resolve(strict=False)
        try:
            history.relative_to(history_root)
        except ValueError as exc:
            raise RootProjectionConflict(
                f"root_projection_archive_conflict:{node_id}:history_path_invalid"
            ) from exc
        if history == history_root or history.parent != history_root:
            raise RootProjectionConflict(
                f"root_projection_archive_conflict:{node_id}:history_path_invalid"
            )
        root_archive_dir = history / "root_projections"
        if not root_archive_dir.is_dir():
            raise NodeStateError(f"root_projection_archive_history_missing:{node_id}")
        if str(journal.get("state") or "") == "committed":
            receipt_path = Path(str(journal.get("receipt_path") or ""))
            if not receipt_path.is_file():
                receipt_path = root_archive_dir / ROOT_PROJECTION_ARCHIVE_RECEIPT_FILENAME
            if receipt_path.is_file():
                return _root_archive_result_from_journal(
                    journal,
                    receipt_path=receipt_path,
                )
        entries = [
            row for row in (journal.get("entries") or [])
            if isinstance(row, Mapping)
        ]
        if not entries:
            raise NodeValidationError(f"root_projection_archive_entries_missing:{node_id}")
        indexed_plan = {
            str(filename): dict(row)
            for filename, row in (journal.get("source_projection_index") or {}).items()
            if isinstance(row, Mapping)
        }
        current_index = _read_projection_index(root)
        projections = current_index["projections"]
        for filename, row in projections.items():
            if not isinstance(row, Mapping) or str(row.get("node_id") or "") != node_id:
                continue
            expected_row = indexed_plan.get(str(filename))
            if expected_row is None:
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:unknown_node_entry"
                )
            if dict(row) != expected_row:
                raise RootProjectionConflict(
                    f"root_projection_archive_conflict:{filename}:index_mismatch"
                )
        try:
            journal["state"] = "continuing"
            _journal_update(journal_path, journal)
            for entry in entries:
                filename = str(entry.get("filename") or "")
                expected_output_hash = str(entry.get("output_sha256") or "")
                source = Path(str(entry.get("source") or "")).expanduser().resolve(strict=False)
                destination = Path(str(entry.get("destination") or "")).expanduser().resolve(strict=False)
                if source != (root / filename).resolve(strict=False) or destination != (
                    root_archive_dir / filename
                ).resolve(strict=False):
                    raise NodeValidationError(
                        f"root_projection_archive_plan_mismatch:{node_id}:{filename}"
                    )
                source_exists = source.is_file()
                destination_exists = destination.is_file()
                if source.exists() and not source_exists:
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:source_not_regular"
                    )
                if destination.exists() and not destination_exists:
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:destination_not_regular"
                    )
                if source_exists and sha256_file(source) != expected_output_hash:
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:source_hash_mismatch"
                    )
                if destination_exists and sha256_file(destination) != expected_output_hash:
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{filename}:destination_hash_mismatch"
                    )
                if not destination_exists:
                    if not source_exists:
                        raise NodeValidationError(
                            f"root_projection_archive_payload_missing:{node_id}:{filename}"
                        )
                    _atomic_copy(source, destination)
                    if not destination.is_file() or sha256_file(destination) != expected_output_hash:
                        raise NodeValidationError(
                            f"root_projection_archive_destination_verify_failed:{filename}"
                        )
                entry["state"] = "archived"
                _journal_update(journal_path, journal)

            remove_names = [
                str(item)
                for item in (journal.get("index_plan") or {}).get("remove_filenames", [])
                if str(item)
            ]
            if not remove_names:
                remove_names = [str(entry.get("filename") or "") for entry in entries]
            current_index = _read_projection_index(root)
            projections = current_index["projections"]
            active_node_names = {
                str(filename)
                for filename, row in projections.items()
                if isinstance(row, Mapping) and str(row.get("node_id") or "") == node_id
            }
            unknown_active = active_node_names - set(remove_names)
            if unknown_active:
                raise RootProjectionConflict(
                    "root_projection_archive_conflict:unknown_index_entries:"
                    + ",".join(sorted(unknown_active))
                )
            if active_node_names:
                updated_index = dict(current_index)
                updated_index["projections"] = {
                    filename: row
                    for filename, row in projections.items()
                    if filename not in set(remove_names)
                }
                atomic_write_json(_projection_path(root), updated_index)
            journal["index_state"] = "committed"
            journal["index_plan"] = {
                **dict(journal.get("index_plan") or {}),
                "state": "committed",
            }
            _journal_update(journal_path, journal, state="indexed")

            for entry in entries:
                source = Path(str(entry.get("source") or "")).expanduser().resolve(strict=False)
                expected_output_hash = str(entry.get("output_sha256") or "")
                if source.is_file():
                    if sha256_file(source) != expected_output_hash:
                        raise RootProjectionConflict(
                            f"root_projection_archive_conflict:{entry.get('filename')}:root_delete_hash_mismatch"
                        )
                    _remove_root_projection_payload(source)
                elif source.exists():
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{entry.get('filename')}:root_delete_not_regular"
                    )
                entry["state"] = "removed"
                _journal_update(journal_path, journal)
            receipt_path = root_archive_dir / ROOT_PROJECTION_ARCHIVE_RECEIPT_FILENAME
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, Mapping) or str(receipt.get("manifest_sha256") or "") != str(journal.get("manifest_sha256") or ""):
                    raise RootProjectionConflict(
                        f"root_projection_archive_conflict:{node_id}:receipt_mismatch"
                    )
            else:
                receipt = {
                    "schema_version": "optomind.upgrade3.phase3_root_projection_archive_receipt.v1",
                    "state": "committed",
                    "node_id": node_id,
                    "manifest_sha256": str(journal.get("manifest_sha256") or ""),
                    "history_path": str(history),
                    "root_projection_archive_path": str(root_archive_dir),
                    "transaction_id": journal["transaction_id"],
                    "archived_projections": [dict(entry) for entry in entries],
                    "created_at": time.time(),
                }
                atomic_write_json(receipt_path, receipt)
            alias_receipt_path = root_archive_dir / ROOT_PROJECTION_ARCHIVE_RECEIPT_ALIAS_FILENAME
            if alias_receipt_path != receipt_path and not alias_receipt_path.exists():
                atomic_write_json(alias_receipt_path, receipt)
            journal["receipt_path"] = str(receipt_path)
            journal["journal_path"] = str(journal_path)
            _journal_update(journal_path, journal, state="committed")
            return _root_archive_result_from_journal(
                journal,
                receipt_path=receipt_path,
            )
        except BaseException as exc:
            try:
                _journal_update(journal_path, journal, state="failed", failure=exc)
            except BaseException:
                pass
            raise


def archive_node_root_projections_for_rebuild(
    output_dir: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    history_path: str | os.PathLike[str],
    *,
    root_filenames: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Archive root projections, continuing a matching incomplete journal."""

    root = phase3_root(output_dir)
    node_id = str(manifest.get("node_id") or "").strip()
    manifest_hash = str(manifest.get("manifest_sha256") or "").strip()
    journal_path = root / ROOT_PROJECTION_ARCHIVE_JOURNAL_FILENAME
    if journal_path.is_file():
        try:
            journal_value = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"root_projection_archive_journal_unreadable:{type(exc).__name__}"
            ) from exc
        if not isinstance(journal_value, Mapping):
            raise Phase3NodeIntegrationError("root_projection_archive_journal_invalid")
        if (
            str(journal_value.get("node_id") or "") == node_id
            and str(journal_value.get("manifest_sha256") or "") == manifest_hash
            and str(journal_value.get("history_path") or "")
            == str(Path(history_path).expanduser().resolve(strict=False))
        ):
            return continue_node_root_projections_archive(
                output_dir,
                manifest,
                history_path,
                root_filenames=root_filenames,
            )
    return _archive_node_root_projections_for_rebuild_new(
        output_dir,
        manifest,
        history_path,
        root_filenames=root_filenames,
    )


# Alias naming the operation as a root-scoped rebuild helper.
archive_root_projections_for_rebuild = archive_node_root_projections_for_rebuild
resume_node_root_projections_archive = continue_node_root_projections_archive
continue_root_projection_archive = continue_node_root_projections_archive


def commit_node_artifacts(
    output_dir: str | os.PathLike[str],
    *,
    node_id: str,
    generation_id: str,
    attempt_id: str,
    inputs: Iterable[Mapping[str, Any]],
    dependencies: Iterable[Mapping[str, Any]],
    fingerprints: Mapping[str, Any],
    outputs: Mapping[str, Mapping[str, Any]],
    root_filenames: Mapping[str, str] | None = None,
    cost_receipt: Mapping[str, Any] | None = None,
    validation_receipt: Mapping[str, Any] | None = None,
    fingerprint_sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write node-local outputs, commit the manifest, then project to root."""

    root = phase3_root(output_dir)
    target = node_dir(root, node_id)
    target.mkdir(parents=True, exist_ok=True)
    existing_manifest_path = target / "NODE_MANIFEST.json"
    if existing_manifest_path.is_file():
        try:
            existing_value = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"existing_node_manifest_unreadable:{node_id}:{type(exc).__name__}"
            ) from exc
        if (
            isinstance(existing_value, Mapping)
            and str(existing_value.get("state") or "") == "committed"
        ):
            # A compatibility projection retry must consume the immutable
            # committed outputs through the explicit replay API.  Never write
            # fresh payloads into a committed node directory before stage_node
            # has had a chance to reject the attempt.
            raise ProjectionError(
                "node_already_committed_use_projection_replay",
                manifest=existing_value,
            )
    output_rows: list[dict[str, Any]] = []
    for role, item in outputs.items():
        if not isinstance(item, Mapping):
            raise Phase3NodeIntegrationError(f"node_output_payload_invalid:{node_id}:{role}")
        payload = item.get("payload")
        filename = str(item.get("filename") or role)
        if Path(filename).name != filename:
            raise Phase3NodeIntegrationError(f"node_output_filename_invalid:{node_id}:{filename}")
        output_path = atomic_write_json(target / filename, payload)
        output_rows.append({
            "role": str(role),
            "path": str(output_path),
            "sha256": sha256_file(output_path),
            "bytes": output_path.stat().st_size,
        })
    final_validation_receipt = dict(
        validation_receipt or {"status": "phase3_node_output_validated"}
    )
    if fingerprint_sources is not None:
        final_validation_receipt["fingerprint_sources"] = dict(fingerprint_sources)
    staged = stage_node(
        root,
        node_id,
        generation_id,
        attempt_id,
        inputs=list(inputs),
        dependencies=list(dependencies),
        fingerprints=dict(fingerprints),
        outputs=output_rows,
        cost_receipt=dict(cost_receipt or {"status": "phase3_node_zero_cost"}),
        validation_receipt=final_validation_receipt,
    )
    del staged
    running = False
    try:
        mark_running(root, node_id, generation_id, attempt_id)
        running = True
        manifest = commit_node(root, node_id, generation_id, attempt_id)
    except Exception as exc:
        if running:
            try:
                fail_node(
                    root,
                    node_id,
                    generation_id,
                    attempt_id,
                    failure={
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:500],
                    },
                )
            except Exception:
                # Preserve the original commit error; the manifest remains
                # staged/running evidence if a fail transition itself cannot
                # be written safely.
                pass
        raise
    project_committed_node(output_dir, manifest, root_filenames=root_filenames)
    # The node states what it proved, in the node's own directory: the validation
    # gate is derived from the committed manifest and never from a claim about it.
    # The committed manifest is returned untouched: its own manifest_sha256 covers
    # its exact body, so attaching anything to it would break the projection
    # contract.  The node's validation level is a separate, derived verdict.
    return manifest


def node_validation_gate(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The level a committed node's own manifest supports (SM09).

    Derived from the manifest, never claimed by the node: it is what the node
    proved, expressed in the same scale the queue uses.
    """

    from optomind_research.runtime.upgrade3 import validation_receipts as _validation

    return _validation.gate_node_manifest(manifest)


def record_failed_node(
    output_dir: str | os.PathLike[str],
    *,
    node_id: str,
    generation_id: str,
    attempt_id: str,
    inputs: Iterable[Mapping[str, Any]],
    dependencies: Iterable[Mapping[str, Any]],
    fingerprints: Mapping[str, Any],
    failure: Mapping[str, Any],
    fingerprint_sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a running node and fail it without projecting any output."""

    root = phase3_root(output_dir)
    existing_path = node_dir(root, node_id) / "NODE_MANIFEST.json"
    if existing_path.is_file():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Phase3NodeIntegrationError(
                f"existing_node_manifest_unreadable:{node_id}:{type(exc).__name__}"
            ) from exc
        if isinstance(existing, Mapping) and existing.get("state") == "committed":
            projection_error = Phase3NodeIntegrationError(
                "committed_node_projection_failure_cannot_transition_to_failed"
            )
            receipt_path = _write_projection_failure_receipt(
                root,
                existing,
                projection_error,
            )
            raise ProjectionError(
                "committed_node_projection_failure_recorded",
                manifest=existing,
                receipt_path=receipt_path,
            )
    validation_receipt: dict[str, Any] = {"status": "phase3_node_failure_recorded"}
    if fingerprint_sources is not None:
        validation_receipt["fingerprint_sources"] = dict(fingerprint_sources)
    staged = stage_node(
        root,
        node_id,
        generation_id,
        attempt_id,
        inputs=list(inputs),
        dependencies=list(dependencies),
        fingerprints=dict(fingerprints),
        outputs=[],
        cost_receipt={"status": "phase3_node_zero_cost"},
        validation_receipt=validation_receipt,
    )
    del staged
    mark_running(root, node_id, generation_id, attempt_id)
    return fail_node(
        root,
        node_id,
        generation_id,
        attempt_id,
        failure=dict(failure),
    )


__all__ = [
    "NODE_INPUT_SNAPSHOT_SCHEMA",
    "ROOT_PROJECTION_FAILURE_FILENAME",
    "ROOT_PROJECTION_JOURNAL_FILENAME",
    "ROOT_PROJECTION_LOCK_FILENAME",
    "ROOT_PROJECTION_RECOVERY_FILENAME",
    "ROOT_PROJECTION_ARCHIVE_JOURNAL_FILENAME",
    "ROOT_PROJECTION_ARCHIVE_RECEIPT_FILENAME",
    "ROOT_PROJECTION_ARCHIVE_RECEIPT_ALIAS_FILENAME",
    "PHASE3_NEW_GENERATION_REQUIRED_FILENAME",
    "ROOT_PROJECTIONS_FILENAME",
    "ROOT_PROJECTIONS_SCHEMA",
    "NO_LLM_CONTRACT_SCHEMA",
    "CANONICAL_NO_LLM_CONTRACT_HASH",
    "NO_LLM_CONTRACT_HASH",
    "Phase3NodeIntegrationError",
    "ProjectionError",
    "RootProjectionConflict",
    "RootProjectionLockError",
    "atomic_write_json",
    "build_node_fingerprint_details",
    "build_node_fingerprints",
    "build_no_llm_contract",
    "archive_node_root_projections_for_rebuild",
    "archive_root_projections_for_rebuild",
    "continue_node_root_projections_archive",
    "continue_root_projection_archive",
    "resume_node_root_projections_archive",
    "canonical_no_llm_contract",
    "canonical_no_llm_contract_hash",
    "commit_node_artifacts",
    "validate_committed_root_projection",
    "validate_committed_root_projections",
    "dependency_row",
    "input_row",
    "input_rows_from_manifest",
    "phase3_root",
    "project_committed_node",
    "replay_committed_node_outputs",
    "replay_committed_node_projection",
    "replay_projection",
    "record_failed_node",
    "sha256_file",
    "write_phase3_new_generation_required",
    "write_new_generation_required_receipt",
    "write_node_input_snapshot",
    "validate_node_input_snapshot_for_resume",
    "validate_phase3_input_snapshot_for_resume",
    "no_llm_contract_hash",
]
