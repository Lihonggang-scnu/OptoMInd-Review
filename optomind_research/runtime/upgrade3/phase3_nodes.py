"""Persistent state primitives for the four Phase 3 recovery nodes.

This module deliberately contains no Phase 3 business logic.  It owns only
the node manifest, atomic state transitions, hash verification, and the
selective-recovery decision table used by a later integration layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


NODE_MANIFEST_SCHEMA = "optomind.upgrade3.phase3_node_manifest.v1"
RECOVERY_PLAN_SCHEMA = "optomind.upgrade3.phase3_node_recovery_plan.v1"
NODE_MANIFEST_FILENAME = "NODE_MANIFEST.json"
NODE_LOCK_FILENAME = ".NODE_MANIFEST.lock"
NODE_ARCHIVE_JOURNAL_FILENAME = "ARCHIVE_TRANSACTION.json"
ARCHIVE_RECEIPT_FILENAME = "ARCHIVE_RECEIPT.json"
NODE_IDS = (
    "P3A_CLAIM_POOL",
    "P3B_CLAIM_BINDING",
    "P3C_COVERAGE",
    "P3D_ACCEPTANCE_HANDOFF",
)
NODE_STATES = ("staged", "running", "committed", "failed", "superseded")
_ALLOWED_TRANSITIONS = {
    "staged": {"running"},
    "running": {"committed", "failed"},
    "committed": {"superseded"},
    "failed": {"superseded"},
    "superseded": set(),
}
NODE_DEPENDENCIES = {
    "P3A_CLAIM_POOL": (),
    "P3B_CLAIM_BINDING": ("P3A_CLAIM_POOL",),
    "P3C_COVERAGE": ("P3B_CLAIM_BINDING",),
    "P3D_ACCEPTANCE_HANDOFF": ("P3C_COVERAGE",),
}
_FINGERPRINT_KEYS = ("code", "prompt", "model", "policy", "schema")
_MANIFEST_FIELDS = {
    "schema_version",
    "node_id",
    "generation_id",
    "attempt_id",
    "state",
    "inputs",
    "dependencies",
    "fingerprints",
    "outputs",
    "cost_receipt",
    "validation_receipt",
    "reused_from",
    "failure",
    "created_at",
    "updated_at",
    "manifest_sha256",
}


class Phase3NodeError(RuntimeError):
    """Base error for fail-closed node operations."""


class NodeStateError(Phase3NodeError):
    """Raised for an illegal node state or generation/attempt transition."""


class NodeValidationError(Phase3NodeError):
    """Raised when a committed manifest or any referenced asset drifts."""


class NodeLockError(NodeStateError):
    """Raised when a node's compare-and-swap lock cannot be acquired safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_node_id(node_id: str) -> str:
    value = str(node_id or "").strip()
    if value not in NODE_IDS:
        raise Phase3NodeError(f"unknown_phase3_node:{value or '<missing>'}")
    return value


def _phase3_root(root: str | os.PathLike[str]) -> Path:
    value = Path(root).expanduser().resolve(strict=False)
    return value if value.name == "phase3_argument_orchestration" else value / "phase3_argument_orchestration"


def node_dir(root: str | os.PathLike[str], node_id: str) -> Path:
    """Return ``phase3_argument_orchestration/nodes/<node_id>``."""

    return _phase3_root(root) / "nodes" / _validate_node_id(node_id)


def _manifest_path(root: str | os.PathLike[str], node_id: str) -> Path:
    return node_dir(root, node_id) / NODE_MANIFEST_FILENAME


def _lock_path(root: str | os.PathLike[str], node_id: str) -> Path:
    return node_dir(root, node_id) / NODE_LOCK_FILENAME


def _path_inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _resolve_path(path: Any, *, base: Path) -> Path:
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    data = _canonical_bytes(payload)
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _atomic_move(source: Path, destination: Path) -> None:
    """Atomically move one archive member, preserving a failure boundary."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(source), str(destination))


def _pid_is_alive(pid: Any) -> bool:
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return True
    if process_id <= 0:
        return True
    if process_id == os.getpid():
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # An indeterminate process state must stay fail-closed.
        return True
    return True


def _read_lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not value.get("token") or not value.get("pid") or not value.get("created_at"):
        return None
    return value


def _acquire_node_lock(
    root: str | os.PathLike[str],
    node_id: str,
    *,
    timeout: float = 0.0,
) -> tuple[Path, str]:
    """Create an exclusive node lock; stale locks are never guessed away.

    A positive timeout is explicit permission to wait and, after the timeout
    age has elapsed, reclaim a lock only when its recorded owner process is
    provably gone.  Malformed or otherwise ambiguous locks remain in place.
    """

    target_dir = node_dir(root, node_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / NODE_LOCK_FILENAME
    try:
        wait_timeout = max(0.0, float(timeout))
    except (TypeError, ValueError):
        raise NodeLockError(f"invalid_node_lock_timeout:{timeout!r}")
    deadline = time.monotonic() + wait_timeout
    while True:
        token = uuid.uuid4().hex
        owner = {
            "pid": os.getpid(),
            "token": token,
            "created_at": _utc_now(),
        }
        try:
            descriptor = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            existing = _read_lock_owner(lock_path)
            now = time.monotonic()
            if wait_timeout > 0 and now < deadline:
                time.sleep(min(0.01, max(0.001, deadline - now)))
                continue
            if (
                wait_timeout > 0
                and existing is not None
                and not _pid_is_alive(existing.get("pid"))
            ):
                try:
                    age = max(0.0, time.time() - lock_path.stat().st_mtime)
                except OSError:
                    age = 0.0
                if age >= wait_timeout:
                    # Recheck the owner token immediately before unlinking so
                    # a replacement lock cannot be removed by a late waiter.
                    current = _read_lock_owner(lock_path)
                    if current is not None and current.get("token") == existing.get("token"):
                        try:
                            lock_path.unlink()
                        except FileNotFoundError:
                            pass
                        continue
            reason = "malformed" if existing is None else f"pid={existing.get('pid')}"
            raise NodeLockError(f"node_lock_busy:{node_id}:{reason}")
        except OSError as exc:
            raise NodeLockError(
                f"node_lock_acquire_failed:{node_id}:{type(exc).__name__}"
            ) from exc
        try:
            payload = _canonical_bytes(owner)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # Leave an ambiguous lock in place if writing its owner record
            # fails; another process must not guess whether it is safe.
            raise
        return lock_path, token


def _release_node_lock(path: Path, token: str) -> None:
    owner = _read_lock_owner(path)
    if owner is None or owner.get("token") != token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _node_lock(
    root: str | os.PathLike[str],
    node_id: str,
    *,
    timeout: float = 0.0,
) -> Iterator[None]:
    lock_path, token = _acquire_node_lock(root, node_id, timeout=timeout)
    try:
        yield
    finally:
        _release_node_lock(lock_path, token)


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return _sha256_bytes(_canonical_bytes(body))


def _normalize_inputs(
    inputs: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    base: Path,
) -> list[dict[str, Any]]:
    if inputs is None:
        rows: list[Any] = []
    elif isinstance(inputs, Mapping):
        rows = []
        for role, value in inputs.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("role", str(role))
            else:
                row = {"role": str(role), "path": value}
            rows.append(row)
    else:
        rows = list(inputs)
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise Phase3NodeError(f"input_not_object:{index}")
        role = str(raw.get("role") or "").strip()
        path = _resolve_path(raw.get("path"), base=base)
        expected = str(raw.get("sha256") or "").strip()
        if not role or not str(raw.get("path") or "").strip() or not expected:
            raise Phase3NodeError(f"input_manifest_fields_missing:{index}")
        if not path.is_file():
            raise Phase3NodeError(f"input_missing:{path}")
        actual = _sha256_file(path)
        if actual != expected:
            raise NodeValidationError(
                f"input_hash_mismatch:{role}:expected={expected}:actual={actual}"
            )
        normalized.append({"role": role, "path": str(path), "sha256": expected})
    return normalized


def _normalize_outputs(
    outputs: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    node_path: Path,
) -> list[dict[str, Any]]:
    if outputs is None:
        rows: list[Any] = []
    elif isinstance(outputs, Mapping):
        rows = []
        for role, value in outputs.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("role", str(role))
            else:
                row = {"role": str(role), "path": value}
            rows.append(row)
    else:
        rows = list(outputs)
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise Phase3NodeError(f"output_not_object:{index}")
        role = str(raw.get("role") or "").strip()
        path = _resolve_path(raw.get("path"), base=node_path)
        expected = str(raw.get("sha256") or "").strip()
        if not role or not str(raw.get("path") or "").strip() or not expected:
            raise Phase3NodeError(f"output_manifest_fields_missing:{index}")
        if not _path_inside(path, node_path):
            raise Phase3NodeError(f"output_outside_node_dir:{path}")
        normalized.append({
            "role": role,
            "path": str(path),
            "sha256": expected,
            "bytes": int(raw.get("bytes") or 0),
        })
    return normalized


def _normalize_dependencies(
    dependencies: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None,
    *,
    node_id: str,
    generation_id: str,
) -> list[dict[str, str]]:
    if dependencies is None:
        rows: list[Any] = []
    elif isinstance(dependencies, Mapping):
        rows = []
        for node, value in dependencies.items():
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("node_id", str(node))
            else:
                row = {"node_id": str(node), "manifest_sha256": value}
            rows.append(row)
    else:
        rows = list(dependencies)
    node_id = _validate_node_id(node_id)
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    node_index = NODE_IDS.index(node_id)
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise Phase3NodeError(f"dependency_not_object:{index}")
        dependency_node = _validate_node_id(str(raw.get("node_id") or ""))
        dependency_hash = str(raw.get("manifest_sha256") or "").strip()
        if not dependency_hash:
            raise Phase3NodeError(f"dependency_manifest_hash_missing:{dependency_node}")
        dependency_generation = str(raw.get("generation_id") or "").strip()
        dependency_attempt = str(raw.get("attempt_id") or "").strip()
        if not dependency_generation or not dependency_attempt:
            raise Phase3NodeError(
                f"dependency_generation_attempt_missing:{dependency_node}"
            )
        if dependency_generation != str(generation_id):
            raise NodeValidationError(
                f"dependency_generation_mismatch:{dependency_node}:"
                f"expected={generation_id}:actual={dependency_generation}"
            )
        if dependency_node in seen:
            raise NodeValidationError(f"dependency_duplicate:{dependency_node}")
        if NODE_IDS.index(dependency_node) >= node_index:
            raise NodeValidationError(
                f"dependency_not_upstream:{node_id}:{dependency_node}"
            )
        seen.add(dependency_node)
        normalized.append({
            "node_id": dependency_node,
            "manifest_sha256": dependency_hash,
            "generation_id": dependency_generation,
            "attempt_id": dependency_attempt,
        })
    required = set(NODE_DEPENDENCIES[node_id])
    missing_required = sorted(required - seen)
    if missing_required:
        raise NodeValidationError(
            f"dependency_required_missing:{node_id}:{','.join(missing_required)}"
        )
    return normalized


def _normalize_fingerprints(fingerprints: Mapping[str, Any] | None) -> dict[str, str]:
    source = fingerprints if isinstance(fingerprints, Mapping) else {}
    return {key: str(source.get(key) or "") for key in _FINGERPRINT_KEYS}


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise NodeValidationError(f"manifest_unreadable:{path}:{type(exc).__name__}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeValidationError(f"manifest_invalid_json:{path}:{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise NodeValidationError(f"manifest_not_object:{path}")
    return value


def _assert_generation_attempt(
    manifest: Mapping[str, Any], generation_id: str, attempt_id: str
) -> None:
    if str(manifest.get("generation_id") or "") != str(generation_id):
        raise NodeStateError(
            f"generation_mismatch:expected={generation_id}:actual={manifest.get('generation_id')}"
        )
    if str(manifest.get("attempt_id") or "") != str(attempt_id):
        raise NodeStateError(
            f"attempt_mismatch:expected={attempt_id}:actual={manifest.get('attempt_id')}"
        )


def _transition(
    root: str | os.PathLike[str],
    node_id: str,
    *,
    generation_id: str,
    attempt_id: str,
    target_state: str,
    failure: Mapping[str, Any] | None = None,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    node_id = _validate_node_id(node_id)
    if target_state not in NODE_STATES:
        raise NodeStateError(f"unknown_node_state:{target_state}")
    path = _manifest_path(root, node_id)
    with _node_lock(root, node_id, timeout=lock_timeout):
        manifest = _read_manifest(path)
        _validate_manifest_shape(manifest, node_dir(root, node_id), node_id)
        _assert_generation_attempt(manifest, generation_id, attempt_id)
        current_state = str(manifest.get("state") or "")
        if target_state not in _ALLOWED_TRANSITIONS.get(current_state, set()):
            raise NodeStateError(
                f"illegal_state_transition:{current_state}->{target_state}:{node_id}"
            )
        manifest = dict(manifest)
        manifest["state"] = target_state
        manifest["updated_at"] = _utc_now()
        if target_state == "failed":
            manifest["failure"] = dict(failure or {"reason": "unspecified_failure"})
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        _atomic_write_json(path, manifest)
        return manifest


def _validate_manifest_shape(
    manifest: Mapping[str, Any],
    node_path: Path,
    expected_node_id: str | None = None,
    *,
    require_commit_fields: bool = False,
) -> None:
    missing = sorted(_MANIFEST_FIELDS - set(manifest))
    if missing:
        raise NodeValidationError(
            f"manifest_fields_missing:{','.join(missing)}"
        )
    if manifest.get("schema_version") != NODE_MANIFEST_SCHEMA:
        raise NodeValidationError(
            f"manifest_schema_mismatch:{manifest.get('schema_version')!r}"
        )
    try:
        manifest_node_id = _validate_node_id(str(manifest.get("node_id") or ""))
    except Phase3NodeError as exc:
        raise NodeValidationError(f"manifest_node_id_invalid:{manifest.get('node_id')!r}") from exc
    if expected_node_id is not None and manifest_node_id != _validate_node_id(expected_node_id):
        raise NodeValidationError(
            f"manifest_node_id_mismatch:expected={expected_node_id}:actual={manifest_node_id}"
        )
    for field in ("generation_id", "attempt_id", "created_at", "updated_at"):
        if not str(manifest.get(field) or "").strip():
            raise NodeValidationError(f"manifest_{field}_invalid")
    if str(manifest.get("state") or "") not in NODE_STATES:
        raise NodeValidationError(f"manifest_state_invalid:{manifest.get('state')!r}")
    fingerprints = manifest.get("fingerprints")
    if not isinstance(fingerprints, Mapping) or any(
        key not in fingerprints for key in _FINGERPRINT_KEYS
    ):
        raise NodeValidationError("manifest_fingerprints_invalid")
    for field in ("cost_receipt", "validation_receipt", "failure"):
        if not isinstance(manifest.get(field), Mapping):
            raise NodeValidationError(f"manifest_{field}_invalid")
    if not isinstance(manifest.get("inputs"), list):
        raise NodeValidationError("manifest_inputs_invalid")
    if not isinstance(manifest.get("dependencies"), list):
        raise NodeValidationError("manifest_dependencies_invalid")
    if not isinstance(manifest.get("outputs"), list):
        raise NodeValidationError("manifest_outputs_invalid")
    if require_commit_fields:
        missing_fingerprints = [
            key for key in _FINGERPRINT_KEYS
            if not str(fingerprints.get(key) or "").strip()
        ]
        if missing_fingerprints:
            raise NodeValidationError(
                "manifest_commit_fingerprints_missing:"
                + ",".join(missing_fingerprints)
            )
        if not manifest.get("outputs"):
            raise NodeValidationError("manifest_commit_outputs_missing")
        for field in ("cost_receipt", "validation_receipt"):
            receipt = manifest.get(field)
            if (
                not isinstance(receipt, Mapping)
                or not isinstance(receipt.get("status"), str)
                or not receipt.get("status", "").strip()
            ):
                raise NodeValidationError(f"manifest_commit_{field}_status_missing")
    for row in manifest.get("outputs") or []:
        if not isinstance(row, Mapping):
            raise NodeValidationError("manifest_output_not_object")
        if (
            not str(row.get("role") or "").strip()
            or not str(row.get("path") or "").strip()
            or not str(row.get("sha256") or "").strip()
            or isinstance(row.get("bytes"), bool)
            or not isinstance(row.get("bytes"), int)
            or row.get("bytes") < 0
        ):
            raise NodeValidationError("manifest_output_fields_invalid")
        path = _resolve_path(row.get("path"), base=node_path)
        if not _path_inside(path, node_path):
            raise NodeValidationError(f"manifest_output_outside_node_dir:{path}")
    for row in manifest.get("inputs") or []:
        if not isinstance(row, Mapping):
            raise NodeValidationError("manifest_input_not_object")
        if (
            not str(row.get("role") or "").strip()
            or not str(row.get("path") or "").strip()
            or not str(row.get("sha256") or "").strip()
        ):
            raise NodeValidationError("manifest_input_fields_invalid")
    dependency_nodes: set[str] = set()
    current_node_index = NODE_IDS.index(manifest_node_id)
    current_generation = str(manifest.get("generation_id") or "")
    for row in manifest.get("dependencies") or []:
        if (
            not isinstance(row, Mapping)
            or not row.get("node_id")
            or not row.get("manifest_sha256")
            or not str(row.get("generation_id") or "").strip()
            or not str(row.get("attempt_id") or "").strip()
        ):
            raise NodeValidationError("manifest_dependency_invalid")
        try:
            dependency_node = _validate_node_id(str(row.get("node_id")))
        except (Phase3NodeError, OSError, TypeError, ValueError) as exc:
            raise NodeValidationError(
                f"manifest_dependency_node_invalid:{row.get('node_id')!r}"
            ) from exc
        if dependency_node in dependency_nodes:
            raise NodeValidationError(f"manifest_dependency_duplicate:{dependency_node}")
        if dependency_node == manifest_node_id:
            raise NodeValidationError(f"dependency_self:{manifest_node_id}")
        if NODE_IDS.index(dependency_node) >= current_node_index:
            raise NodeValidationError(
                f"dependency_not_upstream:{manifest_node_id}:{dependency_node}"
            )
        dependency_generation = str(row.get("generation_id") or "")
        if dependency_generation != current_generation:
            raise NodeValidationError(
                f"dependency_generation_mismatch:{dependency_node}:"
                f"expected={current_generation}:actual={dependency_generation}"
            )
        dependency_nodes.add(dependency_node)
    missing_required = sorted(set(NODE_DEPENDENCIES[manifest_node_id]) - dependency_nodes)
    if missing_required:
        raise NodeValidationError(
            f"dependency_required_missing:{manifest_node_id}:{','.join(missing_required)}"
        )
    expected_hash = str(manifest.get("manifest_sha256") or "")
    if not expected_hash or expected_hash != _manifest_hash(manifest):
        raise NodeValidationError(
            f"manifest_hash_mismatch:expected={expected_hash or '<missing>'}:actual={_manifest_hash(manifest)}"
        )


def stage_node(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    attempt_id: str,
    *,
    inputs: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    dependencies: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    fingerprints: Mapping[str, Any] | None = None,
    outputs: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    cost_receipt: Mapping[str, Any] | None = None,
    validation_receipt: Mapping[str, Any] | None = None,
    reused_from: Any = None,
    failure: Mapping[str, Any] | None = None,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    """Atomically create a new staged manifest for one generation/attempt."""

    node_id = _validate_node_id(node_id)
    generation_id = str(generation_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not generation_id or not attempt_id:
        raise NodeStateError("generation_and_attempt_required")
    target_dir = node_dir(root, node_id)
    manifest_path = target_dir / NODE_MANIFEST_FILENAME
    with _node_lock(root, node_id, timeout=lock_timeout):
        if manifest_path.exists():
            existing = _read_manifest(manifest_path)
            _validate_manifest_shape(existing, target_dir, node_id)
            current_state = str(existing.get("state") or "")
            if current_state != "superseded":
                raise NodeStateError(
                    f"node_manifest_already_active:{node_id}:{current_state}"
                )
            if existing.get("archive_required"):
                raise NodeStateError(
                    f"node_archive_incomplete:{node_id}:superseded_manifest_present"
                )
        archive_journal_path = target_dir / NODE_ARCHIVE_JOURNAL_FILENAME
        if archive_journal_path.is_file():
            try:
                archive_journal = json.loads(
                    archive_journal_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NodeStateError(
                    f"node_archive_journal_unreadable:{node_id}:{type(exc).__name__}"
                ) from exc
            if not isinstance(archive_journal, Mapping):
                raise NodeStateError(f"node_archive_journal_invalid:{node_id}")
            if str(archive_journal.get("state") or "") != "committed":
                raise NodeStateError(f"node_archive_incomplete:{node_id}:journal_pending")
        normalized_inputs = _normalize_inputs(inputs, base=target_dir)
        normalized_dependencies = _normalize_dependencies(
            dependencies,
            node_id=node_id,
            generation_id=generation_id,
        )
        normalized_outputs = _normalize_outputs(outputs, node_path=target_dir)
        now = _utc_now()
        manifest: dict[str, Any] = {
            "schema_version": NODE_MANIFEST_SCHEMA,
            "node_id": node_id,
            "generation_id": generation_id,
            "attempt_id": attempt_id,
            "state": "staged",
            "inputs": normalized_inputs,
            "dependencies": normalized_dependencies,
            "fingerprints": _normalize_fingerprints(fingerprints),
            "outputs": normalized_outputs,
            "cost_receipt": dict(cost_receipt or {}),
            "validation_receipt": dict(validation_receipt or {}),
            "reused_from": reused_from,
            "failure": dict(failure or {}),
            "created_at": now,
            "updated_at": now,
        }
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        _atomic_write_json(manifest_path, manifest)
        return manifest


def mark_running(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    attempt_id: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    return _transition(
        root,
        node_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        target_state="running",
        lock_timeout=lock_timeout,
    )


def _verify_inputs(manifest: Mapping[str, Any], node_path: Path) -> None:
    for row in manifest.get("inputs") or []:
        path = _resolve_path(row.get("path"), base=node_path)
        if not path.is_file():
            raise NodeValidationError(f"input_missing:{path}")
        actual = _sha256_file(path)
        if actual != str(row.get("sha256") or ""):
            raise NodeValidationError(
                f"input_hash_mismatch:{row.get('role')}:expected={row.get('sha256')}:actual={actual}"
            )


def _verify_outputs(manifest: Mapping[str, Any], node_path: Path) -> None:
    for row in manifest.get("outputs") or []:
        path = _resolve_path(row.get("path"), base=node_path)
        if not _path_inside(path, node_path):
            raise NodeValidationError(f"output_outside_node_dir:{path}")
        if not path.is_file():
            raise NodeValidationError(f"output_missing:{path}")
        actual = _sha256_file(path)
        expected = str(row.get("sha256") or "")
        if actual != expected:
            raise NodeValidationError(
                f"output_hash_mismatch:{row.get('role')}:expected={expected}:actual={actual}"
            )
        if int(row.get("bytes") or 0) != path.stat().st_size:
            raise NodeValidationError(
                f"output_size_mismatch:{row.get('role')}:expected={row.get('bytes')}:actual={path.stat().st_size}"
            )


def _verify_dependencies(
    manifest: Mapping[str, Any],
    *,
    root: str | os.PathLike[str] | None = None,
    expected_dependency_hashes: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
) -> None:
    actual = {
        str(row.get("node_id")): str(row.get("manifest_sha256") or "")
        for row in manifest.get("dependencies") or []
    }
    if expected_dependency_hashes is not None:
        if isinstance(expected_dependency_hashes, Mapping):
            expected = {
                str(node): str(
                    value.get("manifest_sha256")
                    if isinstance(value, Mapping)
                    else value
                )
                for node, value in expected_dependency_hashes.items()
            }
        else:
            expected = {
                str(row.get("node_id")): str(row.get("manifest_sha256") or "")
                for row in expected_dependency_hashes
                if isinstance(row, Mapping)
            }
        if actual != expected:
            raise NodeValidationError(
                f"dependency_manifest_hashes_mismatch:expected={expected}:actual={actual}"
            )
    if root is None:
        return

    def verify_upstream(current: Mapping[str, Any], visited: set[str]) -> None:
        current_node = str(current.get("node_id") or "")
        if current_node in visited:
            raise NodeValidationError(f"dependency_cycle:{current_node}")
        visited.add(current_node)
        for row in current.get("dependencies") or []:
            dependency = str(row.get("node_id") or "")
            dependency_path = _manifest_path(root, dependency)
            dependency_manifest = _read_manifest(dependency_path)
            _validate_manifest_shape(
                dependency_manifest,
                dependency_path.parent,
                dependency,
                require_commit_fields=True,
            )
            if dependency_manifest.get("state") != "committed":
                raise NodeValidationError(
                    f"dependency_not_committed:{dependency}:{dependency_manifest.get('state')}"
                )
            if str(dependency_manifest.get("generation_id")) != str(
                row.get("generation_id")
            ):
                raise NodeValidationError(
                    f"dependency_generation_mismatch:{dependency}:"
                    f"expected={row.get('generation_id')}:"
                    f"actual={dependency_manifest.get('generation_id')}"
                )
            if str(dependency_manifest.get("attempt_id")) != str(row.get("attempt_id")):
                raise NodeValidationError(
                    f"dependency_attempt_mismatch:{dependency}:"
                    f"expected={row.get('attempt_id')}:"
                    f"actual={dependency_manifest.get('attempt_id')}"
                )
            actual_hash = _manifest_hash(dependency_manifest)
            if actual_hash != str(row.get("manifest_sha256") or ""):
                raise NodeValidationError(
                    f"dependency_manifest_hash_drift:{dependency}:"
                    f"expected={row.get('manifest_sha256')}:actual={actual_hash}"
                )
            _verify_inputs(dependency_manifest, dependency_path.parent)
            _verify_outputs(dependency_manifest, dependency_path.parent)
            verify_upstream(dependency_manifest, visited.copy())

    verify_upstream(manifest, set())


def commit_node(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    attempt_id: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    """Revalidate every referenced hash, then atomically mark committed."""

    node_id = _validate_node_id(node_id)
    path = _manifest_path(root, node_id)
    with _node_lock(root, node_id, timeout=lock_timeout):
        manifest = _read_manifest(path)
        node_path = node_dir(root, node_id)
        _validate_manifest_shape(
            manifest,
            node_path,
            node_id,
        )
        _assert_generation_attempt(manifest, generation_id, attempt_id)
        if manifest.get("state") != "running":
            raise NodeStateError(
                f"illegal_state_transition:{manifest.get('state')}->committed:{node_id}"
            )
        _validate_manifest_shape(
            manifest,
            node_path,
            node_id,
            require_commit_fields=True,
        )
        _verify_inputs(manifest, node_path)
        _verify_outputs(manifest, node_path)
        _verify_dependencies(manifest, root=root)
        updated = dict(manifest)
        updated["state"] = "committed"
        updated["updated_at"] = _utc_now()
        updated["manifest_sha256"] = _manifest_hash(updated)
        _atomic_write_json(path, updated)
        return updated


def fail_node(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    attempt_id: str,
    *,
    failure: Mapping[str, Any] | None = None,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    return _transition(
        root,
        node_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        target_state="failed",
        failure=failure,
        lock_timeout=lock_timeout,
    )


def supersede_node(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    attempt_id: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    return _transition(
        root,
        node_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        target_state="superseded",
        lock_timeout=lock_timeout,
    )


def _archive_attempt_component(value: Any, *, field: str) -> str:
    component = str(value or "").strip()
    if not component or component in {".", ".."}:
        raise NodeStateError(f"archive_{field}_invalid")
    if any(separator in component for separator in ("/", "\\")):
        raise NodeStateError(f"archive_{field}_invalid")
    # Attempt IDs are generated UUIDs in production.  Requiring a portable
    # filename alphabet prevents an attacker-controlled manifest from moving
    # files outside the node history directory.
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in component):
        raise NodeStateError(f"archive_{field}_invalid")
    return component


def _read_archive_journal(path: Path, node_id: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeStateError(
            f"node_archive_journal_unreadable:{node_id}:{type(exc).__name__}"
        ) from exc
    if not isinstance(value, Mapping):
        raise NodeStateError(f"node_archive_journal_invalid:{node_id}")
    return dict(value)


def _archive_journal_update(
    path: Path,
    journal: dict[str, Any],
    *,
    state: str | None = None,
    failure: BaseException | None = None,
) -> None:
    if state is not None:
        journal["state"] = state
    if failure is not None:
        journal["failure"] = {
            "error_type": type(failure).__name__,
            "reason": str(failure)[:1000],
        }
    journal["updated_at"] = _utc_now()
    _atomic_write_json(path, journal)


def _archive_node_attempt_for_rebuild_new(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    expected_manifest_hash: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    """Archive one committed/failed node attempt before a direct rebuild.

    The current node lock protects validation, the superseded transition, and
    every member move.  A history directory is created with ``exist_ok=False``
    so a same-name archive can never be overwritten.  If any move or receipt
    write fails, the superseded marker and archive journal remain as evidence;
    :func:`stage_node` refuses to start a new attempt until a successful
    archive has cleared that boundary.
    """

    node_id = _validate_node_id(node_id)
    generation_id = str(generation_id or "").strip()
    expected_hash = str(expected_manifest_hash or "").strip()
    if not generation_id or not expected_hash:
        raise NodeStateError("archive_generation_and_manifest_hash_required")
    node_path = node_dir(root, node_id)
    manifest_path = node_path / NODE_MANIFEST_FILENAME
    archive_journal_path = node_path / NODE_ARCHIVE_JOURNAL_FILENAME
    with _node_lock(root, node_id, timeout=lock_timeout):
        current = _read_manifest(manifest_path)
        _validate_manifest_shape(current, node_path, node_id)
        _assert_generation_attempt(current, generation_id, str(current.get("attempt_id")))
        current_state = str(current.get("state") or "")
        if current_state not in {"committed", "failed"}:
            raise NodeStateError(
                f"archive_node_state_invalid:{node_id}:{current_state}"
            )
        actual_hash = _manifest_hash(current)
        if actual_hash != expected_hash or str(current.get("manifest_sha256") or "") != expected_hash:
            raise NodeValidationError(
                f"archive_manifest_hash_mismatch:{node_id}:"
                f"expected={expected_hash}:actual={actual_hash}"
            )
        # Validate every referenced asset before changing the authoritative
        # manifest.  A failed node may have no outputs, which is valid; a
        # listed but missing/tampered output is not archivable.
        _verify_inputs(current, node_path)
        _verify_outputs(current, node_path)
        attempt_id = _archive_attempt_component(current.get("attempt_id"), field="attempt_id")
        history_root = node_path / "_history"
        if history_root.is_symlink():
            raise NodeStateError(f"archive_history_root_symlink:{node_id}")
        history_root.mkdir(parents=True, exist_ok=True)
        history_name = f"{attempt_id}_{expected_hash}"
        history_path = history_root / history_name
        if history_path.exists():
            raise NodeStateError(f"archive_history_already_exists:{node_id}:{history_name}")
        try:
            history_path.mkdir()
        except FileExistsError as exc:
            raise NodeStateError(
                f"archive_history_already_exists:{node_id}:{history_name}"
            ) from exc

        output_rows: list[dict[str, Any]] = []
        for output in current.get("outputs") or []:
            output_path = _resolve_path(output.get("path"), base=node_path)
            try:
                relative = output_path.relative_to(node_path)
            except ValueError as exc:
                raise NodeValidationError(
                    f"archive_output_outside_node_dir:{node_id}:{output.get('role')}"
                ) from exc
            destination = history_path / relative
            output_rows.append({
                "role": str(output.get("role") or ""),
                "source": str(output_path),
                "destination": str(destination),
                "sha256": str(output.get("sha256") or ""),
                "bytes": int(output.get("bytes") or 0),
                "state": "pending",
            })
        journal: dict[str, Any] = {
            "schema_version": "optomind.upgrade3.phase3_node_archive_transaction.v1",
            "transaction_id": uuid.uuid4().hex,
            "state": "prepared",
            "node_id": node_id,
            "generation_id": generation_id,
            "attempt_id": attempt_id,
            "original_manifest_sha256": expected_hash,
            "source_manifest": dict(current),
            "source_manifest_sha256": expected_hash,
            "superseded_manifest": None,
            "manifest_plan": {
                "source": str(manifest_path),
                "destination": str(history_path / NODE_MANIFEST_FILENAME),
                "state": "pending",
            },
            "history_path": str(history_path),
            "journal_path": str(archive_journal_path),
            "outputs": output_rows,
            "manifest_state": "pending",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        try:
            _atomic_write_json(archive_journal_path, journal)
            superseded = dict(current)
            superseded["state"] = "superseded"
            superseded["archive_required"] = True
            superseded["archived_from_manifest_sha256"] = expected_hash
            superseded["updated_at"] = _utc_now()
            superseded["manifest_sha256"] = _manifest_hash(superseded)
            journal["superseded_manifest"] = dict(superseded)
            _atomic_write_json(manifest_path, superseded)
            _archive_journal_update(
                archive_journal_path,
                journal,
                state="superseded",
            )
            for row in output_rows:
                source = Path(row["source"])
                destination = Path(row["destination"])
                _atomic_move(source, destination)
                row["state"] = "moved"
                _archive_journal_update(archive_journal_path, journal)
            superseded = dict(superseded)
            superseded["outputs"] = [
                {
                    **dict(output),
                    "path": str(row["destination"]),
                }
                for output, row in zip(current.get("outputs") or [], output_rows)
            ]
            superseded["archived_from_manifest_sha256"] = expected_hash
            superseded["updated_at"] = _utc_now()
            superseded["manifest_sha256"] = _manifest_hash(superseded)
            journal["superseded_manifest"] = dict(superseded)
            _atomic_write_json(manifest_path, superseded)
            _atomic_move(manifest_path, history_path / NODE_MANIFEST_FILENAME)
            journal["manifest_plan"] = {
                **dict(journal.get("manifest_plan") or {}),
                "state": "moved",
            }
            journal["manifest_state"] = "moved"
            _archive_journal_update(archive_journal_path, journal)
            receipt = {
                "schema_version": "optomind.upgrade3.phase3_node_archive_receipt.v1",
                "state": "committed",
                "node_id": node_id,
                "generation_id": generation_id,
                "attempt_id": attempt_id,
                "original_manifest_sha256": expected_hash,
                "superseded_manifest_sha256": superseded["manifest_sha256"],
                "history_path": str(history_path),
                "archived_outputs": [dict(row) for row in output_rows],
                "transaction_id": journal["transaction_id"],
                "created_at": _utc_now(),
            }
            receipt_path = history_path / ARCHIVE_RECEIPT_FILENAME
            _atomic_write_json(receipt_path, receipt)
            journal["receipt_path"] = str(receipt_path)
            _archive_journal_update(
                archive_journal_path,
                journal,
                state="committed",
            )
            return {
                "node_id": node_id,
                "generation_id": generation_id,
                "attempt_id": attempt_id,
                "state": "archived",
                "original_manifest_sha256": expected_hash,
                "superseded_manifest_sha256": superseded["manifest_sha256"],
                "history_path": str(history_path),
                "archive_dir": str(history_path),
                "archive_receipt_path": str(receipt_path),
                "journal_path": str(archive_journal_path),
                "outputs": [dict(row) for row in output_rows],
            }
        except BaseException as exc:
            try:
                _archive_journal_update(
                    archive_journal_path,
                    journal,
                    state="failed",
                    failure=exc,
                )
            except BaseException:
                # Preserve the original archive error and any already-written
                # superseded/journal evidence.  A later stage call will fail
                # closed if the journal is ambiguous.
                pass
            raise


def _archive_result_from_journal(
    journal: Mapping[str, Any],
    *,
    receipt_path: Path,
) -> dict[str, Any]:
    history_path = str(journal.get("history_path") or "")
    return {
        "node_id": str(journal.get("node_id") or ""),
        "generation_id": str(journal.get("generation_id") or ""),
        "attempt_id": str(journal.get("attempt_id") or ""),
        "state": "archived",
        "original_manifest_sha256": str(
            journal.get("original_manifest_sha256")
            or journal.get("source_manifest_sha256")
            or ""
        ),
        "superseded_manifest_sha256": str(
            (journal.get("superseded_manifest") or {}).get("manifest_sha256")
            if isinstance(journal.get("superseded_manifest"), Mapping)
            else ""
        ),
        "history_path": history_path,
        "archive_dir": history_path,
        "archive_receipt_path": str(receipt_path),
        "journal_path": str(journal.get("journal_path") or ""),
        "outputs": [
            dict(row)
            for row in (journal.get("outputs") or [])
            if isinstance(row, Mapping)
        ],
    }


def continue_archive_node_attempt(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    expected_manifest_hash: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    """Continue a previously interrupted node archive transaction.

    Re-entry classifies each member by source/destination presence and hash.
    A source-only member is moved, a destination-only matching member is
    accepted as complete, and both-present or hash-conflicting members block
    the transaction.  The persisted source/superseded manifests in the
    journal are authoritative once the top manifest has moved.
    """

    node_id = _validate_node_id(node_id)
    generation_id = str(generation_id or "").strip()
    expected_hash = str(expected_manifest_hash or "").strip()
    if not generation_id or not expected_hash:
        raise NodeStateError("archive_generation_and_manifest_hash_required")
    node_path = node_dir(root, node_id)
    manifest_path = node_path / NODE_MANIFEST_FILENAME
    journal_path = node_path / NODE_ARCHIVE_JOURNAL_FILENAME
    with _node_lock(root, node_id, timeout=lock_timeout):
        journal = _read_archive_journal(journal_path, node_id)
        if journal is None:
            raise NodeStateError(f"node_archive_journal_missing:{node_id}")
        if (
            str(journal.get("node_id") or "") != node_id
            or str(journal.get("generation_id") or "") != generation_id
            or str(
                journal.get("original_manifest_sha256")
                or journal.get("source_manifest_sha256")
                or ""
            ) != expected_hash
        ):
            raise NodeValidationError(f"archive_journal_identity_mismatch:{node_id}")
        history_path = Path(str(journal.get("history_path") or "")).expanduser().resolve(
            strict=False
        )
        history_root = (node_path / "_history").resolve(strict=False)
        try:
            history_path.relative_to(history_root)
        except ValueError as exc:
            raise NodeValidationError(f"archive_history_path_invalid:{node_id}") from exc
        if history_path == history_root or history_path.parent != history_root:
            raise NodeValidationError(f"archive_history_path_invalid:{node_id}")
        if not history_path.is_dir():
            raise NodeStateError(f"archive_history_missing:{node_id}")
        source_manifest = journal.get("source_manifest")
        if not isinstance(source_manifest, Mapping):
            raise NodeValidationError(f"archive_source_manifest_missing:{node_id}")
        if _manifest_hash(source_manifest) != expected_hash:
            raise NodeValidationError(f"archive_source_manifest_hash_mismatch:{node_id}")
        try:
            source_manifest_path = manifest_path
            if source_manifest_path.is_file():
                current_top = _read_manifest(source_manifest_path)
                current_hash = str(current_top.get("manifest_sha256") or "")
                current_state = str(current_top.get("state") or "")
                if current_hash != expected_hash and current_state != "superseded":
                    raise NodeValidationError(
                        f"archive_current_manifest_conflict:{node_id}"
                    )
                if current_state not in {"committed", "failed", "superseded"}:
                    raise NodeStateError(
                        f"archive_current_manifest_state_invalid:{node_id}:{current_state}"
                    )
            history_manifest_path = history_path / NODE_MANIFEST_FILENAME
            if source_manifest_path.exists() and history_manifest_path.exists():
                raise NodeStateError(f"archive_manifest_both_present:{node_id}")
            if not source_manifest_path.exists() and history_manifest_path.exists():
                history_manifest = _read_manifest(history_manifest_path)
                if (
                    str(history_manifest.get("state") or "") != "superseded"
                    or str(history_manifest.get("archived_from_manifest_sha256") or "")
                    != expected_hash
                ):
                    raise NodeValidationError(
                        f"archive_history_manifest_conflict:{node_id}"
                    )

            output_rows = [
                row for row in (journal.get("outputs") or [])
                if isinstance(row, Mapping)
            ]
            for row in output_rows:
                source = Path(str(row.get("source") or "")).expanduser().resolve(strict=False)
                destination = Path(str(row.get("destination") or "")).expanduser().resolve(strict=False)
                try:
                    source.relative_to(node_path.resolve())
                    destination.relative_to(history_path)
                except ValueError as exc:
                    raise NodeValidationError(
                        f"archive_output_plan_outside_boundary:{node_id}"
                    ) from exc
                expected_output_hash = str(row.get("sha256") or "")
                if not expected_output_hash:
                    raise NodeValidationError(f"archive_output_hash_missing:{node_id}")
                source_exists = source.is_file()
                destination_exists = destination.is_file()
                if source.exists() and not source_exists:
                    raise NodeStateError(f"archive_source_not_regular_file:{node_id}")
                if destination.exists() and not destination_exists:
                    raise NodeStateError(f"archive_destination_not_regular_file:{node_id}")
                if source_exists and destination_exists:
                    raise NodeStateError(
                        f"archive_output_both_present:{node_id}:{row.get('role')}"
                    )
                if destination_exists:
                    if _sha256_file(destination) != expected_output_hash:
                        raise NodeValidationError(
                            f"archive_destination_hash_mismatch:{node_id}:{row.get('role')}"
                        )
                    row["state"] = "moved"
                    continue
                if not source_exists:
                    raise NodeValidationError(
                        f"archive_output_missing:{node_id}:{row.get('role')}"
                    )
                if _sha256_file(source) != expected_output_hash:
                    raise NodeValidationError(
                        f"archive_source_hash_mismatch:{node_id}:{row.get('role')}"
                    )
                _atomic_move(source, destination)
                if not destination.is_file() or _sha256_file(destination) != expected_output_hash:
                    raise NodeValidationError(
                        f"archive_destination_verify_failed:{node_id}:{row.get('role')}"
                    )
                row["state"] = "moved"
                _archive_journal_update(journal_path, journal)

            superseded = journal.get("superseded_manifest")
            if not isinstance(superseded, Mapping):
                superseded = dict(source_manifest)
                superseded["state"] = "superseded"
                superseded["archive_required"] = True
                superseded["archived_from_manifest_sha256"] = expected_hash
                superseded["updated_at"] = _utc_now()
            superseded = dict(superseded)
            superseded["state"] = "superseded"
            superseded["archive_required"] = True
            superseded["archived_from_manifest_sha256"] = expected_hash
            superseded["outputs"] = [
                {
                    **dict(output),
                    "path": str(row.get("destination") or output.get("path")),
                }
                for output, row in zip(source_manifest.get("outputs") or [], output_rows)
            ]
            superseded["updated_at"] = _utc_now()
            superseded["manifest_sha256"] = _manifest_hash(superseded)
            journal["superseded_manifest"] = dict(superseded)
            journal["state"] = "moving_manifest"
            _archive_journal_update(journal_path, journal)

            history_manifest_path = history_path / NODE_MANIFEST_FILENAME
            if manifest_path.exists() and history_manifest_path.exists():
                raise NodeStateError(f"archive_manifest_both_present:{node_id}")
            if manifest_path.exists():
                current_top = _read_manifest(manifest_path)
                if str(current_top.get("state") or "") != "superseded":
                    _atomic_write_json(manifest_path, superseded)
                else:
                    # The top manifest may already be superseded from the
                    # interrupted first pass; write the path-updated version
                    # before moving it to history.
                    _atomic_write_json(manifest_path, superseded)
                _atomic_move(manifest_path, history_manifest_path)
            elif not history_manifest_path.exists():
                raise NodeValidationError(f"archive_manifest_missing:{node_id}")
            journal["manifest_plan"] = {
                **dict(journal.get("manifest_plan") or {}),
                "state": "moved",
            }
            journal["manifest_state"] = "moved"
            _archive_journal_update(journal_path, journal)

            receipt_path = history_path / ARCHIVE_RECEIPT_FILENAME
            if receipt_path.exists():
                receipt = _read_manifest(receipt_path)
                if (
                    str(receipt.get("state") or "") != "committed"
                    or str(receipt.get("original_manifest_sha256") or "") != expected_hash
                ):
                    raise NodeValidationError(f"archive_receipt_conflict:{node_id}")
            else:
                receipt = {
                    "schema_version": "optomind.upgrade3.phase3_node_archive_receipt.v1",
                    "state": "committed",
                    "node_id": node_id,
                    "generation_id": generation_id,
                    "attempt_id": str(journal.get("attempt_id") or ""),
                    "original_manifest_sha256": expected_hash,
                    "superseded_manifest_sha256": superseded["manifest_sha256"],
                    "history_path": str(history_path),
                    "archived_outputs": [dict(row) for row in output_rows],
                    "transaction_id": journal["transaction_id"],
                    "created_at": _utc_now(),
                }
                _atomic_write_json(receipt_path, receipt)
            journal["receipt_path"] = str(receipt_path)
            _archive_journal_update(journal_path, journal, state="committed")
            result = _archive_result_from_journal(
                journal,
                receipt_path=receipt_path,
            )
            result["superseded_manifest_sha256"] = superseded["manifest_sha256"]
            return result
        except BaseException as exc:
            try:
                _archive_journal_update(
                    journal_path,
                    journal,
                    state="failed",
                    failure=exc,
                )
            except BaseException:
                pass
            raise


def archive_node_attempt_for_rebuild(
    root: str | os.PathLike[str],
    node_id: str,
    generation_id: str,
    expected_manifest_hash: str,
    *,
    lock_timeout: float = 0.0,
) -> dict[str, Any]:
    """Archive a node attempt, continuing an unfinished transaction if any."""

    node_id = _validate_node_id(node_id)
    expected_hash = str(expected_manifest_hash or "").strip()
    journal_path = node_dir(root, node_id) / NODE_ARCHIVE_JOURNAL_FILENAME
    if journal_path.is_file():
        journal = _read_archive_journal(journal_path, node_id)
        if journal is None:
            raise NodeStateError(f"node_archive_journal_invalid:{node_id}")
        journal_hash = str(
            journal.get("original_manifest_sha256")
            or journal.get("source_manifest_sha256")
            or ""
        )
        journal_generation = str(journal.get("generation_id") or "")
        if journal_generation != str(generation_id or ""):
            raise NodeValidationError(f"archive_journal_identity_mismatch:{node_id}")
        if journal_hash != expected_hash:
            # A committed journal belongs to an already archived attempt and
            # may be safely superseded by a new current manifest.  Pending or
            # failed journals must never be overwritten because their source
            # plan still needs continuation.
            if str(journal.get("state") or "") != "committed":
                raise NodeValidationError(f"archive_journal_identity_mismatch:{node_id}")
            return _archive_node_attempt_for_rebuild_new(
                root,
                node_id,
                generation_id,
                expected_manifest_hash,
                lock_timeout=lock_timeout,
            )
        if str(journal.get("state") or "") == "committed":
            receipt_path = Path(str(journal.get("receipt_path") or ""))
            if not receipt_path.is_file():
                receipt_path = Path(str(journal.get("history_path") or "")) / ARCHIVE_RECEIPT_FILENAME
            if receipt_path.is_file():
                return _archive_result_from_journal(journal, receipt_path=receipt_path)
        return continue_archive_node_attempt(
            root,
            node_id,
            generation_id,
            expected_manifest_hash,
            lock_timeout=lock_timeout,
        )
    return _archive_node_attempt_for_rebuild_new(
        root,
        node_id,
        generation_id,
        expected_manifest_hash,
        lock_timeout=lock_timeout,
    )


# Readable compatibility aliases for recovery callers.
resume_node_archive = continue_archive_node_attempt
continue_node_archive = continue_archive_node_attempt


def load_and_validate_committed_node(
    root: str | os.PathLike[str],
    node_id: str,
    *,
    expected_generation_id: str | None = None,
    expected_fingerprints: Mapping[str, Any] | None = None,
    expected_dependency_hashes: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    expected_inputs: Iterable[Mapping[str, Any]] | None = None,
    expected_outputs: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Load a committed manifest and fail closed on any observable drift."""

    node_id = _validate_node_id(node_id)
    node_path = node_dir(root, node_id)
    manifest = _read_manifest(node_path / NODE_MANIFEST_FILENAME)
    _validate_manifest_shape(
        manifest,
        node_path,
        node_id,
        require_commit_fields=True,
    )
    if manifest.get("state") != "committed":
        raise NodeValidationError(
            f"node_not_committed:{node_id}:{manifest.get('state')}"
        )
    if expected_generation_id is not None and str(manifest.get("generation_id")) != str(
        expected_generation_id
    ):
        raise NodeValidationError(
            f"generation_mismatch:expected={expected_generation_id}:actual={manifest.get('generation_id')}"
        )
    if expected_fingerprints is not None:
        actual_fingerprints = dict(manifest.get("fingerprints") or {})
        expected = _normalize_fingerprints(expected_fingerprints)
        for key in _FINGERPRINT_KEYS:
            if actual_fingerprints.get(key) != expected.get(key):
                raise NodeValidationError(
                    f"fingerprint_mismatch:{key}:expected={expected.get(key)!r}:actual={actual_fingerprints.get(key)!r}"
                )
    _verify_inputs(manifest, node_path)
    _verify_outputs(manifest, node_path)
    _verify_dependencies(
        manifest,
        root=root,
        expected_dependency_hashes=expected_dependency_hashes,
    )
    if expected_inputs is not None:
        expected_input_rows = _normalize_inputs(expected_inputs, base=node_path)
        if expected_input_rows != manifest.get("inputs"):
            raise NodeValidationError("input_manifest_identity_mismatch")
    if expected_outputs is not None:
        expected_output_rows = _normalize_outputs(expected_outputs, node_path=node_path)
        if expected_output_rows != manifest.get("outputs"):
            raise NodeValidationError("output_manifest_identity_mismatch")
    return manifest


def _change_tokens(change_set: Any) -> list[str]:
    if change_set is None:
        return []
    if isinstance(change_set, Mapping):
        values = []
        for key, value in change_set.items():
            key_token = str(key).strip().casefold().replace("-", "_").replace(" ", "_")
            if key_token in {"changes", "tokens", "change_set"}:
                if isinstance(value, Mapping):
                    values.extend(_change_tokens(value))
                elif isinstance(value, (list, tuple, set, frozenset)):
                    values.extend(item for item in value if not isinstance(item, bool))
                elif isinstance(value, str) and value.strip():
                    values.append(value)
            else:
                # Metadata values such as True or "v2" do not constitute a
                # separate invalidation class; only the named change key does.
                values.append(key)
    elif isinstance(change_set, (str, bytes)):
        values = [change_set]
    else:
        values = list(change_set)
    return [
        str(value).strip().casefold().replace("-", "_").replace(" ", "_")
        for value in values
        if str(value).strip()
    ]


def _manifest_map(existing_manifests: Any) -> dict[str, dict[str, Any]]:
    if isinstance(existing_manifests, Mapping):
        rows = existing_manifests.items()
    else:
        rows = []
        for value in existing_manifests or []:
            if isinstance(value, Mapping):
                rows.append((value.get("node_id"), value))
    result: dict[str, dict[str, Any]] = {}
    for key, value in rows:
        if isinstance(value, (str, os.PathLike)):
            try:
                value = _read_manifest(Path(value))
            except Phase3NodeError:
                value = {"node_id": key, "state": "invalid"}
        if not isinstance(value, Mapping):
            continue
        node = str(value.get("node_id") or key or "")
        if node in NODE_IDS:
            result[node] = dict(value)
    return result


def _requested_target_nodes(requested_target: Any) -> set[str]:
    token = (
        str(requested_target or "")
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if not token:
        return set(NODE_IDS)
    if token in {
        "phase3_handoff",
        "rebuild_phase3_handoff",
        "handoff",
        "p3d",
        "p3d_acceptance_handoff",
    }:
        return {"P3D_ACCEPTANCE_HANDOFF"}
    if token in {
        "scoped_kb",
        "rebuild_scoped_kb",
        "p3a",
        "p3a_claim_pool",
    }:
        return set(NODE_IDS)
    for node in NODE_IDS:
        if token == node.casefold():
            index = NODE_IDS.index(node)
            return set(NODE_IDS[index:])
    raise Phase3NodeError(f"unknown_recovery_target:{requested_target}")


def _changed_nodes(change_tokens: list[str]) -> dict[str, list[str]]:
    affected: dict[str, list[str]] = {node: [] for node in NODE_IDS}

    def add(nodes: Iterable[str], reason: str) -> None:
        for node in nodes:
            affected[node].append(reason)

    for token in change_tokens:
        if any(marker in token for marker in (
            "material", "chunk", "permission", "source", "overlay", "query",
        )) or ("blueprint" in token and "revision" not in token):
            add(NODE_IDS, token)
        elif any(marker in token for marker in (
            "claim_prompt", "claim_model", "claim_code", "claim_policy",
            # The evidence-first claim factory decides the claim set: an atom
            # producer, atomiser or slice-budget change invalidates it and every
            # node downstream of it.
            "evidence_first", "evidence_atom", "atomic_claim", "atomiser",
            "atomizer", "slice_budget", "atom_code", "atom_policy",
            "atom_prompt", "atom_model",
        )):
            add(NODE_IDS, token)
        elif any(marker in token for marker in (
            "p3a_output", "claim_text", "claim_texts", "claim_pool_output",
        )):
            add(NODE_IDS[1:], token)
        elif any(marker in token for marker in (
            "binding_code", "binding_policy", "binding_span", "span",
        )):
            # A change in the binder itself never touches the claim factory: P3A
            # stays committed and only the binding, coverage and handoff nodes are
            # rebuilt.  This is the whole point of the P3A snapshot.
            add(NODE_IDS[1:], token)
        elif any(marker in token for marker in (
            "coverage_slot", "coverage_role", "coverage_policy", "required_slot", "required_role", "blueprint_revision",
        )):
            add(NODE_IDS[2:], token)
        elif any(marker in token for marker in (
            "acceptance", "handoff",
        )):
            add(NODE_IDS[3:], token)
        else:
            # An unknown change class is neither reusable nor provably confined to
            # one node.  It is unsafe to reuse and it is unsafe to dismiss, so the
            # whole graph is scheduled for rebuild; the token is kept in the reason
            # so the caller can see that the scope was not proven.
            add(NODE_IDS, f"unknown_change:{token}")
    return affected


def plan_phase3_node_recovery(
    root: str | os.PathLike[str],
    existing_manifests: Any,
    change_set: Any = None,
    requested_target: str | None = None,
) -> dict[str, Any]:
    """Plan recovery only after validating every candidate against ``root``.

    ``existing_manifests`` is an index or list supplied by the caller, while
    ``root`` is the authoritative filesystem location.  A node is never
    marked reusable from the index alone: the committed loader rechecks the
    manifest, all referenced files, dependency graph, and fingerprints first.
    """

    if root is None or not str(root).strip():
        raise Phase3NodeError("recovery_root_required")
    if existing_manifests is None:
        existing_manifests = {
            node: _manifest_path(root, node)
            for node in NODE_IDS
        }
    manifests = _manifest_map(existing_manifests)
    target_nodes = _requested_target_nodes(requested_target)
    tokens = _change_tokens(change_set)
    direct_changes = _changed_nodes(tokens)

    actions: dict[str, str] = {}
    reasons: dict[str, list[str]] = {}
    for node in NODE_IDS:
        manifest = manifests.get(node)
        if manifest is None:
            actions[node] = "invalid"
            reasons[node] = ["manifest_missing"]
            continue
        if manifest.get("state") != "committed":
            actions[node] = "invalid"
            reasons[node] = [f"state_not_committed:{manifest.get('state')}"]
            continue
        try:
            validated = load_and_validate_committed_node(
                root,
                node,
                expected_generation_id=manifest.get("generation_id"),
                expected_fingerprints=manifest.get("fingerprints"),
                expected_dependency_hashes=manifest.get("dependencies"),
                expected_inputs=manifest.get("inputs"),
                expected_outputs=manifest.get("outputs"),
            )
        except (Phase3NodeError, OSError, TypeError, ValueError) as exc:
            actions[node] = "invalid"
            reasons[node] = [
                f"committed_validation_failed:{type(exc).__name__}:{exc}"
            ]
            continue
        manifests[node] = validated
        direct = list(direct_changes.get(node) or [])
        if node in target_nodes and requested_target:
            direct.append(f"requested_target:{requested_target or node}")
        if direct:
            actions[node] = "rebuild"
            reasons[node] = sorted(set(direct))
        else:
            actions[node] = "reuse"
            reasons[node] = ["committed_and_unchanged"]

    dependencies: dict[str, tuple[str, ...]] = dict(NODE_DEPENDENCIES)
    for node, manifest in manifests.items():
        rows = manifest.get("dependencies") or []
        if rows:
            dependencies[node] = tuple(
                str(row.get("node_id"))
                for row in rows
                if isinstance(row, Mapping) and str(row.get("node_id")) in NODE_IDS
            )

    changed = True
    while changed:
        changed = False
        for node in NODE_IDS:
            if actions.get(node) not in {"reuse", "rebuild"}:
                continue
            for dependency in dependencies.get(node, ()):
                if actions.get(dependency) == "invalid":
                    actions[node] = "invalid"
                    reasons[node] = [f"upstream_{dependency}_invalid"]
                    changed = True
                    break
                if actions.get(dependency) == "rebuild" and actions.get(node) == "reuse":
                    actions[node] = "rebuild"
                    reasons[node] = [f"upstream_{dependency}_rebuild"]
                    changed = True
                    break

    plan = {
        node: {
            "action": actions.get(node, "invalid"),
            "reason": reasons.get(node) or ["no_plan"],
            "dependencies": list(dependencies.get(node, ())),
        }
        for node in NODE_IDS
    }
    return {
        "schema_version": RECOVERY_PLAN_SCHEMA,
        "requested_target": requested_target,
        "target_nodes": sorted(target_nodes),
        "change_set": tokens,
        "node_plan": plan,
        "reuse_nodes": [node for node in NODE_IDS if actions.get(node) == "reuse"],
        "rebuild_nodes": [node for node in NODE_IDS if actions.get(node) == "rebuild"],
        "invalid_nodes": [node for node in NODE_IDS if actions.get(node) == "invalid"],
    }


__all__ = [
    "NODE_DEPENDENCIES",
    "NODE_IDS",
    "NODE_ARCHIVE_JOURNAL_FILENAME",
    "ARCHIVE_RECEIPT_FILENAME",
    "NODE_LOCK_FILENAME",
    "NODE_MANIFEST_FILENAME",
    "NODE_MANIFEST_SCHEMA",
    "NODE_STATES",
    "NodeStateError",
    "NodeValidationError",
    "NodeLockError",
    "Phase3NodeError",
    "RECOVERY_PLAN_SCHEMA",
    "commit_node",
    "archive_node_attempt_for_rebuild",
    "continue_archive_node_attempt",
    "continue_node_archive",
    "fail_node",
    "load_and_validate_committed_node",
    "mark_running",
    "node_dir",
    "plan_phase3_node_recovery",
    "stage_node",
    "supersede_node",
    "resume_node_archive",
]
