"""Domain admission is not mounting (SM07).

The initial blueprint used to decide what evidence existed: a paper whose role did
not match a section was dropped, and the coverage numbers went down with it.  That
conflates two different questions:

* does this material belong to the review topic at all (domain admission), and
* does it currently have a place to be mounted (section, role, comparison axis)?

This module keeps them apart.  Admission is answered by the existing scope producer
and is the only thing that can keep material out.  Mounting is a proposal: an
in-domain paper that no section can currently host goes to the global unmounted
pool with its scope, permission, locator and domain identity intact, and a consumer
can later propose a mount for it without re-deciding whether it is in domain.

Nothing here grants writing permission.  A pool entry carries the ceiling it already
had; a mount proposal carries a section, a role and an axis, and the permission is
still resolved by the binder.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

POOL_SCHEMA = "optomind.upgrade3.unmounted_evidence_pool.entry.v1"
POOL_MANIFEST_SCHEMA = "optomind.upgrade3.unmounted_evidence_pool.manifest.v1"
PROPOSAL_SCHEMA = "optomind.upgrade3.evidence_mount_proposal.v1"
PROPOSAL_MANIFEST_SCHEMA = "optomind.upgrade3.evidence_mounting.manifest.v1"
MATERIAL_REVISION_SCHEMA = "optomind.upgrade3.material_revision.v1"

POOL_FILENAME = "UNMOUNTED_EVIDENCE_POOL.jsonl"
PROPOSALS_FILENAME = "EVIDENCE_MOUNTING_PROPOSALS.jsonl"
MANIFEST_FILENAME = "EVIDENCE_MOUNTING_MANIFEST.json"

IN_DOMAIN_VERDICTS = ("direct", "adjacent")
OUT_OF_DOMAIN_VERDICTS = ("out_of_scope", "uncertain", "unreviewed", "")


def canonical_scope_verdict(value: Any) -> str:
    """Fold the scope producer vocabulary onto the canonical scope verdicts.

    The scope producer reports acquisition verdicts (reviewed_direct, adjacent,
    background, rejected, uncertain) while the contract vocabulary is
    direct / adjacent / background / out_of_scope / uncertain.  An unmapped or
    missing verdict becomes uncertain, which is not in-domain.
    """

    raw = _text(value).casefold()
    if raw in IN_DOMAIN_VERDICTS or raw in OUT_OF_DOMAIN_VERDICTS:
        return raw
    try:
        from .wiring import VERDICT_TO_SCOPE_FIT

        return VERDICT_TO_SCOPE_FIT.get(raw, "uncertain")
    except Exception:  # pragma: no cover - defensive
        return "uncertain"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        for row in rows:
            handle.write(_canonical(dict(row)))
            handle.write(chr(10))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


# --------------------------------------------------------------------------- #
# material revision reconciliation
# --------------------------------------------------------------------------- #

def material_revision(
    *,
    name: str,
    rows: Sequence[Mapping[str, Any]],
    path: str = "",
    role: str = "authoritative",
    parent: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Describe one knowledge-base revision as a set of material identities.

    The identity of a chunk is (chunk_id, paper_id, text hash): the same id with
    different bytes is a different material, not a duplicate, and a permission or
    scope change is recorded even when the text did not move.
    """

    identities: dict[str, dict[str, Any]] = {}
    for row in rows:
        chunk_id = _text(row.get("chunk_id"))
        paper_id = _text(row.get("paper_id"))
        if not chunk_id:
            continue
        text = _text(row.get("text") or row.get("normalized_text"))
        identities[chunk_id] = {
            "chunk_id": chunk_id,
            "paper_id": paper_id,
            "text_sha256": _sha_text(text),
            "scope_fit": _text(row.get("scope_fit")),
            "content_depth": _text(row.get("content_depth")),
            "use_permission": _text(row.get("use_permission")),
        }
    body = {
        "schema_version": MATERIAL_REVISION_SCHEMA,
        "name": str(name),
        "path": _text(path),
        "role": str(role),
        "parent": _text(parent),
        "notes": _text(notes),
        "chunk_count": len(identities),
        "paper_count": len({row["paper_id"] for row in identities.values()}),
        "identity_sha256": _sha_text(_canonical(identities)),
        "identities": identities,
    }
    return body


def reconcile_material_revisions(
    authority: Mapping[str, Any],
    derived: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain every difference between two revisions.  No silent diffs."""

    a = dict(authority.get("identities") or {})
    b = dict(derived.get("identities") or {})
    only_authority = sorted(set(a) - set(b))
    only_derived = sorted(set(b) - set(a))
    shared = sorted(set(a) & set(b))
    same_hash = [key for key in shared if a[key]["text_sha256"] == b[key]["text_sha256"]]
    same_id_other_hash = [
        key for key in shared if a[key]["text_sha256"] != b[key]["text_sha256"]
    ]
    permission_changed = [
        key for key in same_hash
        if a[key]["use_permission"] != b[key]["use_permission"]
        or a[key]["scope_fit"] != b[key]["scope_fit"]
        or a[key]["content_depth"] != b[key]["content_depth"]
    ]
    unexplained = [
        key for key in same_id_other_hash
        if _text(a[key].get("paper_id")) == _text(b[key].get("paper_id"))
        and a[key]["text_sha256"] != b[key]["text_sha256"]
        and a[key]["text_sha256"] in {
            row["text_sha256"] for row in b.values()
        }
    ]
    return {
        "schema_version": "optomind.upgrade3.material_reconciliation.v1",
        "authority": {
            "name": _text(authority.get("name")),
            "role": _text(authority.get("role")),
            "chunk_count": int(authority.get("chunk_count") or 0),
            "identity_sha256": _text(authority.get("identity_sha256")),
        },
        "derived": {
            "name": _text(derived.get("name")),
            "role": _text(derived.get("role")),
            "parent": _text(derived.get("parent")),
            "chunk_count": int(derived.get("chunk_count") or 0),
            "identity_sha256": _text(derived.get("identity_sha256")),
        },
        "only_in_authority": only_authority,
        "only_in_derived": only_derived,
        "shared_same_text": len(same_hash),
        "same_id_other_hash": same_id_other_hash,
        "permission_or_scope_changed": permission_changed,
        "unexplained_differences": unexplained,
        "explained_percent": round(
            100.0 * (1 - len(unexplained) / max(1, len(shared))), 4
        ),
    }


# --------------------------------------------------------------------------- #
# admission -> mounting
# --------------------------------------------------------------------------- #

def admit_material(
    records: Sequence[Mapping[str, Any]],
    *,
    scope_receipts: Mapping[str, Mapping[str, Any]] | None = None,
    domain_contract_hash: str = "",
    domain_contract_source: str = "",
) -> dict[str, Any]:
    """Answer only the domain question, for every record.

    A record is in domain when its scope receipt says direct or adjacent.  The
    section it happens to sit in, and the role it happens to carry, are deliberately
    not consulted: that is the conflation this module exists to remove.
    """

    receipts = dict(scope_receipts or {})
    admitted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for record in records:
        document_id = _text(
            record.get("document_id")
            or record.get("paper_id")
            or (record.get("source_locator") or {}).get("paper_id")
        )
        receipt = receipts.get(document_id) or {}
        verdict = canonical_scope_verdict(
            receipt.get("verdict") or record.get("scope_fit")
        )
        entry = {
            "document_id": document_id,
            "paper_id": _text(record.get("paper_id")),
            "chunk_id": _text(record.get("chunk_id")),
            "scope_verdict": verdict,
            "scope_receipt_hash": _sha_text(_canonical(receipt)) if receipt else "",
            "scope_receipt_source": _text(receipt.get("source")) or "",
            "domain_contract_hash": _text(domain_contract_hash),
            "domain_contract_source": _text(domain_contract_source),
            "permission_ceiling": _text(
                record.get("permission_ceiling") or record.get("use_permission")
            ),
            "content_depth": _text(record.get("content_depth")),
            "locator": dict(record.get("source_locator") or {}),
            "section_id": _text(record.get("section_id")),
            "literature_role": _text(
                record.get("literature_role") or record.get("role")
            ),
            "literature_roles": _roles_of(record),
        }
        if verdict in IN_DOMAIN_VERDICTS:
            admitted.append(entry)
        else:
            refused.append(dict(entry, refusal_reason="not_in_domain:%s" % (verdict or "missing")))
    return {
        "schema_version": "optomind.upgrade3.domain_admission.v1",
        "admitted": admitted,
        "refused": refused,
        "admitted_count": len(admitted),
        "refused_count": len(refused),
    }


def _roles_of(row: Mapping[str, Any]) -> list[str]:
    """Every role the material is registered under, primary role first."""

    roles: list[str] = []
    for key in ("literature_roles", "original_roles", "roles"):
        declared = row.get(key)
        if isinstance(declared, (list, tuple)):
            roles.extend(_text(item) for item in declared)
    for key in ("literature_role", "original_role", "role", "proposed_role"):
        roles.append(_text(row.get(key)))
    ordered: list[str] = []
    for role in roles:
        if role and role not in ordered:
            ordered.append(role)
    return ordered


def build_unmounted_pool(
    admitted: Sequence[Mapping[str, Any]],
    *,
    mounted_document_ids: Iterable[str] = (),
    mounted_chunk_ids: Iterable[str] = (),
    generation_id: str = "",
) -> list[dict[str, Any]]:
    """In-domain material that currently has no mount, kept with its identity.

    Mounting is decided by the section contract, not by this function; anything
    admitted but not mounted lands here instead of being discarded for carrying the
    wrong role.
    """

    mounted_docs = {_text(item) for item in mounted_document_ids}
    mounted_chunks = {_text(item) for item in mounted_chunk_ids}
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in admitted:
        chunk_id = _text(entry.get("chunk_id"))
        document_id = _text(entry.get("document_id"))
        if chunk_id in mounted_chunks or document_id in mounted_docs:
            continue
        key = chunk_id or document_id
        if key in seen:
            continue
        seen.add(key)
        pool.append({
            "schema_version": POOL_SCHEMA,
            "generation_id": str(generation_id),
            "document_id": document_id,
            "paper_id": _text(entry.get("paper_id")),
            "chunk_id": chunk_id,
            "scope_verdict": _text(entry.get("scope_verdict")),
            "scope_receipt_hash": _text(entry.get("scope_receipt_hash")),
            "domain_contract_hash": _text(entry.get("domain_contract_hash")),
            "permission_ceiling": _text(entry.get("permission_ceiling")),
            "content_depth": _text(entry.get("content_depth")),
            "locator": dict(entry.get("locator") or {}),
            "original_section_id": _text(entry.get("section_id")),
            "original_role": _text(entry.get("literature_role")),
            "original_roles": _roles_of(entry),
            "unmounted_reason": (
                "role_not_used_by_any_section_contract"
                if _text(entry.get("literature_role"))
                else "no_section_contract_referenced_this_material"
            ),
            "created_at": _now(),
        })
    return pool


def propose_mounts(
    pool: Sequence[Mapping[str, Any]],
    *,
    sections: Sequence[Mapping[str, Any]],
    generation_id: str = "",
) -> list[dict[str, Any]]:
    """Propose a mount for pooled material, without granting permission.

    A proposal names the section, the role it would carry and the comparison axis it
    could inform, and records what it was based on: the material already carries a
    role, the section names that role, or the section names the axis.  A proposal is
    never a decision - the coverage verdict and the binder still decide.
    """

    proposals: list[dict[str, Any]] = []
    for entry in pool:
        # A paper may be registered under several roles.  The section is served
        # by the role IT names, not by whichever role happened to be recorded
        # first, so the whole list is offered and the best match is proposed.
        roles = _roles_of(entry) or [_text(entry.get("original_role"))]
        roles = [role for role in roles if role] or [""]
        for section in sections:
            section_id = _text(section.get("section_id"))
            if not section_id:
                continue
            section_role_order: list[str] = []
            for source in (section.get("literature_roles") or (),
                           section.get("required_roles") or ()):
                for item in source:
                    value = _text(item)
                    if value and value not in section_role_order:
                        section_role_order.append(value)
            section_roles = set(section_role_order)
            axes = {_text(item) for item in (section.get("comparison_axes") or ())}
            # When a section names several of the roles the material carries, the
            # section's own declared order decides which one is proposed.
            named = [value for value in section_role_order if value in roles]
            role = named[0] if named else roles[0]
            basis = ""
            if named:
                basis = "section_names_the_material_role"
            elif role and section_roles:
                basis = "role_available_but_not_named_by_this_section"
            elif axes:
                basis = "section_names_comparison_axes"
            if not basis:
                continue
            proposals.append({
                "schema_version": PROPOSAL_SCHEMA,
                "generation_id": str(generation_id),
                "chunk_id": _text(entry.get("chunk_id")),
                "document_id": _text(entry.get("document_id")),
                "section_id": section_id,
                "proposed_role": role or "unassigned",
                "proposed_axes": sorted(axes),
                "basis": basis,
                "permission_ceiling": _text(entry.get("permission_ceiling")),
                "scope_verdict": _text(entry.get("scope_verdict")),
                "locator": dict(entry.get("locator") or {}),
                "requires": "coverage_verdict_and_claim_binding",
            })
    proposals.sort(key=lambda row: (_text(row.get("section_id")),
                                    _text(row.get("chunk_id"))))
    return proposals


def write_mounting_artifacts(
    *,
    output_dir: str | os.PathLike[str],
    pool: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
    generation_id: str,
    material_authority: Mapping[str, Any],
    material_derived: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    material_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pool_path = _write_jsonl(out / POOL_FILENAME, pool)
    proposals_path = _write_jsonl(out / PROPOSALS_FILENAME, proposals)
    body = {
        "schema_version": MANIFEST_FILENAME.replace(".json", "") + ".manifest.v1",
        "generation_id": str(generation_id),
        "pool_manifest_schema": POOL_MANIFEST_SCHEMA,
        "proposal_manifest_schema": PROPOSAL_MANIFEST_SCHEMA,
        "material_authority": {
            "name": _text(material_authority.get("name")),
            "identity_sha256": _text(material_authority.get("identity_sha256")),
            "chunk_count": int(material_authority.get("chunk_count") or 0),
        },
        "material_derived": {
            "name": _text(material_derived.get("name")),
            "identity_sha256": _text(material_derived.get("identity_sha256")),
            "chunk_count": int(material_derived.get("chunk_count") or 0),
        },
        "reconciliation": dict(reconciliation),
        "material_scope": dict(material_scope or {}),
        "counts": {
            "pool": len(pool),
            "proposals": len(proposals),
            "proposals_for_a_named_role": sum(
                1 for row in proposals
                if _text(row.get("basis")) == "section_names_the_material_role"
            ),
        },
        "artifacts": {
            POOL_FILENAME: {
                "filename": POOL_FILENAME,
                "sha256": hashlib.sha256(pool_path.read_bytes()).hexdigest(),
                "rows": len(pool),
            },
            PROPOSALS_FILENAME: {
                "filename": PROPOSALS_FILENAME,
                "sha256": hashlib.sha256(proposals_path.read_bytes()).hexdigest(),
                "rows": len(proposals),
            },
        },
        "created_at": _now(),
    }
    manifest = dict(body)
    manifest["manifest_body_sha256"] = _sha_text(_canonical(body))
    (out / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + chr(10),
        encoding="utf-8",
    )
    return manifest


def read_registered_chunks(
    kb_paths: Sequence[str | os.PathLike[str]],
    *,
    ledger_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Every chunk the registered knowledge bases hold: the generation-global material.

    The unmounted pool is a statement about the whole material set, not about what
    one section happened to serve.  Reading only the served records made the pool
    provably empty (every served record is by definition mounted), so the pool is
    built from the registered libraries instead, and the ledger supplies the role
    and the section each paper was registered under.  Read-only; no verdict here.
    """

    import sqlite3

    index = dict(ledger_index or {})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_path in kb_paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            connection = sqlite3.connect(
                "file:" + str(path).replace("\\", "/") + "?mode=ro&immutable=1",
                uri=True,
            )
        except sqlite3.Error:
            continue
        try:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(text_chunks)")
            }
            if not columns:
                continue
            wanted = [
                name for name in (
                    "chunk_id", "paper_id", "doi", "title", "text", "content_depth",
                    "use_permission", "scope_fit", "source_kind", "raw_json",
                ) if name in columns
            ]
            for row in connection.execute(
                "SELECT %s FROM text_chunks" % ", ".join(wanted)
            ):
                item = dict(row)
                chunk_id = _text(item.get("chunk_id"))
                if not chunk_id or chunk_id in seen:
                    continue
                seen.add(chunk_id)
                entry = index.get(_text(item.get("paper_id"))) or {}
                try:
                    raw = json.loads(item.get("raw_json") or "{}")
                except (TypeError, ValueError):
                    raw = {}
                item["source_locator"] = (
                    raw.get("source_locator") if isinstance(raw, Mapping) else {}
                ) or {"chunk_id": chunk_id, "doi": _text(item.get("doi"))}
                item["literature_role"] = _text(entry.get("literature_role"))
                declared_roles = entry.get("literature_roles")
                item["literature_roles"] = [
                    _text(value) for value in (
                        declared_roles
                        if isinstance(declared_roles, (list, tuple))
                        else ([item["literature_role"]]
                              if item["literature_role"] else []))
                    if _text(value)
                ]
                item["section_id"] = _text(entry.get("section_id"))
                item["permission_ceiling"] = _text(
                    item.get("use_permission") or entry.get("use_permission")
                )
                if not _text(item.get("scope_fit")) and _text(entry.get("scope_fit")):
                    item["scope_fit"] = _text(entry.get("scope_fit"))
                item["registered_kb"] = str(path)
                rows.append(item)
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return rows


def load_pool(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, Mapping):
            rows.append(dict(row))
    return rows


def query_pool(
    rows: Sequence[Mapping[str, Any]],
    *,
    section_id: str = "",
    role: str = "",
    axis: str = "",
) -> list[dict[str, Any]]:
    """The consumer entry point: ask the pool what could serve a need."""

    found: list[dict[str, Any]] = []
    for row in rows:
        if section_id and _text(row.get("original_section_id")) not in ("", section_id):
            continue
        if role and _text(row.get("original_role")) != role:
            continue
        if axis and axis not in (row.get("original_axes") or ()):
            continue
        found.append(dict(row))
    return found


__all__ = [
    "IN_DOMAIN_VERDICTS",
    "MANIFEST_FILENAME",
    "OUT_OF_DOMAIN_VERDICTS",
    "POOL_FILENAME",
    "PROPOSALS_FILENAME",
    "admit_material",
    "build_unmounted_pool",
    "load_pool",
    "material_revision",
    "propose_mounts",
    "read_registered_chunks",
    "query_pool",
    "reconcile_material_revisions",
    "write_mounting_artifacts",
]
