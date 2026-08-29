"""Shared, deterministic selection of section evidence portfolios.

The selector is intentionally independent of the writer and of any network
service.  Both synthesis bundles and section-authoring tools use this module so
that they cannot silently disagree about which papers are the core material.
Candidate inventories remain available for later retrieval; only the bounded
core portfolio is sent to a writing model by default.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .argument_quality_policy import evidence_ceiling


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS = frozenset(
    {
        "about", "after", "again", "also", "among", "and", "are", "because",
        "been", "before", "being", "between", "both", "but", "can", "chapter",
        "claim", "could", "does", "each", "from", "have", "into", "its", "more",
        "most", "must", "not", "only", "other", "our", "paper", "review", "section",
        "should", "show", "such", "than", "that", "their", "these", "this", "through",
        "under", "using", "was", "were", "what", "when", "where", "which", "while",
        "with", "would",
    }
)

_SCOPE_SCORE = {
    "direct": 38.0,
    "adjacent": 20.0,
    "contextual": 12.0,
    "unreviewed": 2.0,
    "out_of_scope": -1000.0,
}
_PERMISSION_SCORE = {
    "factual_support": 26.0,
    "contextual_or_qualified_support": 15.0,
    "background_and_candidate_only": 7.0,
    "discovery_only": -1000.0,
    "unknown": -1000.0,
}


def _tokens(value: Any) -> set[str]:
    return {
        item.casefold()
        for item in _TOKEN_RE.findall(str(value or ""))
        if item.casefold() not in _STOPWORDS
    }


def _unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _as_record(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        raw = dict(item)
    else:
        raw = {
            key: getattr(item, key, "")
            for key in (
                "chunk_id", "paper_id", "paper_title", "paper_year", "normalized_text",
                "text", "scope_fit", "use_permission", "content_depth", "context_complete",
                "evidence_level", "source_kind", "literature_role", "relation_roles",
                "not_usable_for",
                "retrieval_role", "provenance", "route_provenance", "allowed_claim_kinds",
            )
        }
    roles = raw.get("literature_roles") or raw.get("roles") or raw.get("literature_role") or []
    if isinstance(roles, str):
        roles = [part.strip() for part in roles.split(",") if part.strip()]
    else:
        roles = [str(part).strip() for part in roles if str(part).strip()]
    text = str(raw.get("normalized_text") or raw.get("text") or "").strip()
    return {
        **raw,
        "chunk_id": str(raw.get("chunk_id") or "").strip(),
        "paper_id": str(raw.get("paper_id") or "").strip(),
        "paper_title": str(raw.get("paper_title") or raw.get("title") or "").strip(),
        "normalized_text": text,
        "literature_roles": _unique(roles),
        "scope_fit": str(raw.get("scope_fit") or "unreviewed").casefold(),
        "use_permission": str(raw.get("use_permission") or "discovery_only").casefold(),
        "content_depth": str(raw.get("content_depth") or raw.get("evidence_level") or "metadata").casefold(),
        "context_complete": bool(raw.get("context_complete", False)),
        "retrieval_role": str(raw.get("retrieval_role") or "").casefold(),
        "provenance": raw.get("provenance") or raw.get("route_provenance") or {},
        "allowed_claim_kinds": list(raw.get("allowed_claim_kinds") or [])
        if not isinstance(raw.get("allowed_claim_kinds"), str)
        else [str(raw.get("allowed_claim_kinds"))],
    }


def _claim_text(claim: dict[str, Any]) -> str:
    return str(claim.get("statement") or claim.get("claim") or "").strip()


def _is_real_statement(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    if len(text) < 20:
        return False
    return not any(
        marker in text
        for marker in (
            "formulate the supported points",
            "material inventory is available",
            "additional candidates remain available",
            "no claim-level",
        )
    )


@dataclass(slots=True)
class EvidencePortfolio:
    schema_version: str
    selector_version: str
    section_id: str
    core_chunk_ids: list[str] = field(default_factory=list)
    candidate_chunk_ids: list[str] = field(default_factory=list)
    core_paper_ids: list[str] = field(default_factory=list)
    candidate_paper_ids: list[str] = field(default_factory=list)
    core_chunk_ids_by_paper: dict[str, list[str]] = field(default_factory=dict)
    candidate_chunk_ids_by_paper: dict[str, list[str]] = field(default_factory=dict)
    core_roles: list[str] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)
    paper_core_counts: dict[str, int] = field(default_factory=dict)
    eligible_chunk_count: int = 0
    inventory_chunk_count: int = 0
    real_claim_count: int = 0
    usable_relation_count: int = 0
    material_status: str = "inventory_only"
    readiness_status: str = "needs_more_literature"
    status: str = "needs_more_literature"
    reasons: list[str] = field(default_factory=list)
    query_terms: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _required_roles(section: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for key in ("required_roles", "literature_roles", "required_literature_roles"):
        raw = section.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        values.extend(str(item).strip() for item in raw if str(item).strip())
    if isinstance(section.get("role_source_targets"), dict):
        for role, target in section["role_source_targets"].items():
            try:
                active = int(target or 0) > 0
            except (TypeError, ValueError):
                active = False
            if str(role).strip() and active:
                values.append(str(role).strip())
    # Do not invent universal roles.  If the section does not declare roles,
    # use roles actually present only as optional coverage diagnostics.
    if not values:
        values = [role for record in records for role in record["literature_roles"]]
    return _unique(values)


def select_evidence_portfolio(
    *,
    section: dict[str, Any],
    candidates: Iterable[Any],
    claims: Iterable[dict[str, Any]] = (),
    relation_edges: Iterable[dict[str, Any]] = (),
    allowed_paper_ids: Iterable[str] | None = None,
    allowed_chunk_ids: Iterable[str] | None = None,
    max_core_chunks: int = 12,
    max_core_chunks_per_paper: int = 2,
    allow_textless_core: bool = False,
) -> EvidencePortfolio:
    """Select a diverse core portfolio and retain the rest as candidates.

    Selection is role-aware and task-aware.  It first seeks one representative
    from distinct papers and uncovered roles, then adds a second chunk from a
    paper only when the core budget still requires it.  Chunk identifiers are
    never used as the primary ordering signal.
    """

    records = [_as_record(item) for item in candidates]
    records = [item for item in records if item["chunk_id"] and item["paper_id"]]
    allowed_chunks = set(_unique(allowed_chunk_ids)) if allowed_chunk_ids is not None else None
    allowed_papers = set(_unique(allowed_paper_ids)) if allowed_paper_ids is not None else None
    if allowed_chunks is not None:
        records = [item for item in records if item["chunk_id"] in allowed_chunks]
    if allowed_papers is not None:
        records = [item for item in records if item["paper_id"] in allowed_papers]
    dedup: dict[str, dict[str, Any]] = {}
    for item in records:
        dedup.setdefault(item["chunk_id"], item)
    records = list(dedup.values())

    claims_list = [item for item in claims if isinstance(item, dict)]
    edges_list = [item for item in relation_edges if isinstance(item, dict)]
    task_text = " ".join(
        [
            str(section.get("section_id") or ""),
            str(section.get("title") or ""),
            str(section.get("section_title") or ""),
            str(section.get("chapter_argument") or ""),
            str(section.get("argument_role") or ""),
            str(section.get("synthesis_task") or ""),
            str(section.get("section_contract") or ""),
            *[_claim_text(item) for item in claims_list],
        ]
    )
    query_terms = _tokens(task_text)
    required_roles = _required_roles(section, records)
    role_targets = section.get("role_source_targets") if isinstance(section.get("role_source_targets"), dict) else {}
    claim_chunk_ids = {
        str(chunk).strip()
        for claim in claims_list
        for chunk in list(claim.get("supporting_text_chunk_ids") or []) + list(claim.get("context_text_chunk_ids") or [])
        if str(chunk).strip()
    }
    claim_paper_ids = {
        str(paper).strip()
        for claim in claims_list
        for paper in claim.get("citation_paper_ids") or []
        if str(paper).strip()
    }
    relation_basis_ids = {
        str(chunk).strip()
        for edge in edges_list
        for chunk in edge.get("relation_basis_chunk_ids") or []
        if str(chunk).strip()
    }
    relation_papers = {
        str(paper).strip()
        for edge in edges_list
        for paper in (edge.get("source_paper_id"), edge.get("target_paper_id"))
        if str(paper or "").strip()
    }

    scored: list[tuple[float, str, dict[str, Any], set[str]]] = []
    for record in records:
        permission = record["use_permission"]
        scope = record["scope_fit"]
        has_text = bool(record["normalized_text"])
        if scope == "out_of_scope":
            continue
        # Discovery-only records stay in the inventory/candidate pool but can
        # never be selected into the core writing portfolio.
        ceiling, _ = evidence_ceiling(record)
        if ceiling == "discovery_only" or permission in {"discovery_only", "unknown", ""} or (not has_text and not allow_textless_core):
            continue
        matched = query_terms & _tokens(
            f"{record['paper_title']} {record['normalized_text'][:6000]}"
        )
        roles = set(record["literature_roles"])
        role_bonus = sum(10.0 for role in roles if role in required_roles)
        score = (
            _SCOPE_SCORE.get(scope, 0.0)
            + _PERMISSION_SCORE.get(permission, 0.0)
            + min(34.0, 4.0 * len(matched))
            + role_bonus
            + (8.0 if record["chunk_id"] in claim_chunk_ids else 0.0)
            + (7.0 if record["chunk_id"] in relation_basis_ids else 0.0)
            + (5.0 if record["paper_id"] in claim_paper_ids else 0.0)
            + (4.0 if record["paper_id"] in relation_papers else 0.0)
            + (5.0 if record["content_depth"] in {"fulltext", "structured_snippet"} else 0.0)
            + min(6.0, len(record["normalized_text"]) / 1000.0)
        )
        tie = hashlib.sha1(record["chunk_id"].encode("utf-8")).hexdigest()
        scored.append((score, tie, record, roles))
    scored.sort(key=lambda item: (-item[0], item[1]))

    by_id = {item[2]["chunk_id"]: item for item in scored}
    eligible_ids = [item[2]["chunk_id"] for item in scored]
    core_limit = max(0, int(max_core_chunks))
    per_paper_limit = max(1, int(max_core_chunks_per_paper))
    # If there are very few available papers, permit a third chunk from a
    # paper rather than pretending the section has broader source diversity.
    paper_count = len({item[2]["paper_id"] for item in scored})
    if paper_count <= 3:
        per_paper_limit = max(per_paper_limit, min(3, core_limit or 1))

    selected: list[str] = []
    counts: dict[str, int] = {}
    covered_roles: set[str] = set()

    def choose(pool: list[tuple[float, str, dict[str, Any], set[str]]], *, require_new_paper: bool = False) -> bool:
        best = None
        best_key = None
        for score, tie, record, roles in pool:
            cid = record["chunk_id"]
            pid = record["paper_id"]
            if cid in selected or counts.get(pid, 0) >= per_paper_limit:
                continue
            if require_new_paper and pid in counts:
                continue
            novelty = len(roles - covered_roles)
            missing_required = len((roles & set(required_roles)) - covered_roles)
            key = (
                1 if missing_required else 0,
                1 if novelty else 0,
                1 if pid not in counts else 0,
                score,
                tie,
            )
            if best_key is None or key > best_key:
                best_key, best = key, record
        if best is None:
            return False
        cid, pid = best["chunk_id"], best["paper_id"]
        selected.append(cid)
        counts[pid] = counts.get(pid, 0) + 1
        covered_roles.update(best["literature_roles"])
        return True

    # First satisfy declared roles with distinct papers when possible.
    for role in required_roles:
        if len(selected) >= core_limit:
            break
        role_pool = [item for item in scored if role in item[3]]
        if not choose(role_pool, require_new_paper=True):
            choose(role_pool)
    # Then maximize paper and role diversity, followed by relevance score.
    while len(selected) < core_limit and choose(scored, require_new_paper=True):
        pass
    while len(selected) < core_limit and choose(scored):
        pass

    # The candidate pool includes every non-core inventory item in relevance
    # order, including discovery-only items (with their permissions retained in
    # the KB).  It is never sent wholesale to the writing model.
    core_set = set(selected)
    candidate_ids = [item[2]["chunk_id"] for item in scored if item[2]["chunk_id"] not in core_set]
    inventory_ids = [item["chunk_id"] for item in records if item["chunk_id"] not in core_set]
    for item in inventory_ids:
        if item not in candidate_ids:
            candidate_ids.append(item)
    all_paper_ids = _unique(item["paper_id"] for item in records)
    core_papers = _unique(by_id[cid][2]["paper_id"] for cid in selected if cid in by_id)
    candidate_papers = [item for item in all_paper_ids if item not in set(core_papers)]
    core_by_paper: dict[str, list[str]] = {}
    for cid in selected:
        pid = by_id[cid][2]["paper_id"]
        core_by_paper.setdefault(pid, []).append(cid)
    candidate_by_paper: dict[str, list[str]] = {}
    for cid in candidate_ids:
        record = next((item for item in records if item["chunk_id"] == cid), None)
        if record:
            candidate_by_paper.setdefault(record["paper_id"], []).append(cid)

    real_claim_count = sum(1 for claim in claims_list if _is_real_statement(_claim_text(claim)))
    usable_relation_count = sum(
        1
        for edge in edges_list
        if str(edge.get("semantic_relation") or "").strip()
        and str(edge.get("status") or "observed").casefold() in {"inferred", "human_confirmed", "reviewed"}
        and edge.get("relation_basis_chunk_ids")
        and str(edge.get("source_paper_id") or "") in set(all_paper_ids)
        and str(edge.get("target_paper_id") or "") in set(all_paper_ids)
    )
    missing_roles = [role for role in required_roles if role not in covered_roles]
    if not real_claim_count and not usable_relation_count:
        material_status = "inventory_only"
        status = "needs_more_literature"
        reasons = ["no_real_claims_or_usable_semantic_relations"]
    elif not selected:
        material_status = "inventory_only"
        status = "needs_more_literature"
        reasons = ["no_writable_core_chunks"]
    else:
        material_status = "material_ready"
        status = "needs_more_literature" if missing_roles else "material_ready"
        reasons = [f"missing_required_roles:{role}" for role in missing_roles]
    readiness_status = "ready_for_authoring" if status == "material_ready" else "needs_more_literature"
    dominating_ratio = (max(counts.values()) / len(selected)) if selected and counts else 0.0
    return EvidencePortfolio(
        schema_version="research_harness.evidence_portfolio.v1",
        selector_version="r3_3_diverse_role_task_selector.v1",
        section_id=str(section.get("section_id") or "section"),
        core_chunk_ids=selected,
        candidate_chunk_ids=candidate_ids,
        core_paper_ids=core_papers,
        candidate_paper_ids=candidate_papers,
        core_chunk_ids_by_paper=core_by_paper,
        candidate_chunk_ids_by_paper=candidate_by_paper,
        core_roles=sorted(covered_roles),
        missing_roles=missing_roles,
        paper_core_counts=dict(sorted(counts.items())),
        eligible_chunk_count=len(eligible_ids),
        inventory_chunk_count=len(records),
        real_claim_count=real_claim_count,
        usable_relation_count=usable_relation_count,
        material_status=material_status,
        readiness_status=readiness_status,
        status=status,
        reasons=reasons,
        query_terms=sorted(query_terms)[:40],
        diagnostics={
            "distinct_core_papers": len(core_papers),
            "dominating_paper_ratio": round(dominating_ratio, 4),
            "max_core_chunks_per_paper": per_paper_limit,
            "role_targets": {str(k): int(v or 0) for k, v in role_targets.items()},
            "core_selection_is_not_chunk_id_sorted": True,
        },
    )
