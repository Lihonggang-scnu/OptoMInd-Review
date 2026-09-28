"""Literature feeding back into the blueprint, as atomic revisions (SM08).

The initial plan and the discovered literature each hold half the authority over
the review structure.  The plan half is irreducible: the question's required
slots, the domain contract and the science gates are HARD and a revision may
never delete, demote or narrow them.  The literature half is this module: the
in-domain material that no section contract could host (the SM07 global pool)
may push sections, subsections, soft roles, comparison axes and evidence
priority, and every accepted change is materialised in a frozen revision that
Phase 3 consumes by hash.

Three things this module refuses to do, because they are the shortcuts the
ticket forbids:

* lower a breadth target and call it adaptation (metadata_only_change_refused);
* propose a change that is never applied (every planned candidate is either in
  accepted_change_ids or in rejected_changes with a reason);
* grant writing permission - a revision is a structural decision, and the
  coverage verdict and the claim binder still decide what may be written.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HARD_SLOT_SCHEMA = "optomind.upgrade3.blueprint_hard_slots.v1"
CANDIDATE_SCHEMA = "optomind.upgrade3.blueprint_revision_candidate.v1"
PROPOSAL_SCHEMA = "optomind.upgrade3.blueprint_revision_proposal.v1"
MOUNTING_SCHEMA = "optomind.upgrade3.evidence_mounting.v1"
REVISION_SCHEMA = "optomind.upgrade3.blueprint_revision.v1"
MANIFEST_SCHEMA = "optomind.upgrade3.blueprint_revision.manifest.v1"

PROPOSAL_FILENAME = "BLUEPRINT_REVISION_PROPOSAL.json"
MOUNTING_FILENAME = "EVIDENCE_MOUNTING.json"
REVISION_FILENAME = "BLUEPRINT_REVISION.json"

MAX_ROUNDS = 2
MAX_ACCEPTED_PER_ROUND = 8
MIN_DOCUMENTS_FOR_A_SOFT_ROLE = 2
MIN_DOCUMENTS_FOR_AN_AXIS = 2
MIN_DOCUMENTS_FOR_A_SUBSECTION = 2

#: candidate kinds that actually change the structure or the mount set
STRUCTURAL_KINDS = (
    "move",
    "soft_role",
    "comparison_axis",
    "add_subsection",
    "add_section",
    "merge",
    "split",
    "role_demotion",
)

IN_DOMAIN_VERDICTS = ("direct", "adjacent")

ROLE_KEYS = ("required_roles", "literature_roles", "soft_roles")


def canonical_scope_verdict(value: Any) -> str:
    """Fold an acquisition label onto the canonical scope verdicts.

    The single authority is evidence_mounting's fold, so admission, the pool and
    the revision can never disagree about what "in domain" means.
    """

    try:
        from .evidence_mounting import canonical_scope_verdict as _fold

        return _fold(value)
    except Exception:  # pragma: no cover - defensive
        raw = _text(value).casefold()
        return raw if raw in ("direct", "adjacent", "background",
                              "out_of_scope", "uncertain") else "uncertain"


class BlueprintRevisionError(RuntimeError):
    """The revision is not usable: never partially applied, never downgraded."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [_text(value)] if _text(value) else []
    out = []
    for item in value:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _sections(blueprint: Mapping[str, Any]) -> list:
    return [
        dict(item)
        for item in (blueprint.get("sections") or [])
        if isinstance(item, Mapping) and _text(item.get("section_id"))
    ]


def _scope_map(blueprint: Mapping[str, Any]) -> dict:
    value = blueprint.get("review_scope_map")
    return dict(value) if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- #
# hard slots
# --------------------------------------------------------------------------- #

def extract_hard_slots(
    *,
    blueprint: Mapping[str, Any],
    domain_contract: Mapping[str, Any] | None = None,
) -> dict:
    """The irreducible half: question slots, the domain and the science gates."""

    scope = _scope_map(blueprint)
    contract = dict(domain_contract or {})
    slots = contract.get("slots") if isinstance(contract.get("slots"), Mapping) else {}
    required_roles = []
    for source in (
        scope.get("required_literature_roles"),
        slots.get("required_roles") if isinstance(slots, Mapping) else None,
        [s.get("argument_role") for s in _sections(blueprint)],
    ):
        for role in _rows(source):
            if role not in required_roles:
                required_roles.append(role)
    section_required = {
        _text(section.get("section_id")): _rows(section.get("required_roles"))
        for section in _sections(blueprint)
    }
    section_questions = {
        _text(section.get("section_id")): _text(section.get("core_question"))
        for section in _sections(blueprint)
    }
    # A question-required role the plan never hosted is a pre-existing gap, not
    # something a revision can remove: the guard protects what the plan HAS.
    plan_roles = set()
    for section in _sections(blueprint):
        plan_roles |= _section_roles(section)
    enforced = [role for role in required_roles if role in plan_roles]
    absent = [role for role in required_roles if role not in plan_roles]
    envelope = contract.get("envelope")
    body = {
        "schema_version": HARD_SLOT_SCHEMA,
        "core_question": _text(scope.get("core_question")),
        "user_question": _text(scope.get("user_question")),
        "review_thesis": _text(blueprint.get("review_thesis")),
        "methodology_identity": _text(blueprint.get("methodology_identity")),
        "required_questions": _rows(
            (slots or {}).get("required_questions")
            if isinstance(slots, Mapping)
            else None
        ),
        "question_required_roles": sorted(enforced),
        "question_required_roles_absent_from_plan": sorted(absent),
        "section_required_roles": section_required,
        "section_core_questions": section_questions,
        "comparison_axes": _rows(
            (slots or {}).get("comparison_axes") if isinstance(slots, Mapping) else None
        ),
        "target_system": _text((slots or {}).get("target_system"))
        if isinstance(slots, Mapping)
        else "",
        "inclusion_boundaries": _rows(scope.get("inclusion_boundaries")),
        "exclusion_boundaries": _rows(scope.get("exclusion_boundaries")),
        "domain_identity": _text(
            contract.get("domain_identity") or contract.get("domain_id")
        ),
        "domain_contract_hash": _text(
            (envelope or {}).get("content_sha256")
            if isinstance(envelope, Mapping)
            else contract.get("content_sha256")
        ),
        "science_gates": [
            "out_of_domain_material_never_mounted",
            "question_required_slots_never_removed",
            "revision_never_grants_permission",
        ],
    }
    body["hard_slot_fingerprint"] = _sha(body)
    return body


def hard_slot_violations(
    hard_slots: Mapping[str, Any], revised_blueprint: Mapping[str, Any]
) -> list:
    """Every way a candidate revision could break the plan's half."""

    violations = []
    sections = _sections(revised_blueprint)
    present = set()
    for section in sections:
        for key in ROLE_KEYS:
            present |= set(_rows(section.get(key)))
    for role in hard_slots.get("question_required_roles") or ():
        if role not in present:
            violations.append("question_required_role_removed:%s" % role)
    for section_id, roles in (hard_slots.get("section_required_roles") or {}).items():
        section = next(
            (item for item in sections if _text(item.get("section_id")) == section_id),
            None,
        )
        if section is None:
            violations.append("question_required_section_removed:%s" % section_id)
            continue
        still = set(_rows(section.get("required_roles")))
        for role in roles:
            if role not in still:
                violations.append(
                    "section_required_role_removed:%s:%s" % (section_id, role))
        declared_question = _text(
            (hard_slots.get("section_core_questions") or {}).get(section_id))
        if declared_question and not _text(section.get("core_question")):
            violations.append("section_core_question_removed:%s" % section_id)
    scope = _scope_map(revised_blueprint)
    if not _text(scope.get("core_question")) and _text(hard_slots.get("core_question")):
        violations.append("core_question_removed")
    return violations


# --------------------------------------------------------------------------- #
# candidate planning
# --------------------------------------------------------------------------- #

def _section_roles(section: Mapping[str, Any]) -> set:
    roles = set()
    for key in ROLE_KEYS:
        roles |= set(_rows(section.get(key)))
    return roles


def _section_axes(section: Mapping[str, Any]) -> set:
    return set(_rows(section.get("comparison_axes")))


def _candidate(
    *,
    kind: str,
    section_id: str,
    old_value: Any,
    new_value: Any,
    evidence_chunk_ids: Sequence[str],
    evidence_document_ids: Sequence[str],
    basis: str,
    expected_gain_units: int,
    from_section_id: str = "",
    permission_floor: str = "",
    risk_flags: Sequence[str] = (),
    requires_claim_factory_pass: bool = False,
) -> dict:
    body = {
        "schema_version": CANDIDATE_SCHEMA,
        "kind": kind,
        "section_id": section_id,
        "from_section_id": from_section_id,
        "old_value": old_value,
        "new_value": new_value,
        "evidence_chunk_ids": list(evidence_chunk_ids),
        "evidence_document_ids": list(evidence_document_ids),
        "basis": basis,
        "expected_gain_units": int(expected_gain_units),
        "permission_floor": permission_floor,
        "risk_flags": list(risk_flags),
        "requires_claim_factory_pass": bool(requires_claim_factory_pass),
    }
    body["candidate_id"] = "bprc_" + _sha(body)[:20]
    return body


def plan_candidates(
    *,
    blueprint: Mapping[str, Any],
    hard_slots: Mapping[str, Any],
    pool: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]] = (),
    mounted_chunk_ids: Iterable[str] = (),
    round_index: int = 1,
) -> list:
    """Bounded candidates derived from the real mounting signal.

    Every candidate is a mount decision expressed through a structure change:
    which section should host pooled material, through which role or axis, and
    what evidence proves it.  A candidate carries no permission.
    """

    mounted = {_text(item) for item in mounted_chunk_ids}
    sections = _sections(blueprint)
    section_ids = [_text(item.get("section_id")) for item in sections]
    proposals_by_chunk = {}
    for row in proposals:
        proposals_by_chunk.setdefault(_text(row.get("chunk_id")), []).append(dict(row))

    in_domain = []
    for row in pool:
        verdict = _text(row.get("scope_verdict")).casefold()
        if verdict not in IN_DOMAIN_VERDICTS:
            continue
        in_domain.append(dict(row))

    def host_for(row: Mapping[str, Any]) -> str:
        """The section that should host this material, by evidence and not by order.

        A section that NAMES the role wins; otherwise the section the run's own
        ledger registered the paper under is the one that may adopt it.  A
        proposal that merely notes "this role exists somewhere" is not a mandate,
        and picking whichever section comes first would put evidence under a
        section whose contract never claimed it.
        """

        for proposal in proposals_by_chunk.get(_text(row.get("chunk_id")), ()):
            if _text(proposal.get("basis")) != "section_names_the_material_role":
                continue
            sid = _text(proposal.get("section_id"))
            if sid in section_ids:
                return sid
        original = _text(row.get("original_section_id"))
        if original in section_ids:
            return original
        for proposal in proposals_by_chunk.get(_text(row.get("chunk_id")), ()):
            sid = _text(proposal.get("section_id"))
            if sid in section_ids:
                return sid
        return section_ids[0] if section_ids else ""

    def section_by_id(section_id: str):
        return next(
            item for item in sections if _text(item.get("section_id")) == section_id)

    candidates = []

    # -- role driven: pooled material carries a role no section can host ------
    by_role = {}
    for row in in_domain:
        role = _text(row.get("original_role"))
        if not role:
            continue
        section_id = host_for(row)
        if not section_id:
            continue
        if role in _section_roles(section_by_id(section_id)):
            continue  # already hostable through the contract: a capacity matter
        by_role.setdefault((section_id, role), []).append(row)
    for (section_id, role), rows_for_role in sorted(by_role.items()):
        section = section_by_id(section_id)
        documents = sorted({_text(row.get("document_id")) for row in rows_for_role})
        if len(documents) < MIN_DOCUMENTS_FOR_A_SOFT_ROLE:
            continue
        chunks = [
            _text(row.get("chunk_id")) for row in rows_for_role
            if _text(row.get("chunk_id"))
        ]
        if not chunks:
            continue
        adjacency = any(
            _text(row.get("scope_verdict")).casefold() == "adjacent"
            for row in rows_for_role
        )
        old_roles = _rows(section.get("soft_roles"))
        candidates.append(_candidate(
            kind="soft_role",
            section_id=section_id,
            old_value=old_roles,
            new_value=sorted(set(old_roles) | {role}),
            evidence_chunk_ids=chunks,
            evidence_document_ids=documents,
            basis="in_domain_material_of_role_%s_has_no_host_section" % role,
            expected_gain_units=len(chunks),
            permission_floor=(
                "adjacent_contextual_only" if adjacency else "qualified_support"
            ),
        ))
        if len(documents) >= MIN_DOCUMENTS_FOR_A_SUBSECTION:
            subsection_id = "%s-sub-%s" % (section_id, role)
            candidates.append(_candidate(
                kind="add_subsection",
                section_id=section_id,
                old_value=[
                    _text(item.get("subsection_id"))
                    for item in (section.get("subsections") or [])
                    if isinstance(item, Mapping)
                ],
                new_value={
                    "subsection_id": subsection_id,
                    "title": "%s evidence" % role.replace("_", " ").title(),
                    "role": role,
                    "section_id": section_id,
                },
                evidence_chunk_ids=chunks,
                evidence_document_ids=documents,
                basis="a_role_cluster_of_%d_documents_needs_its_own_subsection"
                      % len(documents),
                expected_gain_units=len(chunks),
                permission_floor=(
                    "adjacent_contextual_only" if adjacency else "qualified_support"
                ),
            ))

    # -- axis driven: the literature proposes a comparison axis ---------------
    by_axis = {}
    chunk_to_pool = {_text(row.get("chunk_id")): row for row in in_domain}
    for proposal in proposals:
        chunk_id = _text(proposal.get("chunk_id"))
        row = chunk_to_pool.get(chunk_id)
        if row is None:
            continue
        section_id = _text(proposal.get("section_id"))
        if section_id not in section_ids:
            continue
        section = section_by_id(section_id)
        for axis in _rows(proposal.get("proposed_axes")):
            if axis in _section_axes(section):
                continue
            by_axis.setdefault((section_id, axis), []).append(row)
    # -- named role, but the flat contract could not host the cluster ---------
    named_by_role = {}
    for row in in_domain:
        role = _text(row.get("original_role"))
        chunk_id = _text(row.get("chunk_id"))
        if not role or not chunk_id:
            continue
        section_id = host_for(row)
        if not section_id or role not in _section_roles(section_by_id(section_id)):
            continue
        named_by_role.setdefault((section_id, role), []).append(row)
    for (section_id, role), rows_for_role in sorted(named_by_role.items()):
        documents = sorted({_text(row.get("document_id")) for row in rows_for_role})
        if len(documents) < MIN_DOCUMENTS_FOR_A_SUBSECTION:
            continue
        chunks = [_text(row.get("chunk_id")) for row in rows_for_role
                  if _text(row.get("chunk_id"))]
        if not chunks:
            continue
        section = section_by_id(section_id)
        subsection_id = "%s-sub-%s" % (section_id, role)
        existing = [
            _text(item.get("subsection_id"))
            for item in (section.get("subsections") or [])
            if isinstance(item, Mapping)
        ]
        if subsection_id in existing:
            continue
        adjacency = any(
            _text(row.get("scope_verdict")).casefold() == "adjacent"
            for row in rows_for_role
        )
        candidates.append(_candidate(
            kind="add_subsection",
            section_id=section_id,
            old_value=existing,
            new_value={
                "subsection_id": subsection_id,
                "title": "%s evidence" % role.replace("_", " ").title(),
                "role": role,
                "section_id": section_id,
            },
            evidence_chunk_ids=chunks,
            evidence_document_ids=documents,
            basis="role_is_named_but_the_flat_section_contract_could_not_host_"
                  "a_cluster_of_%d_documents" % len(documents),
            expected_gain_units=len(chunks),
            permission_floor=(
                "adjacent_contextual_only" if adjacency else "qualified_support"
            ),
        ))

    for (section_id, axis), rows_for_axis in sorted(by_axis.items()):
        documents = sorted({_text(row.get("document_id")) for row in rows_for_axis})
        if len(documents) < MIN_DOCUMENTS_FOR_AN_AXIS:
            continue
        section = section_by_id(section_id)
        old_axes = _rows(section.get("comparison_axes"))
        candidates.append(_candidate(
            kind="comparison_axis",
            section_id=section_id,
            old_value=old_axes,
            new_value=sorted(set(old_axes) | {axis}),
            evidence_chunk_ids=[
                _text(row.get("chunk_id")) for row in rows_for_axis],
            evidence_document_ids=documents,
            basis="in_domain_material_proposes_comparison_axis_%s" % axis,
            expected_gain_units=len(rows_for_axis),
        ))

    # -- move: pooled material whose role another section already names -------
    for section in sections:
        section_id = _text(section.get("section_id"))
        roles = _section_roles(section)
        for row in in_domain:
            role = _text(row.get("original_role"))
            chunk_id = _text(row.get("chunk_id"))
            if not role or role not in roles or not chunk_id or chunk_id in mounted:
                continue
            origin = _text(row.get("original_section_id"))
            if origin == section_id:
                continue
            candidates.append(_candidate(
                kind="move",
                section_id=section_id,
                from_section_id=origin,
                old_value={"mounted_in": origin},
                new_value={"mounted_in": section_id, "role": role},
                evidence_chunk_ids=[chunk_id],
                evidence_document_ids=[_text(row.get("document_id"))],
                basis="section_names_role_%s_that_the_origin_section_does_not" % role,
                expected_gain_units=1,
            ))

    # -- merge: two sections hosting the very same documents ------------------
    hosted = {}
    for chunk_id in mounted:
        row = chunk_to_pool.get(chunk_id)
        if row is None:
            continue
        hosted.setdefault(_text(row.get("original_section_id")), set()).add(chunk_id)
    for index, left in enumerate(sections):
        left_id = _text(left.get("section_id"))
        for right in sections[index + 1:]:
            right_id = _text(right.get("section_id"))
            left_set = hosted.get(left_id) or set()
            right_set = hosted.get(right_id) or set()
            if not left_set or left_set != right_set:
                continue
            if set(_rows(left.get("required_roles"))) != set(
                _rows(right.get("required_roles"))
            ):
                continue
            merged = _candidate(
                kind="merge",
                section_id=left_id,
                from_section_id=right_id,
                old_value={"section_ids": [left_id, right_id]},
                new_value={"section_ids": [left_id], "merged_into": left_id},
                evidence_chunk_ids=sorted(left_set),
                evidence_document_ids=sorted({
                    _text(chunk_to_pool[item].get("document_id"))
                    for item in left_set
                }),
                basis="identical_required_roles_and_identical_mounted_documents",
                expected_gain_units=0,
                risk_flags=["duplication_reduction_only"],
            )
            merged["duplication_removed_units"] = len(left_set)
            candidates.append(merged)

    # -- split: one section, two disjoint axis clusters -----------------------
    by_section_axis = {}
    for proposal in proposals:
        chunk_id = _text(proposal.get("chunk_id"))
        row = chunk_to_pool.get(chunk_id)
        if row is None or chunk_id in mounted:
            continue
        section_id = _text(proposal.get("section_id"))
        if section_id not in section_ids:
            continue
        for axis in _rows(proposal.get("proposed_axes")):
            by_section_axis.setdefault(section_id, {}).setdefault(
                axis, set()).add(_text(row.get("document_id")))
    for section_id, axes in sorted(by_section_axis.items()):
        clusters = [
            (axis, documents) for axis, documents in sorted(axes.items())
            if len(documents) >= MIN_DOCUMENTS_FOR_A_SUBSECTION
        ]
        if len(clusters) < 2:
            continue
        left_axis, left_docs = clusters[0]
        right_axis, right_docs = clusters[1]
        if left_docs & right_docs:
            continue  # not disjoint: there are no two structures to separate
        chunks = [
            _text(row.get("chunk_id")) for row in in_domain
            if _text(row.get("chunk_id")) not in mounted
            and _text(row.get("document_id")) in (left_docs | right_docs)
        ]
        if not chunks:
            continue
        candidates.append(_candidate(
            kind="split",
            section_id=section_id,
            old_value={"axes": _rows(section_by_id(section_id).get("comparison_axes"))},
            new_value={
                "subsections": [
                    {"subsection_id": "%s-sub-%s" % (section_id, left_axis),
                     "axis": left_axis},
                    {"subsection_id": "%s-sub-%s" % (section_id, right_axis),
                     "axis": right_axis},
                ]
            },
            evidence_chunk_ids=chunks,
            evidence_document_ids=sorted(left_docs | right_docs),
            basis="two_disjoint_comparison_axis_clusters_in_one_section",
            expected_gain_units=len(chunks),
        ))

    candidates.sort(key=lambda row: (-int(row.get("expected_gain_units") or 0),
                                     _text(row.get("kind")),
                                     _text(row.get("section_id")),
                                     _text(row.get("candidate_id"))))
    return candidates


# --------------------------------------------------------------------------- #
# mounting and rounds
# --------------------------------------------------------------------------- #

def mount_evidence(
    *,
    blueprint: Mapping[str, Any],
    pool: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]] = (),
    served_chunk_ids: Iterable[str] = (),
    served_by_section: Mapping[str, Iterable[str]] | None = None,
) -> dict:
    """Which in-domain chunks each section hosts under this structure.

    Three different things are reported apart, because conflating them is how a
    revision would look productive without mounting anything:

    * served: the chunks a section's claim pool actually used (mounted, real);
    * hostable: pooled in-domain chunks some section's contract could host;
    * newly mounted: pooled chunks a section's evidence_mounts row explicitly
      assigns and that no section served -- the revision's actual contribution.
    """

    sections = _sections(blueprint)
    served_set = {_text(item) for item in served_chunk_ids if _text(item)}
    by_section = {}
    for section in sections:
        section_id = _text(section.get("section_id"))
        ids = (served_by_section or {}).get(section_id) or ()
        by_section[section_id] = {_text(item) for item in ids if _text(item)}

    pool_by_chunk = {
        _text(row.get("chunk_id")): dict(row)
        for row in pool
        if _text(row.get("scope_verdict")).casefold() in IN_DOMAIN_VERDICTS
    }
    proposals_by_chunk = {}
    for row in proposals:
        proposals_by_chunk.setdefault(_text(row.get("chunk_id")), []).append(dict(row))

    hostable = []
    for chunk_id, row in sorted(pool_by_chunk.items()):
        if chunk_id in served_set:
            continue
        role = _text(row.get("original_role"))
        origin = _text(row.get("original_section_id"))
        hosts = [
            _text(section.get("section_id")) for section in sections
            if role and role in _section_roles(section)
        ]
        for proposal in proposals_by_chunk.get(chunk_id, ()):
            section_id = _text(proposal.get("section_id"))
            section = next(
                (item for item in sections
                 if _text(item.get("section_id")) == section_id),
                None,
            )
            if section is None:
                continue
            if _section_axes(section) & set(_rows(proposal.get("proposed_axes"))):
                hosts.append(section_id)
        if not hosts and origin and origin in by_section:
            hosts.append(origin)
        if hosts:
            hostable.append(chunk_id)

    newly = []
    for section in sections:
        section_id = _text(section.get("section_id"))
        for mount_row in section.get("evidence_mounts") or ():
            if not isinstance(mount_row, Mapping):
                continue
            for chunk_id in mount_row.get("chunk_ids") or ():
                chunk_id = _text(chunk_id)
                if (not chunk_id or chunk_id in served_set
                        or chunk_id not in pool_by_chunk):
                    continue
                if chunk_id not in newly:
                    newly.append(chunk_id)
                by_section.setdefault(section_id, set()).add(chunk_id)

    unmounted = [
        chunk_id for chunk_id in sorted(pool_by_chunk)
        if chunk_id not in served_set
        and chunk_id not in set(newly)
        and chunk_id not in set(hostable)
    ]
    return {
        "schema_version": MOUNTING_SCHEMA,
        "served_chunk_ids": sorted(served_set),
        "mounted_by_section": {
            key: sorted(value) for key, value in sorted(by_section.items())
        },
        "mounted_counts": {
            key: len(value) for key, value in sorted(by_section.items())
        },
        "hostable_chunk_ids": sorted(hostable),
        "newly_mounted_chunk_ids": sorted(newly),
        "still_unmounted_chunk_ids": sorted(unmounted),
        "in_domain_pool_size": len(pool_by_chunk),
    }


def _apply_candidate(
    blueprint: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict:
    revised = copy.deepcopy(dict(blueprint))
    sections = revised.get("sections") or []
    section_id = _text(candidate.get("section_id"))
    kind = _text(candidate.get("kind"))

    def find(target: str):
        for item in sections:
            if _text(item.get("section_id")) == target:
                return item
        return None

    if kind == "soft_role":
        section = find(section_id)
        if section is None:
            raise BlueprintRevisionError("unknown_section:%s" % section_id)
        section["soft_roles"] = _rows(candidate.get("new_value"))
    elif kind == "role_demotion":
        section = find(section_id)
        if section is None:
            raise BlueprintRevisionError("unknown_section:%s" % section_id)
        section["required_roles"] = _rows(candidate.get("new_value"))
    elif kind == "comparison_axis":
        section = find(section_id)
        if section is None:
            raise BlueprintRevisionError("unknown_section:%s" % section_id)
        section["comparison_axes"] = _rows(candidate.get("new_value"))
    elif kind in ("add_subsection", "split"):
        section = find(section_id)
        if section is None:
            raise BlueprintRevisionError("unknown_section:%s" % section_id)
        existing = [
            dict(item) for item in (section.get("subsections") or [])
            if isinstance(item, Mapping)
        ]
        new_value = candidate.get("new_value")
        if isinstance(new_value, Mapping):
            additions = [dict(new_value)]
        else:
            additions = [
                dict(item) for item in (new_value or {}).get("subsections") or []
            ]
        for addition in additions:
            if not any(_text(item.get("subsection_id"))
                       == _text(addition.get("subsection_id"))
                       for item in existing):
                existing.append(addition)
        section["subsections"] = existing
    elif kind == "move":
        section = find(section_id)
        if section is None:
            raise BlueprintRevisionError("unknown_section:%s" % section_id)
        mounts = [
            dict(item) for item in (section.get("evidence_mounts") or [])
            if isinstance(item, Mapping)
        ]
        mounts.append({
            "chunk_ids": list(candidate.get("evidence_chunk_ids") or []),
            "document_ids": list(candidate.get("evidence_document_ids") or []),
            "from_section_id": _text(candidate.get("from_section_id")),
            "basis": _text(candidate.get("basis")),
        })
        section["evidence_mounts"] = mounts
    elif kind == "add_section":
        sections.append(dict(candidate.get("new_value") or {}))
    elif kind == "merge":
        new_value = candidate.get("new_value") or {}
        drop = [
            _text(item)
            for item in (candidate.get("old_value") or {}).get("section_ids") or []
            if _text(item) != _text(new_value.get("merged_into"))
        ]
        revised["sections"] = [
            item for item in sections if _text(item.get("section_id")) not in drop
        ]
    else:
        raise BlueprintRevisionError("unsupported_kind:%s" % kind)

    if kind in ("soft_role", "comparison_axis", "add_subsection", "split"):
        section = find(section_id)
        if section is not None:
            mounts = [
                dict(item) for item in (section.get("evidence_mounts") or [])
                if isinstance(item, Mapping)
            ]
            mounts.append({
                "chunk_ids": list(candidate.get("evidence_chunk_ids") or []),
                "document_ids": list(candidate.get("evidence_document_ids") or []),
                "basis": _text(candidate.get("basis")),
                "kind": kind,
            })
            section["evidence_mounts"] = mounts
    return revised


def _in_domain_evidence(
    candidate: Mapping[str, Any], pool_index: Mapping[str, Mapping[str, Any]]
) -> tuple:
    for chunk_id in [_text(item)
                     for item in candidate.get("evidence_chunk_ids") or []]:
        row = pool_index.get(chunk_id)
        if row is None:
            return False, "unknown_evidence:%s" % chunk_id
        verdict = _text(row.get("scope_verdict")).casefold()
        if verdict not in IN_DOMAIN_VERDICTS:
            return False, "out_of_domain_evidence:%s:%s" % (
                chunk_id, verdict or "missing")
    return True, ""


def _measure_gain(candidate: Mapping[str, Any], mounted: set) -> int:
    return len({
        _text(item) for item in candidate.get("evidence_chunk_ids") or []
        if _text(item) and _text(item) not in mounted
    })


def admits_round(
    *,
    candidates: Sequence[Mapping[str, Any]],
    round_index: int,
    prior_rounds: Sequence[Mapping[str, Any]] = (),
    blueprint: Mapping[str, Any] | None = None,
    hard_slots: Mapping[str, Any] | None = None,
    pool_index: Mapping[str, Mapping[str, Any]] | None = None,
    mounted_chunk_ids: Iterable[str] = (),
) -> dict:
    """Decide which planned candidates become real changes this round.

    A candidate is refused - with a reason, never silently - when it is not a
    structural change, carries no evidence, rests on out-of-domain material,
    would break a hard slot, adds no information, or reverses an earlier round.
    """

    pool_index = dict(pool_index or {})
    mounted = {_text(item) for item in mounted_chunk_ids}
    accepted_prior = {}
    for round_row in prior_rounds or ():
        settled = set(round_row.get("accepted") or [])
        for row in round_row.get("candidates") or ():
            if _text(row.get("candidate_id")) in settled:
                accepted_prior[(_text(row.get("kind")),
                                _text(row.get("section_id")))] = row

    accepted = []
    rejected = []
    working = copy.deepcopy(dict(blueprint)) if blueprint is not None else None

    for candidate in candidates:
        kind = _text(candidate.get("kind"))
        section_id = _text(candidate.get("section_id"))
        gain = _measure_gain(candidate, mounted)
        entry = {
            "candidate_id": _text(candidate.get("candidate_id")),
            "kind": kind,
            "section_id": section_id,
            "information_gain_units": gain,
        }

        prior = accepted_prior.get((kind, section_id))
        if round_index > 1 and prior is not None:
            previous = set(_rows(prior.get("new_value")))
            current = set(_rows(candidate.get("new_value")))
            if not current or not current <= previous:
                rejected.append(dict(
                    entry, reason="oscillation_refused:reverses_round_1:%s" % kind))
                continue

        if kind not in STRUCTURAL_KINDS:
            rejected.append(dict(entry, reason="metadata_only_change_refused"))
            continue
        if not candidate.get("evidence_chunk_ids"):
            rejected.append(dict(entry, reason="candidate_without_evidence"))
            continue
        if not candidate.get("evidence_document_ids"):
            rejected.append(dict(entry, reason="candidate_without_a_document"))
            continue
        ok, reason = _in_domain_evidence(candidate, pool_index)
        if not ok:
            rejected.append(dict(entry, reason=reason))
            continue
        if gain <= 0 and not candidate.get("duplication_removed_units"):
            rejected.append(dict(entry, reason="no_information_gain:%d" % gain))
            continue
        if working is not None:
            try:
                trial = _apply_candidate(working, candidate)
            except BlueprintRevisionError as exc:
                rejected.append(dict(entry, reason="unappliable:%s" % exc))
                continue
            if hard_slots is not None:
                violations = hard_slot_violations(hard_slots, trial)
                if violations:
                    rejected.append(dict(
                        entry, reason="hard_slot_protected:%s" % violations[0]))
                    continue
            working = trial
        if len(accepted) >= MAX_ACCEPTED_PER_ROUND:
            rejected.append(dict(entry, reason="round_capacity_reached"))
            continue
        accepted.append(dict(candidate, information_gain_units=gain))

    return {"accepted": accepted, "rejected": rejected}


# --------------------------------------------------------------------------- #
# the revision itself
# --------------------------------------------------------------------------- #

def _force_candidate(forced: Mapping[str, Any]) -> dict:
    body = {
        "schema_version": CANDIDATE_SCHEMA,
        "kind": _text(forced.get("kind")),
        "section_id": _text(forced.get("section_id")),
        "from_section_id": _text(forced.get("from_section_id")),
        "old_value": forced.get("old_value"),
        "new_value": forced.get("new_value"),
        "evidence_chunk_ids": list(forced.get("evidence_chunk_ids") or []),
        "evidence_document_ids": list(forced.get("evidence_document_ids") or []),
        "basis": "forced_candidate_for_negative_test",
        "expected_gain_units": len(forced.get("evidence_chunk_ids") or []),
        "permission_floor": "",
        "risk_flags": [],
        "requires_claim_factory_pass": False,
    }
    body["candidate_id"] = "bprc_" + _sha(body)[:20]
    return body


def build_revision(
    *,
    blueprint: Mapping[str, Any],
    hard_slots: Mapping[str, Any],
    pool: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]] = (),
    mounted_chunk_ids: Iterable[str] = (),
    mounted_document_ids: Iterable[str] = (),
    served_by_section: Mapping[str, Iterable[str]] | None = None,
    generation_id: str = "",
    domain_contract_hash: str = "",
    max_rounds: int = MAX_ROUNDS,
    forced_change: Mapping[str, Any] | None = None,
) -> dict:
    """Run the bounded rounds and freeze the resulting revision."""

    if max_rounds > MAX_ROUNDS:
        raise BlueprintRevisionError(
            "round_cap_exceeded:%d>%d" % (max_rounds, MAX_ROUNDS))
    if max_rounds < 0:
        raise BlueprintRevisionError("round_cap_invalid:%d" % max_rounds)

    base_blueprint = copy.deepcopy(dict(blueprint))
    base_hash = _sha(base_blueprint)
    mounted = {_text(item) for item in mounted_chunk_ids}
    pool_index = {
        _text(row.get("chunk_id")): dict(row)
        for row in pool
        if _text(row.get("chunk_id"))
    }
    served = sorted(mounted)

    mounting_before = mount_evidence(
        blueprint=base_blueprint,
        pool=pool,
        proposals=proposals,
        served_chunk_ids=served,
        served_by_section=served_by_section,
    )

    out_of_domain_refusals = []
    for row in pool:
        verdict = _text(row.get("scope_verdict")).casefold()
        if verdict in IN_DOMAIN_VERDICTS:
            continue
        out_of_domain_refusals.append({
            "candidate_id": "",
            "kind": "mount",
            "chunk_id": _text(row.get("chunk_id")),
            "document_id": _text(row.get("document_id")),
            "information_gain_units": 0,
            "reason": "out_of_domain_material:%s" % (verdict or "missing"),
        })

    refused_forced = {}
    rounds = []
    accepted_changes = []
    rejected_changes = list(out_of_domain_refusals)
    working = base_blueprint
    parent_hash = ""

    planned_forced = []
    if forced_change is not None:
        planned_forced.append(_force_candidate(forced_change))
    # a forced candidate is a diagnostic: exactly one round, never a plan run
    total_rounds = 1 if planned_forced else max_rounds

    for round_index in range(1, total_rounds + 1):
        if round_index == 1 and planned_forced:
            planned = planned_forced
        else:
            planned = plan_candidates(
                blueprint=working,
                hard_slots=hard_slots,
                pool=pool,
                proposals=proposals,
                mounted_chunk_ids=mounted,
                round_index=round_index,
            )
        decision = admits_round(
            candidates=planned,
            round_index=round_index,
            prior_rounds=rounds,
            blueprint=working,
            hard_slots=hard_slots,
            pool_index=pool_index,
            mounted_chunk_ids=mounted,
        )
        if round_index == 1 and planned_forced and decision["rejected"]:
            refused_forced = dict(decision["rejected"][0])
        candidate_by_id = {
            _text(row.get("candidate_id")): dict(row) for row in planned
        }
        accepted_rows = []
        for row in decision["accepted"]:
            applied = dict(row)
            applied["rollback_to"] = parent_hash
            applied["round"] = round_index
            accepted_rows.append(applied)
        revised = working
        for row in accepted_rows:
            revised = _apply_candidate(revised, row)
        violations = hard_slot_violations(hard_slots, revised)
        if violations:
            for row in accepted_rows:
                rejected_changes.append({
                    "candidate_id": row["candidate_id"],
                    "kind": row["kind"],
                    "section_id": row["section_id"],
                    "reason": "hard_slot_protected:%s" % violations[0],
                    "information_gain_units": row.get("information_gain_units", 0),
                })
            accepted_rows = []
            revised = working
        mounting_after = mount_evidence(
            blueprint=revised,
            pool=pool,
            proposals=proposals,
            served_chunk_ids=served,
            served_by_section=served_by_section,
        )
        newly = sorted(set(mounting_after["newly_mounted_chunk_ids"])
                       - set(mounting_before["newly_mounted_chunk_ids"]))
        gain = len(newly)
        duplicated = sum(
            int(candidate_by_id[row["candidate_id"]].get(
                "duplication_removed_units") or 0)
            for row in accepted_rows
        )
        accepted_round = bool(accepted_rows) and (gain > 0 or duplicated > 0)
        if accepted_rows and not accepted_round:
            for row in accepted_rows:
                rejected_changes.append({
                    "candidate_id": row["candidate_id"],
                    "kind": row["kind"],
                    "section_id": row["section_id"],
                    "reason": "no_information_gain:0",
                    "information_gain_units": 0,
                })
            accepted_rows = []
            revised = working
            mounting_after = mounting_before
            newly = []
        score = {
            "information_gain_units": gain if accepted_round else 0,
            "newly_mounted_chunk_ids": newly if accepted_round else [],
            "duplication_removed_units": duplicated if accepted_round else 0,
            "new_soft_roles": sum(
                1 for row in accepted_rows if row["kind"] == "soft_role"),
            "new_comparison_axes": sum(
                1 for row in accepted_rows if row["kind"] == "comparison_axis"),
            "new_subsections": sum(
                1 for row in accepted_rows
                if row["kind"] in ("add_subsection", "split")),
            "permission_risk_candidates": sum(
                1 for row in decision["accepted"]
                if row.get("permission_floor") == "adjacent_contextual_only"),
            "hard_slots_preserved": not hard_slot_violations(hard_slots, revised),
            "accepted": accepted_round,
            "reason": (
                "information_gain_%d" % gain if accepted_round
                else ("no_information_gain" if decision["accepted"]
                      or not decision["rejected"]
                      else decision["rejected"][0]["reason"])
            ),
        }
        if not accepted_round:
            for row in decision["rejected"]:
                rejected_changes.append(dict(row))
        round_hash = _sha({
            "round": round_index,
            "parent": parent_hash,
            "accepted": [row["candidate_id"] for row in accepted_rows],
            "mounting_after": {
                "newly_mounted_chunk_ids": mounting_after["newly_mounted_chunk_ids"],
                "mounted_counts": mounting_after["mounted_counts"],
            },
            "score": score,
        })
        rounds.append({
            "round": round_index,
            "parent_revision_hash": parent_hash,
            "candidates": [dict(row) for row in planned],
            "accepted": [row["candidate_id"] for row in accepted_rows],
            "rejected": [dict(row) for row in decision["rejected"]],
            "mounting_before": {
                "newly_mounted_chunk_ids": mounting_before["newly_mounted_chunk_ids"],
                "mounted_counts": mounting_before["mounted_counts"],
                "still_unmounted_chunk_ids":
                    mounting_before["still_unmounted_chunk_ids"],
            },
            "mounting_after": {
                "newly_mounted_chunk_ids": mounting_after["newly_mounted_chunk_ids"],
                "mounted_counts": mounting_after["mounted_counts"],
                "still_unmounted_chunk_ids":
                    mounting_after["still_unmounted_chunk_ids"],
            },
            "score": score,
            "round_hash": round_hash,
        })
        if accepted_round:
            working = revised
            mounting_before = mounting_after
            accepted_changes.extend(accepted_rows)
            parent_hash = round_hash

    final_violations = hard_slot_violations(hard_slots, working)
    body = {
        "schema_version": REVISION_SCHEMA,
        "generation_id": str(generation_id),
        "domain_contract_hash": _text(domain_contract_hash),
        "base_blueprint_hash": base_hash,
        "hard_slots": dict(hard_slots),
        "hard_slot_fingerprint": _text(hard_slots.get("hard_slot_fingerprint")),
        "rounds": rounds,
        "accepted_change_ids": [row["candidate_id"] for row in accepted_changes],
        "accepted_changes": accepted_changes,
        "rejected_changes": rejected_changes,
        "parent_revision_hash": "",
        "parent_blueprint": base_blueprint,
        "revised_blueprint": working,
        "frozen": True,
        "authority": {
            "plan_half":
                "question slots, domain contract and science gates are absolute",
            "literature_half":
                "sections, subsections, soft roles, comparison axes and mounts",
            "revision_grants_permission": False,
            "revision_is_a_structure_decision_only": True,
        },
        "authority_split": {"plan": 0.5, "literature": 0.5},
    }
    body["hard_slots_preserved"] = not final_violations
    body["hard_slot_violations"] = final_violations
    body["revision_hash"] = _sha(body)
    if refused_forced:
        body["refused_forced_change"] = refused_forced
    if final_violations:
        raise BlueprintRevisionError(
            "hard_slots_broken:%s" % ",".join(final_violations))
    return body


def rollback_to_parent(bundle: Mapping[str, Any]) -> dict:
    """The parent revision, byte for byte.  Rollback is not a repair."""

    parent = bundle.get("parent_blueprint")
    if not isinstance(parent, Mapping):
        raise BlueprintRevisionError("parent_blueprint_missing")
    if _sha(dict(parent)) != _text(bundle.get("base_blueprint_hash")):
        raise BlueprintRevisionError("parent_blueprint_hash_mismatch")
    return copy.deepcopy(dict(parent))


# --------------------------------------------------------------------------- #
# artefacts and the consumer
# --------------------------------------------------------------------------- #

def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-%d" % os.getpid())
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.write(chr(10))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_revision_artifacts(
    *,
    output_dir: str | os.PathLike,
    bundle: Mapping[str, Any],
    source_hashes: Mapping[str, str] | None = None,
) -> dict:
    """The three artefacts, written atomically as one revision."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": PROPOSAL_SCHEMA,
        "generation_id": _text(bundle.get("generation_id")),
        "base_blueprint_hash": _text(bundle.get("base_blueprint_hash")),
        "hard_slot_fingerprint": _text(bundle.get("hard_slot_fingerprint")),
        "hard_slots": dict(bundle.get("hard_slots") or {}),
        "rounds": [
            {
                "round": row["round"],
                "parent_revision_hash": row["parent_revision_hash"],
                "candidates": row["candidates"],
                "accepted": row["accepted"],
                "rejected": row["rejected"],
                "score": row["score"],
            }
            for row in bundle.get("rounds") or ()
        ],
    }
    mounting = {
        "schema_version": MOUNTING_SCHEMA,
        "generation_id": _text(bundle.get("generation_id")),
        "rounds": [
            {
                "round": row["round"],
                "revision_hash_after": row["round_hash"],
                "mounting_before": row["mounting_before"],
                "mounting_after": row["mounting_after"],
            }
            for row in bundle.get("rounds") or ()
        ],
        "final_mounted_counts": (
            (bundle.get("rounds") or [{}])[-1].get("mounting_after") or {}
        ).get("mounted_counts", {}),
    }
    hashes = {PROPOSAL_FILENAME: _write_json(out / PROPOSAL_FILENAME, proposal)}
    hashes[MOUNTING_FILENAME] = _write_json(out / MOUNTING_FILENAME, mounting)
    hashes[REVISION_FILENAME] = _write_json(out / REVISION_FILENAME, dict(bundle))
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "generation_id": _text(bundle.get("generation_id")),
        "revision_hash": _text(bundle.get("revision_hash")),
        "base_blueprint_hash": _text(bundle.get("base_blueprint_hash")),
        "hard_slot_fingerprint": _text(bundle.get("hard_slot_fingerprint")),
        "rounds_executed": len(bundle.get("rounds") or ()),
        "accepted_change_ids": list(bundle.get("accepted_change_ids") or []),
        "counts": {
            "accepted": len(bundle.get("accepted_changes") or ()),
            "rejected": len(bundle.get("rejected_changes") or ()),
            "information_gain_units": sum(
                int((row.get("score") or {}).get("information_gain_units") or 0)
                for row in bundle.get("rounds") or ()
            ),
        },
        "source_hashes": dict(source_hashes or {}),
        "artifacts": {
            name: {"filename": name, "sha256": digest}
            for name, digest in hashes.items()
        },
        "created_at": _now(),
    }
    manifest["manifest_body_sha256"] = _sha({
        key: value for key, value in manifest.items()
        if key not in ("manifest_body_sha256", "created_at")
    })
    _write_json(out / "BLUEPRINT_REVISION_MANIFEST.json", manifest)
    return {"manifest": manifest, "hashes": hashes}


def load_frozen_revision(
    path: str | os.PathLike,
    *,
    expected_revision_hash: str = "",
    expected_generation_id: str = "",
) -> dict:
    """The only consumer entry point.  Everything is verified before use."""

    target = Path(path)
    if not target.is_file():
        raise BlueprintRevisionError("blueprint_revision_missing:%s" % target)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BlueprintRevisionError("blueprint_revision_unreadable:%s" % exc)
    if not isinstance(payload, Mapping):
        raise BlueprintRevisionError("blueprint_revision_not_an_object")
    revision = dict(payload)
    if revision.get("schema_version") != REVISION_SCHEMA:
        raise BlueprintRevisionError("blueprint_revision_schema_mismatch")
    if revision.get("frozen") is not True:
        raise BlueprintRevisionError("blueprint_revision_not_frozen")
    declared = _text(revision.get("revision_hash"))
    body = {key: value for key, value in revision.items()
            if key != "revision_hash"}
    if _sha(body) != declared:
        raise BlueprintRevisionError("blueprint_revision_hash_mismatch")
    if expected_revision_hash and declared != _text(expected_revision_hash):
        raise BlueprintRevisionError(
            "blueprint_revision_hash_not_the_expected_one")
    if expected_generation_id and _text(revision.get("generation_id")) != _text(
        expected_generation_id
    ):
        raise BlueprintRevisionError("blueprint_revision_generation_mismatch")
    hard_slots = revision.get("hard_slots") or {}
    revised = revision.get("revised_blueprint") or {}
    if not isinstance(revised, Mapping) or not revised:
        raise BlueprintRevisionError("blueprint_revision_has_no_blueprint")
    violations = hard_slot_violations(hard_slots, revised)
    if violations:
        raise BlueprintRevisionError(
            "blueprint_revision_breaks_hard_slots:%s" % ",".join(violations))
    rounds = revision.get("rounds") or []
    if len(rounds) > MAX_ROUNDS:
        raise BlueprintRevisionError("blueprint_revision_too_many_rounds")
    # every round descends from the last ACCEPTED revision: a rejected round does
    # not move the parent, so the chain is over accepted revisions, not over rounds
    parent = _text(revision.get("parent_revision_hash"))
    for row in rounds:
        if _text(row.get("parent_revision_hash")) != parent:
            raise BlueprintRevisionError("blueprint_revision_chain_broken")
        if row.get("accepted"):
            parent = _text(row.get("round_hash"))
    planned = {_text(row.get("candidate_id")) for r in rounds
               for row in r.get("candidates") or ()}
    decided = set(_text(item) for item in revision.get("accepted_change_ids") or [])
    decided |= {_text(row.get("candidate_id"))
                for row in revision.get("rejected_changes") or ()}
    undecided = planned - decided
    if undecided:
        raise BlueprintRevisionError(
            "blueprint_revision_has_unmaterialised_proposals:%s"
            % ",".join(sorted(undecided)))
    return revision


def revision_consumer_view(revision: Mapping[str, Any]) -> dict:
    """What Phase 3 is allowed to read out of a frozen revision."""

    revised = revision.get("revised_blueprint") or {}
    sections = {}
    for section in _sections(revised):
        section_id = _text(section.get("section_id"))
        sections[section_id] = {
            "title": _text(section.get("title")),
            "required_roles": _rows(section.get("required_roles")),
            "soft_roles": _rows(section.get("soft_roles")),
            "comparison_axes": _rows(section.get("comparison_axes")),
            "subsections": [
                dict(item) for item in (section.get("subsections") or [])
                if isinstance(item, Mapping)
            ],
            "revision_mounts": [
                dict(item) for item in (section.get("evidence_mounts") or [])
                if isinstance(item, Mapping)
            ],
        }
    newly = []
    for row in revision.get("rounds") or ():
        for chunk_id in (row.get("mounting_after") or {}).get(
            "newly_mounted_chunk_ids"
        ) or ():
            if _text(chunk_id) and _text(chunk_id) not in newly:
                newly.append(_text(chunk_id))
    return {
        "schema_version": "optomind.upgrade3.blueprint_revision_view.v1",
        "generation_id": _text(revision.get("generation_id")),
        "revision_hash": _text(revision.get("revision_hash")),
        "hard_slot_fingerprint": _text(revision.get("hard_slot_fingerprint")),
        "hard_slots_preserved": True,
        "question_required_roles": list(
            (revision.get("hard_slots") or {}).get("question_required_roles") or []
        ),
        "sections": sections,
        "newly_mounted_chunk_ids": newly,
        "accepted_change_ids": list(revision.get("accepted_change_ids") or []),
        "grants_permission": False,
    }


def apply_revision_mounts(
    *,
    revision: Mapping[str, Any],
    served_chunk_ids_by_section: Mapping[str, Iterable[str]],
    available_records: Sequence[Mapping[str, Any]],
    limit_per_section: int = 0,
    scope_receipts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serve what the accepted revision mounted, BEFORE the claims are made.

    The revision is the literature's half of the plan: it says which in-domain
    material a section must now host.  A mount the claim factory never sees is a
    sentence in a file rather than a change, so the mounted chunks join the
    section's served set here.  Nothing is admitted that was not already real,
    in-domain material, and a section is never served past its own limit.
    """

    out: dict[str, list] = {
        section_id: [_text(item) for item in ids if _text(item)]
        for section_id, ids in (served_chunk_ids_by_section or {}).items()
    }
    declarations = {
        _text(section.get("section_id")): section
        for section in _sections(revision.get("revised_blueprint") or {})
    }
    index = {
        _text(row.get("chunk_id")): dict(row)
        for row in available_records or ()
        if _text(row.get("chunk_id"))
    }
    applications: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    added: dict[str, list] = {}

    if revision.get("frozen") is not True or not declarations:
        return {
            "schema_version": "optomind.upgrade3.revision_mount_application.v1",
            "served_chunk_ids_by_section": {
                key: sorted(value) for key, value in sorted(out.items())
            },
            "added_by_section": {},
            "applications": [],
            "refusals": [],
            "counts": {"added": 0, "refused": 0},
            "note": "no frozen revision to apply",
        }

    mounted: list[tuple] = []
    for section_id, section in sorted(declarations.items()):
        for row in section.get("evidence_mounts") or ():
            if not isinstance(row, Mapping):
                continue
            for chunk_id in row.get("chunk_ids") or ():
                mounted.append((section_id, _text(chunk_id)))
    mounted.sort(key=lambda item: (item[0], item[1]))

    limit = int(limit_per_section or 0)
    evicted: dict[str, list] = {}
    accepted: dict[str, list] = {}
    for section_id, chunk_id in mounted:
        if not chunk_id:
            continue
        if section_id not in out:
            out[section_id] = []
        if chunk_id in out[section_id] or chunk_id in accepted.get(section_id, []):
            refusals.append({"section_id": section_id, "chunk_id": chunk_id,
                             "reason": "already_served"})
            continue
        record = index.get(chunk_id)
        if record is None:
            refusals.append({"section_id": section_id, "chunk_id": chunk_id,
                             "reason": "chunk_not_in_the_available_material"})
            continue
        # The domain decision is the run's real scope receipt, folded the same way
        # admission folds it.  A chunk's acquisition label is not a substitute: the
        # slice proved it (the served library labelled a hydrology paper "direct"
        # while the real judge refused it), and an absent receipt is not permission.
        receipts = dict(scope_receipts or {})
        document_id = _text(
            record.get("document_id")
            or record.get("paper_id")
            or (record.get("source_locator") or {}).get("paper_id"))
        receipt = receipts.get(document_id) or {}
        if not receipt:
            refusals.append({
                "section_id": section_id, "chunk_id": chunk_id,
                "reason": "out_of_domain_mount_refused:no_scope_receipt",
            })
            continue
        verdict = canonical_scope_verdict(receipt.get("verdict"))
        if verdict not in IN_DOMAIN_VERDICTS:
            refusals.append({
                "section_id": section_id, "chunk_id": chunk_id,
                "reason": "out_of_domain_mount_refused:%s" % (verdict or "missing"),
            })
            continue
        accepted.setdefault(section_id, []).append(chunk_id)
        added.setdefault(section_id, []).append(chunk_id)
        applications.append({
            "section_id": section_id, "chunk_id": chunk_id,
            "decision": "mounted", "scope_verdict": verdict,
            "literature_role": _text(record.get("literature_role")),
        })

    # The revision's mount is a decision, so it takes precedence over the
    # discretionary serving: the mounted chunks are served first and the section's
    # own material fills the remaining capacity in its original order.  This runs
    # before the claim factory, so nothing has been claimed from what yields.
    for section_id, mounted_here in sorted(accepted.items()):
        original = [item for item in out.get(section_id, [])
                    if item not in set(mounted_here)]
        if limit:
            room = max(0, limit - len(mounted_here))
            kept, dropped = original[:room], original[room:]
        else:
            kept, dropped = original, []
        out[section_id] = list(mounted_here) + kept
        if dropped:
            evicted[section_id] = dropped
            for chunk_id in dropped:
                refusals.append({"section_id": section_id, "chunk_id": chunk_id,
                                 "reason": "not_served_mount_takes_precedence"})

    return {
        "schema_version": "optomind.upgrade3.revision_mount_application.v1",
        "served_chunk_ids_by_section": {
            key: sorted(value) for key, value in sorted(out.items())
        },
        "added_by_section": {
            key: sorted(value) for key, value in sorted(added.items())
        },
        "evicted_by_section": {
            key: list(value) for key, value in sorted(evicted.items())
        },
        "applications": applications,
        "refusals": refusals,
        "counts": {"added": sum(len(v) for v in added.values()),
                   "evicted": sum(len(v) for v in evicted.values()),
                   "refused": len(refusals)},
        "rule": "an accepted mount is served first; the section's own material "
                "fills the remaining capacity and yields when there is none",
    }


def unavailable_payloads(*, reason: str) -> dict:
    """A disabled producer states so explicitly; it never fabricates a revision."""

    return {
        PROPOSAL_FILENAME: "",
        MOUNTING_FILENAME: "",
        REVISION_FILENAME: {
            "schema_version": REVISION_SCHEMA,
            "status": "unavailable",
            "reason": str(reason),
            "revision_hash": "",
            "frozen": False,
            "rounds": [],
            "accepted_change_ids": [],
            "hard_slots_preserved": True,
        },
    }


__all__ = [
    "BlueprintRevisionError",
    "CANDIDATE_SCHEMA",
    "HARD_SLOT_SCHEMA",
    "MANIFEST_SCHEMA",
    "MAX_ACCEPTED_PER_ROUND",
    "MAX_ROUNDS",
    "MOUNTING_FILENAME",
    "MOUNTING_SCHEMA",
    "PROPOSAL_FILENAME",
    "PROPOSAL_SCHEMA",
    "REVISION_FILENAME",
    "REVISION_SCHEMA",
    "STRUCTURAL_KINDS",
    "admits_round",
    "apply_revision_mounts",
    "build_revision",
    "extract_hard_slots",
    "hard_slot_violations",
    "load_frozen_revision",
    "mount_evidence",
    "plan_candidates",
    "revision_consumer_view",
    "rollback_to_parent",
    "unavailable_payloads",
    "write_revision_artifacts",
]
