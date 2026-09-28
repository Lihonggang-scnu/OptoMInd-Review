"""Module 4 input and output boundary contracts.

The module uses JSON-shaped dictionaries at its public boundary so that the
artifacts are easy to inspect and replay.  This file owns identity, task
fingerprints, and the hard input gates; it deliberately does not decide
whether a model statement is scientifically true.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "optomind.module4.v1"
INPUT_SCHEMA_VERSION = "optomind.module4.reading_input.v1"
DOSSIER_SCHEMA_VERSION = "optomind.module4.paper_reading_dossier.v1"
FEEDBACK_SCHEMA_VERSION = "optomind.module4.skeleton_feedback.v1"


class Module4Blocked(ValueError):
    """Raised when a task cannot be read without violating its scope."""


class ContractError(ValueError):
    """Raised when a supplied JSON-shaped contract is malformed."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _load_json_ref(value: Any) -> Any:
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return value


def _snapshot_parts(snapshot: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if hasattr(snapshot, "manifest") and hasattr(snapshot, "blocks"):
        manifest = dict(snapshot.manifest)
        blocks = [dict(row) for row in snapshot.blocks]
        snapshot_id = _text(getattr(snapshot, "snapshot_id", "") or manifest.get("snapshot_id"))
        return manifest, blocks, snapshot_id
    if isinstance(snapshot, Mapping):
        manifest = dict(snapshot.get("manifest") or snapshot)
        blocks = [dict(row) for row in snapshot.get("blocks") or () if isinstance(row, Mapping)]
        snapshot_id = _text(snapshot.get("snapshot_id") or manifest.get("snapshot_id"))
        return manifest, blocks, snapshot_id
    raise ContractError("document_snapshot_required")


def _material_scope(snapshot: Any) -> str:
    if hasattr(snapshot, "reading_policy"):
        try:
            return _text(snapshot.reading_policy().get("material_scope")) or "unknown"
        except Exception:
            pass
    manifest, blocks, _ = _snapshot_parts(snapshot)
    claim = manifest.get("producer_fulltext_claim") or {}
    if claim.get("claimed") and claim.get("content_depth") == "fulltext":
        return "fulltext"
    if any(str(row.get("block_type")) == "paragraph" for row in blocks):
        return "structured_partial"
    return "metadata_only"


def _manifest_ref(snapshot: Any) -> dict[str, Any]:
    manifest, _, snapshot_id = _snapshot_parts(snapshot)
    return {
        "snapshot_id": snapshot_id,
        "canonical_paper_id": _text(manifest.get("canonical_paper_id")),
        "sha256": _json_hash(manifest),
        "schema_version": _text(manifest.get("schema_version")),
    }


def assemble_reading_input(
    upstream_refs: Mapping[str, Any] | str | Path,
    material_refs: Any,
    task_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a task without rewriting the original PLAN/Card shapes.

    ``upstream_refs`` may be an already loaded PLAN-shaped object or a JSON
    path.  The caller may supply M1/M2/M3 references in that object; they are
    retained under ``upstream_snapshot_refs`` and are never used as evidence.
    """

    task = _load_json_ref(upstream_refs)
    if not isinstance(task, Mapping):
        raise ContractError("upstream_task_object_required")
    flags = dict(task_config or {})
    snapshot = material_refs
    manifest, _, snapshot_id = _snapshot_parts(snapshot)
    raw = {key: value for key, value in task.items() if not str(key).startswith("_")}
    identity = dict(raw.get("paper_identity") or {})
    if not identity.get("canonical_paper_id"):
        identity["canonical_paper_id"] = _text(manifest.get("canonical_paper_id"))
    question = dict(raw.get("research_question") or {})
    facets = [dict(item) for item in raw.get("facets") or () if isinstance(item, Mapping)]
    upstream = raw.get("upstream_snapshot_refs")
    if not isinstance(upstream, list):
        upstream = []
    normalized_upstream: list[dict[str, Any]] = []
    for ref in upstream:
        if not isinstance(ref, Mapping):
            continue
        row = dict(ref)
        path = row.get("path") or row.get("file")
        if path and not row.get("sha256") and Path(path).is_file():
            row["sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        normalized_upstream.append(row)
    document_ref = _manifest_ref(snapshot)
    scope = _material_scope(snapshot)
    result: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "task_id": _text(raw.get("task_id") or raw.get("reading_task_id")) or "task-" + _json_hash(raw)[:16],
        "project_run_id": _text(raw.get("project_run_id") or raw.get("run_id")),
        "paper_identity": identity,
        "research_question": question,
        "facets": facets,
        "global_constraints": dict(raw.get("global_constraints") or {}),
        "navigation_context": dict(raw.get("navigation_context") or {}),
        "document_manifest_ref": document_ref,
        "document_snapshot_ref": {"snapshot_id": snapshot_id, "manifest_sha256": document_ref["sha256"]},
        "upstream_snapshot_refs": normalized_upstream,
        "reading_policy": dict(raw.get("reading_policy") or {"scope": "full_available_paper"}),
        "runtime_profile_ref": dict(raw.get("runtime_profile_ref") or {}),
        "output_preferences": dict(raw.get("output_preferences") or {"analysis_language": "zh", "quote_language": "original"}),
        "extensions": {
            **dict(raw.get("extensions") or {}),
            "module4": {
                "material_scope": scope,
                "allow_background_material": bool(flags.get("allow_background_material") or raw.get("allow_background_material")),
                "task_config": {key: value for key, value in flags.items() if key != "secret"},
            },
        },
    }
    result["input_fingerprint"] = _json_hash(result)
    return result


def validate_input(
    reading_input: Mapping[str, Any],
    snapshot: Any,
    *,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Validate identity, scope, and task-preservation invariants."""

    if not isinstance(reading_input, Mapping):
        if raise_on_error:
            raise Module4Blocked("reading_input_object_required")
        return {"schema_version": INPUT_SCHEMA_VERSION, "valid": False, "issues": [{"code": "reading_input_object_required", "severity": "block"}]}
    manifest, _, snapshot_id = _snapshot_parts(snapshot)
    issues: list[dict[str, Any]] = []
    allowed_top_level = {
        "schema_version", "task_id", "project_run_id", "paper_identity", "research_question", "facets", "global_constraints",
        "navigation_context", "document_manifest_ref", "document_snapshot_ref", "upstream_snapshot_refs", "reading_policy",
        "runtime_profile_ref", "output_preferences", "extensions", "input_fingerprint",
    }
    unknown = sorted(str(key) for key in reading_input if str(key) not in allowed_top_level)
    if unknown:
        issues.append({"code": "unknown_top_level_fields", "fields": unknown, "severity": "block"})
    if _text(reading_input.get("schema_version")) != INPUT_SCHEMA_VERSION:
        issues.append({"code": "input_schema_version_invalid", "severity": "block"})
    question = reading_input.get("research_question")
    if not isinstance(question, Mapping):
        issues.append({"code": "research_question_object_required", "severity": "block"})
        question = {}
    if not _text(question.get("original_text")).strip():
        issues.append({"code": "original_question_missing", "severity": "block"})
    facets = reading_input.get("facets")
    if not isinstance(facets, list) or not facets:
        issues.append({"code": "facets_missing", "severity": "block"})
    else:
        if any(not isinstance(item, Mapping) for item in facets):
            issues.append({"code": "facet_object_required", "severity": "block"})
        facet_ids = [_text(item.get("facet_id") or item.get("id")) for item in facets if isinstance(item, Mapping)]
        if any(not value for value in facet_ids):
            issues.append({"code": "facet_id_missing", "severity": "block"})
        if len(set(facet_ids)) != len(facet_ids):
            issues.append({"code": "facet_id_duplicate", "severity": "block"})
        if any(not _text(item.get("ask_original") or item.get("ask")).strip() for item in facets if isinstance(item, Mapping)):
            issues.append({"code": "facet_question_missing", "severity": "block"})
    identity_status = _text((manifest.get("identity_check") or {}).get("status"))
    if identity_status == "conflict":
        issues.append({"code": "identity_conflict", "severity": "block"})
    input_id = _text((reading_input.get("paper_identity") or {}).get("canonical_paper_id"))
    manifest_id = _text(manifest.get("canonical_paper_id"))
    if not input_id:
        issues.append({"code": "canonical_paper_id_missing", "severity": "block"})
    if not manifest_id:
        issues.append({"code": "snapshot_canonical_paper_id_missing", "severity": "block"})
    if input_id and manifest_id and input_id != manifest_id:
        issues.append({"code": "canonical_paper_id_mismatch", "input": input_id, "snapshot": manifest_id, "severity": "block"})
    ref_id = _text((reading_input.get("document_snapshot_ref") or {}).get("snapshot_id"))
    if ref_id and snapshot_id and ref_id != snapshot_id:
        issues.append({"code": "snapshot_id_mismatch", "input": ref_id, "snapshot": snapshot_id, "severity": "block"})
    manifest_ref = reading_input.get("document_manifest_ref")
    if not isinstance(manifest_ref, Mapping):
        issues.append({"code": "document_manifest_ref_required", "severity": "block"})
        manifest_ref = {}
    expected_manifest_hash = _text(manifest_ref.get("sha256"))
    actual_manifest_hash = _json_hash(manifest)
    if not expected_manifest_hash:
        issues.append({"code": "manifest_hash_missing", "severity": "block"})
    elif expected_manifest_hash != actual_manifest_hash:
        issues.append({"code": "manifest_hash_mismatch", "severity": "block"})
    upstream_refs = reading_input.get("upstream_snapshot_refs")
    if not isinstance(upstream_refs, list):
        issues.append({"code": "upstream_snapshot_refs_array_required", "severity": "block"})
        upstream_refs = []
    for ref in upstream_refs:
        if not isinstance(ref, Mapping):
            issues.append({"code": "upstream_ref_object_required", "severity": "block"})
            continue
        path_value = ref.get("path") or ref.get("file")
        if path_value and not _text(ref.get("sha256")):
            issues.append({"code": "upstream_ref_hash_missing", "path": path_value, "severity": "block"})
        elif path_value:
            path = Path(str(path_value))
            if not path.is_file():
                issues.append({"code": "upstream_ref_file_missing", "path": str(path_value), "severity": "block"})
            else:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != _text(ref.get("sha256")):
                    issues.append({"code": "upstream_ref_hash_mismatch", "path": str(path_value), "expected": _text(ref.get("sha256")), "actual": actual, "severity": "block"})
    fingerprint_value = {key: value for key, value in reading_input.items() if key != "input_fingerprint"}
    expected_fingerprint = _text(reading_input.get("input_fingerprint"))
    if not expected_fingerprint:
        issues.append({"code": "input_fingerprint_missing", "severity": "block"})
    elif expected_fingerprint != _json_hash(fingerprint_value):
        issues.append({"code": "input_fingerprint_mismatch", "severity": "block"})
    scope = _material_scope(snapshot)
    flags = ((reading_input.get("extensions") or {}).get("module4") or {})
    requested_scope = _text((reading_input.get("reading_policy") or {}).get("scope")) or "full_available_paper"
    background = scope in {"abstract_only", "abstract_plus_snippets", "snippet_only"}
    allow_background = bool(flags.get("allow_background_material"))
    if scope == "metadata_only":
        issues.append({"code": "metadata_only_material_blocked", "material_scope": scope, "severity": "block"})
    elif requested_scope == "full_available_paper" and background and not allow_background:
        issues.append({"code": "background_scope_requires_override", "material_scope": scope, "severity": "block"})
    errors = [item for item in issues if item.get("severity") == "block"]
    report = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "valid": not errors,
        "issues": issues,
        "scope_mode": "background_supplement" if background else "full_available_text",
        "material_scope": scope,
        "snapshot_id": snapshot_id,
        "identity_status": identity_status or "unknown",
    }
    if errors and raise_on_error:
        raise Module4Blocked(";".join(_text(item.get("code")) for item in errors))
    return report


def snapshot_payload(snapshot: Any) -> dict[str, Any]:
    """Expose a provider-neutral snapshot payload for the reader."""

    manifest, blocks, snapshot_id = _snapshot_parts(snapshot)
    assets = [dict(row) for row in getattr(snapshot, "assets", ()) or ()]
    references = [dict(row) for row in getattr(snapshot, "references", ()) or ()]
    if isinstance(snapshot, Mapping):
        assets = [dict(row) for row in snapshot.get("assets") or assets if isinstance(row, Mapping)]
        references = [dict(row) for row in snapshot.get("references") or references if isinstance(row, Mapping)]
    return {"snapshot_id": snapshot_id, "manifest": manifest, "blocks": blocks, "assets": assets, "references": references}


__all__ = [
    "SCHEMA_VERSION", "INPUT_SCHEMA_VERSION", "DOSSIER_SCHEMA_VERSION", "FEEDBACK_SCHEMA_VERSION",
    "Module4Blocked", "ContractError", "assemble_reading_input", "validate_input", "snapshot_payload",
]
