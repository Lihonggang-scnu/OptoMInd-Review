"""Thin A/B batch and planning-pool bridge for the optics E2E run.

This module deliberately owns no provider implementation.  It consumes the
native acquisition checkpoints and the native upgrade3 paper-reading batch,
and supplies only the small adapter needed by the supervised root runner.

The default command is a read-only preflight.  ``--run`` is the explicit
switch which invokes the native batch through ``SupervisedFactory``.  The
module never prints credential values and does not inspect credential files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any, Callable


PREPARATION_ROOT = Path(__file__).resolve().parent
RUN_ROOT = PREPARATION_ROOT.parent
SOURCE_ROOT = RUN_ROOT / "worktree"

MANIFEST_SCHEMA = "optomind.optics_ab_batch_manifest.v1"
BRIDGE_SCHEMA = "optomind.optics_planning_pool_bridge.v1"
CARD_SCHEMA = "optomind.paper_reading_card.v1"
SUCCESS_STATUSES = {"success", "reused"}
DEFAULT_MODEL = "qwen3.7-flash"
DEFAULT_THINKING_BUDGET = 4096
DEFAULT_MAX_OUTPUT_TOKENS = 12288
DEFAULT_BUDGET_LIMIT_CNY = 80.0


class PreparationError(RuntimeError):
    """A deterministic input or output contract error."""


class BridgeBlocked(PreparationError):
    """The planning pool is not complete enough to write safely."""


def _as_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _read_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = _as_path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreparationError(f"missing JSON: {target}") from exc
    except json.JSONDecodeError as exc:
        raise PreparationError(f"invalid JSON: {target}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise PreparationError(f"expected JSON object: {target}")
    return value


def _read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = _as_path(path)
    if not target.is_file():
        raise PreparationError(f"missing JSONL: {target}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PreparationError(f"invalid JSONL at {target}:{line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise PreparationError(f"JSONL row is not an object at {target}:{line_number}")
        rows.append(value)
    return rows


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: str | os.PathLike[str]) -> str:
    target = _as_path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value))


def _write_json_atomic(path: str | os.PathLike[str], payload: Mapping[str, Any]) -> Path:
    target = _as_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def _write_jsonl_atomic(path: str | os.PathLike[str], rows: Sequence[Mapping[str, Any]]) -> Path:
    target = _as_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def load_binding(binding_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Read the run binding without exposing secret fields."""

    target = _as_path(binding_path or RUN_ROOT / "RUN_BINDING.json")
    value = _read_json(target)
    if value.get("project_limit_cny") is not None and float(value["project_limit_cny"]) != DEFAULT_BUDGET_LIMIT_CNY:
        raise PreparationError(
            f"binding project limit is {value.get('project_limit_cny')!r}; expected {DEFAULT_BUDGET_LIMIT_CNY:g}"
        )
    return value


def _source_head(source_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def check_source_pin(binding: Mapping[str, Any], source_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return source identity for the report; this is read-only."""

    source = _as_path(source_root or binding.get("source_root") or SOURCE_ROOT)
    expected = str(binding.get("source_commit") or "")
    actual = _source_head(source)
    return {
        "source_root": str(source),
        "expected_commit": expected,
        "actual_commit": actual,
        "matches": bool(expected and actual and actual.lower() == expected.lower()),
    }


def _normalise_identity(value: Mapping[str, Any] | None) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("canonical_paper_id", "paper_id", "doi", "title"):
        candidate = value.get(key)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _identity_fields(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    keys = (
        "canonical_paper_id",
        "paper_id",
        "doi",
        "title",
        "year",
        "authors",
        "journal",
    )
    return {key: value[key] for key in keys if key in value and value[key] not in (None, "")}


def _resolve_snapshot_path(raw: Any, checkpoint_path: Path) -> Path | None:
    if raw in (None, ""):
        return None
    path = Path(str(raw))
    if not path.is_absolute():
        path = checkpoint_path.parent / path
    return path.resolve()


def _discover_checkpoint_files(checkpoint_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    root = checkpoint_root.resolve()
    if root.name != "_batch_checkpoints" and (root / "_batch_checkpoints").is_dir():
        root = (root / "_batch_checkpoints").resolve()
    if not root.is_dir():
        return root, []
    paths = sorted(path for path in root.glob("*.json") if path.is_file())
    values: list[dict[str, Any]] = []
    for path in paths:
        try:
            values.append({"path": path, "payload": _read_json(path)})
        except PreparationError as exc:
            values.append({"path": path, "payload": None, "error": str(exc)})
    return root, values


def _expected_acquisition_records(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    value = _read_json(path)
    raw_records = value.get("records")
    if not isinstance(raw_records, list):
        return []
    return [record for record in raw_records if isinstance(record, Mapping)]


def build_acquisition_manifest(
    checkpoint_root: str | os.PathLike[str],
    plan_path: str | os.PathLike[str],
    *,
    acquisition_manifest_path: str | os.PathLike[str] | None = None,
    finalized: bool = True,
) -> dict[str, Any]:
    """Build an A/B manifest from published native acquisition checkpoints.

    Only checkpoints with ``status == published`` and a snapshot directory
    containing ``DOCUMENT_MANIFEST.json`` become batch records.  A published
    status is not upgraded into a full-text claim; the native batch remains
    responsible for deciding whether the snapshot is sufficient.
    """

    checkpoint_dir, discovered = _discover_checkpoint_files(_as_path(checkpoint_root))
    plan = _as_path(plan_path)
    plan_exists = plan.is_file()
    plan_hash = _sha256_file(plan) if plan_exists else None
    source_manifest = _as_path(acquisition_manifest_path) if acquisition_manifest_path else None
    expected = _expected_acquisition_records(source_manifest)

    by_paper: dict[str, dict[str, Any]] = {}
    malformed: list[dict[str, Any]] = []
    for entry in discovered:
        path = entry["path"]
        payload = entry.get("payload")
        if not isinstance(payload, Mapping):
            malformed.append({"path": str(path), "reason": entry.get("error", "invalid_checkpoint")})
            continue
        identity = payload.get("identity") if isinstance(payload.get("identity"), Mapping) else {}
        paper_id = _normalise_identity(identity)
        if not paper_id:
            malformed.append({"path": str(path), "reason": "missing_identity.canonical_paper_id"})
            continue
        by_paper.setdefault(paper_id, {"path": path, "payload": payload})

    expected_ids: list[str] = []
    expected_by_id: dict[str, Mapping[str, Any]] = {}
    for record in expected:
        identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else record
        paper_id = _normalise_identity(identity)
        if paper_id and paper_id not in expected_by_id:
            expected_ids.append(paper_id)
            expected_by_id[paper_id] = record

    # Without a native acquisition manifest, every checkpoint is an explicit
    # input.  With one, missing checkpoint identities remain visible below.
    candidate_ids = expected_ids or sorted(by_paper)
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = list(malformed)
    missing: list[dict[str, Any]] = []

    for paper_id in candidate_ids:
        source_record = expected_by_id.get(paper_id, {})
        found = by_paper.get(paper_id)
        if found is None:
            missing.append(
                {
                    "paper_id": paper_id,
                    "reason": "checkpoint_missing",
                    "expected_record": _identity_fields(
                        source_record.get("identity") if isinstance(source_record.get("identity"), Mapping) else source_record
                    ),
                }
            )
            continue
        checkpoint_path = found["path"]
        payload = found["payload"]
        status = str(payload.get("status") or "")
        if status != "published":
            rejected.append(
                {
                    "paper_id": paper_id,
                    "checkpoint_path": str(checkpoint_path),
                    "status": status or None,
                    "reason": "checkpoint_not_published",
                }
            )
            continue
        snapshot_dir = _resolve_snapshot_path(payload.get("snapshot_path"), checkpoint_path)
        if snapshot_dir is None:
            missing.append({"paper_id": paper_id, "checkpoint_path": str(checkpoint_path), "reason": "snapshot_path_missing"})
            continue
        snapshot_manifest = snapshot_dir / "DOCUMENT_MANIFEST.json"
        if not snapshot_dir.is_dir():
            missing.append(
                {"paper_id": paper_id, "checkpoint_path": str(checkpoint_path), "snapshot_dir": str(snapshot_dir), "reason": "snapshot_dir_missing"}
            )
            continue
        if not snapshot_manifest.is_file():
            missing.append(
                {"paper_id": paper_id, "checkpoint_path": str(checkpoint_path), "snapshot_dir": str(snapshot_dir), "reason": "DOCUMENT_MANIFEST.json_missing"}
            )
            continue
        identity = payload.get("identity") if isinstance(payload.get("identity"), Mapping) else {}
        identity = dict(identity)
        if not identity.get("canonical_paper_id"):
            identity["canonical_paper_id"] = paper_id
        records.append(
            {
                "paper_id": paper_id,
                "title": identity.get("title") or source_record.get("title") or "",
                "identity": _identity_fields(identity),
                "snapshot_dir": str(snapshot_dir),
                "plan_path": str(plan),
                "manifest_sha256": _sha256_file(snapshot_manifest),
                "plan_sha256": plan_hash,
                "checkpoint_path": str(checkpoint_path),
                "acquisition_status": status,
                "material_depth": payload.get("material_depth"),
                "snapshot_id": payload.get("snapshot_id"),
            }
        )

    if not plan_exists:
        missing.append({"path": str(plan), "reason": "plan_missing"})
    if not checkpoint_dir.is_dir():
        missing.append({"path": str(checkpoint_dir), "reason": "checkpoint_root_missing"})
    if source_manifest is not None and not source_manifest.is_file():
        missing.append({"path": str(source_manifest), "reason": "acquisition_manifest_missing"})

    input_files: list[dict[str, Any]] = []
    for entry in discovered:
        path = entry["path"]
        item: dict[str, Any] = {"path": str(path)}
        if path.is_file():
            item["sha256"] = _sha256_file(path)
        if entry.get("payload") is None:
            item["error"] = entry.get("error", "invalid_checkpoint")
        input_files.append(item)
    fingerprint_payload = {
        "checkpoint_root": str(checkpoint_dir),
        "checkpoint_files": input_files,
        "plan_path": str(plan),
        "plan_sha256": plan_hash,
        "acquisition_manifest_path": str(source_manifest) if source_manifest else None,
        "acquisition_manifest_sha256": _sha256_file(source_manifest) if source_manifest and source_manifest.is_file() else None,
        "finalized": bool(finalized),
    }
    return {
        "schema_version": MANIFEST_SCHEMA,
        "source_kind": "native_acquisition_published_checkpoints",
        "finalized": bool(finalized),
        "checkpoint_root": str(checkpoint_dir),
        "acquisition_manifest_path": str(source_manifest) if source_manifest else None,
        "plan_path": str(plan),
        "plan_sha256": plan_hash,
        "input_fingerprint": _sha256_json(fingerprint_payload),
        "input_files": input_files,
        "records": sorted(records, key=lambda row: (str(row["paper_id"]), str(row["snapshot_dir"]))),
        "counts": {
            "checkpoint_files": len(discovered),
            "eligible_records": len(records),
            "missing": len(missing),
            "rejected": len(rejected),
            "material_depth": {
                depth: sum(1 for row in records if str(row.get("material_depth") or "unknown") == depth)
                for depth in sorted({str(row.get("material_depth") or "unknown") for row in records})
            },
        },
        "missing": missing,
        "rejected": rejected,
    }


def write_acquisition_manifest(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    """Persist a manifest exactly where the caller requested."""

    return _write_json_atomic(path, manifest)


def default_card_profile() -> dict[str, Any]:
    """The root-approved native card profile."""

    return {
        "model": DEFAULT_MODEL,
        "thinking": True,
        "thinking_budget": DEFAULT_THINKING_BUDGET,
        "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "json_mode": True,
        "stream": False,
        "timeout_seconds": 300.0,
    }


def make_supervised_client_factory(
    factory: Callable[[str, Path, Mapping[str, Any]], Any],
    batch_root: str | os.PathLike[str],
    *,
    profile: Mapping[str, Any] | None = None,
) -> Callable[[Mapping[str, Any], int], Any]:
    """Adapt ``SupervisedFactory(role, stage_dir, profile)`` to native batch.

    Native ``run_paper_reading_batch`` asks for ``client_factory(item,
    attempt)``.  The wrapper creates one durable supervision directory per
    work key and attempt.  Each client is still constructed through the
    existing root request/response gate, so native bounded retries remain
    resumable without bypassing supervision.
    """

    root = _as_path(batch_root)
    selected_profile = dict(profile or default_card_profile())

    def client_factory(item: Mapping[str, Any], attempt: int) -> Any:
        work_key = str(item.get("work_key") or item.get("paper_id") or "unknown")
        stage_dir = root / "_supervision" / work_key / f"attempt-{int(attempt):02d}"
        return factory("paper_reading_card", stage_dir, dict(selected_profile))

    return client_factory


def _load_card_tools(source_root: str | os.PathLike[str] | None = None) -> Any:
    source = _as_path(source_root or SOURCE_ROOT)
    source_string = str(source)
    if source_string not in sys.path:
        sys.path.insert(0, source_string)
    try:
        from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
        from optomind_research.runtime.upgrade3.paper_reading_card import build_card_input, load_plan
    except ImportError as exc:
        raise PreparationError(f"cannot load native card validators from {source}: {exc}") from exc

    class _Tools:
        pass

    tools = _Tools()
    tools.PreparedSnapshotProvider = PreparedSnapshotProvider
    tools.build_card_input = build_card_input
    tools.load_plan = load_plan
    return tools


def _tool(tools: Any, name: str) -> Any:
    value = getattr(tools, name, None)
    if value is None and isinstance(tools, Mapping):
        value = tools.get(name)
    if value is None:
        raise PreparationError(f"card validator missing {name}")
    return value


def _compare(label: str, actual: Any, expected: Any, checks: dict[str, bool]) -> None:
    checks[label] = actual == expected
    if actual != expected:
        raise PreparationError(f"card binding mismatch: {label}")


def _saved_projection_compatible(card_file, snapshot, card, expected):
    """Prove unchanged source content using the committed historical packet.

    A projection-version change may alter observation layout, while the saved
    source manifest/blocks/assets/references and topic remain identical.  This
    does not forgive changed source content or rewrite the old card.
    """
    commit = _read_json(card_file.parent / "COMMIT.json")
    saved = _read_json(card_file.parent / "INPUT_PACKET.json")
    if commit.get("status") != "success":
        raise PreparationError("card committed success required")
    files = commit.get("files") or {}
    for required in ("INPUT_PACKET.json", "PAPER_READING_CARD.json"):
        if not files.get(required):
            raise PreparationError("card committed input hash required")
    for name, digest in files.items():
        path = card_file.parent / name
        if Path(name).name != name or not path.is_file() or _sha256_file(path) != digest:
            raise PreparationError("card commit file hash mismatch")
    for field in ("card_id", "snapshot_id", "snapshot_sha256", "topic_sha256"):
        if saved.get(field) != card.get(field) or commit.get(field) != card.get(field):
            raise PreparationError("card saved input binding mismatch:" + field)
    if saved.get("paper_identity") != card.get("paper_identity"):
        raise PreparationError("card saved paper identity mismatch")
    if saved.get("topic_sha256") != expected.get("topic_sha256"):
        raise PreparationError("card saved topic mismatch")
    packet = saved.get("reading_packet")
    if not isinstance(packet, Mapping):
        raise PreparationError("card saved reading packet required")
    payload = {"manifest": dict(snapshot.manifest),
               "blocks": [dict(v) for v in snapshot.blocks],
               "assets": [dict(v) for v in snapshot.assets],
               "references": [dict(v) for v in snapshot.references],
               "reading_packet": dict(packet)}
    if _sha256_bytes(_canonical_json(payload)) != card.get("snapshot_sha256"):
        raise PreparationError("card source content changed; snapshot_sha256 mismatch")
    return True


def verify_card_binding(
    card_path: str | os.PathLike[str],
    item: Mapping[str, Any],
    *,
    source_root: str | os.PathLike[str] | None = None,
    tools: Any | None = None,
) -> dict[str, Any]:
    """Verify card identity, snapshot lineage, and current PLAN topic hash."""

    card_file = _as_path(card_path)
    card = _read_json(card_file)
    checks: dict[str, bool] = {}
    schema = card.get("schema_version")
    if schema != CARD_SCHEMA:
        raise PreparationError(f"card schema mismatch: {schema!r}")
    checks["schema_version"] = True
    if not (card_file.parent / "COMMIT.json").is_file():
        raise PreparationError(f"card COMMIT.json missing: {card_file.parent}")
    checks["commit_marker"] = True

    paper_id = str(item.get("paper_id") or "")
    card_identity = card.get("paper_identity") if isinstance(card.get("paper_identity"), Mapping) else {}
    card_id = _normalise_identity(card_identity)
    if not paper_id or not card_id:
        raise PreparationError("card or batch item missing canonical paper identity")
    _compare("paper_id", card_id, paper_id, checks)

    snapshot_dir = _as_path(item.get("snapshot_dir")) if item.get("snapshot_dir") else None
    plan_path = _as_path(item.get("plan_path")) if item.get("plan_path") else None
    if snapshot_dir is None or plan_path is None:
        raise PreparationError("batch item missing snapshot_dir or plan_path")
    if not snapshot_dir.is_dir():
        raise PreparationError(f"snapshot directory missing: {snapshot_dir}")
    if not plan_path.is_file():
        raise PreparationError(f"plan missing: {plan_path}")

    validators = tools or _load_card_tools(source_root)
    provider = _tool(validators, "PreparedSnapshotProvider")(snapshot_dir)
    snapshot = provider.load()
    plan = _tool(validators, "load_plan")(plan_path)
    expected = _tool(validators, "build_card_input")(snapshot, plan)
    expected_identity = expected.get("paper_identity") if isinstance(expected, Mapping) else {}
    expected_id = _normalise_identity(expected_identity)
    _compare("expected_paper_id", card_id, expected_id, checks)

    expected_snapshot_id = str(expected.get("snapshot_id") or "")
    actual_snapshot_id = str(card.get("snapshot_id") or "")
    _compare("snapshot_id", actual_snapshot_id, expected_snapshot_id, checks)
    planning_view = card.get("planning_view") if isinstance(card.get("planning_view"), Mapping) else {}
    if planning_view.get("snapshot_id") is not None:
        _compare("planning_view.snapshot_id", str(planning_view.get("snapshot_id") or ""), expected_snapshot_id, checks)

    expected_snapshot_hash = str(expected.get("snapshot_sha256") or "")
    actual_snapshot_hash = str(card.get("snapshot_sha256") or "")
    if actual_snapshot_hash != expected_snapshot_hash:
        _saved_projection_compatible(card_file, snapshot, card, expected)
        checks["unchanged_source_historical_projection"] = True
    else:
        checks["snapshot_sha256"] = True
    if planning_view.get("snapshot_sha256") is not None:
        _compare("planning_view.snapshot_sha256", str(planning_view.get("snapshot_sha256") or ""), actual_snapshot_hash, checks)

    expected_topic_hash = str(expected.get("topic_sha256") or "")
    actual_topic_hash = str(card.get("topic_sha256") or "")
    _compare("topic_sha256", actual_topic_hash, expected_topic_hash, checks)
    if planning_view.get("topic_sha256") is not None:
        _compare("planning_view.topic_sha256", str(planning_view.get("topic_sha256") or ""), expected_topic_hash, checks)

    expected_manifest_hash = item.get("manifest_sha256")
    snapshot_manifest = snapshot_dir / "DOCUMENT_MANIFEST.json"
    if expected_manifest_hash:
        _compare("manifest_sha256", _sha256_file(snapshot_manifest), str(expected_manifest_hash), checks)

    return {
        "card_path": str(card_file),
        "paper_id": paper_id,
        "snapshot_dir": str(snapshot_dir),
        "plan_path": str(plan_path),
        "snapshot_id": expected_snapshot_id,
        "snapshot_sha256": actual_snapshot_hash,
        "current_projection_sha256": expected_snapshot_hash,
        "topic_sha256": expected_topic_hash,
        "checks": checks,
        "planning_view": dict(planning_view),
    }


def _state_items(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = state.get("items")
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items() if isinstance(value, Mapping)}
    if isinstance(raw, list):
        result: dict[str, Mapping[str, Any]] = {}
        for value in raw:
            if isinstance(value, Mapping) and value.get("work_key"):
                result[str(value["work_key"])] = value
        return result
    return {}


def _is_identity_conflict(reason: str) -> bool:
    text = reason.lower()
    markers = (
        "schema mismatch",
        "commit.json missing",
        "paper_id",
        "snapshot_id",
        "snapshot_sha256",
        "topic_sha256",
        "manifest_sha256",
        "duplicate_verified",
        "binding mismatch",
    )
    return any(marker in text for marker in markers)


def _missing_category(status: str) -> str:
    if status in {"insufficient_material", "metadata_only"}:
        return "metadata_only_or_insufficient_material"
    if status in {"failed", "budget_blocked"}:
        return "batch_failure"
    if status in {"waiting_material", "pending", "running", "preflight"}:
        return "waiting_or_pending"
    return "batch_item_not_emitted"


def bridge_batch_to_pool(
    batch_root: str | os.PathLike[str],
    *,
    pool_path: str | os.PathLike[str] | None = None,
    report_path: str | os.PathLike[str] | None = None,
    plan_path: str | os.PathLike[str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
    acquisition_manifest: Mapping[str, Any] | None = None,
    tools: Any | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Create a planner pool only from verified native success/reuse cards."""

    root = _as_path(batch_root)
    state_path = root / "BATCH_STATE.json"
    index_path = root / "BATCH_CARDS_INDEX.jsonl"
    report: dict[str, Any] = {
        "schema_version": BRIDGE_SCHEMA,
        "batch_root": str(root),
        "state_path": str(state_path),
        "cards_index_path": str(index_path),
        "pool_path": str(_as_path(pool_path)) if pool_path else None,
        "report_path": str(_as_path(report_path)) if report_path else None,
        "missing": [],
        "rejected": [],
        "identity_conflicts": [],
        "accepted": [],
    }
    if not state_path.is_file():
        report["missing"].append({"path": str(state_path), "reason": "BATCH_STATE.json_missing"})
        report["status"] = "waiting_batch"
        report["input_fingerprint"] = _sha256_json({"state": None, "index": None, "plan": str(plan_path) if plan_path else None})
        if report_path:
            _write_json_atomic(report_path, report)
        return report
    if not index_path.is_file():
        report["missing"].append({"path": str(index_path), "reason": "BATCH_CARDS_INDEX.jsonl_missing"})
        report["status"] = "waiting_batch"
        report["input_fingerprint"] = _sha256_json({"state": _sha256_file(state_path), "index": None})
        if report_path:
            _write_json_atomic(report_path, report)
        return report

    state = _read_json(state_path)
    index_rows = _read_jsonl(index_path)
    items = _state_items(state)
    card_tools = tools
    accepted_rows: list[dict[str, Any]] = []
    seen_papers: set[str] = set()
    indexed_keys: set[str] = set()

    for index_row in index_rows:
        work_key = str(index_row.get("work_key") or "")
        indexed_keys.add(work_key)
        item = items.get(work_key)
        if item is None:
            report["rejected"].append({"work_key": work_key, "reason": "index_work_key_missing_from_state"})
            continue
        status = str(item.get("status") or "")
        if status not in SUCCESS_STATUSES:
            report["rejected"].append({"work_key": work_key, "paper_id": item.get("paper_id"), "status": status, "reason": "batch_status_not_success_or_reused"})
            continue
        output_dir_value = index_row.get("output_dir") or item.get("output_dir")
        if not output_dir_value:
            report["rejected"].append({"work_key": work_key, "reason": "card_output_dir_missing"})
            continue
        output_dir = _as_path(output_dir_value)
        card_path = output_dir / "PAPER_READING_CARD.json"
        if not card_path.is_file():
            report["missing"].append({"work_key": work_key, "paper_id": item.get("paper_id"), "path": str(card_path), "reason": "card_missing"})
            continue
        try:
            verified = verify_card_binding(card_path, item, source_root=source_root, tools=card_tools)
        except PreparationError as exc:
            rejection = {"work_key": work_key, "paper_id": item.get("paper_id"), "card_path": str(card_path), "reason": str(exc)}
            report["rejected"].append(rejection)
            if _is_identity_conflict(str(exc)):
                report["identity_conflicts"].append(rejection)
            continue
        paper_id = str(verified["paper_id"])
        if paper_id in seen_papers:
            report["rejected"].append({"work_key": work_key, "paper_id": paper_id, "reason": "duplicate_verified_paper_id"})
            continue
        seen_papers.add(paper_id)
        accepted_rows.append(
            {
                "paper_id": paper_id,
                "card_path": verified["card_path"],
                "planning_view": verified["planning_view"],
                "snapshot_dir": verified["snapshot_dir"],
                "snapshot_id": verified["snapshot_id"],
                "snapshot_sha256": verified["snapshot_sha256"],
                "topic_sha256": verified["topic_sha256"],
                "plan_path": str(item.get("plan_path") or ""),
                "plan_sha256": item.get("plan_sha256"),
                "manifest_sha256": item.get("manifest_sha256"),
                "source_batch_work_key": work_key,
                "source_batch_status": status,
            }
        )

    pending = []
    for work_key, item in sorted(items.items()):
        if work_key in indexed_keys:
            continue
        status = str(item.get("status") or "")
        pending.append(
            {
                "work_key": work_key,
                "paper_id": item.get("paper_id"),
                "status": status,
                "category": _missing_category(status),
                "reason": "batch_item_not_in_success_index",
            }
        )
    report["missing"].extend(pending)

    state_hash = _sha256_file(state_path)
    index_hash = _sha256_file(index_path)
    selected_plan = _as_path(plan_path) if plan_path else None
    plan_hash = _sha256_file(selected_plan) if selected_plan and selected_plan.is_file() else None
    acquisition_blockers = []
    if acquisition_manifest:
        acquisition_blockers.extend(acquisition_manifest.get("missing") or [])
        acquisition_blockers.extend(acquisition_manifest.get("rejected") or [])
    if acquisition_blockers:
        report["missing"].extend({"reason": "acquisition_input_blocker", "detail": value} for value in acquisition_blockers)
    finalized = bool(state.get("manifest_finalized", True))
    if selected_plan is not None and not selected_plan.is_file():
        report["missing"].append({"path": str(selected_plan), "reason": "plan_missing"})
    complete = bool(finalized and accepted_rows and not report["missing"] and not report["rejected"])
    if report["identity_conflicts"]:
        report["status"] = "identity_conflict"
    elif complete:
        report["status"] = "complete"
    elif accepted_rows:
        report["status"] = "partial"
    elif not finalized:
        report["status"] = "waiting_material"
    else:
        report["status"] = "blocked"
    pool_rows = sorted(accepted_rows, key=lambda row: (row["paper_id"], row["card_path"]))
    # Keep the durable report bounded: the full planning_view is written only
    # to the planner pool, while the report retains exact accepted paths.
    report["accepted"] = [
        {
            "paper_id": row["paper_id"],
            "card_path": row["card_path"],
            "source_batch_work_key": row["source_batch_work_key"],
            "source_batch_status": row["source_batch_status"],
        }
        for row in pool_rows
    ]
    report["counts"] = {
        "accepted": len(pool_rows),
        "rejected": len(report["rejected"]),
        "missing": len(report["missing"]),
        "state_items": len(items),
    }
    fingerprint_payload = {
        "state_path": str(state_path),
        "state_sha256": state_hash,
        "cards_index_path": str(index_path),
        "cards_index_sha256": index_hash,
        "plan_path": str(selected_plan) if selected_plan else None,
        "plan_sha256": plan_hash,
        "acquisition_input_fingerprint": acquisition_manifest.get("input_fingerprint") if acquisition_manifest else None,
    }
    report["input_fingerprint"] = _sha256_json(fingerprint_payload)
    report["input_files"] = {
        "state_path": str(state_path),
        "state_sha256": state_hash,
        "cards_index_path": str(index_path),
        "cards_index_sha256": index_hash,
        "plan_path": str(selected_plan) if selected_plan else None,
        "plan_sha256": plan_hash,
    }

    if write and pool_rows:
        target_pool = _as_path(pool_path) if pool_path else root / "PLANNING_POOL.jsonl"
        _write_jsonl_atomic(target_pool, pool_rows)
        report["pool_written"] = str(target_pool)
    else:
        report["pool_written"] = None
    if write and not pool_rows:
        report["write_blocked_reason"] = "no verified success/reused cards are available"
    if report_path:
        _write_json_atomic(report_path, report)
    return report


def _load_batch_runtime(source_root: str | os.PathLike[str] | None = None) -> Any:
    source = _as_path(source_root or SOURCE_ROOT)
    source_string = str(source)
    if source_string not in sys.path:
        sys.path.insert(0, source_string)
    try:
        from optomind_research.runtime.upgrade3 import paper_reading_batch
    except ImportError as exc:
        raise PreparationError(f"cannot load native paper batch from {source}: {exc}") from exc
    return paper_reading_batch


def _default_paths(binding: Mapping[str, Any]) -> dict[str, Path]:
    stage_root = _as_path(binding.get("stage_output_root") or RUN_ROOT / "worktree" / "outputs" / "optics_e2e")
    materials_name = str(binding.get("materials_directory") or "04_local_materials")
    if Path(materials_name).name != materials_name:
        raise PreparationError("materials_directory_must_be_one_local_name")
    materials_root = stage_root / materials_name
    batch_root = stage_root / "05_ab_cards"
    pool_root = stage_root / "06_planning_pool"
    return {
        "stage_root": stage_root,
        "materials_root": materials_root,
        "checkpoint_root": materials_root / "snapshots" / "_batch_checkpoints",
        "acquisition_manifest": materials_root / "ACQUISITION_MANIFEST.json",
        "plan": stage_root / "01_question_retrieval_plan" / "PLAN.json",
        "batch_manifest": materials_root / "AB_BATCH_MANIFEST.json",
        "batch_root": batch_root,
        "pool_path": pool_root / "PLANNING_POOL.jsonl",
        "report_path": pool_root / "PLANNING_POOL_BRIDGE_REPORT.json",
    }


def run_ab(
    *,
    mode: str = "preflight",
    binding_path: str | os.PathLike[str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
    plan_path: str | os.PathLike[str] | None = None,
    checkpoint_root: str | os.PathLike[str] | None = None,
    acquisition_manifest_path: str | os.PathLike[str] | None = None,
    batch_manifest_path: str | os.PathLike[str] | None = None,
    batch_root: str | os.PathLike[str] | None = None,
    pool_path: str | os.PathLike[str] | None = None,
    report_path: str | os.PathLike[str] | None = None,
    finalized: bool = True,
    reparse_saved_responses: bool = False,
) -> dict[str, Any]:
    """Run preflight or the explicit supervised native A/B batch."""

    if mode not in {"preflight", "run"}:
        raise PreparationError(f"unsupported mode: {mode}")
    binding = load_binding(binding_path)
    workers = int(binding.get("ab_workers", 1))
    # Keep the local bridge bounded even though the native batch has a wider
    # internal guard.  One scheduler owns a batch root; this is a worker
    # concurrency bound, not permission to launch competing processes.
    if not 1 <= workers <= 32:
        raise PreparationError("ab_workers_must_be_1_to_32")
    defaults = _default_paths(binding)
    source = _as_path(source_root or binding.get("source_root") or SOURCE_ROOT)
    paths = {
        "plan": _as_path(plan_path or defaults["plan"]),
        "checkpoint_root": _as_path(checkpoint_root or defaults["checkpoint_root"]),
        "acquisition_manifest": _as_path(acquisition_manifest_path or defaults["acquisition_manifest"]),
        "batch_manifest": _as_path(batch_manifest_path or defaults["batch_manifest"]),
        "batch_root": _as_path(batch_root or defaults["batch_root"]),
        "pool_path": _as_path(pool_path or defaults["pool_path"]),
        "report_path": _as_path(report_path or defaults["report_path"]),
    }
    source_pin = check_source_pin(binding, source)
    acquisition = build_acquisition_manifest(
        paths["checkpoint_root"],
        paths["plan"],
        acquisition_manifest_path=paths["acquisition_manifest"],
        finalized=finalized,
    )
    write_acquisition_manifest(paths["batch_manifest"], acquisition)
    preview: dict[str, Any] = {
        "schema_version": BRIDGE_SCHEMA,
        "mode": mode,
        "reparse_saved_responses": bool(reparse_saved_responses),
        "source_pin": source_pin,
        "binding_path": str(_as_path(binding_path or RUN_ROOT / "RUN_BINDING.json")),
        "material_parser_mode": str(binding.get("material_parser_mode") or (
            "deep_layout" if binding.get("parser_runtime") else "fast_native"
        )),
        "plan_path": str(paths["plan"]),
        "checkpoint_root": str(paths["checkpoint_root"]),
        "acquisition_manifest_path": str(paths["acquisition_manifest"]),
        "batch_manifest_path": str(paths["batch_manifest"]),
        "batch_root": str(paths["batch_root"]),
        "pool_path": str(paths["pool_path"]),
        "records_count": len(acquisition.get("records") or []),
        "acquisition_missing": acquisition.get("missing") or [],
        "acquisition_rejected": acquisition.get("rejected") or [],
        "input_fingerprint": acquisition.get("input_fingerprint"),
        "defaults": {
            "model": DEFAULT_MODEL,
            "thinking_budget": DEFAULT_THINKING_BUDGET,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "budget_limit_cny": DEFAULT_BUDGET_LIMIT_CNY,
            "initial_workers": workers,
            "max_workers": workers,
            "native_max_attempts": 2,
        },
    }

    # A preview records exact paths and missing inputs and never loads a
    # provider client.  An open manifest is still eligible for the native
    # local-only preflight when it has at least one published row.
    if mode == "preflight":
        if not acquisition.get("records"):
            preview["status"] = "waiting_material"
        elif not paths["plan"].is_file() or not paths["checkpoint_root"].is_dir():
            preview["status"] = "blocked_material"
        elif not acquisition.get("finalized"):
            # Native batch intentionally waits for an open manifest to close.
            # Preview individual published rows instead of entering that loop.
            batch_runtime = _load_batch_runtime(source)
            preview["native_preflight"] = []
            for row in acquisition["records"]:
                try:
                    receipt = batch_runtime.preflight_card(
                        snapshot_dir=row["snapshot_dir"], plan_path=row["plan_path"],
                        input_profile="economy", thinking=True, concise_output=False,
                        thinking_budget=DEFAULT_THINKING_BUDGET,
                        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                        timeout_seconds=300, max_retries=0,
                    )
                    preview["native_preflight"].append({"paper_id": row["paper_id"], "receipt": receipt})
                except Exception as exc:
                    preview["native_preflight"].append({"paper_id": row["paper_id"], "error_type": type(exc).__name__, "error": str(exc)})
            preview["status"] = "preflight_ready_open"
        else:
            batch_runtime = _load_batch_runtime(source)
            native_preview = batch_runtime.run_paper_reading_batch(
                manifest_path=paths["batch_manifest"],
                output_root=paths["batch_root"],
                mode="preflight",
                input_profile="economy",
                thinking=True,
                thinking_budget=DEFAULT_THINKING_BUDGET,
                concise_output=False,
                max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
                timeout_seconds=300,
                budget_ledger_path=None,
                budget_limit_cny=None,
                initial_workers=workers,
                max_workers=workers,
                retry_delay_seconds=0,
                poll_seconds=0,
                client_factory=None,
            )
            preview["native_batch"] = native_preview
            preview["status"] = "preflight_ready"
        _write_json_atomic(paths["report_path"], preview)
        return preview

    # --run never waits for missing checkpoints.  It can consume the published
    # subset of an open or partially complete acquisition manifest; the bridge
    # records the remaining material gaps beside the usable pool rows.
    if not source_pin["matches"]:
        preview["status"] = "source_pin_mismatch"
        _write_json_atomic(paths["report_path"], preview)
        return preview
    if not paths["plan"].is_file() or not paths["checkpoint_root"].is_dir() or not acquisition.get("records"):
        preview["status"] = "blocked_material"
        _write_json_atomic(paths["report_path"], preview)
        return preview

    batch_runtime = _load_batch_runtime(source)
    # Importing the factory is deliberately delayed until --run.  The factory
    # itself supplies the existing per-call approval/ledger gates.
    supervised_path = PREPARATION_ROOT
    if str(supervised_path) not in sys.path:
        sys.path.insert(0, str(supervised_path))
    try:
        from supervised_qwen import SupervisedFactory
    except ImportError as exc:
        raise PreparationError(f"cannot load SupervisedFactory from {supervised_path}: {exc}") from exc

    factory_root = _as_path(binding_path).parent if binding_path else RUN_ROOT
    factory = SupervisedFactory(run_root=factory_root, stage="ab-cards",
                                binding_path=_as_path(binding_path or RUN_ROOT / "RUN_BINDING.json"))
    client_factory = make_supervised_client_factory(factory, paths["batch_root"])
    native_result = batch_runtime.run_paper_reading_batch(
        manifest_path=paths["batch_manifest"],
        output_root=paths["batch_root"],
        mode="run",
        input_profile="economy",
        thinking=True,
        thinking_budget=DEFAULT_THINKING_BUDGET,
        concise_output=False,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds=300,
        budget_ledger_path=_as_path(binding.get("project_ledger") or RUN_ROOT / "budget.sqlite"),
        budget_limit_cny=DEFAULT_BUDGET_LIMIT_CNY,
        initial_workers=workers,
        max_workers=workers,
        retry_delay_seconds=0,
        poll_seconds=1,
        client_factory=client_factory,
        reparse_saved_responses=reparse_saved_responses,
    )

    bridge = bridge_batch_to_pool(
        paths["batch_root"],
        pool_path=paths["pool_path"],
        report_path=None,
        plan_path=paths["plan"],
        source_root=source,
        acquisition_manifest=acquisition,
        write=True,
    )
    preview["native_batch"] = native_result
    preview["bridge"] = bridge
    preview["status"] = bridge.get("status") if bridge.get("pool_written") else "root_intervention_required"
    _write_json_atomic(paths["report_path"], preview)
    return preview


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--preflight", action="store_true", help="read-only native batch preview (default)")
    group.add_argument("--run", action="store_true", help="invoke the supervised native batch")
    parser.add_argument("--reparse-saved-responses", action="store_true", help="free reparse of saved validation failures before new live attempts; preserves original artifacts and attempt counts")
    parser.add_argument("--binding", type=Path, default=None)
    parser.add_argument("--source-root", type=Path, default=None)
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--checkpoint-root", type=Path, default=None)
    parser.add_argument("--acquisition-manifest", type=Path, default=None)
    parser.add_argument("--batch-manifest", type=Path, default=None)
    parser.add_argument("--batch-root", type=Path, default=None)
    parser.add_argument("--pool", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--open", dest="finalized", action="store_false", help="mark acquisition as still open")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = "run" if args.run else "preflight"
    result = run_ab(
        mode=mode,
        binding_path=args.binding,
        source_root=args.source_root,
        plan_path=args.plan,
        checkpoint_root=args.checkpoint_root,
        acquisition_manifest_path=args.acquisition_manifest,
        batch_manifest_path=args.batch_manifest,
        batch_root=args.batch_root,
        pool_path=args.pool,
        report_path=args.report,
        finalized=args.finalized,
        reparse_saved_responses=args.reparse_saved_responses,
    )
    safe_summary = {
        "status": result.get("status"),
        "mode": result.get("mode"),
        "records_count": result.get("records_count"),
        "report_path": result.get("report_path"),
        "batch_manifest_path": result.get("batch_manifest_path"),
        "pool_path": result.get("pool_path"),
        "input_fingerprint": result.get("input_fingerprint"),
    }
    print(json.dumps(safe_summary, ensure_ascii=False, sort_keys=True))
    if result.get("status") == "identity_conflict":
        return 2
    return 0 if result.get("status") in {
        "preflight_ready",
        "preflight_ready_open",
        "complete",
        "partial",
        "waiting_material",
        "blocked_material",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
