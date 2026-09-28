"""Read-only quarantine guard for diagnostic harness runs.

The quarantine marker lives beside a run directory so creating or validating
one never writes inside the run it protects.  The receipt is kept in a caller
owned diagnostics directory and the marker commits its resolved path and
content hash.  A marker is therefore an explicit refusal boundary: a valid
diagnostic marker refuses resume, and a malformed marker refuses resume too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


QUARANTINE_SCHEMA = "optomind.run_diagnostic_quarantine.v1"
QUARANTINE_STATE = "diagnostic_quarantined"
MARKER_SUFFIX = ".diagnostic-quarantine.json"
RECEIPT_FILENAME = "DIAGNOSTIC_QUARANTINE.json"
MARKER_FIELDS = frozenset({
    "schema",
    "source_run",
    "quarantine_state",
    "receipt_path",
    "receipt_sha256",
})


def resolve_run_dir(run_dir: str | os.PathLike[str]) -> Path:
    """Return a stable absolute path without requiring the directory to exist."""

    return Path(run_dir).expanduser().resolve(strict=False)


def marker_path_for_run(run_dir: str | os.PathLike[str]) -> Path:
    """Return the side-channel marker path for ``run_dir``."""

    resolved = resolve_run_dir(run_dir)
    return resolved.parent / f"{resolved.name}{MARKER_SUFFIX}"


# Short aliases make the side-channel convention easy to use at call sites.
quarantine_marker_path = marker_path_for_run
diagnostic_marker_path = marker_path_for_run


class QuarantinedRunError(RuntimeError):
    """Raised whenever a run cannot be safely resumed."""

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        reason: str,
        *,
        marker_path: str | os.PathLike[str] | None = None,
        receipt_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.run_dir = resolve_run_dir(run_dir)
        self.reason = str(reason)
        self.marker_path = (
            Path(marker_path).expanduser().resolve(strict=False)
            if marker_path is not None
            else marker_path_for_run(self.run_dir)
        )
        self.receipt_path = (
            Path(receipt_path).expanduser().resolve(strict=False)
            if receipt_path is not None
            else None
        )
        super().__init__(
            f"Refusing to resume run directory {self.run_dir}: {self.reason}"
        )


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest used by receipt/marker integrity checks."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Hash a file without loading it all into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace one file, retaining a complete old/new state."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
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


def atomic_write_json(
    path: str | os.PathLike[str], payload: Mapping[str, Any]
) -> str:
    """Atomically write JSON and return the digest of the exact stored bytes."""

    data = _json_bytes(payload)
    target = Path(path).expanduser()
    _atomic_write_bytes(target, data)
    return sha256_bytes(data)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _assert_receipt_outside_run(run_dir: Path, receipt_path: Path) -> None:
    if _is_within(receipt_path, run_dir):
        raise QuarantinedRunError(
            run_dir,
            "receipt_path_inside_run_dir: quarantine receipt must be written "
            "outside the protected run directory",
            receipt_path=receipt_path,
        )


def write_quarantine_marker(
    run_dir: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    *,
    receipt_sha256: str | None = None,
) -> Path:
    """Atomically write the five-field marker beside ``run_dir``.

    The receipt is hashed from disk unless an expected digest is supplied.  A
    supplied digest is checked before the marker is written so the marker can
    never attest to bytes that were not actually stored.
    """

    resolved_run = resolve_run_dir(run_dir)
    resolved_receipt = Path(receipt_path).expanduser().resolve(strict=False)
    _assert_receipt_outside_run(resolved_run, resolved_receipt)
    if not resolved_receipt.is_file():
        raise QuarantinedRunError(
            resolved_run,
            "receipt_missing: cannot create a marker without the authoritative receipt",
            receipt_path=resolved_receipt,
        )
    actual_hash = sha256_file(resolved_receipt)
    if receipt_sha256 is not None and not hmac.compare_digest(
        str(receipt_sha256), actual_hash
    ):
        raise QuarantinedRunError(
            resolved_run,
            "receipt_hash_mismatch_before_marker: supplied digest does not "
            "match the authoritative receipt",
            receipt_path=resolved_receipt,
        )
    marker = {
        "schema": QUARANTINE_SCHEMA,
        "source_run": str(resolved_run),
        "quarantine_state": QUARANTINE_STATE,
        "receipt_path": str(resolved_receipt),
        "receipt_sha256": actual_hash,
    }
    atomic_write_json(marker_path_for_run(resolved_run), marker)
    return marker_path_for_run(resolved_run)


def write_quarantine_receipt(
    run_dir: str | os.PathLike[str],
    receipt_path: str | os.PathLike[str],
    receipt: Mapping[str, Any],
) -> tuple[Path, Path, str]:
    """Atomically write a receipt and its sibling marker.

    The returned tuple is ``(receipt_path, marker_path, receipt_sha256)``.
    Caller data is copied before the required source/state fields are added.
    """

    resolved_run = resolve_run_dir(run_dir)
    resolved_receipt = Path(receipt_path).expanduser().resolve(strict=False)
    _assert_receipt_outside_run(resolved_run, resolved_receipt)
    payload = dict(receipt)
    payload.setdefault("schema_version", QUARANTINE_SCHEMA)
    payload.setdefault("source_run", str(resolved_run))
    payload.setdefault("status", QUARANTINE_STATE)
    payload.setdefault("quarantine_state", QUARANTINE_STATE)
    receipt_hash = atomic_write_json(resolved_receipt, payload)
    marker_path = write_quarantine_marker(
        resolved_run, resolved_receipt, receipt_sha256=receipt_hash
    )
    return resolved_receipt, marker_path, receipt_hash


# Explicit names for callers that prefer the operation to say "atomic".
atomic_write_receipt = write_quarantine_receipt
write_diagnostic_quarantine = write_quarantine_receipt


def _read_json_object(path: Path, *, kind: str, run_dir: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise QuarantinedRunError(
            run_dir,
            f"{kind}_unreadable:{type(exc).__name__}",
            receipt_path=path if kind == "receipt" else None,
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuarantinedRunError(
            run_dir,
            f"{kind}_invalid_json:{type(exc).__name__}",
            receipt_path=path if kind == "receipt" else None,
        ) from exc
    if not isinstance(value, dict):
        raise QuarantinedRunError(
            run_dir,
            f"{kind}_not_object",
            receipt_path=path if kind == "receipt" else None,
        )
    return value


def _resolve_declared_path(value: Any, *, base: Path) -> Path:
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve(strict=False)


def _resolve_required_absolute_path(
    value: Any, *, field: str, base: Path
) -> Path:
    """Resolve a path only when its stored spelling is already canonical."""

    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError(f"{field}_missing")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{field}_must_be_absolute_resolved")
    resolved = candidate.resolve(strict=False)
    if os.path.normcase(str(candidate)) != os.path.normcase(str(resolved)):
        raise ValueError(f"{field}_must_be_absolute_resolved")
    return resolved


def _validate_receipt_minimum(
    receipt: Mapping[str, Any], *, run_dir: Path, receipt_path: Path
) -> None:
    """Validate the fields needed before trusting a quarantine receipt."""

    if receipt.get("schema_version") != QUARANTINE_SCHEMA:
        raise QuarantinedRunError(
            run_dir,
            f"receipt_schema_mismatch:{receipt.get('schema_version')!r}",
            receipt_path=receipt_path,
        )
    if "source_run" not in receipt or not str(receipt.get("source_run") or "").strip():
        raise QuarantinedRunError(
            run_dir,
            "receipt_source_run_missing",
            receipt_path=receipt_path,
        )
    try:
        declared_source = _resolve_required_absolute_path(
            receipt.get("source_run"), field="receipt_source_run", base=receipt_path.parent
        )
    except ValueError as exc:
        raise QuarantinedRunError(
            run_dir,
            str(exc),
            receipt_path=receipt_path,
        ) from exc
    if os.path.normcase(str(declared_source)) != os.path.normcase(str(run_dir)):
        raise QuarantinedRunError(
            run_dir,
            "receipt_source_run_mismatch: authoritative receipt points to "
            f"{declared_source}",
            receipt_path=receipt_path,
        )

    for field in ("status", "quarantine_state"):
        if field not in receipt:
            raise QuarantinedRunError(
                run_dir,
                f"receipt_{field}_missing",
                receipt_path=receipt_path,
            )
        if receipt.get(field) != QUARANTINE_STATE:
            raise QuarantinedRunError(
                run_dir,
                f"receipt_{field}_not_diagnostic_quarantined:{receipt.get(field)!r}",
                receipt_path=receipt_path,
            )

    for field in ("source_hash_manifest", "cost_reconciliation", "code_identity"):
        value = receipt.get(field)
        if not isinstance(value, Mapping):
            raise QuarantinedRunError(
                run_dir,
                f"receipt_{field}_not_object",
                receipt_path=receipt_path,
            )
        status = str(value.get("status") or "").strip()
        reference_path = value.get("path")
        reference_hash = (
            value.get("sha256")
            or value.get("receipt_sha256")
            or value.get("hash")
        )
        if not status and not (
            isinstance(reference_path, (str, os.PathLike))
            and str(reference_path).strip()
            and str(reference_hash or "").strip()
        ):
            raise QuarantinedRunError(
                run_dir,
                f"receipt_{field}_reference_missing: require status or path+hash",
                receipt_path=receipt_path,
            )
        if reference_path:
            try:
                resolved_reference = _resolve_required_absolute_path(
                    reference_path, field=f"receipt_{field}_path", base=receipt_path.parent
                )
            except ValueError as exc:
                raise QuarantinedRunError(
                    run_dir,
                    str(exc),
                    receipt_path=receipt_path,
                ) from exc
            if _is_within(resolved_reference, run_dir):
                raise QuarantinedRunError(
                    run_dir,
                    f"receipt_{field}_path_inside_run_dir",
                    receipt_path=receipt_path,
                )


def assert_run_resumable(run_dir: str | os.PathLike[str]) -> bool:
    """Fail closed if a side-channel marker says a run is quarantined.

    A missing marker means this is a new or ordinary run and returns ``True``.
    Any present marker is validated completely.  A valid quarantine still
    raises: its purpose is to make the diagnostic run permanently non-resumable.
    """

    resolved_run = resolve_run_dir(run_dir)
    marker_path = marker_path_for_run(resolved_run)
    try:
        marker_path.stat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise QuarantinedRunError(
            resolved_run,
            f"marker_unreadable:{type(exc).__name__}",
            marker_path=marker_path,
        ) from exc

    marker = _read_json_object(marker_path, kind="marker", run_dir=resolved_run)
    if set(marker) != MARKER_FIELDS:
        raise QuarantinedRunError(
            resolved_run,
            "marker_fields_invalid: marker must contain exactly schema, "
            "source_run, quarantine_state, receipt_path, receipt_sha256",
            marker_path=marker_path,
        )
    if marker.get("schema") != QUARANTINE_SCHEMA:
        raise QuarantinedRunError(
            resolved_run,
            f"marker_schema_mismatch:{marker.get('schema')!r}",
            marker_path=marker_path,
        )

    try:
        declared_source = _resolve_required_absolute_path(
            marker.get("source_run"),
            field="marker_source_run",
            base=marker_path.parent,
        )
    except ValueError as exc:
        raise QuarantinedRunError(
            resolved_run,
            str(exc),
            marker_path=marker_path,
        ) from exc
    if os.path.normcase(str(declared_source)) != os.path.normcase(
        str(resolved_run)
    ):
        raise QuarantinedRunError(
            resolved_run,
            f"source_run_mismatch:expected={resolved_run}:actual={declared_source}",
            marker_path=marker_path,
        )

    state = marker.get("quarantine_state")
    if state != QUARANTINE_STATE:
        raise QuarantinedRunError(
            resolved_run,
            f"quarantine_state_not_diagnostic_quarantined:{state!r}",
            marker_path=marker_path,
        )

    try:
        receipt_path = _resolve_required_absolute_path(
            marker.get("receipt_path"),
            field="marker_receipt_path",
            base=marker_path.parent,
        )
    except ValueError as exc:
        raise QuarantinedRunError(
            resolved_run,
            str(exc),
            marker_path=marker_path,
        ) from exc
    if _is_within(receipt_path, resolved_run):
        raise QuarantinedRunError(
            resolved_run,
            "receipt_path_inside_run_dir: refusing to trust a receipt stored "
            "inside the protected run",
            marker_path=marker_path,
            receipt_path=receipt_path,
        )
    if not receipt_path.is_file():
        raise QuarantinedRunError(
            resolved_run,
            "receipt_missing: authoritative quarantine receipt is absent",
            marker_path=marker_path,
            receipt_path=receipt_path,
        )
    try:
        actual_hash = sha256_file(receipt_path)
    except OSError as exc:
        raise QuarantinedRunError(
            resolved_run,
            f"receipt_unreadable:{type(exc).__name__}",
            marker_path=marker_path,
            receipt_path=receipt_path,
        ) from exc
    expected_hash = str(marker.get("receipt_sha256") or "")
    if not expected_hash or not hmac.compare_digest(expected_hash, actual_hash):
        raise QuarantinedRunError(
            resolved_run,
            f"receipt_hash_mismatch:expected={expected_hash or '<missing>'}:"
            f"actual={actual_hash}",
            marker_path=marker_path,
            receipt_path=receipt_path,
        )

    receipt = _read_json_object(receipt_path, kind="receipt", run_dir=resolved_run)
    try:
        _validate_receipt_minimum(
            receipt, run_dir=resolved_run, receipt_path=receipt_path
        )
    except QuarantinedRunError as exc:
        exc.marker_path = marker_path
        raise

    raise QuarantinedRunError(
        resolved_run,
        "diagnostic_quarantined: diagnostic run is permanently non-resumable",
        marker_path=marker_path,
        receipt_path=receipt_path,
    )


__all__ = [
    "MARKER_FIELDS",
    "MARKER_SUFFIX",
    "QUARANTINE_SCHEMA",
    "QUARANTINE_STATE",
    "RECEIPT_FILENAME",
    "QuarantinedRunError",
    "assert_run_resumable",
    "atomic_write_json",
    "atomic_write_receipt",
    "diagnostic_marker_path",
    "marker_path_for_run",
    "quarantine_marker_path",
    "resolve_run_dir",
    "sha256_bytes",
    "sha256_file",
    "write_diagnostic_quarantine",
    "write_quarantine_marker",
    "write_quarantine_receipt",
]
