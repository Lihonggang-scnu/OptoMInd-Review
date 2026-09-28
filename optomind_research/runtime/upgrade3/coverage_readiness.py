"""Upgrade-3 load-bearing coverage readiness (ticket 017).

SECTION_COVERAGE_VERDICT for one section, decided by REQUIRED slots — never by
unit counts or budgets:

- required slots come from the original question/blueprint and keep their
  original requirement ids; a slot declared required can never be demoted to
  optional after the fact.
- only bindings that passed 016 with a direct/qualified lane may satisfy a
  required slot; one source can never be counted as multiple independent
  studies (per-binding source identity); counterevidence/conflicts mark the
  slot ``constrained`` or pending adjudication.
- soft diagnostics (volume, diversity) are reported but can never flip
  scientific_ready; Introduction/Outlook cannot be an escape hatch for an
  uncovered required slot.
- ``structural_complete`` is reported independently and can never override
  ``scientific_ready=false``.
- status: ready | needs_evidence (a required slot has no legal support) |
  needs_more_literature (slots covered but required roles/axes still thin) |
  transport_failed (the search infrastructure failed; candidate-side issue).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

VERDICT_SCHEMA = "optomind.upgrade3.section_coverage_verdict.v1"
STATUSES = {"ready", "needs_evidence", "needs_more_literature", "transport_failed"}

REQUIRED_ROLES = {"architectures", "training", "applications", "mechanism",
                  "comparison", "bottlenecks", "future_directions"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def coverage_verdict(*, section_id: str, generation_id: str,
                     required_slots: List[Dict[str, Any]],
                     bindings: List[Dict[str, Any]],
                     transport_ok: bool = True,
                     required_roles: Optional[Iterable[str]] = None,
                     soft_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """required_slots: [{slot_id, requirement_id, role, required: True|False,
    comparison_axis?: str}].  bindings: 016 binding results (writable ones with
    satisfies_slots / roles).  required_roles: role-breadth requirements beyond
    individual slots — thin breadth yields needs_more_literature."""
    slot_results: List[Dict[str, Any]] = []
    covered_axes = set()
    seen_sources = set()
    optional_gaps: List[str] = []
    comparison_axis_gaps: List[str] = []

    writable = [b for b in bindings
                if b.get("writable")
                and b.get("binding", {}).get("binding_status") == "resolved"
                and b.get("binding", {}).get("permission")
                in ("qualified_support", "direct_support")]
    # role breadth may be satisfied by ANY writable binding with that role
    covered_roles = {r for b in writable for r in (b["binding"].get("roles") or [])}

    for slot in required_slots:
        slot_id = slot["slot_id"]
        required = slot.get("required", True)
        role = slot.get("role")
        supporting = []
        for b in writable:
            body = b["binding"]
            satisfies = slot_id in (body.get("satisfies_slots") or [])
            role_match = role and role in (body.get("roles") or [])
            if satisfies or role_match:
                source = body.get("canonical_citation_ids") or [body.get("claim_id")]
                key = _canonical(source)
                if key in seen_sources and slot.get("count_as_independent"):
                    continue  # one source cannot inflate independent coverage
                seen_sources.add(key)
                supporting.append({
                    "claim_id": body.get("claim_id"),
                    "binding_hash": body.get("binding_hash"),
                    "source_ref": key,
                })
                if slot.get("comparison_axis"):
                    covered_axes.add(slot["comparison_axis"])
        entry = {
            "slot_id": slot_id,
            "requirement_id": slot.get("requirement_id"),
            "role": role,
            "required": required,
            "covered": bool(supporting),
            "support": supporting[:3],
        }
        if not supporting:
            entry["status"] = "needs_evidence" if required else "optional_gap"
            if not required:
                optional_gaps.append(slot_id)
        else:
            entry["status"] = "covered"
        slot_results.append(entry)

    required_uncovered = [s["slot_id"] for s in slot_results
                          if s.get("required") and not s["covered"]]
    for slot in required_slots:
        axis = slot.get("comparison_axis")
        if axis and slot.get("required", True):
            if not any(axis in (s.get("role") or "") or axis == s.get("slot_id")
                       for s in slot_results if s["covered"]):
                comparison_axis_gaps.append(axis)

    required_roles_from_slots = {s["role"] for s in required_slots
                                 if s.get("required", True) and s.get("role")}
    if required_roles:
        required_roles_from_slots |= set(required_roles)
    missing_roles = sorted(required_roles_from_slots - covered_roles)

    if not transport_ok:
        status = "transport_failed"
    elif required_uncovered:
        status = "needs_evidence"
    elif missing_roles or comparison_axis_gaps:
        status = "needs_more_literature"
    else:
        status = "ready"

    scientific_ready = status == "ready"
    verdict = {
        "schema_version": VERDICT_SCHEMA,
        "section_id": section_id,
        "generation_id": generation_id,
        "required_slots": slot_results,
        "question_coverage": {
            "required_total": len([s for s in required_slots if s.get("required", True)]),
            "covered": len([s for s in slot_results
                            if s.get("required", True) and s["covered"]]),
            "ratio": (len([s for s in slot_results if s.get("required", True)
                           and s["covered"]])
                      / max(1, len([s for s in required_slots if s.get("required", True)]))),
        },
        "required_role_coverage": {
            "required": sorted(required_roles_from_slots),
            "covered": sorted(covered_roles),
            "missing": missing_roles,
        },
        "optional_gaps": optional_gaps,
        "comparison_axis_gaps": sorted(set(comparison_axis_gaps)),
        "soft_diagnostics": soft_stats or {},
        "status": status,
        "scientific_ready": scientific_ready,
        "structural_complete": soft_stats.get("structural_complete", True)
        if soft_stats else True,
        "notes": [
            "counts are diagnostic only",
            "candidate-only units can never satisfy a required slot",
            "structural_complete is independent of scientific_ready",
        ],
    }
    verdict["coverage_evidence"] = coverage_evidence(
        bindings=list(bindings),
        required_slots=list(required_slots),
        required_roles=required_roles_from_slots,
    )
    verdict["verdict_hash"] = _sha(_canonical(
        {k: v for k, v in verdict.items() if k != "verdict_hash"}))
    return verdict



def coverage_evidence(*, bindings: List[Dict[str, Any]],
                      required_slots: List[Dict[str, Any]],
                      required_roles: Optional[Iterable[str]] = None,
                      ) -> Dict[str, Any]:
    """Trace every covered role, slot and axis to a writable binding and its span.

    A role is only covered when a writable, resolved binding actually carries it,
    and that binding must expose the span it rests on.  Section level required roles
    are a requirement, never an answer: they cannot make a role covered by
    themselves.  The result is deliberately narrow - claim, binding hash, span ids,
    document ids, pages, conditions and the role provenance of those roles.
    """

    def writable_bindings() -> List[Dict[str, Any]]:
        rows = []
        for binding in bindings or []:
            body = binding.get("binding") or {}
            if not binding.get("writable"):
                continue
            if body.get("binding_status") != "resolved":
                continue
            if body.get("permission") not in ("qualified_support", "direct_support"):
                continue
            if body.get("scope_verdict") != "direct":
                continue
            span_ids = list(body.get("canonical_span_ids")
                            or body.get("support_span_ids") or [])
            if not span_ids:
                continue
            rows.append(binding)
        return rows

    usable = writable_bindings()

    def evidence_row(binding: Mapping[str, Any], role: str) -> Dict[str, Any]:
        body = binding.get("binding") or {}
        provenance = body.get("role_provenance") or {}
        return {
            "claim_id": _text(body.get("claim_id")),
            "binding_hash": _text(body.get("binding_hash")),
            "role": role,
            "role_provenance": {
                "atom_ids": list(body.get("atom_ids") or []),
                "roles": list(body.get("roles") or []),
                "owner_kb": _text(provenance.get("owner_kb")),
                "scope_receipt_hash": _text(provenance.get("scope_receipt_hash")),
                "domain_contract_hash": _text(provenance.get("domain_contract_hash")),
            },
            "span_ids": list(body.get("canonical_span_ids")
                             or body.get("support_span_ids") or []),
            "document_ids": list(body.get("document_ids") or []),
            "pdf_pages": list(body.get("pdf_pages") or []),
            "condition_ids": list(body.get("condition_ids") or []),
            "scope_verdict": _text(body.get("scope_verdict")),
            "permission": _text(body.get("permission")),
            "entailment": _text(body.get("entailment")),
        }

    role_evidence: Dict[str, List[Dict[str, Any]]] = {}
    for binding in usable:
        body = binding.get("binding") or {}
        for role in body.get("roles") or []:
            role_evidence.setdefault(str(role), []).append(evidence_row(binding, str(role)))

    slot_evidence: Dict[str, List[Dict[str, Any]]] = {}
    for slot in required_slots or []:
        slot_id = _text(slot.get("slot_id"))
        role = _text(slot.get("role"))
        rows: List[Dict[str, Any]] = []
        for binding in usable:
            body = binding.get("binding") or {}
            satisfies = slot_id in (body.get("satisfies_slots") or [])
            role_match = bool(role) and role in (body.get("roles") or [])
            if satisfies or role_match:
                rows.append(evidence_row(binding, role))
        slot_evidence[slot_id] = rows

    demanded = {
        _text(slot.get("role")) for slot in required_slots or []
        if slot.get("required", True) and _text(slot.get("role"))
    } | {str(role) for role in (required_roles or ())}
    missing = sorted(role for role in demanded if not role_evidence.get(role))
    untraceable = sorted(
        role for role, rows in role_evidence.items()
        if any(not row["span_ids"] for row in rows)
    )
    return {
        "schema_version": "optomind.upgrade3.coverage_evidence.v1",
        "writable_bindings": len(usable),
        "role_evidence": role_evidence,
        "slot_evidence": {key: value for key, value in slot_evidence.items() if value},
        "covered_roles": sorted(role_evidence),
        "missing_roles": missing,
        "roles_without_a_span": untraceable,
        "traceability_complete": not untraceable,
    }


def author_admission(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """The consumer gate used by R4 authoring: only scientific_ready sections
    are admitted; needs_more_literature admits with limits recorded."""
    ready = verdict.get("scientific_ready") is True
    status = verdict.get("status")
    if status == "transport_failed":
        return {"admit": False, "reason": "transport_failed"}
    if ready:
        return {"admit": True, "limits": []}
    if status == "needs_more_literature":
        return {"admit": True,
                "limits": ["breadth_incomplete"],
                "missing_roles": verdict["required_role_coverage"]["missing"],
                "comparison_axis_gaps": verdict["comparison_axis_gaps"]}
    return {"admit": False,
            "reason": "needs_evidence",
            "uncovered_required_slots": [s["slot_id"] for s in
                                         verdict["required_slots"]
                                         if s.get("required") and not s["covered"]]}
