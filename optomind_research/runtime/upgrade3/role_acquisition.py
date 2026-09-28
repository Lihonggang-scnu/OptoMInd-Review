"""Role-targeted semantic acquisition: asking the material for a role by meaning.

SM10's material block was diagnosed as "the literature does not exist".  It was not:
the generation held 50 role-assigned ledger sources and a 39,295-unit central cache,
and the only way anything was ever asked for a role was a ledger field that was
already filled in.  The semantic channel existed and was never switched on.

This module is that channel, with the contract spelled out:

* a role may only be asked for when the section's blueprint requires it, and only
  when the section does not already cover it, so acquisition can never invent a
  requirement the review does not have;
* a hit is material only if the unit really is registered, carries a chunk
  identity, is deep enough for the role it is being asked to carry, is not
  reference furniture, and is long enough to argue with;
* one chunk carries one role, and the role it carries records the query, the
  model, the representation and the score that found it, so the assignment is
  auditable rather than asserted;
* an acquisition writes NO scope verdict and raises NO permission ceiling.  It
  proposes material; the real domain judge and the binder still decide.

The consumer side is verify_acquisition: a manifest is only usable when the
knowledge base it names holds exactly those chunks, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ACQUISITION_SCHEMA = "optomind.upgrade3.role_acquisition.v1"
MANIFEST_FILENAME = "ROLE_ACQUISITION.json"

RETRIEVAL_BACKEND = "central_material_semantic_cache"
ACQUISITION_METHOD = "role_targeted_semantic_acquisition"
DEFAULT_TOP_K = 12
DEFAULT_SCORE_FLOOR = 0.55

#: how much text a chunk needs before it can be argued with at all
MIN_UNIT_CHARS = 160

#: the depth ladder the material store itself uses
DEPTH_ORDER = {"metadata": 0, "abstract": 1, "structured_snippet": 2, "fulltext": 3}

#: the least depth each argument role can be carried by.  The pipeline's own
#: source cap is what sets this: only fulltext reaches qualified_support, while
#: a structured snippet and even a partial fulltext cap out at reported_only,
#: which can never be load-bearing.  Acquiring a snippet for a role the
#: blueprint requires therefore buys a card that is guaranteed to be rejected,
#: so every role that has to become evidence needs fulltext.
ROLE_MIN_DEPTH = {
    "foundation": "fulltext",
    "mechanism": "fulltext",
    "method": "fulltext",
    "controversy": "fulltext",
    "comparison": "fulltext",
    "application": "fulltext",
    "background": "structured_snippet",
    "trend": "structured_snippet",
    "frontier": "structured_snippet",
}
DEFAULT_MIN_DEPTH = "fulltext"

#: what each role is looking for, in the words the review itself would use
ROLE_QUERY_INTENT = {
    "foundation": (
        "physical and theoretical foundations: the principles, governing equations "
        "and enabling phenomena that the review's claims rest on"
    ),
    "mechanism": (
        "the mechanism by which the system performs its function: the internal "
        "process, the structure that implements it and how the two connect"
    ),
    "method": (
        "methods for training, designing or optimising the system: algorithms, "
        "backpropagation and in-situ or hardware-in-the-loop training, and the "
        "sample budgets and hardware each requires"
    ),
    "controversy": (
        "limitations, contradictions, unresolved disagreements and negative "
        "results: where reported findings conflict, where simulation and "
        "experiment disagree, and under what conditions each claim holds"
    ),
    "comparison": (
        "comparative evaluations of competing approaches under a shared metric or "
        "benchmark"
    ),
    "application": "demonstrated applications and the performance achieved on them",
    "trend": "emerging directions and how the field has moved over time",
    "frontier": "open problems and work that is only beginning",
    "background": "established background that the review can take as given",
}

_ROLE_FALLBACK_INTENT = "material that can carry this section's argumentative role"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _depth_rank(value: Any) -> int:
    raw = _text(value).casefold()
    if raw in DEPTH_ORDER:
        return DEPTH_ORDER[raw]
    for name, rank in DEPTH_ORDER.items():
        if name and name in raw:
            return rank
    return -1


def min_depth_for_role(role: str) -> str:
    return ROLE_MIN_DEPTH.get(_text(role).casefold(), DEFAULT_MIN_DEPTH)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #

def role_query_text(
    role: str,
    *,
    topic: str,
    section_title: str = "",
    key_questions: Sequence[Any] = (),
) -> str:
    """The words an acquisition asks the material store with.

    The topic is always present so a role can never be filled with material from
    another review, and the section title keeps two sections that demand the same
    role from asking the identical question.
    """

    parts = [_text(topic)]
    if _text(section_title):
        parts.append(_text(section_title))
    parts.append(ROLE_QUERY_INTENT.get(_text(role).casefold(), _ROLE_FALLBACK_INTENT))
    questions = [_text(q) for q in key_questions if _text(q)]
    if questions:
        parts.append(" ; ".join(questions[:3]))
    return " :: ".join(p for p in parts if p)


def coverage_gaps_from_verdicts(
    verdicts: Mapping[str, Any] | None,
    *,
    section_ids: Iterable[str] = (),
) -> list[dict[str, str]]:
    """The roles a run's own coverage verdict says are still missing.

    This is the honest gap list.  Deciding from the ledger asks "was a paper
    registered for this role"; deciding from the verdict asks "did the run bind
    a claim under this role", and only the second one is a statement about the
    review.  A run that bound mechanism and methods while reporting foundation
    missing has said exactly what to go and get.
    """

    wanted = {_text(item) for item in section_ids if _text(item)}
    gaps: list[dict[str, str]] = []
    for section_id, body in (verdicts or {}).items():
        sid = _text(section_id)
        if wanted and sid not in wanted:
            continue
        if not isinstance(body, Mapping):
            continue
        evidence = body.get("coverage_evidence")
        if not isinstance(evidence, Mapping):
            continue
        for role in evidence.get("missing_roles") or ():
            role = _text(role)
            if role:
                gaps.append({"section_id": sid, "role": role})
    return gaps


def plan_role_acquisitions(
    sections: Sequence[Mapping[str, Any]],
    *,
    topic: str,
    covered: Iterable[Mapping[str, Any]] = (),
    gaps: Iterable[Mapping[str, Any]] | None = None,
    top_k: int = DEFAULT_TOP_K,
    score_floor: float = DEFAULT_SCORE_FLOOR,
) -> list[dict[str, Any]]:
    """One request per (section, required role the section does not yet cover).

    ``gaps`` is the run's own verdict about what is missing and, when given, is
    the only thing that decides.  A role is still only ever requested when the
    section's blueprint requires it, so neither the ledger nor a verdict can
    make acquisition invent a requirement the review does not have.
    """

    wanted: set[tuple[str, str]] | None = None
    if gaps is not None:
        wanted = {
            (_text(gap.get("section_id")),
             _text(gap.get("role") or gap.get("literature_role")))
            for gap in gaps
        }
        wanted = {(sid, role) for sid, role in wanted if sid and role}
    have: dict[str, set[str]] = {}
    for row in covered:
        section_id = _text(row.get("section_id"))
        role = _text(row.get("literature_role") or row.get("role"))
        if section_id and role:
            have.setdefault(section_id, set()).add(role.casefold())
    plan: list[dict[str, Any]] = []
    for section in sections:
        section_id = _text(section.get("section_id"))
        if not section_id:
            continue
        required = [_text(r) for r in section.get("required_roles") or () if _text(r)]
        for role in required:
            if wanted is not None:
                if (section_id, role) not in wanted:
                    continue
            elif role.casefold() in have.get(section_id, set()):
                continue
            plan.append({
                "section_id": section_id,
                "section_title": _text(section.get("title")),
                "role": role,
                "query": role_query_text(
                    role,
                    topic=topic,
                    section_title=_text(section.get("title")),
                    key_questions=section.get("key_questions") or (),
                ),
                "top_k": int(top_k),
                "score_floor": float(score_floor),
                "min_depth": min_depth_for_role(role),
            })
    return plan


#: the permission vocabulary that may carry a factual claim at all
FACTUAL_PERMISSIONS = ("factual_support", "writable", "full_text", "evidence")

#: the evidence-card anchor a role has to be able to produce.  The symptom of
#: choosing the wrong chunk for a role is card_field_not_reported, and a chunk
#: that yields no anchor for its own role is the wrong chunk whatever its
#: similarity score says.
ROLE_ANCHOR_FIELD = {
    "method": "method",
    "controversy": "limitations",
    "frontier": "limitations",
    "mechanism": "results",
    "foundation": "results",
    "comparison": "results",
    "application": "results",
    "trend": "results",
    "background": "results",
}


def anchor_field_for_role(role: str) -> str:
    return ROLE_ANCHOR_FIELD.get(_text(role).casefold(), "results")


#: what a role needs from the material that claims to carry it
ROLE_MIN_PERMISSION = {
    "foundation": "background_only",
    "background": "background_only",
    "trend": "background_only",
    "application": "factual_support",
    "frontier": "background_only",
    "mechanism": "factual_support",
    "method": "factual_support",
    "controversy": "factual_support",
    "comparison": "factual_support",
}

#: the ceiling vocabulary the material store actually uses, weakest first
_PERMISSION_RANK = {
    "": 0,
    "discovery_only": 1,
    "metadata_only": 1,
    "background_and_candidate_only": 1,
    "contextual_or_qualified_support": 2,
    "contextual_support": 2,
    "qualified_support": 2,
    "background_only": 2,
    "factual_support": 3,
    "writable": 4,
}


def _permission_rank(value: Any) -> int:
    raw = _text(value).casefold()
    if raw in _PERMISSION_RANK:
        return _PERMISSION_RANK[raw]
    for name in sorted(_PERMISSION_RANK, key=len, reverse=True):
        if name and name in raw:
            return _PERMISSION_RANK[name]
    return 0


def min_permission_for_role(role: str) -> str:
    return ROLE_MIN_PERMISSION.get(_text(role).casefold(), "factual_support")


def usable_role_coverage(
    sources: Iterable[Mapping[str, Any]],
    *,
    registered_chunk_ids: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    """Which (section, role) pairs the ledger really covers with usable material.

    A ledger row is a claim that a paper was collected for a role, not that the
    section can argue with it.  Counting rows as coverage is what let the slice
    believe it had foundation, mechanism and controversy material while every
    claim it could actually bind was a method claim: the papers registered under
    those roles were metadata-only, discovery-only or out of domain.  Coverage
    therefore means depth at or above the role's floor, a permission that can
    carry a factual claim, and an in-domain scope.
    """

    from .evidence_mounting import canonical_scope_verdict

    registered = None
    if registered_chunk_ids is not None:
        registered = {_text(item) for item in registered_chunk_ids if _text(item)}
    covered: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in sources:
        section_id = _text(row.get("section_id"))
        role = _text(row.get("literature_role") or row.get("role"))
        if not section_id or not role:
            continue
        key = (section_id, role.casefold())
        if key in seen:
            continue
        # A ledger entry for a paper whose chunks are not in the knowledge base
        # the generation registers is a plan, not material.  The 047-slice
        # generation registered 50 sources and held 25 papers, and the roles it
        # could not serve were exactly the ones whose chunks were never there.
        if registered is not None:
            chunks = [_text(item) for item in row.get("canonical_chunk_ids") or ()]
            if not any(chunk in registered for chunk in chunks):
                continue
        if canonical_scope_verdict(row.get("scope_fit")) not in ("direct", "adjacent"):
            continue
        role_key = role.casefold()
        permission_floor = _PERMISSION_RANK[
            ROLE_MIN_PERMISSION.get(role_key, "factual_support")
        ]
        if _permission_rank(row.get("use_permission")) < permission_floor:
            continue
        if _depth_rank(row.get("content_depth")) < _depth_rank(
            min_depth_for_role(role)
        ):
            continue
        seen.add(key)
        covered.append({"section_id": section_id, "literature_role": role})
    return covered


# --------------------------------------------------------------------------- #
# taking hits
# --------------------------------------------------------------------------- #

def _unit_text(unit: Mapping[str, Any]) -> str:
    content = unit.get("durable_content")
    if isinstance(content, Mapping):
        return _text(content.get("raw_text") or content.get("normalized_text"))
    return ""


def _unit_identity(unit: Mapping[str, Any]) -> dict[str, Any]:
    identity = unit.get("identity")
    return dict(identity) if isinstance(identity, Mapping) else {}


def _unit_depth(unit: Mapping[str, Any]) -> str:
    content = unit.get("durable_content")
    depth = _text(content.get("content_depth")) if isinstance(content, Mapping) else ""
    if depth:
        return depth
    card = unit.get("durable_content_card")
    if isinstance(card, Mapping):
        quality = card.get("content_quality")
        if isinstance(quality, Mapping):
            return _text(quality.get("source_kind"))
    return ""


def _unit_ceiling(unit: Mapping[str, Any]) -> str:
    card = unit.get("durable_content_card")
    if isinstance(card, Mapping):
        quality = card.get("content_quality")
        if isinstance(quality, Mapping) and _text(quality.get("evidence_ceiling")):
            return _text(quality.get("evidence_ceiling"))
    audit = unit.get("audit")
    if isinstance(audit, Mapping):
        provenance = audit.get("source_provenance")
        if isinstance(provenance, Mapping):
            return _text(provenance.get("use_permission"))
    return ""


def _unit_source_kind(unit: Mapping[str, Any]) -> str:
    card = unit.get("durable_content_card")
    if isinstance(card, Mapping):
        quality = card.get("content_quality")
        if isinstance(quality, Mapping) and _text(quality.get("source_kind")):
            return _text(quality.get("source_kind"))
    return _unit_depth(unit)


def _unit_locator(unit: Mapping[str, Any]) -> dict[str, Any]:
    identity = _unit_identity(unit)
    locator = identity.get("locator")
    locator = dict(locator) if isinstance(locator, Mapping) else {}
    content = unit.get("durable_content")
    if isinstance(content, Mapping):
        for key, source in (("section_path", "section_path"), ("ordinal", "ordinal"),
                            ("char_start", "char_start"), ("char_end", "char_end")):
            if content.get(source) not in (None, ""):
                locator.setdefault(key, content.get(source))
    locator.setdefault("chunk_id", _text(identity.get("chunk_id")))
    locator.setdefault("doi", _text(identity.get("doi")))
    return locator


def _assignment(
    unit: Mapping[str, Any],
    hit: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    rank: int,
    model: str,
    representation_version: str,
) -> dict[str, Any]:
    identity = _unit_identity(unit)
    return {
        "schema_version": "optomind.upgrade3.role_acquisition.assignment.v1",
        "unit_id": _text(unit.get("unit_id")),
        "paper_id": _text(identity.get("paper_id")),
        "chunk_id": _text(identity.get("chunk_id")),
        "doi": _text(identity.get("doi")),
        "title": _text(identity.get("title")),
        "section_id": _text(request.get("section_id")),
        "role": _text(request.get("role")),
        "query": _text(request.get("query")),
        "query_sha256": _sha_text(request.get("query") or ""),
        "query_model": model,
        "representation_version": representation_version,
        "score": float(hit.get("score") or 0.0),
        "rank": rank,
        "content_depth": _unit_depth(unit),
        "use_permission": _unit_ceiling(unit),
    }


def accept_hits(
    hits: Sequence[Mapping[str, Any]],
    *,
    request: Mapping[str, Any],
    units: Mapping[str, Mapping[str, Any]],
    taken_chunk_ids: Iterable[str] = (),
    model: str = "",
    representation_version: str = "",
    min_chars: int = MIN_UNIT_CHARS,
    furniture_detector=None,
    anchor_selector=None,
) -> dict[str, Any]:
    """Which of the returned hits may actually carry the requested role."""

    from optomind_research.runtime.upgrade3.evidence_first import (
        _looks_like_reference_furniture as _furniture,
        _select_anchors as _anchors,
    )

    detector = furniture_detector or _furniture
    selector = anchor_selector or _anchors
    wanted_field = anchor_field_for_role(_text(request.get("role")))

    def _anchors_for(unit):
        try:
            return dict(selector(_unit_text(unit)) or {})
        except Exception:  # pragma: no cover - defensive
            return {}
    floor = float(request.get("score_floor", DEFAULT_SCORE_FLOOR))
    top_k = int(request.get("top_k", DEFAULT_TOP_K))
    wanted_rank = _depth_rank(request.get("min_depth") or min_depth_for_role(
        _text(request.get("role"))))
    taken = {_text(item) for item in taken_chunk_ids if _text(item)}
    accepted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    seen_chunks: set[str] = set(taken)
    seen_papers: set[str] = set()

    # A chunk that yields the role's own anchor is worth more than a slightly
    # closer chunk that yields some other field, so the role anchor is the
    # first key and the similarity score only breaks ties inside it.
    def _rank(hit):
        unit = units.get(_text(hit.get("unit_id")))
        anchors = _anchors_for(unit) if isinstance(unit, Mapping) else {}
        return (
            _depth_rank(_unit_depth(unit)) if isinstance(unit, Mapping) else -1,
            1 if _text(anchors.get(wanted_field)) else 0,
            1 if any(_text(value) for value in anchors.values()) else 0,
            float(hit.get("score") or 0.0),
        )

    ordered = sorted((dict(hit) for hit in hits), key=_rank, reverse=True)
    for rank, hit in enumerate(ordered):
        unit_id = _text(hit.get("unit_id"))
        score = float(hit.get("score") or 0.0)
        unit = units.get(unit_id)
        reason = ""
        if score < floor:
            reason = "hit_below_score_floor"
        elif not isinstance(unit, Mapping):
            reason = "hit_not_a_registered_unit"
        else:
            identity = _unit_identity(unit)
            chunk_id = _text(identity.get("chunk_id"))
            paper_id = _text(identity.get("paper_id"))
            body = _unit_text(unit)
            if not chunk_id or not paper_id:
                reason = "unit_missing_identity"
            elif chunk_id in seen_chunks:
                reason = "chunk_already_carries_a_role"
            elif paper_id in seen_papers:
                reason = "paper_already_taken_for_this_request"
            elif _depth_rank(_unit_depth(unit)) < wanted_rank:
                reason = "unit_depth_below_role_requirement"
            elif _permission_rank(_unit_ceiling(unit)) < _PERMISSION_RANK[
                min_permission_for_role(_text(request.get("role")))
            ]:
                # A role is not carried by material that is not permitted to
                # carry it.  Selecting purely by similarity picked units whose
                # ceiling is qualified/background, and every card built from
                # them was rejected downstream as permission_below_support.
                reason = "unit_permission_below_role_requirement"
            elif len(body) < int(min_chars):
                reason = "unit_text_too_short"
            elif detector(body):
                reason = "unit_is_reference_furniture"
            else:
                anchors = _anchors_for(unit)
                if not any(_text(value) for value in anchors.values()):
                    # The similarity score ranks a chunk by what it is about;
                    # the card producer needs a paragraph it can quote.  A
                    # chunk that yields no anchor at all cannot become a card
                    # however close it is to the query, and taking it is what
                    # produced card_field_not_reported downstream.  Requiring
                    # the role's OWN field would be too strong -- controversy
                    # material rarely states a limitation in one sentence --
                    # so the role field orders the candidates instead.
                    reason = "unit_yields_no_usable_anchor"
        if reason:
            refused.append({"unit_id": unit_id, "score": round(score, 6),
                            "rank": rank, "reason": reason})
            continue
        if len(accepted) >= top_k:
            refused.append({"unit_id": unit_id, "score": round(score, 6),
                            "rank": rank, "reason": "above_top_k"})
            continue
        assignment = _assignment(unit, hit, request, rank=rank, model=model,
                                 representation_version=representation_version)
        accepted.append(assignment)
        seen_chunks.add(assignment["chunk_id"])
        seen_papers.add(assignment["paper_id"])
    return {"accepted": accepted, "refused": refused, "request": dict(request)}


def role_verdict(request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    accepted = list(result.get("accepted") or ())
    refused = list(result.get("refused") or ())
    return {
        "section_id": _text(request.get("section_id")),
        "role": _text(request.get("role")),
        "met": bool(accepted),
        "accepted": len(accepted),
        "refused": len(refused),
        "query_sha256": _sha_text(request.get("query") or ""),
    }


# --------------------------------------------------------------------------- #
# the rows an acquisition may write
# --------------------------------------------------------------------------- #

def material_row(
    unit: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    documents_root: str | os.PathLike[str] | None = None,
    relative_prefix: str = "",
) -> dict[str, Any]:
    """One knowledge-base text_chunks row.  Scope is left to the real judge.

    When a document root is given the row also declares the held text that backs
    it, so the resolver can verify the bytes instead of refusing a paper the
    generation has read but never downloaded.
    """

    identity = _unit_identity(unit)
    body = _unit_text(unit)
    locator = _unit_locator(unit)
    locator["acquisition"] = {
        "role": _text(assignment.get("role")),
        "section_id": _text(assignment.get("section_id")),
        "score": assignment.get("score"),
        "query_sha256": _text(assignment.get("query_sha256")),
        "unit_id": _text(assignment.get("unit_id")),
    }
    if documents_root is not None:
        from . import material_documents as _documents

        declaration = _documents.materialise_document(
            root=documents_root,
            chunk_id=_text(identity.get("chunk_id")),
            paper_id=_text(identity.get("paper_id")),
            doi=_text(identity.get("doi")),
            title=_text(identity.get("title")),
            text=body,
            locator=locator,
        )
        prefix = _text(relative_prefix).strip("/")
        if prefix:
            declaration = dict(
                declaration,
                relative_path="%s/%s" % (prefix, declaration["relative_path"]),
            )
        locator[_documents.DECLARATION_KEY] = declaration
    return {
        "chunk_id": _text(identity.get("chunk_id")),
        "paper_id": _text(identity.get("paper_id")),
        "doi": _text(identity.get("doi")),
        "title": _text(identity.get("title")),
        "text": body,
        "text_hash": _sha_text(body),
        "content_depth": _unit_depth(unit),
        "use_permission": _unit_ceiling(unit),
        "scope_fit": "unreviewed",
        "source_kind": _unit_source_kind(unit),
        "raw_json": json.dumps({
            "source_locator": locator,
            "content_hash": _text((unit.get("durable_content") or {}).get("content_hash")),
            "unit_id": _text(unit.get("unit_id")),
        }, ensure_ascii=False, sort_keys=True),
    }


def ledger_source(unit: Mapping[str, Any], assignment: Mapping[str, Any]) -> dict[str, Any]:
    """The ledger entry that registers the acquired chunk under its role."""

    identity = _unit_identity(unit)
    return {
        "paper_id": _text(identity.get("paper_id")),
        "doi": _text(identity.get("doi")),
        "title": _text(identity.get("title")),
        "year": None,
        "venue": "",
        "authors": [],
        "literature_role": _text(assignment.get("role")),
        "scope_fit": "unreviewed",
        "retrieval_query": _text(assignment.get("query")),
        "retrieval_backend": RETRIEVAL_BACKEND,
        "adoption_reason": (
            "role-targeted semantic retrieval: similarity %.4f to the %s role "
            "description of %s" % (
                float(assignment.get("score") or 0.0),
                _text(assignment.get("role")),
                _text(assignment.get("section_id")),
            )
        ),
        "expected_section_use": _text(assignment.get("section_id")),
        "canonical_chunk_ids": [_text(identity.get("chunk_id"))],
        "local_prior": True,
        "new_this_run": True,
        "acquisition_status": _unit_depth(unit),
        "normalization_status": "",
        "section_id": _text(assignment.get("section_id")),
        "not_usable_for": [],
        "discovery_route": "central_long_term_material_cache",
        "materialization_route": _text(
            ((unit.get("audit") or {}).get("source_provenance") or {}).get(
                "materialization_route")) or "material_unit_store",
        "content_depth": _unit_depth(unit),
        "use_permission": _unit_ceiling(unit),
        "allowed_claim_kinds": list(
            (((unit.get("durable_content_card") or {}).get("content_quality") or {})
             .get("allowed_claim_kinds") or ())),
        "route_events": [{
            "event": "role_targeted_semantic_acquisition",
            "route": RETRIEVAL_BACKEND,
            "query_sha256": _text(assignment.get("query_sha256")),
            "score": assignment.get("score"),
            "rank": assignment.get("rank"),
            "representation_version": _text(assignment.get("representation_version")),
            "embedding_model": _text(assignment.get("query_model")),
        }],
        "metadata_conflicts": [],
        "relation_roles": [],
        "role_provenance": {
            "method": ACQUISITION_METHOD,
            "role": _text(assignment.get("role")),
            "section_id": _text(assignment.get("section_id")),
            "query": _text(assignment.get("query")),
            "query_sha256": _text(assignment.get("query_sha256")),
            "query_model": _text(assignment.get("query_model")),
            "representation_version": _text(assignment.get("representation_version")),
            "score": assignment.get("score"),
            "rank": assignment.get("rank"),
            "unit_id": _text(assignment.get("unit_id")),
        },
    }


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #

def write_kb(path: str | os.PathLike[str], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-%d" % os.getpid())
    for stale in (temporary, target):
        if stale.exists():
            stale.unlink()
    connection = sqlite3.connect(str(temporary))
    try:
        connection.execute(
            "CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, doi TEXT, "
            "title TEXT, text TEXT, content_depth TEXT, use_permission TEXT, "
            "scope_fit TEXT, source_kind TEXT, raw_json TEXT)"
        )
        connection.execute("CREATE UNIQUE INDEX text_chunks_chunk_id ON text_chunks(chunk_id)")
        for row in rows:
            connection.execute(
                "INSERT INTO text_chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row.get("chunk_id"), row.get("paper_id"), row.get("doi"),
                 row.get("title"), row.get("text"), row.get("content_depth"),
                 row.get("use_permission"), row.get("scope_fit"),
                 row.get("source_kind"), row.get("raw_json")),
            )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, target)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"path": str(target), "rows": len(rows), "sha256": digest.hexdigest()}


def read_kb_rows(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every chunk a previously written acquisition knowledge base holds."""

    target = Path(path)
    if not target.is_file():
        return []
    try:
        connection = sqlite3.connect(
            "file:" + str(target).replace(chr(92), "/") + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        columns = {str(row[1]) for row in connection.execute(
            "PRAGMA table_info(text_chunks)")}
        if not columns:
            return []
        rows = [
            {"chunk_id": row[0], "paper_id": row[1], "doi": row[2],
             "title": row[3], "text": row[4], "content_depth": row[5],
             "use_permission": row[6], "scope_fit": row[7],
             "source_kind": row[8], "raw_json": row[9]}
            for row in connection.execute(
                "SELECT chunk_id, paper_id, doi, title, text, content_depth, "
                "use_permission, scope_fit, source_kind, raw_json FROM text_chunks")
        ]
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    for row in rows:
        row["text_hash"] = _sha_text(_text(row.get("text")))
    return rows


def merge_material_rows(
    existing: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Accumulate acquired material; never let a later run drop an earlier one.

    Acquisition is monotonic.  Each run answers the gaps the last one reported,
    and rewriting the knowledge base from the new answer alone silently deleted
    the material the previous answer had already made usable -- including the
    only paper that had ever carried a mechanism claim.  Material identity is
    (chunk_id, text): a later row for the same chunk id with different bytes is
    a conflict that keeps the bytes already on record, never a silent swap.
    """

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in existing:
        chunk_id = _text(row.get("chunk_id"))
        if not chunk_id or chunk_id in merged:
            continue
        merged[chunk_id] = dict(row)
        order.append(chunk_id)
    outcomes: list[dict[str, Any]] = []
    for row in incoming:
        chunk_id = _text(row.get("chunk_id"))
        if not chunk_id:
            continue
        current = merged.get(chunk_id)
        if current is None:
            merged[chunk_id] = dict(row)
            order.append(chunk_id)
            outcomes.append({"chunk_id": chunk_id, "outcome": "added"})
            continue
        if _text(current.get("text")) == _text(row.get("text")):
            outcomes.append({"chunk_id": chunk_id, "outcome": "kept_existing"})
            continue
        outcomes.append({"chunk_id": chunk_id, "outcome": "text_conflict",
                         "kept_text_hash": _sha_text(_text(current.get("text"))),
                         "offered_text_hash": _sha_text(_text(row.get("text")))})
    return [merged[key] for key in order], outcomes


def extend_ledger(
    base_ledger: Mapping[str, Any] | None,
    sources: Sequence[Mapping[str, Any]],
    *,
    sections: Sequence[str] = (),
    topic: str = "",
) -> dict[str, Any]:
    """The base ledger plus the acquired sources, never a replacement of it."""

    ledger = dict(base_ledger or {})
    existing = [dict(row) for row in ledger.get("sources") or ()]
    known = {(row.get("paper_id"), _text(row.get("literature_role")))
             for row in existing}
    added: list[dict[str, Any]] = []
    for row in sources:
        key = (row.get("paper_id"), _text(row.get("literature_role")))
        if key in known:
            continue
        known.add(key)
        added.append(dict(row))
    ledger["sources"] = existing + added
    section_ids = [_text(item) for item in sections if _text(item)] or list(
        ledger.get("sections") or ())
    ledger["sections"] = section_ids
    ledger["schema_version"] = _text(ledger.get("schema_version")) or (
        "optomind.review.section_source_ledger.v1")
    ledger["source_of_truth"] = "role_targeted_semantic_acquisition"
    ledger["role_acquisition"] = {
        "topic": _text(topic),
        "added_sources": len(added),
        "base_sources": len(existing),
        "method": ACQUISITION_METHOD,
    }
    return ledger


def acquisition_manifest(
    *,
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    topic: str,
    model: str = "",
    representation_version: str = "",
    cache_path: str = "",
    cache_units: int = 0,
    cache_vectors: int = 0,
    kb: Mapping[str, Any] | None = None,
    ledger_path: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    by_request = {(_text(r.get("section_id")), _text(r.get("role"))): r for r in requests}
    verdicts: list[dict[str, Any]] = []
    for result in results:
        request = result.get("request") or {}
        verdicts.append(role_verdict(request, result))
    unmet = [v for v in verdicts if not v["met"]]
    body: dict[str, Any] = {
        "schema_version": ACQUISITION_SCHEMA,
        "method": ACQUISITION_METHOD,
        "topic": _text(topic),
        "retrieval_backend": RETRIEVAL_BACKEND,
        "embedding_model": _text(model),
        "representation_version": _text(representation_version),
        "cache_path": _text(cache_path),
        "cache_units": int(cache_units),
        "cache_vectors": int(cache_vectors),
        "requests": [dict(by_request.get((_text(r.get("request", {}).get("section_id")),
                                          _text(r.get("request", {}).get("role"))))
                          or dict(r.get("request") or {})) for r in results],
        "verdicts": verdicts,
        "unmet_roles": unmet,
        "refusals": [
            dict(row) for result in results for row in result.get("refused") or ()
        ],
        "assignments": [
            dict(row) for result in results for row in result.get("accepted") or ()
        ],
        "chunks": [{"chunk_id": _text(row.get("chunk_id")),
                    "paper_id": _text(row.get("paper_id")),
                    "text_sha256": _text(row.get("text_hash"))} for row in rows],
        "knowledge_base": dict(kb or {}),
        "ledger_path": _text(ledger_path),
        "created_at": _now(),
    }
    if extra:
        body.update(dict(extra))
    body["manifest_body_sha256"] = _sha_text(_canonical(body))
    return body


def write_acquisition(path: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-%d" % os.getpid())
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + chr(10),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load_acquisition(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def reseal_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Re-seal a manifest after a later channel added material to it."""

    body = {key: value for key, value in dict(payload).items()
            if key != "manifest_body_sha256"}
    body["manifest_body_sha256"] = _sha_text(_canonical(body))
    return body

def verify_acquisition(
    manifest: Mapping[str, Any],
    *,
    kb_paths: Sequence[str | os.PathLike[str]],
) -> dict[str, Any]:
    """The consumer check: the named knowledge base must hold exactly these chunks."""

    claimed = {_text(row.get("chunk_id")): _text(row.get("text_sha256"))
               for row in manifest.get("chunks") or () if _text(row.get("chunk_id"))}
    held: dict[str, str] = {}
    for raw in kb_paths:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            connection = sqlite3.connect(
                "file:" + str(path).replace(chr(92), "/") + "?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            columns = {str(row[1]) for row in connection.execute(
                "PRAGMA table_info(text_chunks)")}
            if not columns:
                continue
            for chunk_id, text in connection.execute(
                "SELECT chunk_id, text FROM text_chunks"
            ):
                key = _text(chunk_id)
                if key and key not in held:
                    held[key] = _sha_text(text or "")
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    missing = sorted(key for key in claimed if key not in held)
    changed = sorted(key for key, digest in claimed.items()
                     if key in held and held[key] != digest)
    unclaimed = sorted(key for key in held if key not in claimed)
    return {
        "verified": not missing and not changed,
        "claimed": len(claimed),
        "held": len(held),
        "missing_chunk_ids": missing,
        "changed_text_chunk_ids": changed,
        "unclaimed_chunk_ids": unclaimed,
    }