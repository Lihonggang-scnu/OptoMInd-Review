"""Upgrade-3 unified object contracts and compatible projections.

Scope (ticket 004): schema + parse layer + one-way legacy read projections for
nine canonical objects.  No retrieval/authoring behaviour is modified here and
no second master controller is introduced: this module is a pure library that
later tickets (005-030) consume through their own single-owner wiring.

Design rules enforced by this module (global contract):
- envelope on every object; model-called fields may be 'not_applicable' but never empty strings.
- JSON canonicalization: UTF-8, sorted keys, no NaN.
- IDs/status/budget/terminal authority are computed by code; model-provided
  pass flags are advisory and never authoritative.
- legacy ``ready_with_limits`` and friends are read-only projections and can
  never be converted into a science-passed state.
- empty-but-legal sets are expressed as status + audited_count == 0; a missing
  required key is invalid, never "empty".
- unknown enum/version values are invalid; original ``unknown`` never auto-passes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .domain_contract import contract_hash as domain_contract_hash

SCHEMA_BASE = "optomind.upgrade3"
CONTRACTS_VERSION = "v1"

# ---------------------------------------------------------------- enums ----
SCOPE_VERDICTS = {"direct", "adjacent", "background", "out_of_scope", "uncertain"}
CLAIM_ROLES = {"support", "partial", "contradict", "background", "not_enough_information"}
PERMISSIONS = {"discovery_only", "background_only", "reported_only", "qualified_support", "direct_support"}
SOURCE_DEPTHS = {
    "metadata", "abstract", "abstract_claim", "structured_snippet",
    "partial_fulltext", "fulltext",
}
SOURCE_PERMISSION_CAPS = {
    "metadata": "discovery_only",
    "abstract": "reported_only",
    "abstract_claim": "reported_only",
    "structured_snippet": "reported_only",
    "partial_fulltext": "reported_only",
    "fulltext": "qualified_support",
}
SCOPE_PERMISSION_CAPS = {
    "direct": "direct_support",
    "adjacent": "background_only",
    "background": "background_only",
    "uncertain": "discovery_only",
    "out_of_scope": "discovery_only",
}
ROLE_PERMISSION_CAPS = {
    "load_bearing": "direct_support",
    "supporting": "reported_only",
    "background": "background_only",
}
PERMISSION_STRENGTH = {
    "discovery_only": 0,
    "background_only": 1,
    "reported_only": 2,
    "qualified_support": 3,
    "direct_support": 4,
}
IMPORTANCE = {"load_bearing", "supporting", "background"}
ENTAILMENT = {"support", "partial", "contradict", "not_enough_information"}
BINDING_STATUS = {"resolved", "unresolved", "invalid"}
PACKET_STATE = {"ready", "partial", "empty", "needs_fulltext", "conflicting", "invalid"}
EXECUTION_STATE = {"pending", "running", "waiting", "succeeded", "failed", "cancelled"}
SCIENCE_STATE = {"not_evaluated", "needs_evidence", "revision_required", "passed"}
COMPILE_STATE = {"not_started", "failed", "compiled"}
AUX_STATE = {"not_requested", "not_run", "passed", "degraded", "failed"}
FIELD_STATUS = {"reported", "not_reported", "not_applicable", "conflicting"}
STOP_REASONS = {
    "transport_error", "auth_error", "rate_limit", "schema_invalid", "max_iters",
    "token_limit", "budget_exhausted", "no_gain", "evidence_insufficient",
    "review_unavailable", "approval_required",
}
ISSUE_SEVERITY = {"critical", "major", "minor"}
ISSUE_LIFECYCLE = {"open", "revision_proposed", "revision_applied", "independent_review",
                   "resolved", "waived_invalid", "superseded"}
DELIVERY_STATUS = {"failed", "degraded", "passed"}

_OBJECT_KINDS = {
    "DOMAIN_CONTRACT", "EVIDENCE_SPAN", "EVIDENCE_ATOM", "CLAIM_BINDING", "FACT_REGISTRY",
    "STAGE_RECEIPT", "AUTHOR_CANDIDATE", "MANUSCRIPT_MANIFEST",
    "SCIENTIFIC_ISSUE", "TERMINAL_MANIFEST",
}


# ------------------------------------------------------- canonical JSON ----
def canonical_json(obj: Any) -> str:
    """UTF-8, sorted keys, no NaN — the only serialization used for hashes."""

    def _check(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("NaN/Inf forbidden in canonical objects")

    json.dumps(obj, allow_nan=False, ensure_ascii=False)  # raises on NaN
    _check(obj)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def content_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def quote_sha256(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- envelope ----
ENVELOPE_REQUIRED = (
    "run_id", "generation_id", "attempt_id", "artifact_id", "producer",
    "parent_artifact_hashes", "created_at", "content_sha256",
    "policy_sha256", "prompt_manifest_hash", "model_manifest_hash",
    "material_snapshot_hash",
)
_FINGERPRINT_FIELDS = {
    "policy_sha256", "prompt_manifest_hash", "model_manifest_hash",
    "material_snapshot_hash",
}


def make_envelope(run_id: str, generation_id: str, attempt_id: str, artifact_id: str,
                  producer: str, body: Dict[str, Any], policy_sha256: str,
                  parent_artifact_hashes: Optional[List[str]] = None,
                  prompt_manifest_hash: str = "not_applicable",
                  model_manifest_hash: str = "not_applicable",
                  material_snapshot_hash: str = "not_applicable") -> Dict[str, Any]:
    env = {
        "run_id": run_id,
        "generation_id": generation_id,
        "attempt_id": attempt_id,
        "artifact_id": artifact_id,
        "producer": producer,
        "parent_artifact_hashes": list(parent_artifact_hashes or []),
        "created_at": "",
        "content_sha256": "",
        "policy_sha256": policy_sha256,
        "prompt_manifest_hash": prompt_manifest_hash,
        "model_manifest_hash": model_manifest_hash,
        "material_snapshot_hash": material_snapshot_hash,
    }
    body_sha = content_sha256(body)
    env["content_sha256"] = body_sha
    env["created_at"] = _utc_now()
    env["artifact_sha256"] = ""  # placeholder, filled by callers that wrap body+envelope
    return env


def _utc_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_envelope(obj: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    env = obj.get("envelope")
    if not isinstance(env, dict):
        return ["envelope.missing"]
    for key in ENVELOPE_REQUIRED:
        if key not in env:
            errs.append("envelope.%s.missing" % key)
        elif env[key] == "":
            errs.append("envelope.%s.empty_string" % key)
    for key in _FINGERPRINT_FIELDS:
        if key in env and env[key] == "":
            errs.append("envelope.%s.empty_fingerprint" % key)
    if isinstance(env.get("parent_artifact_hashes"), str):
        errs.append("envelope.parent_artifact_hashes.must_be_list")
    return errs


# ------------------------------------------------------------ parse core ----
@dataclass
class ParseResult:
    ok: bool
    object: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)
    demotions: List[str] = field(default_factory=list)  # never upgraded, only tightened

    def first_error(self) -> str:
        return self.errors[0] if self.errors else ""


def _require(raw: Dict[str, Any], keys: Tuple[str, ...], prefix: str, errs: List[str]) -> None:
    for key in keys:
        if key not in raw:
            errs.append("%s.%s.missing" % (prefix, key))


def _enum(raw: Dict[str, Any], key: str, allowed: set, prefix: str, errs: List[str]) -> None:
    val = raw.get(key)
    if key in raw and val not in allowed:
        errs.append("%s.%s.unknown_enum:%r" % (prefix, key, val))


_UNSET = object()


def _cross(raw: Dict[str, Any], key: str, prefix: str, errs: List[str],
           expected: Any = _UNSET, forbidden: Any = _UNSET, reason: str = "contradiction") -> None:
    if key not in raw:
        return
    val = raw[key]
    if expected is not _UNSET and val == expected:
        errs.append("%s.%s.%s" % (prefix, key, reason))
    if forbidden is not _UNSET and isinstance(val, (list, dict)) and len(val) == 0 and forbidden is None:
        errs.append("%s.%s.%s" % (prefix, key, reason))


# ------------------------------------------------- nine object validators ----
def validate_domain_contract(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    # R13 consumes the canonical question-derived domain contract emitted by
    # ``upgrade3.domain_contract``.  Keep the earlier ticket's legacy shape
    # parseable for existing callers, but validate the current shape against
    # its own producer hash rather than a second JSON canonicalisation.
    if "object_phrases" in raw or "original_question" in raw:
        _require(raw, (
            "original_question", "original_question_hash", "object_kind",
            "object_phrases", "object_aliases", "target_system", "task",
            "regime", "inclusions", "exclusions", "provenance",
            "rejected_model_phrases", "required_questions", "comparison_axes",
            "time_scope", "status", "version",
        ),
                 "domain_contract", errs)
        _enum(raw, "status", {"draft", "validated", "frozen", "needs_scope"},
              "domain_contract", errs)
        _enum(raw, "object_kind", {"research_object", "methods+target_system"},
              "domain_contract", errs)
        for key in (
            "object_phrases", "object_aliases", "task", "inclusions", "exclusions",
            "rejected_model_phrases", "required_questions", "comparison_axes",
        ):
            if key in raw and not isinstance(raw.get(key), list):
                errs.append("domain_contract.%s.must_be_list" % key)
        for key in ("original_question", "target_system", "regime", "time_scope"):
            if key in raw and not isinstance(raw.get(key), str):
                errs.append("domain_contract.%s.must_be_string" % key)
        if "provenance" in raw and not isinstance(raw.get("provenance"), dict):
            errs.append("domain_contract.provenance.must_be_object")
        if "original_question_hash" in raw and (
                not isinstance(raw["original_question_hash"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", raw["original_question_hash"])
        ):
            errs.append("domain_contract.original_question_hash.not_a_content_fingerprint")
        elif "original_question" in raw and isinstance(raw.get("original_question"), str):
            expected_question_hash = hashlib.sha256(
                raw["original_question"].encode("utf-8")
            ).hexdigest()
            if raw.get("original_question_hash") != expected_question_hash:
                errs.append("domain_contract.original_question_hash.mismatch")
        if "version" in raw and (
                not isinstance(raw["version"], int) or isinstance(raw["version"], bool)
                or raw["version"] < 1
        ):
            errs.append("domain_contract.version.must_be_positive_integer")
        envelope = raw.get("envelope")
        expected_hash = domain_contract_hash(dict(raw))
        recorded_hash = envelope.get("content_sha256") if isinstance(envelope, dict) else None
        if not isinstance(recorded_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_hash):
            errs.append("domain_contract.envelope.content_sha256.not_a_content_fingerprint")
        elif recorded_hash != expected_hash:
            errs.append("domain_contract.envelope.content_sha256.mismatch")
    else:
        _require(raw, ("topic_fingerprint", "research_object", "task_type",
                       "scope_boundary", "slots", "legacy_read_only"),
                 "domain_contract", errs)
        _enum(raw, "status", {"draft", "adjudicated", "frozen"}, "domain_contract", errs)
        if raw.get("legacy_read_only") is not True:
            errs.append("domain_contract.legacy_read_only.required")
    if "object_phrases" not in raw and "original_question" not in raw:
        boundary = raw.get("scope_boundary")
        if isinstance(boundary, dict):
            if not boundary.get("included_regimes") and not boundary.get("excluded_regimes") \
                    and not boundary.get("boundary_notes"):
                errs.append("domain_contract.scope_boundary.empty_boundary_forbidden")
    return _finish(raw, "DOMAIN_CONTRACT", errs)


def validate_evidence_span(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("span_id", "paper_id", "document_id", "chunk_id",
                   "char_start", "char_end", "quote", "quote_sha256",
                   "section", "pdf_page", "parser_version", "source_path",
                   "source_sha256", "checked_at", "version_id",
                   "source_provenance", "pdf_page_kind"), "evidence_span", errs)
    if "quote" in raw and "quote_sha256" in raw:
        if raw["quote_sha256"] != quote_sha256(raw["quote"]):
            errs.append("evidence_span.quote_sha256.mismatch")
    cs, ce = raw.get("char_start"), raw.get("char_end")
    if isinstance(cs, int) and isinstance(ce, int) and not (0 <= cs < ce):
        errs.append("evidence_span.char_range.invalid")
    if raw.get("pdf_page") in (None, ""):
        errs.append("evidence_span.pdf_page.unambiguous_page_required")
    if raw.get("pdf_page_kind") != "physical":
        errs.append("evidence_span.pdf_page_kind.must_be_physical")
    logical_page = raw.get("logical_page")
    if logical_page is not None and (not isinstance(logical_page, int) or logical_page < 1):
        errs.append("evidence_span.logical_page.must_be_positive_integer")
    if logical_page is not None and not raw.get("logical_page_source"):
        errs.append("evidence_span.logical_page_source.required")
    if raw.get("logical_page_source") is not None and not isinstance(raw.get("logical_page_source"), str):
        errs.append("evidence_span.logical_page_source.must_be_string")
    hash_re = re.compile(r"^[0-9a-f]{64}$")
    for key in ("quote_sha256", "source_sha256", "canonical_text_hash"):
        if key in raw and (not isinstance(raw[key], str) or not hash_re.fullmatch(raw[key])):
            errs.append("evidence_span.%s.not_a_content_fingerprint" % key)
    provenance = raw.get("source_provenance")
    if not isinstance(provenance, dict):
        if "source_provenance" in raw:
            errs.append("evidence_span.source_provenance.must_be_object")
    else:
        _require(provenance, ("document_id", "version_id", "raw_sha256",
                              "canonical_text_sha256", "parser", "source_path"),
                 "evidence_span.source_provenance", errs)
        for key in ("raw_sha256", "canonical_text_sha256"):
            if key in provenance and (not isinstance(provenance[key], str)
                                      or not hash_re.fullmatch(provenance[key])):
                errs.append("evidence_span.source_provenance.%s.not_a_content_fingerprint" % key)
        if (raw.get("source_sha256") and provenance.get("raw_sha256")
                and raw["source_sha256"] != provenance["raw_sha256"]):
            errs.append("evidence_span.source_provenance.raw_sha256.mismatch")
        if (raw.get("document_id") and provenance.get("document_id")
                and raw["document_id"] != provenance["document_id"]):
            errs.append("evidence_span.source_provenance.document_id.mismatch")
        if (raw.get("version_id") and provenance.get("version_id")
                and raw["version_id"] != provenance["version_id"]):
            errs.append("evidence_span.source_provenance.version_id.mismatch")
        if (raw.get("parser_version") and provenance.get("parser")
                and raw["parser_version"] != provenance["parser"]):
            errs.append("evidence_span.source_provenance.parser.mismatch")
        if (raw.get("source_path") and provenance.get("source_path")
                and raw["source_path"] != provenance["source_path"]):
            errs.append("evidence_span.source_provenance.source_path.mismatch")
    envelope = raw.get("envelope")
    if isinstance(envelope, dict):
        if (raw.get("span_id") and envelope.get("artifact_id")
                and envelope.get("artifact_id") != raw.get("span_id")):
            errs.append("evidence_span.envelope.artifact_id.mismatch")
        for key in ("content_sha256", "material_snapshot_hash", "policy_sha256"):
            if key in envelope and (not isinstance(envelope[key], str)
                                    or not hash_re.fullmatch(envelope[key])):
                errs.append("evidence_span.envelope.%s.not_a_content_fingerprint" % key)
        body = {key: value for key, value in raw.items()
                if key not in {"schema_version", "envelope"}}
        if (isinstance(envelope.get("content_sha256"), str)
                and hash_re.fullmatch(envelope["content_sha256"])
                and envelope["content_sha256"] != content_sha256(body)):
            errs.append("evidence_span.envelope.content_sha256.mismatch")
    return _finish(raw, "EVIDENCE_SPAN", errs)


def validate_evidence_atom(raw: Dict[str, Any]) -> ParseResult:
    """Validate the canonical evidence-first atom boundary.

    This validator intentionally keeps the source span and the atom envelope
    independent.  The atom can describe a rejected candidate, but it may never
    turn a ceiling or an extraction flag into an authorable claim.
    """
    errs: List[str] = []
    _require(raw, ("atom_id", "span", "experiment", "field",
                   "role_provenance", "permission_receipt",
                   "permission_ceiling", "source_resolution_status",
                   "claim_input_status", "authorable", "binding_required",
                   "permission_receipt_interpretation"), "evidence_atom", errs)
    span = raw.get("span")
    if not isinstance(span, dict):
        errs.append("evidence_atom.span.must_be_object")
    else:
        span_result = validate_evidence_span(span)
        errs.extend("evidence_atom.span." + error for error in span_result.errors)

    experiment = raw.get("experiment")
    if not isinstance(experiment, dict):
        errs.append("evidence_atom.experiment.must_be_object")
    else:
        _require(experiment, ("experiment_id", "sim_or_experiment",
                              "experiment_level"), "evidence_atom.experiment", errs)
        _enum(experiment, "sim_or_experiment",
              {"simulation", "experiment", "not_reported"},
              "evidence_atom.experiment", errs)
        _enum(experiment, "experiment_level",
              {"material_device", "full_system", "not_reported"},
              "evidence_atom.experiment", errs)

    field = raw.get("field")
    if not isinstance(field, dict):
        errs.append("evidence_atom.field.must_be_object")
    else:
        _require(field, ("name", "status", "value", "source_value",
                         "extracted_value", "value_binding_status",
                         "anchor_quote", "condition_ids", "condition_atom_ids"),
                 "evidence_atom.field", errs)
        _enum(field, "name", {"method", "results", "conditions", "limitations"},
              "evidence_atom.field", errs)
        _enum(field, "status", FIELD_STATUS, "evidence_atom.field", errs)
        _enum(field, "value_binding_status",
              {"locally_verified", "rejected_value_binding"},
              "evidence_atom.field", errs)
        for key in ("condition_ids", "condition_atom_ids"):
            if key in field and not isinstance(field[key], list):
                errs.append("evidence_atom.field.%s.must_be_list" % key)
        if field.get("name") == "conditions" and not field.get("condition_ids"):
            errs.append("evidence_atom.field.conditions.condition_id_required")
        if field.get("name") == "conditions" and field.get("condition_atom_ids"):
            errs.append("evidence_atom.field.conditions.cannot_link_condition_atom")
        if field.get("name") != "conditions" and field.get("condition_ids"):
            errs.append("evidence_atom.field.non_conditions.cannot_define_condition_id")
        if isinstance(span, dict):
            if field.get("source_value") != span.get("quote"):
                errs.append("evidence_atom.field.source_value.span_mismatch")
            if field.get("value") != field.get("source_value"):
                errs.append("evidence_atom.field.value.must_be_source_value")
            normalize = lambda value: re.sub(r"\s+", " ", str(value or "").casefold()).strip()
            if (field.get("value_binding_status") == "locally_verified"
                    and normalize(field.get("extracted_value"))
                    not in normalize(field.get("source_value"))):
                errs.append("evidence_atom.field.value_binding.local_substring_required")

    role = raw.get("role_provenance")
    if not isinstance(role, dict):
        errs.append("evidence_atom.role_provenance.must_be_object")
    else:
        _require(role, (
            "paper_id", "chunk_id", "scope_verdict", "source_depth", "roles",
            "scope_receipt_hash", "scope_receipt_object_sha256",
            "scope_receipt_source", "scope_receipt_source_path",
            "scope_receipt_source_sha256", "scope_receipt_permission",
            "scope_receipt_source_depth", "scope_receipt_paper_id",
            "scope_receipt_chunk_id", "domain_contract_hash",
            "domain_contract_object_sha256", "domain_contract_source",
            "domain_contract_source_path", "domain_contract_source_sha256",
            "owner_text_sha256", "owner_kb", "owner_kb_path",
            "owner_source_path", "owner_source_sha256", "owner_object_sha256",
        ),
                 "evidence_atom.role_provenance", errs)
        _enum(role, "scope_verdict", SCOPE_VERDICTS,
              "evidence_atom.role_provenance", errs)
        if role.get("source_depth") not in SOURCE_DEPTHS:
            errs.append("evidence_atom.role_provenance.source_depth.unknown_enum:%r"
                        % role.get("source_depth"))
        if not isinstance(role.get("roles"), list) or not role.get("roles"):
            errs.append("evidence_atom.role_provenance.roles.required")
        hash_re = re.compile(r"^[0-9a-f]{64}$")
        for key in (
            "scope_receipt_hash", "scope_receipt_object_sha256",
            "scope_receipt_source_sha256", "domain_contract_hash",
            "domain_contract_object_sha256", "domain_contract_source_sha256",
            "owner_text_sha256", "owner_source_sha256", "owner_object_sha256",
        ):
            if key in role and (not isinstance(role[key], str)
                                or not hash_re.fullmatch(role[key])):
                errs.append("evidence_atom.role_provenance.%s.not_a_content_fingerprint" % key)
        if (role.get("scope_receipt_object_sha256")
                and role.get("scope_receipt_hash")
                and role.get("scope_receipt_object_sha256")
                != role.get("scope_receipt_hash")):
            errs.append("evidence_atom.role_provenance.scope_receipt_object_hash.mismatch")
        if (role.get("domain_contract_object_sha256")
                and role.get("domain_contract_hash")
                and role.get("domain_contract_object_sha256")
                != role.get("domain_contract_hash")):
            errs.append("evidence_atom.role_provenance.domain_contract_object_hash.mismatch")
        if (role.get("scope_receipt_paper_id")
                and role.get("scope_receipt_paper_id") != role.get("paper_id")):
            errs.append("evidence_atom.role_provenance.scope_receipt_paper_id.mismatch")
        if (role.get("scope_receipt_chunk_id")
                and role.get("scope_receipt_chunk_id") != role.get("chunk_id")):
            errs.append("evidence_atom.role_provenance.scope_receipt_chunk_id.mismatch")
        receipt_depth = role.get("scope_receipt_source_depth")
        role_depth = role.get("source_depth")
        if receipt_depth not in SOURCE_DEPTHS:
            errs.append("evidence_atom.role_provenance.scope_receipt_source_depth.unknown_enum:%r"
                        % receipt_depth)
        elif role_depth in SOURCE_DEPTHS and (
                PERMISSION_STRENGTH[SOURCE_PERMISSION_CAPS[role_depth]]
                > PERMISSION_STRENGTH[SOURCE_PERMISSION_CAPS[receipt_depth]]):
            errs.append("evidence_atom.role_provenance.source_depth.exceeds_receipt")

    provenance = raw.get("source_provenance")
    if not isinstance(provenance, dict):
        errs.append("evidence_atom.source_provenance.must_be_object")
    else:
        span_provenance = span.get("source_provenance") if isinstance(span, dict) else None
        if isinstance(span_provenance, dict) and provenance != span_provenance:
            errs.append("evidence_atom.source_provenance.span_mismatch")
        provenance_result = {
            "document_id": provenance.get("document_id"),
            "version_id": provenance.get("version_id"),
            "raw_sha256": provenance.get("raw_sha256"),
            "canonical_text_sha256": provenance.get("canonical_text_sha256"),
            "parser": provenance.get("parser"),
            "source_path": provenance.get("source_path"),
        }
        _require(provenance, tuple(provenance_result),
                 "evidence_atom.source_provenance", errs)
        hash_re = re.compile(r"^[0-9a-f]{64}$")
        for key in ("raw_sha256", "canonical_text_sha256"):
            if key in provenance and (not isinstance(provenance[key], str)
                                      or not hash_re.fullmatch(provenance[key])):
                errs.append("evidence_atom.source_provenance.%s.not_a_content_fingerprint" % key)

    _enum(raw, "permission_ceiling", PERMISSIONS, "evidence_atom", errs)
    _enum(raw, "source_resolution_status",
          {"current", "needs_source", "stale", "conflict", "inventory_only"},
          "evidence_atom", errs)
    _enum(raw, "claim_input_status",
          {"eligible_for_claim_audit", "inventory_only", "needs_source", "stale",
           "rejected_scope", "rejected_permission", "rejected_nonreported",
           "rejected_identity", "rejected_unknown_enum", "rejected_contract",
           "rejected_value_binding"},
          "evidence_atom", errs)
    if raw.get("authorable") is not False:
        errs.append("evidence_atom.authorable.must_be_false")
    if raw.get("binding_required") is not True:
        errs.append("evidence_atom.binding_required.must_be_true")
    if raw.get("permission_receipt_interpretation") != "claim_audit_ceiling_only":
        errs.append("evidence_atom.permission_receipt_interpretation.invalid")
    receipt = raw.get("permission_receipt")
    if not isinstance(receipt, dict):
        errs.append("evidence_atom.permission_receipt.must_be_object")
    else:
        _require(receipt, ("effective_permission", "source_cap", "scope_cap",
                           "claim_role_cap", "writable"),
                 "evidence_atom.permission_receipt", errs)
        for key in ("effective_permission", "source_cap", "scope_cap", "claim_role_cap"):
            _enum(receipt, key, PERMISSIONS,
                  "evidence_atom.permission_receipt", errs)
        if not isinstance(receipt.get("writable"), bool):
            errs.append("evidence_atom.permission_receipt.writable.must_be_boolean")
        elif receipt.get("writable") != (
                PERMISSION_STRENGTH.get(receipt.get("effective_permission"), -1)
                >= PERMISSION_STRENGTH["qualified_support"]):
            errs.append("evidence_atom.permission_receipt.writable.mismatch")
        if receipt.get("effective_permission") != raw.get("permission_ceiling"):
            errs.append("evidence_atom.permission_receipt.effective_permission.mismatch")
        if isinstance(role, dict):
            if isinstance(span, dict):
                for key in ("paper_id", "chunk_id"):
                    if span.get(key) != role.get(key):
                        errs.append("evidence_atom.role_provenance.%s.span_mismatch" % key)
            if isinstance(span, dict) and isinstance(provenance, dict):
                for key in ("document_id", "version_id"):
                    if span.get(key) != provenance.get(key):
                        errs.append("evidence_atom.source_provenance.%s.span_mismatch" % key)
                if span.get("source_sha256") != provenance.get("raw_sha256"):
                    errs.append("evidence_atom.source_provenance.raw_sha256.span_mismatch")
            expected_source = SOURCE_PERMISSION_CAPS.get(role.get("source_depth"))
            expected_scope = SCOPE_PERMISSION_CAPS.get(role.get("scope_verdict"))
            if expected_source and receipt.get("source_cap") != expected_source:
                errs.append("evidence_atom.permission_receipt.source_cap.mismatch")
            if expected_scope and receipt.get("scope_cap") != expected_scope:
                errs.append("evidence_atom.permission_receipt.scope_cap.mismatch")
            expected_role = ROLE_PERMISSION_CAPS.get("load_bearing")
            if receipt.get("claim_role_cap") != expected_role:
                errs.append("evidence_atom.permission_receipt.claim_role_cap.mismatch")
            caps = [expected_source, expected_scope, expected_role]
            if all(cap in PERMISSION_STRENGTH for cap in caps):
                expected_effective = min(
                    caps,
                    key=lambda cap: PERMISSION_STRENGTH[cap],
                )
                if receipt.get("effective_permission") != expected_effective:
                    errs.append("evidence_atom.permission_receipt.effective_permission.ceiling_mismatch")

    if raw.get("claim_input_status") == "eligible_for_claim_audit":
        scope = role.get("scope_verdict") if isinstance(role, dict) else None
        field_status = field.get("status") if isinstance(field, dict) else None
        strength = PERMISSION_STRENGTH.get(raw.get("permission_ceiling"), -1)
        if raw.get("source_resolution_status") != "current":
            errs.append("evidence_atom.eligible.source_not_current")
        if scope != "direct":
            errs.append("evidence_atom.eligible.scope_not_direct")
        if field_status != "reported":
            errs.append("evidence_atom.eligible.field_not_reported")
        if strength < PERMISSION_STRENGTH["qualified_support"]:
            errs.append("evidence_atom.eligible.permission_below_qualified")
        if isinstance(field, dict) and field.get("value_binding_status") != "locally_verified":
            errs.append("evidence_atom.eligible.value_not_locally_verified")

    envelope = raw.get("envelope")
    if isinstance(envelope, dict):
        if (raw.get("atom_id") and envelope.get("artifact_id")
                and envelope.get("artifact_id") != raw.get("atom_id")):
            errs.append("evidence_atom.envelope.artifact_id.mismatch")
        hash_re = re.compile(r"^[0-9a-f]{64}$")
        for key in ("content_sha256", "material_snapshot_hash", "policy_sha256"):
            if key in envelope and (not isinstance(envelope[key], str)
                                    or not hash_re.fullmatch(envelope[key])):
                errs.append("evidence_atom.envelope.%s.not_a_content_fingerprint" % key)
        body = {key: value for key, value in raw.items()
                if key not in {"schema_version", "envelope"}}
        if (isinstance(envelope.get("content_sha256"), str)
                and hash_re.fullmatch(envelope["content_sha256"])
                and envelope["content_sha256"] != content_sha256(body)):
            errs.append("evidence_atom.envelope.content_sha256.mismatch")
        if isinstance(span, dict) and isinstance(span.get("envelope"), dict):
            span_material = span["envelope"].get("material_snapshot_hash")
            atom_material = envelope.get("material_snapshot_hash")
            if (span_material and atom_material and span_material != atom_material):
                errs.append("evidence_atom.envelope.material_snapshot_hash.span_mismatch")
    return _finish(raw, "EVIDENCE_ATOM", errs)


def validate_claim_binding(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    demotions: List[str] = []
    _require(raw, ("claim_id", "revision", "atomic_statement", "importance",
                   "required_components", "support_span_ids", "context_span_ids",
                   "condition_ids", "scope_verdict", "permission",
                   "entailment", "binding_status"), "claim_binding", errs)
    _enum(raw, "importance", IMPORTANCE, "claim_binding", errs)
    _enum(raw, "scope_verdict", SCOPE_VERDICTS, "claim_binding", errs)
    _enum(raw, "permission", PERMISSIONS, "claim_binding", errs)
    _enum(raw, "entailment", ENTAILMENT, "claim_binding", errs)
    _enum(raw, "binding_status", BINDING_STATUS, "claim_binding", errs)
    scope = raw.get("scope_verdict")
    perm = raw.get("permission")
    if scope in {"out_of_scope", "uncertain"}:
        strength = PERMISSION_STRENGTH.get(perm, -1)
        if strength >= PERMISSION_STRENGTH["reported_only"]:
            errs.append("claim_binding.permission.escalation_forbidden_for_%s" % scope)
    if raw.get("binding_status") != "resolved" and raw.get("claim_classification") == "supported":
        demotions.append("legacy_supported_with_unresolved_binding.demoted_to_unresolved")
    core_missing = not raw.get("support_span_ids")
    if core_missing and raw.get("context_span_ids"):
        demotions.append("context_spans_cannot_replace_core_support")
    if raw.get("authorable") and (core_missing or raw.get("binding_status") != "resolved"):
        errs.append("claim_binding.authorable_without_resolved_core_support")
    return _finish(raw, "CLAIM_BINDING", errs, demotions)


def validate_fact_registry(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("registry_id", "facts", "audited_count", "status"), "fact_registry", errs)
    facts = raw.get("facts")
    if facts is None:
        errs.append("fact_registry.facts.missing")
        facts = []
    if not isinstance(facts, list):
        errs.append("fact_registry.facts.must_be_list")
        facts = []
    if raw.get("audited_count") != len(facts):
        errs.append("fact_registry.audited_count.mismatch")
    if raw.get("audited_count") == 0 and raw.get("status") not in {None, "empty_legal", "not_evaluated"}:
        errs.append("fact_registry.empty_requires_explicit_status")
    for i, f in enumerate(facts):
        if not isinstance(f, dict):
            errs.append("fact_registry.facts[%d].must_be_object" % i)
            continue
        _require(f, ("fact_id", "claim_id", "source_span_id", "metric", "field_status"),
                 "fact_registry.facts[%d]" % i, errs)
        _enum(f, "field_status", FIELD_STATUS, "fact_registry.facts[%d]" % i, errs)
        has_number = f.get("raw_value") is not None or f.get("normalized_value") is not None
        if has_number and not f.get("source_span_id"):
            errs.append("fact_registry.facts[%d].number_without_source_span" % i)
        if has_number:
            for cond in ("task_or_dataset", "sim_or_experiment", "system_boundary"):
                if cond not in f or f.get(cond) in (None, ""):
                    # conditions must be explicit; 'not_applicable' is legal, silence is not
                    errs.append("fact_registry.facts[%d].condition.%s.missing" % (i, cond))
        if f.get("derived"):
            _require(f, ("derivation_formula", "operand_fact_ids", "unit_transform"),
                     "fact_registry.facts[%d]" % i, errs)
    return _finish(raw, "FACT_REGISTRY", errs)


def validate_stage_receipt(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("receipt_id", "stage_id", "generation_id", "execution",
                   "science", "stop_reason", "consumed_artifact_hashes",
                   "cost_micro_cny"), "stage_receipt", errs)
    _enum(raw, "execution", EXECUTION_STATE, "stage_receipt", errs)
    _enum(raw, "science", SCIENCE_STATE, "stage_receipt", errs)
    if raw.get("stop_reason") is not None and raw.get("stop_reason") not in STOP_REASONS:
        errs.append("stage_receipt.stop_reason.unknown_enum:%r" % raw.get("stop_reason"))
    exec_state = raw.get("execution")
    if exec_state == "succeeded" and raw.get("science") == "passed" and raw.get("evidence_facts_count") == 0:
        errs.append("stage_receipt.pass_with_zero_facts.contradiction")
    if raw.get("admission_status") == "full" and raw.get("acceptance_status") == "failed":
        errs.append("stage_receipt.admission_full_while_acceptance_failed.contradiction")
    if raw.get("blocking_gap_count", 0) and raw.get("blocked_sections") == {} and raw.get("admission_status") == "full":
        errs.append("stage_receipt.blocking_gaps_with_empty_blocked_sections.contradiction")
    return _finish(raw, "STAGE_RECEIPT", errs)


def validate_author_candidate(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    demotions: List[str] = []
    _require(raw, ("candidate_id", "section_id", "generation_id", "draft_text_sha256",
                   "evidence_packet", "citation_map", "validation_level",
                   "candidate_class"), "author_candidate", errs)
    _enum(raw, "validation_level", {"syntax", "scientific", "none"}, "author_candidate", errs)
    _enum(raw, "candidate_class", {"syntax_only", "scientific"}, "author_candidate", errs)
    if raw.get("validation_level") == "syntax" and raw.get("candidate_class") == "scientific":
        errs.append("author_candidate.scientific_class_requires_scientific_validation")
    if raw.get("validation_level") == "none":
        errs.append("author_candidate.validation_level_none_forbidden")
    ep = raw.get("evidence_packet")
    if ep is None:
        errs.append("author_candidate.evidence_packet.missing")
    elif not isinstance(ep, list):
        errs.append("author_candidate.evidence_packet.must_be_list")
    paper_ids = set()
    if isinstance(ep, list):
        for i, row in enumerate(ep):
            if not isinstance(row, dict):
                errs.append("author_candidate.evidence_packet[%d].must_be_object" % i)
                continue
            pid, cid = row.get("paper_id"), row.get("chunk_id")
            if not pid or not cid:
                errs.append("author_candidate.evidence_packet[%d].identity_missing" % i)
            elif pid in paper_ids:
                pass
            paper_ids.add(pid)
            if row.get("span_fallback") and row.get("support_relation") in {"core_support", "component_support"}:
                demotions.append("evidence_packet[%d].fallback_span_demoted_to_context" % i)
    return _finish(raw, "AUTHOR_CANDIDATE", errs, demotions)


def validate_manuscript_manifest(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("manifest_id", "generation_id", "sections", "abstract",
                   "citations", "figures", "compile", "translation", "visual"),
             "manuscript_manifest", errs)
    _enum(raw, "compile", COMPILE_STATE, "manuscript_manifest", errs)
    for aux in ("translation", "visual"):
        _enum(raw, aux, AUX_STATE, "manuscript_manifest", errs)
    sections = raw.get("sections")
    if isinstance(sections, list):
        seen = set()
        for s in sections:
            sid = s.get("section_id") if isinstance(s, dict) else None
            if not sid:
                errs.append("manuscript_manifest.sections.entry_without_id")
            elif sid in seen:
                errs.append("manuscript_manifest.sections.duplicate:%s" % sid)
            seen.add(sid)
    return _finish(raw, "MANUSCRIPT_MANIFEST", errs)


def validate_scientific_issue(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("issue_id", "generation_id", "target_ids", "severity",
                   "issue_type", "lifecycle", "raised_by"), "scientific_issue", errs)
    _enum(raw, "severity", ISSUE_SEVERITY, "scientific_issue", errs)
    _enum(raw, "lifecycle", ISSUE_LIFECYCLE, "scientific_issue", errs)
    sev, life = raw.get("severity"), raw.get("lifecycle")
    if sev == "critical" and life in {"resolved", "waived_invalid"} and not raw.get("independent_review_receipt_hash"):
        errs.append("scientific_issue.critical_resolved_without_independent_review")
    return _finish(raw, "SCIENTIFIC_ISSUE", errs)


def validate_terminal_manifest(raw: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    _require(raw, ("manifest_id", "generation_id", "code_sha256", "policy_sha256",
                   "model_manifest_hash", "material_snapshot_hash", "inputs_sha256",
                   "pdf_sha256", "delivery_status"), "terminal_manifest", errs)
    _enum(raw, "delivery_status", DELIVERY_STATUS, "terminal_manifest", errs)
    for key in ("code_sha256", "policy_sha256", "material_snapshot_hash",
                "inputs_sha256", "pdf_sha256"):
        val = raw.get(key)
        if val in (None, "", "unknown"):
            errs.append("terminal_manifest.%s.fingerprint_required" % key)
        elif not (isinstance(val, str) and re.fullmatch(r"[0-9a-f]{64}", val)):
            errs.append("terminal_manifest.%s.not_a_content_fingerprint" % key)
    return _finish(raw, "TERMINAL_MANIFEST", errs)


_VALIDATORS = {
    "DOMAIN_CONTRACT": validate_domain_contract,
    "EVIDENCE_SPAN": validate_evidence_span,
    "EVIDENCE_ATOM": validate_evidence_atom,
    "CLAIM_BINDING": validate_claim_binding,
    "FACT_REGISTRY": validate_fact_registry,
    "STAGE_RECEIPT": validate_stage_receipt,
    "AUTHOR_CANDIDATE": validate_author_candidate,
    "MANUSCRIPT_MANIFEST": validate_manuscript_manifest,
    "SCIENTIFIC_ISSUE": validate_scientific_issue,
    "TERMINAL_MANIFEST": validate_terminal_manifest,
}


def _finish(raw: Dict[str, Any], kind: str, errs: List[str],
            demotions: Optional[List[str]] = None) -> ParseResult:
    env_errs = validate_envelope(raw)
    schema_version = str(raw.get("schema_version", ""))
    expected_version = "%s.%s.%s" % (SCHEMA_BASE, kind.lower(), CONTRACTS_VERSION)
    if schema_version != expected_version:
        errs.append("schema_version.unknown_or_mismatched:%r" % schema_version)
    all_errors = env_errs + errs
    return ParseResult(ok=not all_errors, object=raw if not all_errors else None,
                       errors=all_errors, demotions=demotions or [])


def parse_object(kind: str, raw: Dict[str, Any]) -> ParseResult:
    """Single entry for the parse boundary. Unknown kinds are rejected."""
    if kind not in _OBJECT_KINDS:
        return ParseResult(ok=False, errors=["object_kind.unknown:%r" % kind])
    if not isinstance(raw, dict):
        return ParseResult(ok=False, errors=["payload.must_be_object"])
    return _VALIDATORS[kind](raw)


# ------------------------------------------------- single terminal commit ----
def commit_terminal(generation_receipts: Dict[str, Dict[str, Any]],
                    manifest: Dict[str, Any]) -> ParseResult:
    """Idempotent single-commit authority for TERMINAL_MANIFEST.

    Same content re-finalize is a no-op; a different manifest for an already
    committed generation is rejected (no post-hoc override)."""
    gen = manifest.get("generation_id")
    existing = generation_receipts.get(gen)
    result = validate_terminal_manifest(manifest)
    if not result.ok:
        return result
    if existing is not None:
        if content_sha256(existing) == content_sha256(manifest):
            return ParseResult(ok=True, object=existing, demotions=["idempotent_recommit"])
        return ParseResult(ok=False, object=None,
                           errors=["terminal_manifest.second_commit_forbidden:%s" % gen])
    generation_receipts[gen] = manifest
    return ParseResult(ok=True, object=manifest)


# ------------------------------------------------------ legacy projections ----
def project_legacy_claim(legacy: Dict[str, Any]) -> ParseResult:
    """One-way read conversion of a legacy Phase3 claim binding.

    Never upgrades: unknown domain stays unknown; unverified/contextual_fallback
    stays unresolved; qualified_only never becomes direct_support; 'supported'
    with unresolved evidence is demoted, not preserved as authorable."""
    binding_status = legacy.get("evidence_binding_status")
    if binding_status in {"matched", "bound"} and legacy.get("factual_support_chunk_ids"):
        canonical_binding = "unresolved"  # legacy 'matched' never proved resolution
    elif binding_status in {"unverified", "contextual_fallback", None}:
        canonical_binding = "unresolved"
    else:
        canonical_binding = "unresolved"
    perm = legacy.get("permission_status")
    # Legacy objects carry no adjudicated scientific domain, so the projection
    # can never inherit any positive permission: everything lands at
    # discovery_only (isolated candidate library).  Raw values are preserved in
    # legacy_display_only for 010/012 to re-adjudicate.
    canonical_perm = "discovery_only"
    scope = "uncertain"
    projected = {
        "schema_version": "%s.claim_binding.%s" % (SCHEMA_BASE, CONTRACTS_VERSION),
        "envelope": {
            "run_id": "legacy_projection",
            "generation_id": str(legacy.get("generation_id") or "unknown_generation"),
            "attempt_id": "not_applicable",
            "artifact_id": str(legacy.get("claim_id") or "unknown_claim"),
            "producer": "upgrade3.contracts.project_legacy_claim(one-way)",
            "parent_artifact_hashes": [],
            "created_at": _utc_now(),
            "content_sha256": content_sha256(legacy),
            "policy_sha256": "not_applicable",
            "prompt_manifest_hash": "not_applicable",
            "model_manifest_hash": "not_applicable",
            "material_snapshot_hash": "not_applicable",
        },
        "claim_id": legacy.get("claim_id"),
        "revision": "legacy_import",
        "atomic_statement": legacy.get("statement") or legacy.get("effective_statement"),
        "importance": "load_bearing" if legacy.get("load_bearing") else "supporting",
        "required_components": legacy.get("missing_evidence_components") or [],
        "support_span_ids": list(legacy.get("factual_support_chunk_ids") or []),
        "context_span_ids": list(legacy.get("contextual_support_chunk_ids") or []),
        "condition_ids": list(legacy.get("boundary_conditions") or []),
        "scope_verdict": scope,
        "permission": canonical_perm,
        "entailment": "not_enough_information" if canonical_binding != "resolved" else "support",
        "binding_status": canonical_binding,
        "authorable": False,
        "review_receipt_hash": "not_applicable",
        "legacy_display_only": {
            "claim_classification": legacy.get("claim_classification"),
            "write_status": legacy.get("write_status"),
            "permission_status_raw": perm,
            "evidence_binding_status_raw": binding_status,
        },
    }
    res = parse_object("CLAIM_BINDING", projected)
    # supported-with-unresolved is a demotion, not a parse failure
    legacy_supported = legacy.get("claim_classification") == "supported" or legacy.get("support_classification") == "supported"
    if legacy_supported and canonical_binding == "unresolved":
        res.demotions.append("legacy_supported_with_unresolved_binding.demoted_to_unresolved")
        res.ok = not res.errors
        res.object = projected if not res.errors else None
    return res


def project_legacy_gate_pair(acceptance: Dict[str, Any], admission: Dict[str, Any]) -> ParseResult:
    """Read R3 acceptance + R4 admission as one stage receipt.

    The legacy pair where acceptance failed but admission is full parses as a
    contradictory receipt (invalid), which is the honest reading."""
    receipt = {
        "schema_version": "%s.stage_receipt.%s" % (SCHEMA_BASE, CONTRACTS_VERSION),
        "envelope": {
            "run_id": "legacy_projection",
            "generation_id": "unknown_generation",
            "attempt_id": "not_applicable",
            "artifact_id": "legacy_gate_pair",
            "producer": "upgrade3.contracts.project_legacy_gate_pair(one-way)",
            "parent_artifact_hashes": [],
            "created_at": _utc_now(),
            "content_sha256": content_sha256({"a": acceptance, "b": admission}),
            "policy_sha256": "not_applicable",
            "prompt_manifest_hash": "not_applicable",
            "model_manifest_hash": "not_applicable",
            "material_snapshot_hash": "not_applicable",
        },
        "receipt_id": "legacy_gate_pair",
        "stage_id": "r3_to_r4_admission",
        "generation_id": "unknown_generation",
        "execution": "succeeded" if admission.get("status") == "full" else "failed",
        "science": "not_evaluated",
        "stop_reason": None,
        "acceptance_status": acceptance.get("status"),
        "admission_status": admission.get("status"),
        "blocking_gap_count": len(acceptance.get("blocking_gaps") or []),
        "blocked_sections": admission.get("blocked_sections") or {},
        "consumed_artifact_hashes": [],
        "cost_micro_cny": 0,
    }
    return parse_object("STAGE_RECEIPT", receipt)


def project_legacy_preflight(preflight: Dict[str, Any]) -> ParseResult:
    errs: List[str] = []
    facts = preflight.get("evidence_facts_count", 0)
    numeric_errors = preflight.get("numeric_errors") or []
    ok = preflight.get("ok")
    if ok and not facts:
        errs.append("preflight.ok_true_with_zero_facts.contradiction")
    projected = {
        "schema_version": "%s.stage_receipt.%s" % (SCHEMA_BASE, CONTRACTS_VERSION),
        "envelope": {
            "run_id": "legacy_projection",
            "generation_id": "unknown_generation",
            "attempt_id": "not_applicable",
            "artifact_id": "legacy_preflight",
            "producer": "upgrade3.contracts.project_legacy_preflight(one-way)",
            "parent_artifact_hashes": [],
            "created_at": _utc_now(),
            "content_sha256": content_sha256(preflight),
            "policy_sha256": "not_applicable",
            "prompt_manifest_hash": "not_applicable",
            "model_manifest_hash": "not_applicable",
            "material_snapshot_hash": "not_applicable",
        },
        "receipt_id": "legacy_preflight",
        "stage_id": "publication_preflight",
        "generation_id": "unknown_generation",
        "execution": "succeeded" if ok else "failed",
        "science": "not_evaluated",
        "stop_reason": None,
        "evidence_facts_count": facts,
        "numeric_error_count": len(numeric_errors),
        "consumed_artifact_hashes": [],
        "cost_micro_cny": 0,
    }
    return parse_object("STAGE_RECEIPT", receipt=projected) if False else \
        _parse_with_extra_errors("STAGE_RECEIPT", projected, errs)


def _parse_with_extra_errors(kind: str, raw: Dict[str, Any], extra: List[str]) -> ParseResult:
    res = parse_object(kind, raw)
    res.errors = extra + res.errors
    if extra:
        res.ok = False
        res.object = None
    return res


def project_legacy_author_packet(packet: Dict[str, Any]) -> ParseResult:
    """Lossless read conversion of a legacy enhancement input packet.

    Paper identities and exact spans are preserved verbatim; anything the
    legacy packet does not prove is marked unverified instead of being dropped
    or upgraded."""
    rows = []
    demotions: List[str] = []
    for i, row in enumerate(packet.get("evidence_packets") or []):
        rows.append({
            "paper_id": row.get("paper_id"),
            "chunk_id": row.get("chunk_id"),
            "exact_spans": row.get("exact_spans"),
            "support_relation": row.get("support_relation"),
            "span_source": row.get("span_source"),
            "span_fallback": bool(row.get("span_fallback")),
            "legacy_scope_fit_display_only": row.get("scope_fit"),
        })
        if row.get("span_fallback"):
            demotions.append("evidence_packet[%d].fallback_span_demoted_to_context" % i)
    projected = {
        "schema_version": "%s.author_candidate.%s" % (SCHEMA_BASE, CONTRACTS_VERSION),
        "envelope": {
            "run_id": "legacy_projection",
            "generation_id": "unknown_generation",
            "attempt_id": "not_applicable",
            "artifact_id": str(packet.get("section_id") or "legacy_packet"),
            "producer": "upgrade3.contracts.project_legacy_author_packet(one-way)",
            "parent_artifact_hashes": [],
            "created_at": _utc_now(),
            "content_sha256": content_sha256({"claims": packet.get("claims"),
                                              "evidence": packet.get("evidence_packets")}),
            "policy_sha256": "not_applicable",
            "prompt_manifest_hash": "not_applicable",
            "model_manifest_hash": "not_applicable",
            "material_snapshot_hash": "not_applicable",
        },
        "candidate_id": str(packet.get("section_id") or "legacy_packet") + ":legacy_import",
        "section_id": packet.get("section_id"),
        "generation_id": "unknown_generation",
        "draft_text_sha256": "not_applicable_no_draft_in_input_packet",
        "evidence_packet": rows,
        "citation_map": [],
        "validation_level": "none",
        "candidate_class": "syntax_only",
    }
    res = parse_object("AUTHOR_CANDIDATE", projected)
    # validation_level 'none' is forbidden for production candidates, but a
    # legacy *input packet* carries no draft at all: record as unverified
    # instead of failing the lossless-content guarantee.
    if res.errors and all(e.startswith("author_candidate.validation_level_none") for e in res.errors):
        res.errors = []
        res.demotions.append("legacy_packet_has_no_draft.marked_unverified_not_scientific")
        res.ok = True
        res.object = projected
    res.demotions.extend(demotions)
    return res


PRODUCER_CONSUMER_TABLE = {
    "DOMAIN_CONTRACT": {
        "producer": "009 (research object & question contract)",
        "consumers": ["010 domain admission", "013 cache projection", "018 gap retrieval"],
        "legacy_source": "topic_identity + topic_scope_contract (read-only projection)",
    },
    "EVIDENCE_SPAN": {
        "producer": "011 (fulltext & span parsing)",
        "consumers": ["014 evidence cards", "016 atomic binding", "015 fact baseline", "027 sentence audit"],
        "legacy_source": "m3gap/s2chunk records (read-only projection until 011 re-derives)",
    },
    "CLAIM_BINDING": {
        "producer": "016 (atomic claim-span binding)",
        "consumers": ["017 role coverage", "019 R3->R4 predicate", "020 author workspace", "024 issue ledger"],
        "legacy_source": "phase3 MATERIAL_BINDINGS claims",
    },
    "FACT_REGISTRY": {
        "producer": "015 (numeric condition fact baseline)",
        "consumers": ["021 authoring", "027 sentence audit", "028 numeric hard gate", "042 figures"],
        "legacy_source": "none (facts=0 in R6 preflight proves absence)",
    },
    "STAGE_RECEIPT": {
        "producer": "007 (single event reduction) for every stage",
        "consumers": ["019 admission predicate", "029 terminal commit", "030 recovery"],
        "legacy_source": "PHASE3_ACCEPTANCE / R4_AUTHORING_ADMISSION / preflight receipts",
    },
    "AUTHOR_CANDIDATE": {
        "producer": "020/021/022 (author workspace & promotion)",
        "consumers": ["023 enhancer gate", "026 merge", "027 audit"],
        "legacy_source": "SECTION_AUTHORING_PACKAGE / enhancement input_packet",
    },
    "MANUSCRIPT_MANIFEST": {
        "producer": "026 (merge) updated by 027/028/042-044",
        "consumers": ["029 terminal commit", "030 invalidation"],
        "legacy_source": "REVIEW_CONTENT_PACKAGE / LATEX_BUILD_REPORT (display only)",
    },
    "SCIENTIFIC_ISSUE": {
        "producer": "024 unified issue ledger (from 014-018 checks and reviews)",
        "consumers": ["025 correction & independent review", "027 audit", "029 gate"],
        "legacy_source": "BLOCK_SCIENTIFIC_REVIEW / whole_manuscript_review findings",
    },
    "TERMINAL_MANIFEST": {
        "producer": "029 (single terminal commit)",
        "consumers": ["030 recovery", "045 delivery package"],
        "legacy_source": "DELIVERY_GATE (formal); FINAL_DELIVERY_GATE has no commit authority",
    },
}
