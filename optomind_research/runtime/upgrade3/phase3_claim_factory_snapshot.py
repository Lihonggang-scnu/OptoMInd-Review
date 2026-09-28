"""Typed P3A claim-factory snapshot and the P3B resume preflight (SM03).

The P3A node already commits its artifacts and hashes them.  What was missing is a
*typed* description of what the claim factory actually decided - which atoms were
eligible, which conditions each claim pinned, which claims were selected and which
were only inventoried - so that a later attempt can reuse that decision instead of
paying for it again.

The snapshot is a witness, not a second authority: it never grants permission, it
is written as an ordinary P3A node output (so it is hashed, projected and validated
by the existing node machinery), and the loader refuses anything that does not
re-verify against the committed node on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import phase3_nodes as PN

SNAPSHOT_SCHEMA = "optomind.upgrade3.phase3_claim_factory_snapshot.v1"
SNAPSHOT_FILENAME = "P3A_CLAIM_FACTORY_SNAPSHOT.json"
RESUME_RECEIPT_SCHEMA = "optomind.upgrade3.p3b_resume_preflight.v1"

P3A_NODE_ID = "P3A_CLAIM_POOL"
P3B_NODE_ID = "P3B_CLAIM_BINDING"


class SnapshotError(PN.Phase3NodeError):
    """Raised when a snapshot or a resume request cannot be trusted."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _row_list(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def build_claim_factory_snapshot(
    *,
    generation_id: str,
    attempt_id: str,
    atom_manifest: Mapping[str, Any],
    atom_rows: Sequence[Mapping[str, Any]],
    claim_manifest: Mapping[str, Any],
    claim_rows: Sequence[Mapping[str, Any]],
    input_hashes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the typed P3A snapshot from the artifacts this attempt produced."""

    atoms = _row_list(atom_rows)
    claims = _row_list(claim_rows)
    atoms_by_id = {_text(atom.get("atom_id")): atom for atom in atoms}
    eligible_ids = [
        _text(row.get("atom_id"))
        for row in (atom_manifest.get("eligible_atoms") or [])
        if _text(row.get("atom_id"))
    ]
    selected_ids = [
        _text(row.get("claim_id"))
        for row in (claim_manifest.get("selected_claims") or [])
        if _text(row.get("claim_id"))
    ]
    unselected_ids = [
        _text(row.get("claim_id"))
        for row in (claim_manifest.get("unselected_claims") or [])
        if _text(row.get("claim_id"))
    ]

    condition_links: list[dict[str, Any]] = []
    source_links: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = _text(claim.get("claim_id"))
        atom_ids = [_text(item) for item in (claim.get("atom_ids") or [])]
        condition_ids = [_text(item) for item in (claim.get("condition_atom_ids") or [])]
        spans = [_text(item) for item in (claim.get("support_span_ids") or [])]
        condition_links.append({
            "claim_id": claim_id,
            "atom_ids": atom_ids,
            "condition_atom_ids": condition_ids,
            "missing_atom_ids": [item for item in atom_ids if item not in atoms_by_id],
            "missing_condition_atom_ids": [
                item for item in condition_ids if item not in atoms_by_id
            ],
            "condition_anchors": list(claim.get("condition_anchors") or []),
        })
        canonical_span_ids: list[str] = []
        pdf_pages: list[Any] = []
        for atom_id in atom_ids:
            span = ((atoms_by_id.get(atom_id) or {}).get("span") or {})
            value = _text(span.get("span_id"))
            if value:
                canonical_span_ids.append(value)
            pdf_pages.append(span.get("pdf_page"))
        source_links.append({
            "claim_id": claim_id,
            "paper_id": _text(claim.get("paper_id")),
            "document_id": _text(claim.get("document_id")),
            "chunk_id": _text(claim.get("chunk_id")),
            "span_ids": spans,
            "canonical_span_ids": canonical_span_ids,
            "pdf_pages": pdf_pages,
            "primary_span_in_atoms": bool(
                spans and set(spans).issubset(set(canonical_span_ids))
            ),
        })

    snapshot_body = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "atom_manifest": {
            "schema_version": _text(atom_manifest.get("schema_version")),
            "manifest_body_sha256": _text(atom_manifest.get("manifest_body_sha256")),
            "counts": dict(atom_manifest.get("counts") or {}),
            "policy_sha256": _text(atom_manifest.get("policy_sha256")),
            "material_snapshot_hash": _text(atom_manifest.get("material_snapshot_hash")),
            "domain_contract_hash": _text(
                (atom_manifest.get("domain_contract") or {}).get("contract_hash")
            ),
            "scope_receipts_hash": _text(
                (atom_manifest.get("scope_receipts") or {}).get("source_sha256")
            ),
            "producer_code_sha256": dict(
                (atom_manifest.get("producer") or {}).get("code_sha256") or {}
            ),
        },
        "claim_manifest": {
            "schema_version": _text(claim_manifest.get("schema_version")),
            "manifest_body_sha256": _text(claim_manifest.get("manifest_body_sha256")),
            "counts": dict(claim_manifest.get("counts") or {}),
            "selection_status": _text(claim_manifest.get("selection_status")),
            "target_range": list(claim_manifest.get("target_range") or []),
            "policy_sha256": _text(claim_manifest.get("policy_sha256")),
            "material_snapshot_hash": _text(claim_manifest.get("material_snapshot_hash")),
            "producer_code_sha256": dict(
                (claim_manifest.get("producer") or {}).get("code_sha256") or {}
            ),
            "atomiser_prompt_manifest_hash": _text(
                (claim_manifest.get("atomiser") or {}).get("prompt_manifest_hash")
            ),
            "atomiser_model_manifest_hash": _text(
                (claim_manifest.get("atomiser") or {}).get("model_manifest_hash")
            ),
        },
        "atom_ids": sorted(_text(atom.get("atom_id")) for atom in atoms),
        "eligible_atom_ids": sorted(eligible_ids),
        # Every claim the attempt considered, whether or not its row was written.
        # The producer writes only the selected claims to ATOMIC_CLAIMS.jsonl and
        # leaves the rest in the manifest, so a snapshot built from the rows alone
        # listed unselected ids that were absent from its own claim list -- which
        # the loader rightly refuses.  It could not show while every section met
        # its target and the unselected set was empty.
        "claim_ids": sorted(
            {_text(claim.get("claim_id")) for claim in claims}
            | set(selected_ids) | set(unselected_ids)
        ),
        "selected_claim_ids": selected_ids,
        "unselected_claim_ids": unselected_ids,
        "condition_links": condition_links,
        "source_links": source_links,
        "input_hashes": dict(input_hashes or {}),
    }
    snapshot = dict(snapshot_body)
    snapshot["snapshot_body_sha256"] = _sha_text(_canonical(snapshot_body))
    return snapshot


def write_snapshot(path: str | os.PathLike[str], snapshot: Mapping[str, Any]) -> Path:
    target = Path(path)
    PN._atomic_write_json(target, snapshot)
    return target


def load_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Strictly load a snapshot: shape, self hash and internal consistency."""

    target = Path(path)
    if not target.is_file():
        raise SnapshotError("claim_factory_snapshot_missing")
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError("claim_factory_snapshot_unparseable") from exc
    if not isinstance(snapshot, Mapping):
        raise SnapshotError("claim_factory_snapshot_not_an_object")
    if _text(snapshot.get("schema_version")) != SNAPSHOT_SCHEMA:
        raise SnapshotError("claim_factory_snapshot_schema_mismatch")
    body = {key: value for key, value in snapshot.items()
            if key != "snapshot_body_sha256"}
    if _sha_text(_canonical(body)) != _text(snapshot.get("snapshot_body_sha256")):
        raise SnapshotError("claim_factory_snapshot_hash_mismatch")

    atom_ids = {_text(item) for item in snapshot.get("atom_ids") or ()}
    eligible = {_text(item) for item in snapshot.get("eligible_atom_ids") or ()}
    claim_ids = {_text(item) for item in snapshot.get("claim_ids") or ()}
    selected = [_text(item) for item in snapshot.get("selected_claim_ids") or ()]
    unselected = [_text(item) for item in snapshot.get("unselected_claim_ids") or ()]
    if not eligible <= atom_ids:
        raise SnapshotError("eligible_atom_not_in_atom_list")
    if not set(selected) <= claim_ids or not set(unselected) <= claim_ids:
        raise SnapshotError("selected_claim_not_in_claim_list")
    if set(selected) & set(unselected):
        raise SnapshotError("claim_both_selected_and_unselected")
    for link in snapshot.get("condition_links") or ():
        if not isinstance(link, Mapping):
            raise SnapshotError("condition_link_not_an_object")
        if _text(link.get("claim_id")) not in claim_ids:
            raise SnapshotError("condition_link_unknown_claim")
        if link.get("missing_atom_ids") or link.get("missing_condition_atom_ids"):
            raise SnapshotError("condition_link_references_missing_atom")
    for link in snapshot.get("source_links") or ():
        if not isinstance(link, Mapping):
            raise SnapshotError("source_link_not_an_object")
        if _text(link.get("claim_id")) not in claim_ids:
            raise SnapshotError("source_link_unknown_claim")
        if not link.get("span_ids") or not link.get("canonical_span_ids"):
            raise SnapshotError("source_link_without_span")
    return dict(snapshot)


def resume_p3b_from_committed_p3a(
    root: str | os.PathLike[str],
    *,
    expected_input_hashes: Mapping[str, Any] | None = None,
    expected_policy_sha256: str = "",
    expected_material_snapshot_hash: str = "",
) -> dict[str, Any]:
    """Preflight a P3B resume from a committed P3A node.

    Returns a typed receipt.  It never rebuilds anything and never calls a model:
    it either proves that P3A may be reused as-is, or it refuses with a concrete
    reason.  model_calls and retrieval_calls are always zero.
    """

    receipt: dict[str, Any] = {
        "schema_version": RESUME_RECEIPT_SCHEMA,
        "p3a_node": P3A_NODE_ID,
        "p3b_node": P3B_NODE_ID,
        "resume_allowed": False,
        "reused_files": [],
        "model_calls": 0,
        "retrieval_calls": 0,
        "reason": "",
    }
    try:
        manifest = PN.load_and_validate_committed_node(root, P3A_NODE_ID)
    except (PN.Phase3NodeError, OSError, TypeError, ValueError) as exc:
        receipt["reason"] = "p3a_not_reusable:%s" % type(exc).__name__
        receipt["detail"] = str(exc)[:300]
        return receipt
    receipt["p3a_manifest_sha256"] = _text(manifest.get("manifest_sha256"))
    receipt["generation_id"] = _text(manifest.get("generation_id"))

    # The node chain check (SM06) is layered underneath this snapshot check: the node
    # must be committed and its upstream identities must agree before the
    # claim-factory record is even inspected.
    try:
        from . import node_resume as _node_resume

        chain = _node_resume.reuse_candidate_p3a(root)
    except Exception as exc:  # pragma: no cover - defensive
        chain = {"resume_allowed": False,
                 "reason": "chain_check_failed:%s" % type(exc).__name__}
    receipt["chain"] = {
        "resume_allowed": bool(chain.get("resume_allowed")),
        "reason": _text(chain.get("reason")),
    }
    if not chain.get("resume_allowed"):
        receipt["reason"] = "chain_%s" % _text(chain.get("reason"))
        return receipt

    snapshot_path = PN.node_dir(root, P3A_NODE_ID) / SNAPSHOT_FILENAME
    try:
        snapshot = load_snapshot(snapshot_path)
    except SnapshotError as exc:
        # A P3A committed before the typed snapshot existed stays readable through
        # the legacy adapter, but it can never be reused: the claim-factory decision
        # it made is not recoverable from the node manifest alone.
        receipt["reason"] = "legacy_p3a_without_snapshot"
        receipt["legacy_readonly"] = True
        receipt["detail"] = str(exc)
        return receipt

    if _text(snapshot.get("generation_id")) != _text(manifest.get("generation_id")):
        receipt["reason"] = "snapshot_generation_mismatch"
        return receipt
    if _text(snapshot.get("attempt_id")) != _text(manifest.get("attempt_id")):
        receipt["reason"] = "snapshot_attempt_mismatch"
        return receipt

    claim_meta = snapshot.get("claim_manifest") or {}
    atom_meta = snapshot.get("atom_manifest") or {}
    if expected_policy_sha256:
        recorded = {
            _text(atom_meta.get("policy_sha256")),
            _text(claim_meta.get("policy_sha256")),
        }
        if expected_policy_sha256 not in recorded:
            receipt["reason"] = "policy_changed"
            receipt["recorded_policy_sha256"] = sorted(item for item in recorded if item)
            return receipt
    if expected_material_snapshot_hash:
        recorded = {
            _text(atom_meta.get("material_snapshot_hash")),
            _text(claim_meta.get("material_snapshot_hash")),
        }
        if expected_material_snapshot_hash not in recorded:
            receipt["reason"] = "material_changed"
            receipt["recorded_material_snapshot_hash"] = sorted(
                item for item in recorded if item
            )
            return receipt
    if expected_input_hashes:
        recorded_inputs = dict(snapshot.get("input_hashes") or {})
        drifted = [
            key for key, value in dict(expected_input_hashes).items()
            if _text(recorded_inputs.get(key)) != _text(value)
        ]
        if drifted:
            receipt["reason"] = "claim_factory_inputs_changed"
            receipt["drifted_inputs"] = sorted(drifted)
            receipt["recorded_input_hashes"] = recorded_inputs
            return receipt

    receipt["resume_allowed"] = True
    receipt["reason"] = "committed_p3a_reusable"
    receipt["reused_files"] = sorted(
        _text(row.get("path")) for row in (manifest.get("outputs") or [])
        if isinstance(row, Mapping)
    )
    receipt["snapshot_body_sha256"] = _text(snapshot.get("snapshot_body_sha256"))
    receipt["selected_claim_ids"] = list(snapshot.get("selected_claim_ids") or [])
    receipt["unselected_claim_ids"] = list(snapshot.get("unselected_claim_ids") or [])
    receipt["eligible_atom_ids"] = list(snapshot.get("eligible_atom_ids") or [])
    receipt["counts"] = {
        "atoms": len(snapshot.get("atom_ids") or ()),
        "eligible_atoms": len(snapshot.get("eligible_atom_ids") or ()),
        "claims": len(snapshot.get("claim_ids") or ()),
        "selected_claims": len(snapshot.get("selected_claim_ids") or ()),
    }
    return receipt


__all__ = [
    "P3A_NODE_ID",
    "P3B_NODE_ID",
    "RESUME_RECEIPT_SCHEMA",
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_SCHEMA",
    "SnapshotError",
    "build_claim_factory_snapshot",
    "load_snapshot",
    "resume_p3b_from_committed_p3a",
    "write_snapshot",
]
