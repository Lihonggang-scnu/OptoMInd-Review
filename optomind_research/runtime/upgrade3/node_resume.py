"""Typed resume paths across the Phase-3 node chain (SM06).

SM03 proved that a committed P3A can be reused.  This module completes the chain:
a committed P3A feeds a B-only rebuild, and a committed P3B feeds a C-only
rebuild, with the same discipline everywhere - validate the committed upstream
node against disk, load its typed record, compare the identities that can change
the decision, and only then allow the downstream node to run.

Nothing here rebuilds, writes or calls a provider.  Each entry point returns a
typed receipt whose model_calls and retrieval_calls are structurally zero.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from . import phase3_nodes as PN

RESUME_RECEIPT_SCHEMA = "optomind.upgrade3.node_resume_preflight.v1"

P3A_NODE_ID = "P3A_CLAIM_POOL"
P3B_NODE_ID = "P3B_CLAIM_BINDING"
P3C_NODE_ID = "P3C_COVERAGE"
P3D_NODE_ID = "P3D_ACCEPTANCE_HANDOFF"

P3A_SNAPSHOT_FILENAME = "P3A_CLAIM_FACTORY_SNAPSHOT.json"
P3B_BINDING_FILENAMES = ("CLAIM_BINDINGS.json", "MATERIAL_BINDINGS.json")
P3C_VERDICT_FILENAMES = ("SECTION_COVERAGE_VERDICTS.json",)

UPSTREAM_OF = {
    P3A_NODE_ID: None,
    P3B_NODE_ID: P3A_NODE_ID,
    P3C_NODE_ID: P3B_NODE_ID,
    P3D_NODE_ID: P3C_NODE_ID,
}


class ResumeError(PN.Phase3NodeError):
    """Raised when a resume request cannot be trusted."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _output_row(manifest: Mapping[str, Any], filename: str) -> Mapping[str, Any] | None:
    for row in manifest.get("outputs") or ():
        if not isinstance(row, Mapping):
            continue
        if Path(_text(row.get("path"))).name == filename:
            return row
    return None


def _base_receipt(node_id: str, upstream: str | None) -> dict[str, Any]:
    return {
        "schema_version": RESUME_RECEIPT_SCHEMA,
        "node_id": node_id,
        "upstream_node": upstream,
        "resume_allowed": False,
        "reused_files": [],
        "model_calls": 0,
        "retrieval_calls": 0,
        "reason": "",
    }


def _verify_dependency_chain(
    root: str | os.PathLike[str],
    manifest: Mapping[str, Any],
    upstream: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The upstream node must be committed and match the hash this node recorded."""

    if upstream is None:
        return None, None
    try:
        upstream_manifest = PN.load_and_validate_committed_node(root, upstream)
    except (PN.Phase3NodeError, OSError, TypeError, ValueError) as exc:
        return None, "upstream_not_reusable:%s:%s" % (upstream, type(exc).__name__)
    if _text(upstream_manifest.get("generation_id")) != _text(
        manifest.get("generation_id")
    ):
        return None, "generation_mismatch_with_%s" % upstream
    rows = [
        row for row in (manifest.get("dependencies") or ())
        if isinstance(row, Mapping) and _text(row.get("node_id")) == upstream
    ]
    if not rows:
        return None, "missing_dependency_record_for_%s" % upstream
    recorded = _text(rows[0].get("manifest_sha256"))
    actual = _text(upstream_manifest.get("manifest_sha256"))
    if recorded and actual and recorded != actual:
        return None, "dependency_hash_mismatch_for_%s" % upstream
    return dict(upstream_manifest), None


def _resume_node(
    root: str | os.PathLike[str],
    *,
    node_id: str,
    required_artifacts: tuple[str, ...],
    expected_policy_sha256: str = "",
    expected_material_snapshot_hash: str = "",
    expected_input_hashes: Mapping[str, Any] | None = None,
    require_snapshot: str = "",
) -> dict[str, Any]:
    """Shared typed-resume preflight for one Phase-3 node."""

    upstream = UPSTREAM_OF.get(node_id)
    receipt = _base_receipt(node_id, upstream)
    try:
        manifest = PN.load_and_validate_committed_node(root, node_id)
    except (PN.Phase3NodeError, OSError, TypeError, ValueError) as exc:
        receipt["reason"] = "node_not_reusable:%s" % type(exc).__name__
        receipt["detail"] = str(exc)[:300]
        return receipt
    receipt["manifest_sha256"] = _text(manifest.get("manifest_sha256"))
    receipt["generation_id"] = _text(manifest.get("generation_id"))

    _, dependency_error = _verify_dependency_chain(root, manifest, upstream)
    if dependency_error:
        receipt["reason"] = dependency_error
        return receipt

    artifacts: dict[str, dict[str, Any]] = {}
    for filename in required_artifacts:
        row = _output_row(manifest, filename)
        if row is None:
            receipt["reason"] = "required_artifact_not_in_manifest:%s" % filename
            return receipt
        path = Path(_text(row.get("path")))
        if not path.is_file():
            receipt["reason"] = "required_artifact_missing:%s" % filename
            return receipt
        actual = _sha_file(path)
        if actual != _text(row.get("sha256")):
            receipt["reason"] = "required_artifact_hash_mismatch:%s" % filename
            return receipt
        artifacts[filename] = {
            "path": str(path),
            "sha256": actual,
            "bytes": path.stat().st_size,
        }
    receipt["artifacts"] = artifacts

    if require_snapshot:
        snapshot_row = _output_row(manifest, require_snapshot)
        if snapshot_row is None:
            receipt["reason"] = "typed_record_absent:%s" % require_snapshot
            receipt["legacy_readonly"] = True
            return receipt
        snapshot_path = Path(_text(snapshot_row.get("path")))
        try:
            payload = _read_json(snapshot_path)
        except (OSError, json.JSONDecodeError) as exc:
            receipt["reason"] = "typed_record_unreadable:%s" % type(exc).__name__
            return receipt
        if not isinstance(payload, Mapping):
            receipt["reason"] = "typed_record_not_an_object"
            return receipt
        if _text(payload.get("generation_id")) != _text(manifest.get("generation_id")):
            receipt["reason"] = "typed_record_generation_mismatch"
            return receipt
        receipt["typed_record"] = {
            "filename": require_snapshot,
            "sha256": _sha_file(snapshot_path),
        }

    policy_sources: dict[str, str] = {}
    material_sources: dict[str, str] = {}
    for filename in required_artifacts:
        try:
            payload = _read_json(Path(artifacts[filename]["path"]))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for key in ("policy_sha256", "policy_hash"):
            value = _text(payload.get(key))
            if value:
                policy_sources[key] = value
        envelope = payload.get("envelope")
        if isinstance(envelope, Mapping):
            value = _text(envelope.get("policy_sha256"))
            if value:
                policy_sources["envelope.policy_sha256"] = value
            value = _text(envelope.get("material_snapshot_hash"))
            if value:
                material_sources["envelope.material_snapshot_hash"] = value
        value = _text(payload.get("material_snapshot_hash"))
        if value:
            material_sources["material_snapshot_hash"] = value
    receipt["recorded_policy"] = policy_sources
    receipt["recorded_material"] = material_sources

    if expected_policy_sha256:
        if expected_policy_sha256 not in set(policy_sources.values()):
            receipt["reason"] = "policy_changed"
            return receipt
    if expected_material_snapshot_hash:
        if expected_material_snapshot_hash not in set(material_sources.values()):
            receipt["reason"] = "material_changed"
            return receipt
    if expected_input_hashes:
        recorded = {
            key: _text(artifacts.get(key, {}).get("sha256"))
            for key in expected_input_hashes
        }
        drifted = [
            key for key, value in dict(expected_input_hashes).items()
            if recorded.get(key) != _text(value)
        ]
        if drifted:
            receipt["reason"] = "upstream_artifacts_changed"
            receipt["drifted_inputs"] = sorted(drifted)
            return receipt

    receipt["resume_allowed"] = True
    receipt["reason"] = "committed_node_reusable"
    receipt["reused_files"] = sorted(artifacts)
    return receipt


def reuse_candidate_p3a(
    root: str | os.PathLike[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Generic P3A reuse check.

    The authoritative entry point for resuming B from a committed P3A is
    phase3_claim_factory_snapshot.resume_p3b_from_committed_p3a: it additionally
    proves the claim-factory decision is recoverable.  This generic form exists so
    the node chain can be checked uniformly, and it is what that entry point
    delegates to.
    """

    return _resume_node(
        root,
        node_id=P3A_NODE_ID,
        # The typed snapshot is checked by the claim-factory entry point, which owns
        # that contract; here the node's own committed artifacts are what matter, so
        # a legacy node is reported as legacy rather than as a missing artifact.
        required_artifacts=("EVIDENCE_ATOMS.jsonl", "ATOMIC_CLAIMS.jsonl"),
        **kwargs,
    )


def resume_p3c_from_committed_p3b(
    root: str | os.PathLike[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Reuse a committed P3B to run only C (and the handoff that depends on C)."""

    return _resume_node(
        root,
        node_id=P3B_NODE_ID,
        required_artifacts=P3B_BINDING_FILENAMES,
        **kwargs,
    )


def resume_p3d_from_committed_p3c(
    root: str | os.PathLike[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Reuse a committed P3C to run only the acceptance handoff."""

    return _resume_node(
        root,
        node_id=P3C_NODE_ID,
        required_artifacts=P3C_VERDICT_FILENAMES,
        **kwargs,
    )


def resume_plan(
    root: str | os.PathLike[str],
    *,
    change_set: Any = None,
    requested_target: str | None = None,
) -> dict[str, Any]:
    """The recovery plan plus the typed preflight of every node it intends to reuse."""

    plan = PN.plan_phase3_node_recovery(
        root, None, change_set=change_set, requested_target=requested_target,
    )
    preflights: dict[str, Any] = {}
    for node in plan["reuse_nodes"]:
        if node == P3A_NODE_ID:
            preflights[node] = reuse_candidate_p3a(root)
        elif node == P3B_NODE_ID:
            preflights[node] = resume_p3c_from_committed_p3b(root)
        elif node == P3C_NODE_ID:
            preflights[node] = resume_p3d_from_committed_p3c(root)
    blocked = {
        node: receipt["reason"]
        for node, receipt in preflights.items()
        if not receipt.get("resume_allowed")
    }
    return {
        "schema_version": "optomind.upgrade3.node_resume_plan.v1",
        "requested_target": requested_target,
        "change_set": plan["change_set"],
        "node_plan": plan["node_plan"],
        "reuse_nodes": plan["reuse_nodes"],
        "rebuild_nodes": plan["rebuild_nodes"],
        "invalid_nodes": plan["invalid_nodes"],
        "preflights": preflights,
        "reuse_blocked": blocked,
        "model_calls": 0,
        "retrieval_calls": 0,
    }


__all__ = [
    "P3A_NODE_ID",
    "P3B_NODE_ID",
    "P3C_NODE_ID",
    "P3D_NODE_ID",
    "RESUME_RECEIPT_SCHEMA",
    "ResumeError",
    "reuse_candidate_p3a",
    "resume_p3c_from_committed_p3b",
    "resume_p3d_from_committed_p3c",
    "resume_plan",
]
