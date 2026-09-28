"""Compact relationship-aware material bundles for section writing.

The bundle is a planning object, not an evidence verdict.  It deliberately
keeps the author's synthesis space separate from established/conditional
material so a writer can make a useful review judgement without pretending
that every sentence was copied from a source.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .evidence_portfolio_selector import EvidencePortfolio, select_evidence_portfolio


@dataclass(slots=True)
class SynthesisBundle:
    bundle_id: str
    section_id: str
    argument_task: str
    relationship_pattern: str = "progression"
    paper_ids: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    established_points: list[str] = field(default_factory=list)
    conditional_points: list[str] = field(default_factory=list)
    conflicts_or_boundaries: list[str] = field(default_factory=list)
    claim_category_assignments: list[dict[str, Any]] = field(default_factory=list)
    argument_task_coverage: list[dict[str, Any]] = field(default_factory=list)
    paper_content_depth_summary: dict[str, str] = field(default_factory=dict)
    author_synthesis_space: list[str] = field(default_factory=list)
    forbidden_overclaims: list[str] = field(default_factory=list)
    relation_evidence: list[dict[str, Any]] = field(default_factory=list)
    source_permission_summary: dict[str, int] = field(default_factory=dict)
    candidate_pool_ref: str = ""
    candidate_pool_count: int = 0
    candidate_paper_count: int = 0
    # Machine-only inventory.  to_dict intentionally omits these IDs so a
    # writer never receives a hundred-item candidate list in its prompt.
    candidate_chunk_ids: list[str] = field(default_factory=list, repr=False)
    candidate_paper_ids: list[str] = field(default_factory=list, repr=False)
    invalid_paper_ids: list[str] = field(default_factory=list)
    invalid_chunk_ids: list[str] = field(default_factory=list)
    id_audit: list[dict[str, Any]] = field(default_factory=list)
    permission_audit: list[dict[str, Any]] = field(default_factory=list)
    allowlist_source: str = "section_claim_allowlist"
    selector_version: str = ""
    material_status: str = "inventory_only"
    readiness_status: str = "needs_more_literature"
    status: str = "needs_more_literature"
    selection_reasons: list[str] = field(default_factory=list)
    selection_diagnostics: dict[str, Any] = field(default_factory=dict)
    real_claim_count: int = 0
    usable_relation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("candidate_chunk_ids", None)
        payload.pop("candidate_paper_ids", None)
        return payload


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _pattern(edge_types: Iterable[str]) -> str:
    kinds = {str(item).casefold() for item in edge_types}
    if {"cited_by", "citations"} & kinds:
        return "progression"
    if {"semantic_recommendation", "same_research_branch"} & kinds:
        return "complementarity"
    if {"co_cited_with", "bibliographic_coupling"} & kinds:
        return "comparison"
    return "progression"


def build_synthesis_bundle(
    *,
    section: dict[str, Any],
    claims: Iterable[dict[str, Any]] = (),
    relation_edges: Iterable[Any] = (),
    source_permissions: dict[str, str] | None = None,
    chunk_permissions: dict[str, str] | None = None,
    allowed_paper_ids: Iterable[str] | None = None,
    allowed_chunk_ids: Iterable[str] | None = None,
    chunk_to_paper: dict[str, str] | None = None,
    chunk_records: Iterable[Any] | None = None,
    max_core_chunks: int = 12,
    preselected_portfolio: EvidencePortfolio | None = None,
    argument_task_coverage: Iterable[dict[str, Any]] = (),
    paper_content_depth_summary: dict[str, str] | None = None,
) -> SynthesisBundle:
    """Build a deterministic bundle from already validated IDs.

    No new paper or chunk ID is created here.  Unknown IDs are left out of the
    bundle, which makes it safe to pass this object to an LLM writer.
    """

    section_id = str(section.get("section_id") or "section")
    task = str(
        section.get("synthesis_task")
        or section.get("chapter_argument")
        or section.get("argument_role")
        or "Explain the section's role in the review argument."
    ).strip()
    claims = [item for item in claims if isinstance(item, dict)]
    relation_edge_input = list(relation_edges)
    raw_claim_papers = _unique(
        paper
        for claim in claims
        for paper in claim.get("citation_paper_ids", [])
    )
    raw_claim_chunks = _unique(
        chunk
        for claim in claims
        for chunk in (
            list(claim.get("supporting_text_chunk_ids") or [])
            + list(claim.get("context_text_chunk_ids") or [])
        )
    )
    explicit_papers = allowed_paper_ids is not None or bool(
        section.get("allowed_paper_ids") or section.get("paper_ids")
    )
    explicit_chunks = allowed_chunk_ids is not None or bool(
        section.get("allowed_chunk_ids") or section.get("chunk_ids")
    )
    paper_allow = set(
        _unique(
            allowed_paper_ids
            if allowed_paper_ids is not None
            else section.get("allowed_paper_ids")
            or section.get("paper_ids")
            or raw_claim_papers
        )
    )
    chunk_allow = set(
        _unique(
            allowed_chunk_ids
            if allowed_chunk_ids is not None
            else section.get("allowed_chunk_ids")
            or section.get("chunk_ids")
            or raw_claim_chunks
        )
    )
    invalid_papers = sorted(set(raw_claim_papers) - paper_allow)
    invalid_chunks = sorted(set(raw_claim_chunks) - chunk_allow)
    id_audit: list[dict[str, Any]] = [
        {"id_type": "paper_id", "id": item, "reason": "not_in_section_allowlist"}
        for item in invalid_papers
    ] + [
        {"id_type": "chunk_id", "id": item, "reason": "not_in_section_allowlist"}
        for item in invalid_chunks
    ]
    chunk_to_paper = dict(chunk_to_paper or {})
    paper_ids: list[str] = []
    chunk_ids: list[str] = []
    established: list[str] = []
    conditional: list[str] = []
    boundaries: list[str] = []
    category_assignments: list[dict[str, Any]] = []
    synthesis_space: list[str] = []
    forbidden = [
        "Do not turn metadata-only records into factual support.",
        "Do not present adjacent-domain results as direct in-domain measurements.",
        "Do not strengthen a conditional or disputed claim without a new source.",
    ]
    for claim in claims:
        statement = str(
            claim.get("effective_statement")
            or claim.get("supported_rewrite")
            or claim.get("statement")
            or ""
        ).strip()
        if not statement:
            continue
        paper_ids.extend(
            str(item)
            for item in claim.get("citation_paper_ids", [])
            if str(item) in paper_allow
        )
        candidate_chunks = list(claim.get("supporting_text_chunk_ids") or []) + list(
            claim.get("context_text_chunk_ids") or []
        )
        for item in candidate_chunks:
            chunk_id = str(item)
            if chunk_id not in chunk_allow:
                continue
            owner = chunk_to_paper.get(chunk_id)
            if owner and owner not in paper_allow:
                invalid_chunks.append(chunk_id)
                id_audit.append(
                    {
                        "id_type": "chunk_id",
                        "id": chunk_id,
                        "reason": "chunk_owner_not_in_section_allowlist",
                        "paper_id": owner,
                    }
                )
                continue
            chunk_ids.append(chunk_id)
        binding_status = str(
            claim.get("evidence_binding_status")
            or claim.get("status")
            or ""
        ).casefold().strip()
        permission_status = str(claim.get("permission_status") or "").casefold().strip()
        claim_state = str(claim.get("claim_state") or "").casefold().strip()
        section_fit = str(claim.get("section_fit") or "").casefold().strip()
        flags = {
            str(flag).casefold()
            for flag in (claim.get("critic_flags") or [])
            if str(flag).strip()
        }
        uncertain_flag = any(
            any(token in flag for token in (
                "partial", "qualified", "insufficient", "unsupported", "unverified",
                "permission", "contradict", "conflict", "scope", "missing",
            ))
            for flag in flags
        )
        boundary = (
            section_fit in {"boundary", "off_scope"}
            or binding_status in {"contradicted", "off_scope"}
            or any(any(token in flag for token in ("boundary", "conflict", "off_scope")) for flag in flags)
        )
        established_ok = (
            binding_status in {"direct", "synthesized", "bound"}
            and permission_status not in {"qualified_only", "contextual_or_qualified_support", "discovery_only"}
            and claim_state not in {"partially_grounded", "open_question", "contested", "uncertain"}
            and not uncertain_flag
            and not claim.get("missing_evidence_components")
            and not claim.get("supported_rewrite")
        )
        # Legacy standalone callers may not provide the new lifecycle fields.
        # Their explicit status and attached IDs remain a conservative fallback;
        # saturation alone is never sufficient.
        if not binding_status and not permission_status and not claim_state:
            established_ok = bool(
                claim.get("supporting_text_chunk_ids")
                and not flags
                and not claim.get("supported_rewrite")
                and float(claim.get("saturation_score") or 0.0) >= 1.5
            )
        category = "conflicts_or_boundaries" if boundary else (
            "established_points" if established_ok else "conditional_points"
        )
        category_assignments.append({
            "claim_id": str(claim.get("claim_id") or ""),
            "category": category,
            "effective_statement": statement,
            "original_statement": str(claim.get("original_statement") or claim.get("statement") or "").strip(),
            "evidence_binding_status": binding_status,
            "permission_status": permission_status,
            "claim_state": claim_state,
        })
        if category == "established_points":
            established.append(statement)
        elif category == "conflicts_or_boundaries":
            boundaries.append(statement)
        else:
            conditional.append(statement)
    # A blueprint can legitimately reach this layer before M2a claims exist.
    # In that case the section source/chunk allowlist is still a valid
    # material inventory and must not disappear from the bundle.
    if explicit_papers:
        paper_ids.extend(sorted(paper_allow))
    if explicit_chunks:
        chunk_ids.extend(sorted(chunk_allow))

    # Keep the writing payload compact.  The complete candidate inventory is
    # retained by the caller in a separate local index and can be retrieved by
    # ID when the writer discovers a real gap.  Both this bundle and the
    # section-authoring portfolio use the same selector; a bundle must not fall
    # back to chunk-ID order.
    all_chunk_ids = _unique(chunk_ids)
    role_by_chunk: dict[str, list[str]] = {}
    for role, ids in (section.get("chunk_ids_by_role") or {}).items():
        if isinstance(ids, list):
            for chunk_id in ids:
                role_by_chunk.setdefault(str(chunk_id), []).append(str(role))
    records_by_id: dict[str, Any] = {}
    for record in list(chunk_records or []):
        if isinstance(record, dict):
            chunk_id = str(record.get("chunk_id") or "")
        else:
            chunk_id = str(getattr(record, "chunk_id", "") or "")
        if chunk_id:
            records_by_id.setdefault(chunk_id, record)
    synthetic_records: list[dict[str, Any]] = []
    claim_owner_by_chunk: dict[str, str] = {}
    for claim in claims:
        claim_papers = _unique(claim.get("citation_paper_ids") or [])
        claim_chunks = _unique(
            list(claim.get("supporting_text_chunk_ids") or [])
            + list(claim.get("context_text_chunk_ids") or [])
        )
        if claim_papers:
            for chunk_id in claim_chunks:
                claim_owner_by_chunk.setdefault(chunk_id, claim_papers[0])
    for chunk_id in all_chunk_ids:
        if chunk_id in records_by_id:
            continue
        owner = chunk_to_paper.get(chunk_id, "") or claim_owner_by_chunk.get(chunk_id, "")
        supplied_permission = (chunk_permissions or {}).get(chunk_id)
        synthetic_records.append(
            {
                "chunk_id": chunk_id,
                "paper_id": owner,
                # With no canonical record available this is an ID-only
                # candidate reference, never verified direct evidence.  A
                # caller that supplies permissions/records takes the strict
                # production path.
                "use_permission": supplied_permission or (
                    "contextual_or_qualified_support"
                    if chunk_id in claim_owner_by_chunk
                    else "discovery_only"
                ),
                "scope_fit": "direct",
                "literature_roles": role_by_chunk.get(chunk_id, []),
                # The actual text is retrieved by the authoring tools from the
                # canonical KB.  This is an ID-only selection path, not fake
                # evidence text.
                "normalized_text": "",
            }
        )
    selector_records = list(records_by_id.values()) + synthetic_records
    # Phase 3 can hand us the exact section portfolio already selected for
    # M2a/authoring.  Reusing that object prevents a subtle split-brain bug in
    # which the bundle and the writer independently select different core
    # chunks from the same inventory.  Standalone callers retain the previous
    # deterministic selection path.
    portfolio: EvidencePortfolio = preselected_portfolio or select_evidence_portfolio(
        section=section,
        candidates=selector_records,
        claims=claims,
        relation_edges=[edge for edge in relation_edge_input if isinstance(edge, dict)],
        allowed_paper_ids=paper_allow,
        allowed_chunk_ids=all_chunk_ids,
        max_core_chunks=max_core_chunks,
        max_core_chunks_per_paper=2,
        allow_textless_core=not bool(records_by_id),
    )
    if (
        not records_by_id
        and source_permissions is None
        and chunk_permissions is None
        and portfolio.core_chunk_ids
    ):
        portfolio.material_status = "inventory_only"
        portfolio.readiness_status = "needs_more_literature"
        portfolio.status = "needs_more_literature"
        portfolio.reasons.append("permission_metadata_missing_for_id_only_claim_references")
    core_chunk_ids = portfolio.core_chunk_ids
    candidate_chunk_ids = portfolio.candidate_chunk_ids
    chunk_ids = core_chunk_ids

    all_paper_ids = _unique(paper_ids + sorted(paper_allow))
    core_paper_ids = _unique(portfolio.core_paper_ids)
    core_paper_ids = [item for item in core_paper_ids if item in all_paper_ids]
    candidate_paper_ids = [item for item in all_paper_ids if item not in core_paper_ids]
    paper_ids = core_paper_ids
    edges = []
    edge_types = []
    for edge in relation_edge_input:
        if isinstance(edge, dict):
            source_id = str(edge.get("source_paper_id") or "")
            target_id = str(edge.get("target_paper_id") or "")
            basis_ids = _unique(edge.get("relation_basis_chunk_ids") or [])
            if source_id not in paper_allow or target_id not in paper_allow:
                continue
            if any(item not in chunk_allow for item in basis_ids):
                id_audit.append(
                    {
                        "id_type": "relation_edge",
                        "id": f"{source_id}->{target_id}",
                        "reason": "relation_basis_outside_section_allowlist",
                        "basis_chunk_ids": basis_ids,
                    }
                )
                continue
            edge_types.append(str(edge.get("edge_type") or ""))
            edges.append(
                {
                    "source_paper_id": source_id,
                    "target_paper_id": target_id,
                    "observed_relation": edge.get("observed_relation") or edge.get("edge_type", ""),
                    "semantic_relation": edge.get("semantic_relation", ""),
                    "status": edge.get("status", "observed"),
                    "relation_basis_chunk_ids": basis_ids,
                    "confidence": edge.get("confidence", 0.0),
                }
            )
        else:
            kind = str(getattr(edge, "edge_type", ""))
            source_id = str(getattr(edge, "source_paper_id", "") or "")
            target_id = str(getattr(edge, "target_paper_id", "") or "")
            basis_ids = _unique(getattr(edge, "relation_basis_chunk_ids", []) or [])
            if source_id not in paper_allow or target_id not in paper_allow:
                continue
            if any(item not in chunk_allow for item in basis_ids):
                id_audit.append(
                    {
                        "id_type": "relation_edge",
                        "id": f"{source_id}->{target_id}",
                        "reason": "relation_basis_outside_section_allowlist",
                        "basis_chunk_ids": basis_ids,
                    }
                )
                continue
            edge_types.append(kind)
            edges.append(
                {
                    "source_paper_id": source_id,
                    "target_paper_id": target_id,
                    "observed_relation": getattr(edge, "observed_relation", "") or kind,
                    "semantic_relation": getattr(edge, "semantic_relation", ""),
                    "status": getattr(edge, "status", "observed"),
                    "relation_basis_chunk_ids": basis_ids,
                    "confidence": getattr(edge, "confidence", 0.0),
                }
            )
    if established:
        synthesis_space.append("Compare the established points and explain the common mechanism or progression.")
    if conditional:
        synthesis_space.append("State which conditions separate the supported cases from the unresolved cases.")
    if boundaries:
        synthesis_space.append("Explain whether each boundary is caused by physics, method, measurement, or scope.")
    if not boundaries:
        if portfolio.status == "material_ready":
            boundaries.append(
                "Keep every conclusion within the recorded scope, content depth, and source permission."
            )
        else:
            boundaries.append(
                "No claim-level boundary is established because the current portfolio lacks a validated claim or usable semantic relation."
            )
    if not synthesis_space:
        if portfolio.status == "material_ready":
            synthesis_space.append(f"Explain how the selected sources jointly address: {task}")
        else:
            synthesis_space.append(
                f"Do not draft the section yet; first obtain material that can support: {task}"
            )
    permissions = source_permissions or {}
    chunk_permissions = chunk_permissions or {}
    permission_counts: dict[str, int] = {}
    permission_audit: list[dict[str, Any]] = []
    for paper_id in _unique(paper_ids):
        value = permissions.get(paper_id, "")
        if not value:
            permission_audit.append(
                {
                    "id_type": "paper_id",
                    "id": paper_id,
                    "reason": "permission_not_provided",
                }
            )
            value = "unknown"
        key = str(value or "unknown")
        permission_counts[key] = permission_counts.get(key, 0) + 1
    for chunk_id in _unique(core_chunk_ids):
        value = chunk_permissions.get(chunk_id)
        if value:
            key = str(value)
            permission_counts[key] = permission_counts.get(key, 0) + 1
        elif chunk_id not in chunk_to_paper:
            permission_audit.append(
                {
                    "id_type": "chunk_id",
                    "id": chunk_id,
                    "reason": "chunk_permission_not_provided",
                }
            )
    digest = hashlib.sha1(f"{section_id}|{task}".encode("utf-8")).hexdigest()[:12]
    return SynthesisBundle(
        bundle_id=f"bundle:{section_id}:{digest}",
        section_id=section_id,
        argument_task=task,
        relationship_pattern=_pattern(edge_types),
        paper_ids=_unique(paper_ids),
        chunk_ids=_unique(chunk_ids),
        established_points=_unique(established),
        conditional_points=_unique(conditional),
        conflicts_or_boundaries=_unique(boundaries),
        claim_category_assignments=category_assignments,
        argument_task_coverage=[
            dict(item) for item in argument_task_coverage if isinstance(item, dict)
        ],
        paper_content_depth_summary={
            str(key): str(value)
            for key, value in (paper_content_depth_summary or {}).items()
            if str(key).strip()
        },
        author_synthesis_space=_unique(synthesis_space),
        forbidden_overclaims=forbidden,
        relation_evidence=edges,
        source_permission_summary=permission_counts,
        candidate_pool_ref=f"section_candidate_pool:{section_id}",
        candidate_pool_count=len(candidate_chunk_ids),
        candidate_paper_count=len(candidate_paper_ids),
        candidate_chunk_ids=candidate_chunk_ids,
        candidate_paper_ids=candidate_paper_ids,
        invalid_paper_ids=sorted(set(invalid_papers)),
        invalid_chunk_ids=sorted(set(invalid_chunks)),
        id_audit=id_audit,
        permission_audit=permission_audit,
        allowlist_source=(
            "explicit_section_allowlist"
            if explicit_papers or explicit_chunks
            else "claim_reference_allowlist"
        ),
        selector_version=portfolio.selector_version,
        material_status=portfolio.material_status,
        readiness_status=portfolio.readiness_status,
        status=portfolio.status,
        selection_reasons=portfolio.reasons,
        selection_diagnostics=portfolio.diagnostics,
        real_claim_count=portfolio.real_claim_count,
        usable_relation_count=portfolio.usable_relation_count,
    )


def build_bundles_for_blueprint(
    blueprint: dict[str, Any],
    *,
    relation_edges: Iterable[Any] = (),
    section_allowlists: dict[str, dict[str, list[str]]] | None = None,
    source_permissions_by_paper: dict[str, str] | None = None,
    chunk_permissions: dict[str, str] | None = None,
    chunk_to_paper: dict[str, str] | None = None,
    chunk_records_by_section: dict[str, Iterable[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Create one compact bundle per section without calling an LLM."""

    edges = list(relation_edges)
    bundles = []
    for section in blueprint.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id") or "")
        claims = [
            item for item in section.get("claims") or [] if isinstance(item, dict)
        ]
        allow = (section_allowlists or {}).get(section_id, {})
        bundles.append(
            build_synthesis_bundle(
                section=section,
                claims=claims,
                relation_edges=edges,
                source_permissions=source_permissions_by_paper,
                chunk_permissions=chunk_permissions,
                allowed_paper_ids=allow.get("paper_ids") if allow else None,
                allowed_chunk_ids=allow.get("chunk_ids") if allow else None,
                chunk_to_paper=chunk_to_paper,
                chunk_records=(chunk_records_by_section or {}).get(section_id),
            ).to_dict()
        )
    return bundles
