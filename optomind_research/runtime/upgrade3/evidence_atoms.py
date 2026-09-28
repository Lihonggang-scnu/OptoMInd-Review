"""Canonical evidence atoms built from source-checked evidence cards.

Evidence cards are convenient extraction indexes, but their span identifiers
and source metadata are not authoritative.  This module deliberately requires
an explicit identity record and a :class:`DocumentResolver` current version,
then rebuilds the canonical ``EVIDENCE_SPAN`` object before attaching one
field's experiment semantics.  It does not decompose claims and it never
calls a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import contracts
from .domain_contract import contract_hash as _domain_contract_hash
from .evidence_permission import (
    PERMISSION_STRENGTH,
    SCOPE_CAPS,
    SOURCE_CAPS,
    effective_permission,
)
from .source_resolver import DocumentResolver


ATOM_SCHEMA = "optomind.upgrade3.evidence_atom.v1"
EVIDENCE_SPAN_SCHEMA = "optomind.upgrade3.evidence_span.v1"
EVIDENCE_ATOMS_COLLECTION_SCHEMA = "optomind.upgrade3.evidence_atoms.v1"

CARD_FIELDS = ("method", "results", "conditions", "limitations")
EXPERIMENT_TYPES = {"simulation", "experiment", "not_reported"}
EXPERIMENT_LEVELS = {"material_device", "full_system", "not_reported"}
SCOPE_VERDICTS = {"direct", "adjacent", "background", "uncertain", "out_of_scope"}

SOURCE_DEPTH_ALIASES = {
    "full_text": "fulltext",
    "full-text": "fulltext",
    "abstract_only": "abstract",
    "abstract-only": "abstract",
    "metadata_only": "metadata",
    "tldr": "abstract",
    "snippet": "structured_snippet",
    "s2_snippet": "structured_snippet",
    "s2-snippet": "structured_snippet",
    "s2_body": "partial_fulltext",
    "s2-body": "partial_fulltext",
}

SOURCE_RESOLUTION_STATUSES = {
    "current",
    "needs_source",
    "stale",
    "conflict",
    "inventory_only",
}
CLAIM_INPUT_STATUSES = {
    "eligible_for_claim_audit",
    "inventory_only",
    "needs_source",
    "stale",
    "rejected_scope",
    "rejected_permission",
    "rejected_nonreported",
    "rejected_identity",
    "rejected_unknown_enum",
    "rejected_contract",
    "rejected_value_binding",
}

VALUE_BINDING_STATUSES = {"locally_verified", "rejected_value_binding"}
RECEIPT_PERMISSION_CAPS = {
    "direct_support": "direct_support",
    "qualified_support": "qualified_support",
    "factual_support": "qualified_support",
    "reported_only": "reported_only",
    "contextual_or_qualified_support": "reported_only",
    "background_only": "background_only",
    "background_and_candidate_only": "background_only",
    "discovery_only": "discovery_only",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMERIC_UNIT_RE = re.compile(
    r"(?<![a-z0-9])[-+]?\d+(?:\.\d+)?"
    r"(?:\s*(?:[×x*]\s*10\s*(?:\^|\*\*)?\s*[-+]?\d+))?"
    r"(?:\s*[a-zµμΩ%°][a-z0-9µμΩ%°/\-]*)?",
    re.IGNORECASE,
)


class EvidenceAtomError(ValueError):
    """Raised when a card field cannot produce a canonical evidence atom."""

    def __init__(self, message: str, *, rejection: Mapping[str, Any] | None = None):
        self.rejection = dict(rejection or {})
        super().__init__(message)


def _canonical(value: Any) -> str:
    return contracts.canonical_json(value)


def _sha(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _hash_json(value: Any) -> str:
    return _sha(_canonical(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _normalise_source_depth(value: Any) -> str:
    raw = _text(value).casefold()
    return SOURCE_DEPTH_ALIASES.get(raw, raw)


def _normalise_roles(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    roles = sorted({
        _text(item)
        for item in value
        if _text(item)
    })
    return roles or None


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH_RE.fullmatch(value))


def _source_path_without_fragment(value: str) -> str:
    """Return the filesystem part of a provenance locator.

    Scope receipts may use a ``#row`` fragment to identify the selected JSON
    object.  The fragment is part of the recorded locator but not part of the
    path that can be hashed locally.
    """

    return value.split("#", 1)[0]


def _verify_source_file_hash(source: str, expected: str) -> bool | None:
    """Verify a file-backed receipt hash when the locator names a file.

    URI locators (for example ``fixture://scope``) remain opaque test
    provenance and return ``None``.  A filesystem path returns a strict
    boolean so a missing or changed artifact cannot be accepted silently.
    """

    path_text = _source_path_without_fragment(_text(source))
    if not path_text or "://" in path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.is_file():
        # Human-readable provenance labels used by small offline fixtures are
        # intentionally opaque.  A path-looking locator must fail closed when
        # its file is missing, while a label such as ``fixture receipt`` may
        # still be checked by its supplied object hash.
        path_like = (
            "/" in path_text
            or "\\" in path_text
            or bool(re.search(r"\.(?:json|jsonl|sqlite|pdf|txt)$", path_text, re.I))
        )
        return False if path_like else None
    try:
        return _sha(path.read_bytes()) == expected
    except OSError:
        return False


def _normalised_numeric_units(value: Any) -> list[str]:
    """Return number-plus-unit tokens from a value after source folding.

    The value remains a candidate only when its numbers and attached units are
    present in the source slice.  This catches a seemingly plausible value
    such as ``9999 TOPS`` copied onto a quote that says ``1500 TOPS``.
    """

    normalized = _normalise_for_binding(_text(value))
    return [match.group(0).strip() for match in _NUMERIC_UNIT_RE.finditer(normalized)]


def _normalise_for_binding(value: str) -> str:
    # Keep this local to the atom module so value binding follows the same
    # quote normalization as the resolver without making card values an
    # authority.  NFKC handles PDF ligatures; punctuation/whitespace folding
    # is delegated to the resolver's public helper.
    from .source_resolver import _norm

    return _norm(value)


def _value_binding(source_value: str, extracted_value: str) -> tuple[str, str | None, dict[str, Any]]:
    source_normalized = _normalise_for_binding(source_value)
    extracted_normalized = _normalise_for_binding(extracted_value)
    if not extracted_normalized:
        return "rejected_value_binding", "extracted_value_empty", {}
    if extracted_normalized not in source_normalized:
        return (
            "rejected_value_binding",
            "extracted_value_not_local_substring",
            {
                "source_value": source_value,
                "extracted_value": extracted_value,
            },
        )
    source_tokens = set(_normalised_numeric_units(source_value))
    missing_tokens = [
        token for token in _normalised_numeric_units(extracted_value)
        if token not in source_tokens
    ]
    if missing_tokens:
        return (
            "rejected_value_binding",
            "numeric_or_unit_not_in_source_slice",
            {"missing_numeric_or_unit_tokens": missing_tokens},
        )
    return "locally_verified", None, {}


def _card_identity(card: Mapping[str, Any]) -> tuple[str, str]:
    return _text(card.get("document_id")), _text(card.get("card_id"))


def _rejection(
    *,
    card: Mapping[str, Any],
    field_name: str,
    reason: str,
    source_resolution_status: str = "inventory_only",
    permission_ceiling: str = "discovery_only",
    claim_input_status: str | None = None,
    detail: Any = None,
) -> dict[str, Any]:
    status = claim_input_status or source_resolution_status
    if status not in CLAIM_INPUT_STATUSES:
        status = "inventory_only"
    document_id, card_id = _card_identity(card)
    fields = card.get("fields")
    field = fields.get(field_name) if isinstance(fields, Mapping) else None
    return {
        "schema_version": ATOM_SCHEMA,
        "atom_id": "",
        "card_id": card_id,
        "document_id": document_id,
        "experiment_id": _text(card.get("experiment_id")),
        "field_name": field_name,
        "legacy_span_ids": list(field.get("span_ids") or [])
        if isinstance(field, Mapping)
        else [],
        "source_resolution_status": source_resolution_status,
        "permission_ceiling": permission_ceiling,
        "claim_input_status": status,
        "reason": reason,
        "detail": detail,
    }


def _identity_values(
    card: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(identity, Mapping):
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_record_missing",
            claim_input_status="rejected_identity",
        )
    document_id = _text(card.get("document_id"))
    paper_id = _text(identity.get("paper_id"))
    chunk_id = _text(identity.get("chunk_id"))
    scope = _text(identity.get("scope_verdict") or identity.get("scope"))
    depth = _normalise_source_depth(
        identity.get("source_depth") or identity.get("content_depth")
    )
    roles = _normalise_roles(identity.get("roles") or identity.get("literature_roles"))
    if not paper_id or not chunk_id or not scope or not depth or roles is None:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_record_incomplete",
            claim_input_status="rejected_identity",
            detail={
                "has_paper_id": bool(paper_id),
                "has_chunk_id": bool(chunk_id),
                "has_scope_verdict": bool(scope),
                "has_source_depth": bool(depth),
                "has_roles": roles is not None,
            },
        )
    role_provenance = identity.get("role_provenance")
    if role_provenance is not None and not isinstance(role_provenance, Mapping):
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_role_provenance_must_be_object",
            claim_input_status="rejected_identity",
        )
    reserved_role_keys = {
        "paper_id", "chunk_id", "scope_verdict", "source_depth", "roles",
        "scope_receipt_hash", "scope_receipt_source", "scope_receipt_source_sha256",
        "scope_receipt_object_sha256", "scope_receipt_source_path",
        "scope_receipt_permission", "scope_receipt_source_depth",
        "scope_receipt_paper_id", "scope_receipt_chunk_id",
        "domain_contract_hash", "domain_contract_source",
        "domain_contract_source_sha256", "domain_contract_object_sha256",
        "domain_contract_source_path", "owner_text_sha256",
        "owner_kb", "owner_kb_path", "owner_source_path", "owner_source_sha256",
        "owner_object_sha256",
    }
    if isinstance(role_provenance, Mapping):
        overridden = sorted(reserved_role_keys.intersection(role_provenance.keys()))
        if overridden:
            return None, _rejection(
                card=card,
                field_name="",
                reason="identity_role_provenance_reserved_key_override",
                claim_input_status="rejected_identity",
                detail={"keys": overridden},
            )
    identity_document_id = _text(identity.get("document_id"))
    if identity_document_id and identity_document_id != document_id:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_document_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "card_document_id": document_id,
                "identity_document_id": identity_document_id,
            },
        )
    if scope not in SCOPE_VERDICTS or depth not in SOURCE_CAPS:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_scope_or_source_depth_unknown",
            claim_input_status="rejected_unknown_enum",
            detail={"scope_verdict": scope, "source_depth": depth},
        )
    card_paper_id = _text(card.get("paper_id"))
    card_chunk_id = _text(card.get("chunk_id"))
    if card_paper_id and card_paper_id != paper_id:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_paper_id_mismatch",
            claim_input_status="rejected_identity",
            detail={"card_paper_id": card_paper_id, "identity_paper_id": paper_id},
        )
    if card_chunk_id and card_chunk_id != chunk_id:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_chunk_id_mismatch",
            claim_input_status="rejected_identity",
            detail={"card_chunk_id": card_chunk_id, "identity_chunk_id": chunk_id},
        )
    scope_receipt_hash = _text(
        identity.get("scope_receipt_hash") or identity.get("scope_receipt_sha256")
    )
    domain_contract_hash = _text(
        identity.get("domain_contract_hash") or identity.get("domain_contract_sha256")
    )
    if not _is_hash(scope_receipt_hash) or not _is_hash(domain_contract_hash):
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_domain_or_scope_receipt_missing_or_invalid",
            claim_input_status="rejected_identity",
            detail={
                "scope_receipt_hash": scope_receipt_hash,
                "domain_contract_hash": domain_contract_hash,
            },
        )
    scope_receipt_source = _text(identity.get("scope_receipt_source"))
    scope_receipt_source_sha256 = _text(
        identity.get("scope_receipt_source_sha256")
    )
    domain_contract_source = _text(identity.get("domain_contract_source"))
    domain_contract_source_sha256 = _text(
        identity.get("domain_contract_source_sha256")
    )
    if (
        not scope_receipt_source
        or not _is_hash(scope_receipt_source_sha256)
        or not domain_contract_source
        or not _is_hash(domain_contract_source_sha256)
    ):
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_receipt_source_path_or_hash_missing_or_invalid",
            claim_input_status="rejected_identity",
            detail={
                "scope_receipt_source": scope_receipt_source,
                "scope_receipt_source_sha256": scope_receipt_source_sha256,
                "domain_contract_source": domain_contract_source,
                "domain_contract_source_sha256": domain_contract_source_sha256,
            },
        )
    scope_file_hash_ok = _verify_source_file_hash(
        scope_receipt_source,
        scope_receipt_source_sha256,
    )
    if scope_file_hash_ok is False:
        return None, _rejection(
            card=card,
            field_name="",
            reason="scope_receipt_source_hash_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "scope_receipt_source": scope_receipt_source,
                "scope_receipt_source_sha256": scope_receipt_source_sha256,
            },
        )
    declared_scope_path = _text(identity.get("scope_receipt_source_path"))
    if declared_scope_path and declared_scope_path != _source_path_without_fragment(scope_receipt_source):
        return None, _rejection(
            card=card,
            field_name="",
            reason="scope_receipt_source_path_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "declared_scope_receipt_source_path": declared_scope_path,
                "actual_scope_receipt_source_path": _source_path_without_fragment(scope_receipt_source),
            },
        )
    supplied_scope_object_hash = _text(
        identity.get("scope_receipt_object_sha256")
        or identity.get("scope_receipt_object_hash")
    )
    if supplied_scope_object_hash and supplied_scope_object_hash != scope_receipt_hash:
        return None, _rejection(
            card=card,
            field_name="",
            reason="scope_receipt_object_hash_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "scope_receipt_hash": scope_receipt_hash,
                "scope_receipt_object_sha256": supplied_scope_object_hash,
            },
        )
    scope_receipt = identity.get("scope_receipt")
    receipt_paper = ""
    receipt_chunk_ids: list[str] = []
    receipt_depth = ""
    receipt_permission = ""
    if isinstance(scope_receipt, Mapping):
        receipt_scope = _text(
            scope_receipt.get("scope_verdict")
            or scope_receipt.get("scope")
            or scope_receipt.get("scope_fit")
            or scope_receipt.get("verdict")
        )
        if receipt_scope and receipt_scope != scope:
            return None, _rejection(
                card=card,
                field_name="",
                reason="scope_receipt_scope_mismatch",
                claim_input_status="rejected_identity",
                detail={"receipt_scope": receipt_scope, "identity_scope": scope},
            )
        receipt_paper = _text(
            scope_receipt.get("paper_id")
            or scope_receipt.get("document_id")
            or scope_receipt.get("identity")
        )
        if receipt_paper and receipt_paper != paper_id:
            return None, _rejection(
                card=card,
                field_name="",
                reason="scope_receipt_paper_id_mismatch",
                claim_input_status="rejected_identity",
                detail={"receipt_paper_id": receipt_paper, "identity_paper_id": paper_id},
            )
        receipt_chunks = (
            [scope_receipt.get("chunk_id")]
            if scope_receipt.get("chunk_id")
            else
            [scope_receipt.get("canonical_chunk_id")]
            if scope_receipt.get("canonical_chunk_id")
            else
            scope_receipt.get("canonical_chunk_ids")
            or scope_receipt.get("chunk_ids")
            or scope_receipt.get("s2_body_chunk_ids")
            or scope_receipt.get("oa_fulltext_chunk_ids")
        )
        if isinstance(receipt_chunks, (list, tuple, set)) and receipt_chunks:
            receipt_chunk_ids = [str(item) for item in receipt_chunks]
            if chunk_id not in {str(item) for item in receipt_chunks}:
                return None, _rejection(
                    card=card,
                    field_name="",
                    reason="scope_receipt_chunk_id_mismatch",
                    claim_input_status="rejected_identity",
                    detail={"identity_chunk_id": chunk_id},
                )
        receipt_depth = _normalise_source_depth(
            scope_receipt.get("source_depth")
            or scope_receipt.get("content_depth")
        )
        if receipt_depth and receipt_depth not in SOURCE_CAPS:
            return None, _rejection(
                card=card,
                field_name="",
                reason="scope_receipt_source_depth_unknown",
                claim_input_status="rejected_identity",
                detail={"source_depth": receipt_depth},
            )
        receipt_permission = _text(
            scope_receipt.get("use_permission")
            or scope_receipt.get("permission")
            or scope_receipt.get("effective_permission")
        )
        receipt_permission_cap = RECEIPT_PERMISSION_CAPS.get(receipt_permission)
        if receipt_permission and receipt_permission_cap is None:
            return None, _rejection(
                card=card,
                field_name="",
                reason="scope_receipt_permission_unknown",
                claim_input_status="rejected_identity",
                detail={"permission": receipt_permission},
            )
        declared_source_cap = SOURCE_CAPS[depth]
        if receipt_depth and PERMISSION_STRENGTH[SOURCE_CAPS[receipt_depth]] < PERMISSION_STRENGTH[declared_source_cap]:
            return None, _rejection(
                card=card,
                field_name="",
                reason="identity_source_depth_exceeds_scope_receipt",
                claim_input_status="rejected_identity",
                detail={"identity_source_depth": depth, "receipt_source_depth": receipt_depth},
            )
        if receipt_permission_cap and PERMISSION_STRENGTH[receipt_permission_cap] < PERMISSION_STRENGTH[declared_source_cap]:
            return None, _rejection(
                card=card,
                field_name="",
                reason="identity_permission_exceeds_scope_receipt",
                claim_input_status="rejected_identity",
                detail={"identity_source_depth": depth, "receipt_permission": receipt_permission},
            )
        for flag in ("evidence_eligible", "factual_support"):
            if flag in scope_receipt and depth in {"fulltext", "partial_fulltext"} and scope == "direct" and scope_receipt.get(flag) is not True:
                return None, _rejection(
                    card=card,
                    field_name="",
                    reason=f"scope_receipt_{flag}_not_verified",
                    claim_input_status="rejected_identity",
                )
        if _hash_json(scope_receipt) != scope_receipt_hash:
            return None, _rejection(
                card=card,
                field_name="",
                reason="scope_receipt_hash_mismatch",
                claim_input_status="rejected_identity",
            )
    domain_contract = identity.get("domain_contract")
    if isinstance(domain_contract, Mapping):
        computed_domain_hash = _domain_contract_hash(dict(domain_contract))
        envelope = domain_contract.get("envelope")
        envelope_hash = envelope.get("content_sha256") if isinstance(envelope, Mapping) else None
        if computed_domain_hash != domain_contract_hash:
            return None, _rejection(
                card=card,
                field_name="",
                reason="domain_contract_hash_mismatch",
                claim_input_status="rejected_identity",
                detail={
                    "computed_domain_contract_hash": computed_domain_hash,
                    "identity_domain_contract_hash": domain_contract_hash,
                },
            )
        if domain_contract.get("status") != "frozen":
            return None, _rejection(
                card=card,
                field_name="",
                reason="domain_contract_not_frozen",
                claim_input_status="rejected_identity",
                detail={"status": domain_contract.get("status")},
            )
        if envelope_hash != computed_domain_hash:
            return None, _rejection(
                card=card,
                field_name="",
                reason="domain_contract_envelope_hash_mismatch",
                claim_input_status="rejected_identity",
                detail={
                    "computed_domain_contract_hash": computed_domain_hash,
                    "envelope_content_sha256": envelope_hash,
                },
            )
    supplied_domain_object_hash = _text(
        identity.get("domain_contract_object_sha256")
        or identity.get("domain_contract_object_hash")
    )
    if supplied_domain_object_hash and supplied_domain_object_hash != domain_contract_hash:
        return None, _rejection(
            card=card,
            field_name="",
            reason="domain_contract_object_hash_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "domain_contract_hash": domain_contract_hash,
                "domain_contract_object_sha256": supplied_domain_object_hash,
            },
        )
    domain_file_hash_ok = _verify_source_file_hash(
        domain_contract_source,
        domain_contract_source_sha256,
    )
    if domain_file_hash_ok is False:
        return None, _rejection(
            card=card,
            field_name="",
            reason="domain_contract_source_hash_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "domain_contract_source": domain_contract_source,
                "domain_contract_source_sha256": domain_contract_source_sha256,
            },
        )
    declared_domain_path = _text(identity.get("domain_contract_source_path"))
    if declared_domain_path and declared_domain_path != _source_path_without_fragment(domain_contract_source):
        return None, _rejection(
            card=card,
            field_name="",
            reason="domain_contract_source_path_mismatch",
            claim_input_status="rejected_identity",
            detail={
                "declared_domain_contract_source_path": declared_domain_path,
                "actual_domain_contract_source_path": _source_path_without_fragment(domain_contract_source),
            },
        )
    owner_text_hash = _text(
        identity.get("chunk_text_sha256")
        or identity.get("owner_text_sha256")
        or identity.get("chunk_text_hash")
        or identity.get("owner_text_hash")
        or identity.get("text_hash")
        or identity.get("text_sha256")
    )
    if not _is_hash(owner_text_hash):
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_owner_text_hash_missing_or_invalid",
            claim_input_status="rejected_identity",
            detail={"owner_text_sha256": owner_text_hash},
        )
    scope_receipt_permission = _text(
        identity.get("scope_receipt_permission") or receipt_permission
    )
    scope_receipt_source_depth = _normalise_source_depth(
        identity.get("scope_receipt_source_depth") or receipt_depth
    )
    if not scope_receipt_permission or not scope_receipt_source_depth:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_receipt_permission_or_depth_missing",
            claim_input_status="rejected_identity",
        )
    if scope_receipt_source_depth not in SOURCE_CAPS:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_receipt_source_depth_unknown",
            claim_input_status="rejected_identity",
            detail={"source_depth": scope_receipt_source_depth},
        )
    scope_receipt_permission_cap = RECEIPT_PERMISSION_CAPS.get(
        scope_receipt_permission
    )
    if scope_receipt_permission_cap is None:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_receipt_permission_unknown",
            claim_input_status="rejected_identity",
            detail={"permission": scope_receipt_permission},
        )
    if PERMISSION_STRENGTH[SOURCE_CAPS[scope_receipt_source_depth]] < PERMISSION_STRENGTH[SOURCE_CAPS[depth]]:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_source_depth_exceeds_declared_receipt_depth",
            claim_input_status="rejected_identity",
        )
    if PERMISSION_STRENGTH[scope_receipt_permission_cap] < PERMISSION_STRENGTH[SOURCE_CAPS[depth]]:
        return None, _rejection(
            card=card,
            field_name="",
            reason="identity_permission_exceeds_declared_receipt_permission",
            claim_input_status="rejected_identity",
        )
    return {
        "paper_id": paper_id,
        "chunk_id": chunk_id,
        "scope_verdict": scope,
        "source_depth": depth,
        "roles": roles,
        "document_id": identity_document_id or document_id,
        "version_id": _text(identity.get("version_id")),
        "role_provenance": dict(role_provenance or {})
        if isinstance(role_provenance, Mapping)
        else {},
        "scope_receipt_hash": scope_receipt_hash,
        "scope_receipt_object_sha256": scope_receipt_hash,
        "domain_contract_hash": domain_contract_hash,
        "domain_contract_object_sha256": domain_contract_hash,
        "scope_receipt_source": scope_receipt_source,
        "scope_receipt_source_path": _source_path_without_fragment(scope_receipt_source),
        "scope_receipt_source_sha256": scope_receipt_source_sha256,
        "scope_receipt_permission": scope_receipt_permission,
        "scope_receipt_source_depth": scope_receipt_source_depth,
        "scope_receipt_paper_id": receipt_paper or paper_id,
        "scope_receipt_chunk_id": chunk_id,
        "domain_contract_source": domain_contract_source,
        "domain_contract_source_path": _source_path_without_fragment(domain_contract_source),
        "domain_contract_source_sha256": domain_contract_source_sha256,
        "owner_text_sha256": owner_text_hash,
    }, None


def _permission_for_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    # The ceiling is intentionally evaluated at the strongest possible claim
    # role.  This reports the source/scope upper bound without authorizing a
    # later claim; ClaimBinder must still perform its own entailment check.
    return effective_permission(
        source_depth=str(identity["source_depth"]),
        scope_verdict=str(identity["scope_verdict"]),
        claim_role="load_bearing",
    )


def _claim_status_for_permission(
    *,
    scope_verdict: str,
    permission_ceiling: str,
) -> tuple[str, str | None]:
    if scope_verdict in {"out_of_scope", "uncertain", "adjacent", "background"}:
        return "rejected_scope", f"scope_{scope_verdict}_is_not_direct"
    if PERMISSION_STRENGTH.get(permission_ceiling, -1) < PERMISSION_STRENGTH[
        "qualified_support"
    ]:
        return "rejected_permission", "permission_below_qualified_support"
    return "eligible_for_claim_audit", None


def _document_verification(
    resolver: DocumentResolver,
    document_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        result = resolver.verify_document_current(document_id)
    except Exception as exc:
        return None, {
            "status": "needs_source",
            "reason": f"document_verification_failed:{type(exc).__name__}",
            "detail": str(exc)[:300],
        }
    if not isinstance(result, Mapping):
        return None, {
            "status": "needs_source",
            "reason": "document_verification_invalid",
        }
    status = _text(result.get("status")) or "unavailable"
    if status != "current":
        mapped = "stale" if status == "stale" else "needs_source"
        if status == "conflict":
            mapped = "conflict"
        return None, {
            "status": mapped,
            "reason": str(result.get("reason") or f"document_not_current:{status}"),
            "detail": dict(result),
        }
    document = result.get("document")
    canonical_text = result.get("canonical_text")
    if not isinstance(document, Mapping) or not isinstance(canonical_text, str):
        return None, {
            "status": "needs_source",
            "reason": "document_current_payload_incomplete",
        }
    if not _text(document.get("version_id")):
        return None, {
            "status": "conflict",
            "reason": "document_version_id_missing",
        }
    if _text(document.get("body_status")) not in {"complete", "partial"}:
        return None, {
            "status": "needs_source",
            "reason": "document_body_status_not_resolvable",
            "detail": {"body_status": document.get("body_status")},
        }
    source_path = Path(_text(document.get("source_path"))).expanduser().resolve(
        strict=False
    )
    if not source_path.is_file():
        return None, {
            "status": "needs_source",
            "reason": "document_source_missing",
        }
    try:
        raw_hash = _sha(source_path.read_bytes())
    except OSError as exc:
        return None, {
            "status": "needs_source",
            "reason": f"document_source_unreadable:{type(exc).__name__}",
        }
    canonical_hash = _sha(canonical_text)
    expected_raw = _text(document.get("raw_hash"))
    expected_canonical = _text(document.get("canonical_text_hash"))
    if not expected_raw or raw_hash != expected_raw:
        return None, {
            "status": "stale",
            "reason": "document_raw_hash_changed",
            "actual_raw_hash": raw_hash,
            "expected_raw_hash": expected_raw,
        }
    if not expected_canonical:
        return None, {
            "status": "conflict",
            "reason": "document_canonical_text_hash_missing",
        }
    if canonical_hash != expected_canonical:
        return None, {
            "status": "stale",
            "reason": "document_canonical_text_hash_changed",
            "actual_canonical_text_hash": canonical_hash,
            "expected_canonical_text_hash": expected_canonical,
        }
    return {
        "document": dict(document),
        "canonical_text": canonical_text,
        "raw_hash": raw_hash,
        "canonical_text_hash": canonical_hash,
        "source_path": str(source_path),
        "parser": _text(document.get("parser")),
    }, None


def _source_quality(document: Mapping[str, Any], canonical_text: str) -> tuple[str, str | None]:
    """Return the strongest source depth the persisted document can support.

    A source marked partial, a login/references page, or a short extraction can
    never be promoted to fulltext by an identity record that merely says
    ``fulltext``.  The downgrade is deterministic and remains visible to the
    permission receipt.
    """

    body_status = _text(document.get("body_status"))
    normalized = _normalise_for_binding(canonical_text)
    if body_status not in {"complete", "partial"}:
        return "metadata", "document_body_status_not_resolvable"
    if body_status != "complete":
        return "partial_fulltext", "document_body_status_partial"
    if len(normalized) < 200:
        return "partial_fulltext", "document_canonical_text_short"
    head = normalized[:400]
    if re.search(r"sign in|log ?in|create account|subscribe to continue", head):
        return "partial_fulltext", "document_login_or_gate_page"
    if re.match(r"references(?:\s|$)", head):
        return "partial_fulltext", "document_references_only"
    return "fulltext", None


def _effective_source_depth(identity_depth: str, document_depth: str) -> str:
    """Intersect the identity's declared depth with source quality."""

    identity_cap = SOURCE_CAPS.get(identity_depth, "discovery_only")
    document_cap = SOURCE_CAPS.get(document_depth, "discovery_only")
    if PERMISSION_STRENGTH.get(document_cap, -1) < PERMISSION_STRENGTH.get(identity_cap, -1):
        return document_depth
    return identity_depth


def _field_input(
    card: Mapping[str, Any],
    field_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if field_name not in CARD_FIELDS:
        return None, _rejection(
            card=card,
            field_name=field_name,
            reason="field_name_unknown",
            claim_input_status="rejected_unknown_enum",
        )
    fields = card.get("fields")
    if not isinstance(fields, Mapping):
        return None, _rejection(
            card=card,
            field_name=field_name,
            reason="card_fields_missing",
        )
    field = fields.get(field_name)
    if not isinstance(field, Mapping):
        return None, _rejection(
            card=card,
            field_name=field_name,
            reason="card_field_missing",
            claim_input_status="rejected_nonreported",
        )
    status = _text(field.get("status"))
    if status != "extracted":
        if status not in {"not_reported", "not_applicable", "conflicting"}:
            return None, _rejection(
                card=card,
                field_name=field_name,
                reason="card_field_status_unknown",
                claim_input_status="rejected_unknown_enum",
                detail=status,
            )
        return None, _rejection(
            card=card,
            field_name=field_name,
            reason="card_field_not_reported",
            claim_input_status="rejected_nonreported",
            detail=status,
        )
    value = _text(field.get("value"))
    anchor = _text(field.get("anchor_quote") or field.get("quote"))
    if not value or not anchor:
        return None, _rejection(
            card=card,
            field_name=field_name,
            reason="reported_field_value_or_anchor_missing",
            claim_input_status="inventory_only",
        )
    return {
        "status": "reported",
        "value": value,
        "anchor_quote": anchor,
        "legacy_span_ids": list(field.get("span_ids") or []),
    }, None


def _field_condition_id(
    *,
    document_raw_hash: str,
    paper_id: str,
    chunk_id: str,
    experiment_id: str,
    value: str,
    policy_sha256: str,
) -> str:
    return "condition_" + _hash_json(
        {
            "document_raw_hash": document_raw_hash,
            "paper_id": paper_id,
            "chunk_id": chunk_id,
            "experiment_id": experiment_id,
            "value": value,
            "policy_sha256": policy_sha256,
        }
    )[:24]


def _build_attempt(
    *,
    card: Mapping[str, Any],
    field_name: str,
    identity: Mapping[str, Any],
    resolver: DocumentResolver,
    run_id: str,
    generation_id: str,
    attempt_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    condition_atom_ids: Sequence[str] = (),
) -> dict[str, Any]:
    document_id = _text(card.get("document_id"))
    if _text(card.get("schema_version")) != "optomind.upgrade3.evidence_card.v1":
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="card_schema_version_invalid",
                claim_input_status="rejected_contract",
                detail={"schema_version": card.get("schema_version")},
            ),
        }
    missing_card_keys = [
        key for key in ("card_id", "document_id", "experiment_id", "fields")
        if key not in card or card.get(key) in (None, "")
    ]
    if missing_card_keys:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="card_required_field_missing",
                claim_input_status="rejected_contract",
                detail={"keys": missing_card_keys},
            ),
        }
    if not _is_hash(policy_sha256) or not _is_hash(material_snapshot_hash):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="policy_or_material_snapshot_hash_invalid",
                claim_input_status="rejected_contract",
                detail={
                    "policy_sha256": policy_sha256,
                    "material_snapshot_hash": material_snapshot_hash,
                },
            ),
        }
    field, rejection = _field_input(card, field_name)
    if rejection is not None:
        return {"atom": None, "rejection": rejection}
    identity_values, identity_rejection = _identity_values(card, identity)
    if identity_rejection is not None:
        identity_rejection["field_name"] = field_name
        identity_rejection["legacy_span_ids"] = list(field.get("legacy_span_ids") or [])
        return {"atom": None, "rejection": identity_rejection}
    assert identity_values is not None
    current, document_error = _document_verification(resolver, document_id)
    # Permission is calculated only after the persisted document version is
    # known.  A partial/login/reference source can lower a fulltext identity's
    # ceiling and must never be allowed to pass through unchanged.
    permission_receipt = _permission_for_identity(identity_values)
    permission_ceiling = _text(permission_receipt.get("effective_permission"))
    claim_status, claim_reason = _claim_status_for_permission(
        scope_verdict=str(identity_values["scope_verdict"]),
        permission_ceiling=permission_ceiling,
    )
    if document_error is not None:
        status = str(document_error.get("status") or "needs_source")
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason=str(document_error.get("reason") or "document_not_current"),
                source_resolution_status=status,
                permission_ceiling=permission_ceiling,
                claim_input_status=status,
                detail=document_error,
            ),
        }
    assert current is not None
    document = current["document"]
    source_quality, quality_reason = _source_quality(
        document,
        current["canonical_text"],
    )
    effective_identity = dict(identity_values)
    effective_identity["source_depth"] = _effective_source_depth(
        str(identity_values["source_depth"]),
        source_quality,
    )
    permission_receipt = _permission_for_identity(effective_identity)
    permission_ceiling = _text(permission_receipt.get("effective_permission"))
    claim_status, claim_reason = _claim_status_for_permission(
        scope_verdict=str(effective_identity["scope_verdict"]),
        permission_ceiling=permission_ceiling,
    )
    if quality_reason:
        claim_reason = quality_reason
        if claim_status == "eligible_for_claim_audit":
            claim_status = "rejected_permission"
    owner_result = None
    try:
        owner_result = resolver.resolve_chunk(
            str(identity_values["chunk_id"]),
            str(identity_values["paper_id"]),
        )
    except Exception as exc:
        owner_result = {
            "resolved": False,
            "reason": f"owner_resolver_failed:{type(exc).__name__}",
        }
    if not isinstance(owner_result, Mapping) or owner_result.get("resolved") is not True:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason=(str(owner_result.get("reason") or "owner_chunk_not_resolved")
                        if isinstance(owner_result, Mapping)
                        else "owner_chunk_not_resolved"),
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail=dict(owner_result) if isinstance(owner_result, Mapping) else None,
            ),
        }
    if (_text(owner_result.get("paper_id")) != str(identity_values["paper_id"])
            or _text(owner_result.get("chunk_id")) != str(identity_values["chunk_id"])):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="owner_chunk_identity_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail=dict(owner_result),
            ),
        }
    if _text(owner_result.get("text_sha256")) != str(identity_values["owner_text_sha256"]):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="owner_chunk_text_hash_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail={
                    "expected_text_sha256": identity_values["owner_text_sha256"],
                    "actual_text_sha256": owner_result.get("text_sha256"),
                },
            ),
        }
    declared_owner_path = _text(
        identity.get("owner_source_path") or identity.get("owner_kb_path")
    )
    actual_owner_path = _text(
        owner_result.get("source_path") or owner_result.get("path")
    )
    if declared_owner_path and actual_owner_path and declared_owner_path != actual_owner_path:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="owner_source_path_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail={
                    "declared_owner_source_path": declared_owner_path,
                    "actual_owner_source_path": actual_owner_path,
                },
            ),
        }
    declared_owner_source_hash = _text(identity.get("owner_source_sha256"))
    actual_owner_source_hash = _text(owner_result.get("source_sha256"))
    if declared_owner_source_hash and (
        not _is_hash(actual_owner_source_hash)
        or declared_owner_source_hash != actual_owner_source_hash
    ):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="owner_source_hash_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail={
                    "declared_owner_source_sha256": declared_owner_source_hash,
                    "actual_owner_source_sha256": actual_owner_source_hash,
                },
            ),
        }
    declared_owner_object_hash = _text(identity.get("owner_object_sha256"))
    actual_owner_object_hash = _text(owner_result.get("object_sha256"))
    if declared_owner_object_hash and (
        not _is_hash(actual_owner_object_hash)
        or declared_owner_object_hash != actual_owner_object_hash
    ):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="owner_object_hash_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail={
                    "declared_owner_object_sha256": declared_owner_object_hash,
                    "actual_owner_object_sha256": actual_owner_object_hash,
                },
            ),
        }
    card_version_hash = _text(card.get("version_raw_hash"))
    if not card_version_hash:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="card_version_raw_hash_missing",
                source_resolution_status="inventory_only",
                permission_ceiling=permission_ceiling,
                claim_input_status="inventory_only",
            ),
        }
    if card_version_hash != current["raw_hash"]:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="card_version_raw_hash_mismatch",
                source_resolution_status="stale",
                permission_ceiling=permission_ceiling,
                claim_input_status="stale",
                detail={
                    "card_version_raw_hash": card_version_hash,
                    "current_raw_hash": current["raw_hash"],
                },
            ),
        }
    identity_version = _text(identity_values.get("version_id"))
    document_version = _text(document.get("version_id"))
    if identity_version and identity_version != document_version:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="identity_document_version_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_identity",
                detail={
                    "identity_version_id": identity_version,
                    "current_version_id": document_version,
                },
            ),
        }
    try:
        resolved = resolver.resolve_span(
            document_id,
            str(field["anchor_quote"]),
            section=field_name,
        )
    except Exception as exc:
        resolved = {
            "resolved": False,
            "reason": f"resolver_failed:{type(exc).__name__}",
        }
    if not isinstance(resolved, Mapping) or resolved.get("resolved") is not True:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason=str(resolved.get("reason") or "quote_not_resolved")
                if isinstance(resolved, Mapping)
                else "quote_not_resolved",
                source_resolution_status="needs_source",
                permission_ceiling=permission_ceiling,
                claim_input_status="needs_source",
                detail=dict(resolved) if isinstance(resolved, Mapping) else None,
            ),
        }
    span_id = _text(resolved.get("span_id"))
    start = resolved.get("char_start")
    end = resolved.get("char_end")
    page = resolved.get("physical_page")
    canonical_text = current["canonical_text"]
    if (
        not span_id
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not (0 <= start < end <= len(canonical_text))
        or not isinstance(page, int)
        or page < 1
        or resolved.get("pdf_page_kind", "physical") != "physical"
    ):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_span_locator_incomplete",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail=dict(resolved),
            ),
        }
    canonical_slice = canonical_text[start:end]
    if not canonical_slice:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_span_slice_empty",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    if _normalise_for_binding(canonical_slice) != _normalise_for_binding(
        str(field["anchor_quote"])
    ):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_span_anchor_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail={
                    "anchor_quote": str(field["anchor_quote"]),
                    "canonical_slice": canonical_slice,
                },
            ),
        }
    expected_page = canonical_text[:start].count("\f") + 1
    if page != expected_page:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_span_page_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail={"resolved_page": page, "expected_page": expected_page},
            ),
        }
    resolved_quote = _text(resolved.get("quote"))
    if (not resolved_quote
            or _normalise_for_binding(resolved_quote)
            != _normalise_for_binding(str(field["anchor_quote"]))
            or resolved.get("quote_sha256") != _sha(resolved_quote)):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="cached_span_quote_hash_or_anchor_mismatch",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail=dict(resolved),
            ),
        }
    value_binding_status, value_binding_reason, value_binding_detail = _value_binding(
        canonical_slice,
        str(field["value"]),
    )
    logical_page = field.get("logical_page")
    if logical_page is None:
        logical_page = card.get("logical_page")
    logical_page_source = _text(
        field.get("logical_page_source")
        or card.get("logical_page_source")
    )
    if logical_page is not None:
        try:
            logical_page = int(logical_page)
        except (TypeError, ValueError):
            return {
                "atom": None,
                "rejection": _rejection(
                    card=card,
                    field_name=field_name,
                    reason="logical_page_invalid",
                    source_resolution_status="conflict",
                    permission_ceiling=permission_ceiling,
                    claim_input_status="rejected_contract",
                ),
            }
    if logical_page is not None and logical_page < 1:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="logical_page_invalid",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    if logical_page is not None and not logical_page_source:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="logical_page_source_missing",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    parser = _text(document.get("parser"))
    parser_version = _text(document.get("parser_version")) or parser
    if not parser_version:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="parser_version_missing",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    quote_hash = contracts.quote_sha256(canonical_slice)
    span_body = {
        "span_id": span_id,
        "paper_id": str(identity_values["paper_id"]),
        "document_id": document_id,
        "chunk_id": str(identity_values["chunk_id"]),
        "char_start": start,
        "char_end": end,
        "quote": canonical_slice,
        "quote_sha256": quote_hash,
        "section": _text(resolved.get("section")) or field_name,
        "pdf_page": page,
        "pdf_page_kind": "physical",
        "parser_version": parser_version,
        "source_path": current["source_path"],
        "source_sha256": current["raw_hash"],
        "canonical_text_hash": current["canonical_text_hash"],
        "version_id": document["version_id"],
        "source_provenance": {
            "document_id": document_id,
            "version_id": document["version_id"],
            "raw_sha256": current["raw_hash"],
            "canonical_text_sha256": current["canonical_text_hash"],
            "parser": parser_version,
            "source_path": current["source_path"],
        },
        "checked_at": _now(),
    }
    if logical_page is not None:
        span_body["logical_page"] = logical_page
        span_body["logical_page_source"] = logical_page_source
    span_envelope = contracts.make_envelope(
        run_id=run_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        artifact_id=span_id,
        producer="optomind_research.runtime.upgrade3.evidence_atoms",
        body=span_body,
        policy_sha256=policy_sha256,
        parent_artifact_hashes=[current["raw_hash"], current["canonical_text_hash"]],
        prompt_manifest_hash="not_applicable",
        model_manifest_hash="not_applicable",
        material_snapshot_hash=material_snapshot_hash,
    )
    span = {
        "schema_version": EVIDENCE_SPAN_SCHEMA,
        "envelope": span_envelope,
        **span_body,
    }
    span_parse = contracts.parse_object("EVIDENCE_SPAN", span)
    if not span_parse.ok:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_evidence_span_contract_failed",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail=list(span_parse.errors),
            ),
        }
    if span_envelope.get("content_sha256") != contracts.content_sha256(span_body):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="evidence_span_envelope_content_hash_failed",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    experiment_id = _text(card.get("experiment_id"))
    sim_or_experiment = _text(card.get("sim_or_experiment")) or "not_reported"
    experiment_level = _text(card.get("experiment_level")) or "not_reported"
    if sim_or_experiment not in EXPERIMENT_TYPES or experiment_level not in EXPERIMENT_LEVELS:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="experiment_enum_unknown",
                source_resolution_status="current",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_unknown_enum",
                detail={
                    "sim_or_experiment": sim_or_experiment,
                    "experiment_level": experiment_level,
                },
            ),
        }
    if value_binding_status != "locally_verified":
        claim_status = "rejected_value_binding"
        claim_reason = value_binding_reason or "value_not_locally_verified"
    condition_ids: list[str] = []
    if field_name == "conditions":
        condition_ids = [
            _field_condition_id(
                document_raw_hash=current["raw_hash"],
                paper_id=str(identity_values["paper_id"]),
                chunk_id=str(identity_values["chunk_id"]),
                experiment_id=experiment_id,
                value=canonical_slice,
                policy_sha256=policy_sha256,
            )
        ]
    atom_identity = {
        "document_raw_hash": current["raw_hash"],
        "document_id": document_id,
        "paper_id": str(identity_values["paper_id"]),
        "chunk_id": str(identity_values["chunk_id"]),
        "span_id": span_id,
        "experiment_id": experiment_id,
        "field": field_name,
        "value_hash": _sha(canonical_slice),
        "policy_sha256": policy_sha256,
        "condition_ids": condition_ids,
        "condition_atom_ids": sorted({str(item) for item in condition_atom_ids if str(item)}),
    }
    atom_id = "atom_" + _hash_json(atom_identity)[:24]
    atom_body = {
        "atom_id": atom_id,
        "span": span,
        "source_provenance": {
            "document_id": document_id,
            "version_id": document["version_id"],
            "raw_sha256": current["raw_hash"],
            "canonical_text_sha256": current["canonical_text_hash"],
            "parser": parser_version,
            "source_path": current["source_path"],
        },
        "experiment": {
            "experiment_id": experiment_id,
            "sim_or_experiment": sim_or_experiment,
            "experiment_level": experiment_level,
        },
        "field": {
            "name": field_name,
            "status": str(field["status"]),
            "value": canonical_slice,
            "source_value": canonical_slice,
            "extracted_value": str(field["value"]),
            "value_binding_status": value_binding_status,
            "value_binding_detail": value_binding_detail,
            "anchor_quote": str(field["anchor_quote"]),
            "condition_ids": condition_ids,
            "condition_atom_ids": sorted({
                str(item) for item in condition_atom_ids if str(item)
            }),
        },
        "role_provenance": {
            "paper_id": str(identity_values["paper_id"]),
            "chunk_id": str(identity_values["chunk_id"]),
            "scope_verdict": str(identity_values["scope_verdict"]),
            "source_depth": str(effective_identity["source_depth"]),
            "roles": list(effective_identity["roles"]),
            "scope_receipt_hash": str(identity_values["scope_receipt_hash"]),
            "scope_receipt_object_sha256": str(
                identity_values.get("scope_receipt_object_sha256")
                or identity_values["scope_receipt_hash"]
            ),
            "domain_contract_hash": str(identity_values["domain_contract_hash"]),
            "domain_contract_object_sha256": str(
                identity_values.get("domain_contract_object_sha256")
                or identity_values["domain_contract_hash"]
            ),
            "scope_receipt_source": str(identity_values.get("scope_receipt_source") or ""),
            "scope_receipt_source_path": str(
                identity_values.get("scope_receipt_source_path")
                or _source_path_without_fragment(
                    str(identity_values.get("scope_receipt_source") or "")
                )
            ),
            "scope_receipt_source_sha256": str(
                identity_values.get("scope_receipt_source_sha256") or ""
            ),
            "scope_receipt_permission": str(
                identity_values.get("scope_receipt_permission") or ""
            ),
            "scope_receipt_source_depth": str(
                identity_values.get("scope_receipt_source_depth") or ""
            ),
            "scope_receipt_paper_id": str(
                identity_values.get("scope_receipt_paper_id") or ""
            ),
            "scope_receipt_chunk_id": str(
                identity_values.get("scope_receipt_chunk_id") or ""
            ),
            "domain_contract_source": str(identity_values.get("domain_contract_source") or ""),
            "domain_contract_source_path": str(
                identity_values.get("domain_contract_source_path")
                or _source_path_without_fragment(
                    str(identity_values.get("domain_contract_source") or "")
                )
            ),
            "domain_contract_source_sha256": str(
                identity_values.get("domain_contract_source_sha256") or ""
            ),
            "owner_text_sha256": str(identity_values["owner_text_sha256"]),
            "owner_kb": _text(owner_result.get("kb")),
            "owner_kb_path": actual_owner_path,
            "owner_source_path": actual_owner_path,
            "owner_source_sha256": actual_owner_source_hash,
            "owner_object_sha256": actual_owner_object_hash,
            **dict(identity_values.get("role_provenance") or {}),
        },
        "permission_receipt": dict(permission_receipt),
        "permission_ceiling": permission_ceiling,
        "source_resolution_status": "current",
        "claim_input_status": claim_status,
        "claim_input_reason": claim_reason or "source_and_scope_ceiling_verified",
        "authorable": False,
        "binding_required": True,
        "permission_receipt_interpretation": "claim_audit_ceiling_only",
        "legacy_card": {
            "card_id": _text(card.get("card_id")),
            "card_schema_version": _text(card.get("schema_version")),
            "legacy_span_ids": list(field.get("legacy_span_ids") or []),
        },
    }
    atom_envelope = contracts.make_envelope(
        run_id=run_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        artifact_id=atom_id,
        producer="optomind_research.runtime.upgrade3.evidence_atoms",
        body=atom_body,
        policy_sha256=policy_sha256,
        parent_artifact_hashes=[contracts.content_sha256(span)],
        prompt_manifest_hash="not_applicable",
        model_manifest_hash="not_applicable",
        material_snapshot_hash=material_snapshot_hash,
    )
    atom = {
        "schema_version": ATOM_SCHEMA,
        "envelope": atom_envelope,
        **atom_body,
    }
    if atom_envelope.get("content_sha256") != contracts.content_sha256(atom_body):
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="evidence_atom_envelope_content_hash_failed",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
            ),
        }
    atom_parse = contracts.parse_object("EVIDENCE_ATOM", atom)
    if not atom_parse.ok:
        return {
            "atom": None,
            "rejection": _rejection(
                card=card,
                field_name=field_name,
                reason="canonical_evidence_atom_contract_failed",
                source_resolution_status="conflict",
                permission_ceiling=permission_ceiling,
                claim_input_status="rejected_contract",
                detail=list(atom_parse.errors),
            ),
        }
    return {"atom": atom, "rejection": None}


def try_build_evidence_atom(
    *,
    card: Mapping[str, Any],
    field_name: str,
    identity: Mapping[str, Any],
    resolver: DocumentResolver,
    run_id: str,
    generation_id: str,
    attempt_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    condition_atom_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Return ``atom`` or a structured rejection without raising."""

    return _build_attempt(
        card=card,
        field_name=field_name,
        identity=identity,
        resolver=resolver,
        run_id=run_id,
        generation_id=generation_id,
        attempt_id=attempt_id,
        policy_sha256=policy_sha256,
        material_snapshot_hash=material_snapshot_hash,
        condition_atom_ids=condition_atom_ids,
    )


def build_evidence_atom(**kwargs: Any) -> dict[str, Any]:
    """Build one canonical atom, raising with its audit record on rejection."""

    result = try_build_evidence_atom(**kwargs)
    atom = result.get("atom")
    if isinstance(atom, Mapping):
        return dict(atom)
    rejection = dict(result.get("rejection") or {})
    raise EvidenceAtomError(
        str(rejection.get("reason") or "evidence_atom_rejected"),
        rejection=rejection,
    )


def _identity_for_card(
    card: Mapping[str, Any],
    identity_records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    document_id, card_id = _card_identity(card)
    if isinstance(identity_records, Mapping):
        for key in (card_id, document_id):
            candidate = identity_records.get(key)
            if isinstance(candidate, Mapping):
                return candidate
        return None
    candidates = [row for row in identity_records if isinstance(row, Mapping)]
    exact_card = [row for row in candidates if _text(row.get("card_id")) == card_id]
    if len(exact_card) == 1:
        return exact_card[0]
    exact_document = [
        row for row in candidates if _text(row.get("document_id")) == document_id
    ]
    return exact_document[0] if len(exact_document) == 1 else None


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(_canonical(dict(row)))
                handle.write("\n")
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
    return path


def materialize_evidence_atoms(
    cards: Iterable[Mapping[str, Any]],
    *,
    identity_records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    resolver: DocumentResolver,
    run_id: str,
    generation_id: str,
    attempt_id: str,
    policy_sha256: str,
    material_snapshot_hash: str,
    output_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build order-independent atoms and retain every rejection reason."""

    card_rows = [dict(card) for card in cards if isinstance(card, Mapping)]
    card_rows.sort(
        key=lambda card: (
            _text(card.get("document_id")),
            _text(card.get("experiment_id")),
            _text(card.get("card_id")),
        )
    )
    atoms: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for card in card_rows:
        identity = _identity_for_card(card, identity_records)
        if identity is None:
            for field_name in CARD_FIELDS:
                rejection = _rejection(
                    card=card,
                    field_name=field_name,
                    reason="identity_record_not_found_for_card",
                    claim_input_status="rejected_identity",
                )
                rejections.append(rejection)
            continue
        condition_atom_ids: list[str] = []
        condition_result = try_build_evidence_atom(
            card=card,
            field_name="conditions",
            identity=identity,
            resolver=resolver,
            run_id=run_id,
            generation_id=generation_id,
            attempt_id=attempt_id,
            policy_sha256=policy_sha256,
            material_snapshot_hash=material_snapshot_hash,
        )
        if isinstance(condition_result.get("atom"), Mapping):
            condition_atom_ids = [str(condition_result["atom"]["atom_id"])]
            atoms.append(dict(condition_result["atom"]))
        elif isinstance(condition_result.get("rejection"), Mapping):
            rejections.append(dict(condition_result["rejection"]))
        for field_name in ("method", "results", "limitations"):
            result = try_build_evidence_atom(
                card=card,
                field_name=field_name,
                identity=identity,
                resolver=resolver,
                run_id=run_id,
                generation_id=generation_id,
                attempt_id=attempt_id,
                policy_sha256=policy_sha256,
                material_snapshot_hash=material_snapshot_hash,
                condition_atom_ids=condition_atom_ids,
            )
            if isinstance(result.get("atom"), Mapping):
                atoms.append(dict(result["atom"]))
            elif isinstance(result.get("rejection"), Mapping):
                rejections.append(dict(result["rejection"]))
    atoms.sort(key=lambda atom: _text(atom.get("atom_id")))
    rejections.sort(
        key=lambda row: (
            _text(row.get("document_id")),
            _text(row.get("experiment_id")),
            _text(row.get("field_name")),
            _text(row.get("reason")),
        )
    )
    result = {
        "schema_version": EVIDENCE_ATOMS_COLLECTION_SCHEMA,
        "run_id": str(run_id),
        "generation_id": str(generation_id),
        "attempt_id": str(attempt_id),
        "policy_sha256": str(policy_sha256),
        "material_snapshot_hash": str(material_snapshot_hash),
        "atoms": atoms,
        "rejections": rejections,
        "stats": {
            "cards": len(card_rows),
            "atoms": len(atoms),
            "eligible_atoms": sum(
                1
                for atom in atoms
                if atom.get("claim_input_status") == "eligible_for_claim_audit"
            ),
            "rejected_atoms": sum(
                1
                for atom in atoms
                if atom.get("claim_input_status") != "eligible_for_claim_audit"
            ),
            "rejections_without_canonical_span": len(rejections),
        },
        "zero_model_calls": True,
    }
    if output_path is not None:
        _write_jsonl(
            Path(output_path).expanduser().resolve(strict=False),
            atoms,
        )
        result["output_path"] = str(
            Path(output_path).expanduser().resolve(strict=False)
        )
    return result


build_atoms_from_cards = materialize_evidence_atoms


__all__ = [
    "ATOM_SCHEMA",
    "EVIDENCE_SPAN_SCHEMA",
    "EVIDENCE_ATOMS_COLLECTION_SCHEMA",
    "CARD_FIELDS",
    "CLAIM_INPUT_STATUSES",
    "VALUE_BINDING_STATUSES",
    "SOURCE_RESOLUTION_STATUSES",
    "EvidenceAtomError",
    "build_evidence_atom",
    "try_build_evidence_atom",
    "materialize_evidence_atoms",
    "build_atoms_from_cards",
]
