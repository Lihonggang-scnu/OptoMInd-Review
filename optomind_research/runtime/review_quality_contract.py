"""Shared quality contracts for review-scale planning and literature use.

This module is deliberately deterministic.  It does not decide whether a
scientific statement is true; it makes the requested review scale, source
permissions, and discovery stopping rules explicit so that the LLM stages do
not silently use different standards.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence


# These vocabularies are shared by discovery, migration, the canonical asset
# graph, and authoring.  Keep the serialized values as plain strings so older
# JSON/Pydantic artifacts remain readable.
CANONICAL_SCOPE_FITS = frozenset(
    {"direct", "adjacent", "contextual", "out_of_scope", "unreviewed"}
)
CANONICAL_CONTENT_DEPTHS = frozenset(
    {
        "metadata",
        "abstract",
        "abstract_claim",
        "tldr",
        "structured_snippet",
        "partial_fulltext",
        "fulltext",
    }
)
CANONICAL_USE_PERMISSIONS = frozenset(
    {
        "discovery_only",
        "background_and_candidate_only",
        "contextual_or_qualified_support",
        "factual_support",
    }
)
CANONICAL_OBSERVED_RELATIONS = frozenset(
    {
        "cites",
        "cited_by",
        "snippet_ref_mention",
        "s2_recommended",
        "semantic_recommendation",
        "co_cited_with",
        "bibliographic_coupling",
    }
)
CANONICAL_SEMANTIC_RELATIONS = frozenset(
    {
        "foundation_of",
        "extends",
        "complements",
        "contradicts",
        "compares_with",
        "uses_method_from",
        "sets_boundary_for",
        "translates_to_application",
        "progression",
        "complementarity",
        "controversy",
        "tradeoff",
        "boundary",
    }
)
CANONICAL_RELATION_STATUSES = frozenset(
    {
        "observed", "inferred", "reviewed", "human_confirmed", "disputed",
        # Legacy semantic edges are never silently retained when their
        # endpoints or basis cannot be revalidated.  They remain auditable in
        # the graph with this status, but downstream coverage/authoring code
        # must treat them as non-semantic evidence.
        "discovery_lead", "unverified_legacy",
    }
)


ADAPTIVE_COVERAGE_OUTCOMES = frozenset(
    {
        "material_ready",
        "material_ready_with_limits",
        "merge_required",
        "needs_more_literature",
    }
)
ADAPTIVE_COVERAGE_CONTRACT_VERSION = (
    "review_harness.adaptive_coverage_contract.v1"
)

# Keep this local to avoid importing the coverage tool module (which would
# create a runtime-contract import cycle).  The values intentionally match
# coverage_decision_contract.COVERAGE_ROLES.
ADAPTIVE_ROLE_ORDER = (
    "foundation",
    "mechanism",
    "method",
    "frontier",
    "controversy",
    "application",
)

_ADAPTIVE_RISKS = frozenset({"low", "moderate", "high", "critical"})
_SECTION_ROLE_ALIASES = {
    "intro": "introduction",
    "background": "introduction",
    "opening": "introduction",
    "future": "outlook",
    "future_directions": "outlook",
    "perspective": "outlook",
    "conclusion": "outlook",
    "closing": "outlook",
}


def _adaptive_text(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[: max(0, int(limit))]


def _adaptive_values(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(
        _adaptive_text(item, 120)
        for item in value
        if _adaptive_text(item, 120)
    ))


def _adaptive_section_role(section: Mapping[str, Any]) -> str:
    for key in ("section_role", "role", "section_type", "narrative_role"):
        value = _adaptive_text(section.get(key), 80).casefold().replace("-", "_").replace(" ", "_")
        if value:
            return _SECTION_ROLE_ALIASES.get(value, value)
    text = " ".join(
        _adaptive_text(section.get(key), 120).casefold()
        for key in ("title", "section_title", "chapter_argument", "synthesis_task")
    )
    if re.search(r"\b(introduction|intro|background|scope)\b", text):
        return "introduction"
    if re.search(r"\b(outlook|future|perspective|conclusion|closing)\b", text):
        return "outlook"
    if re.search(r"\b(method|fabrication|measurement|characterization)\b", text):
        return "method"
    if re.search(r"\b(mechanism|physics|principle|theory)\b", text):
        return "mechanism"
    if re.search(r"\b(application|device|deployment|system)\b", text):
        return "application"
    return "general"


def _adaptive_target_words(section: Mapping[str, Any]) -> int:
    for key in ("target_word_count", "planned_word_count", "estimated_word_budget"):
        value = section.get(key)
        if isinstance(value, Mapping):
            value = value.get("min") or value.get("target") or value.get("max")
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    value = section.get("target_word_range")
    if isinstance(value, Mapping):
        value = value.get("min") or value.get("target") or value.get("max")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        value = value[0] if value else 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 900


def _adaptive_claims(section: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for key in (
        "load_bearing_claims",
        "load_bearing_claim_seeds",
        "claim_seeds",
        "key_claims",
        "claims",
    ):
        raw = section.get(key) or []
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            if isinstance(item, Mapping):
                if item.get("load_bearing") is False or item.get("is_load_bearing") is False:
                    continue
                claims.append(dict(item))
            elif str(item or "").strip():
                claims.append({"statement": _adaptive_text(item, 300)})
        if claims:
            break
    return claims


def _adaptive_claim_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, Mapping):
        value = list(value.keys())
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(
        _adaptive_text(item, 120)
        for item in value
        if _adaptive_text(item, 120)
    ))


def _adaptive_source_roles(source: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("literature_roles", "roles", "role_fit"):
        values.extend(_adaptive_values(source.get(key)))
    values.extend(_adaptive_values(source.get("literature_role")))
    return list(dict.fromkeys(value.casefold() for value in values if value))


def _adaptive_source_permission(source: Mapping[str, Any]) -> str:
    explicit = _adaptive_text(source.get("use_permission"), 100).casefold()
    depth = normalize_content_depth(source.get("content_depth"), default="metadata")
    scope = normalize_scope_fit(source.get("scope_fit"))
    if explicit:
        return normalize_use_permission(explicit, default="discovery_only")
    return str(permission_for_content(
        depth,
        scope_fit=scope,
        context_complete=bool(source.get("context_complete", True)),
    )["use_permission"])


def _adaptive_source_claim_ids(source: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in (
        "supported_claim_ids",
        "load_bearing_claim_ids",
        "claim_ids",
        "supports_claims",
    ):
        ids.extend(_adaptive_claim_ids(source.get(key)))
    return list(dict.fromkeys(ids))


@dataclass(slots=True)
class AdaptiveCoverageContract:
    """Section-specific evidence obligations.

    The contract deliberately separates core writing readiness from optional
    article-scale breadth.  Required roles are load-bearing for this section;
    optional roles and a legacy article-wide reference target become limits or
    merge instructions instead of universal blockers.
    """

    version: str = ADAPTIVE_COVERAGE_CONTRACT_VERSION
    section_role: str = "general"
    risk: str = "moderate"
    target_words: int = 900
    load_bearing_claim_count: int = 0
    required_roles: list[str] = field(default_factory=list)
    optional_roles: list[str] = field(default_factory=list)
    minimum_unique_sources: int = 2
    minimum_direct_sources: int = 1
    minimum_distinct_papers: int = 2
    minimum_distinct_venues: int = 1
    visual_asset_required: bool = False
    merge_if_under_supported: bool = False
    basis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AdaptiveCoverageReadiness:
    """Deterministic readiness result with an explicit editorial outcome."""

    outcome: str
    contract: AdaptiveCoverageContract
    unique_sources: int = 0
    direct_sources: int = 0
    # ``scoped_direct_sources`` counts direct-scope rows before permission
    # filtering; ``factual_direct_sources`` counts only direct rows that are
    # allowed to support facts.  Keep ``direct_sources`` as the historical
    # factual alias for downstream compatibility.
    scoped_direct_sources: int = 0
    factual_direct_sources: int = 0
    distinct_papers: int = 0
    distinct_venues: int = 0
    covered_required_roles: list[str] = field(default_factory=list)
    missing_required_roles: list[str] = field(default_factory=list)
    missing_optional_roles: list[str] = field(default_factory=list)
    supported_load_bearing_claims: int = 0
    unsupported_load_bearing_claims: list[str] = field(default_factory=list)
    factual_permission_sources: int = 0
    permission_failures: list[str] = field(default_factory=list)
    visual_asset_ready: bool = True
    limitations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def scientific_coverage_ready(self) -> bool:
        return self.outcome in {
            "material_ready",
            "material_ready_with_limits",
            "merge_required",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "contract": self.contract.to_dict(),
            "unique_sources": self.unique_sources,
            "direct_sources": self.direct_sources,
            "scoped_direct_sources": self.scoped_direct_sources,
            "factual_direct_sources": self.factual_direct_sources,
            "distinct_papers": self.distinct_papers,
            "distinct_venues": self.distinct_venues,
            "covered_required_roles": list(self.covered_required_roles),
            "missing_required_roles": list(self.missing_required_roles),
            "missing_optional_roles": list(self.missing_optional_roles),
            "supported_load_bearing_claims": self.supported_load_bearing_claims,
            "unsupported_load_bearing_claims": list(self.unsupported_load_bearing_claims),
            "factual_permission_sources": self.factual_permission_sources,
            "permission_failures": list(self.permission_failures),
            "visual_asset_ready": self.visual_asset_ready,
            "limitations": list(self.limitations),
            "reasons": list(self.reasons),
            "scientific_coverage_ready": self.scientific_coverage_ready,
        }


def build_adaptive_coverage_contract(
    section: Mapping[str, Any] | None = None,
    *,
    section_count: int | None = None,
) -> AdaptiveCoverageContract:
    """Build section obligations from role, length, claims, diversity and risk.

    This is intentionally not a scaled-down copy of an article reference
    count.  A short introduction/outlook has fewer role obligations, while a
    long, high-risk mechanism section gains source and direct-permission
    requirements.  The optional ``literature_coverage_target`` remains an
    article planning signal and is reported separately by callers.
    """

    section = section if isinstance(section, Mapping) else {}
    role = _adaptive_section_role(section)
    target_words = _adaptive_target_words(section)
    claims = _adaptive_claims(section)
    claim_count = len(claims)
    raw_risk = _adaptive_text(
        section.get("risk_level")
        or section.get("claim_risk")
        or section.get("evidence_risk"),
        40,
    ).casefold()
    risk = raw_risk if raw_risk in _ADAPTIVE_RISKS else "moderate"
    required = [
        value.casefold()
        for value in _adaptive_values(section.get("required_roles"))
        if value.casefold() in ADAPTIVE_ROLE_ORDER
    ]
    optional = [
        value.casefold()
        for value in _adaptive_values(section.get("optional_roles"))
        if value.casefold() in ADAPTIVE_ROLE_ORDER and value.casefold() not in required
    ]
    explicit_load_bearing = [
        value.casefold()
        for value in _adaptive_values(
            section.get("load_bearing_roles")
            or section.get("load_bearing_literature_roles")
        )
        if value.casefold() in ADAPTIVE_ROLE_ORDER
    ]
    if explicit_load_bearing:
        required = list(dict.fromkeys(explicit_load_bearing))
    elif role == "introduction":
        declared = set(required)
        required = ["foundation"] if "foundation" in declared or not declared else [
            "mechanism"
        ]
        if section.get("mechanism_load_bearing") or section.get("requires_mechanism"):
            required.append("mechanism")
    elif role == "outlook":
        declared = set(required)
        required = ["frontier"] if "frontier" in declared or not declared else [
            "application"
        ]
        if section.get("application_load_bearing") or section.get("requires_application"):
            required.append("application")
    if not required and role in ADAPTIVE_ROLE_ORDER and claim_count:
        required = [role]
    required = list(dict.fromkeys(required))
    default_optional = [item for item in ADAPTIVE_ROLE_ORDER if item not in required]
    optional = list(dict.fromkeys([*optional, *default_optional]))

    if target_words <= 700:
        length_base = 1
    elif target_words <= 1400:
        length_base = 2
    elif target_words <= 2400:
        length_base = 3
    elif target_words <= 4000:
        length_base = 4
    else:
        length_base = 5
    unique = length_base
    if claim_count >= 3:
        unique += 1
    if len(required) >= 3:
        unique += 1
    if risk in {"high", "critical"}:
        unique += 1
    if role in {"introduction", "outlook"}:
        unique = max(1, unique - 1)
    unique = min(10, max(1, unique))
    direct = max(1 if (claim_count or required) else 0, math.ceil(unique * 0.45))
    if claim_count >= 3:
        direct = max(direct, math.ceil(claim_count * 0.5))
    if risk in {"high", "critical"}:
        direct = max(direct, min(unique, 2))
    direct = min(unique, direct)
    distinct_papers = min(unique, max(1, 2 if unique <= 3 else 3))
    distinct_venues = 2 if unique >= 4 or risk in {"high", "critical"} else 1
    visual_required = bool(
        section.get("visual_asset_required")
        or section.get("requires_visual_asset")
        or str(section.get("visual_evidence_mode") or "").casefold() == "visual_first"
    )
    merge_signal = bool(
        section.get("merge_if_under_supported")
        or section.get("merge_required_if_shortfall")
        or section.get("merge_with_section")
        or section.get("merge_target_section")
    )
    basis = [
        f"section_role={role}",
        f"target_words={target_words}",
        f"load_bearing_claims={claim_count}",
        f"risk={risk}",
        f"required_roles={','.join(required) or 'none'}",
        f"source_diversity={distinct_papers} papers/{distinct_venues} venues",
    ]
    if section_count:
        basis.append(f"article_sections={int(section_count)}")
    return AdaptiveCoverageContract(
        section_role=role,
        risk=risk,
        target_words=target_words,
        load_bearing_claim_count=claim_count,
        required_roles=required,
        optional_roles=optional,
        minimum_unique_sources=unique,
        minimum_direct_sources=direct,
        minimum_distinct_papers=distinct_papers,
        minimum_distinct_venues=distinct_venues,
        visual_asset_required=visual_required,
        merge_if_under_supported=merge_signal,
        basis=basis,
    )


def evaluate_adaptive_coverage(
    section: Mapping[str, Any] | None = None,
    sources: Iterable[Mapping[str, Any]] = (),
    *,
    claims: Iterable[Mapping[str, Any]] = (),
    legacy_targets: Mapping[str, Any] | None = None,
) -> AdaptiveCoverageReadiness:
    """Evaluate a section without a universal unique/direct gate.

    Metadata and abstract-only rows remain discovery/background material.  A
    load-bearing claim is factual only when its source is direct and carries
    ``factual_support`` permission.  Optional role/breadth shortfalls become
    explicit limitations once the core section is writable.
    """

    section = section if isinstance(section, Mapping) else {}
    contract = build_adaptive_coverage_contract(section)
    rows = [dict(item) for item in sources if isinstance(item, Mapping)]
    claim_rows = [dict(item) for item in claims if isinstance(item, Mapping)]
    if not claim_rows:
        claim_rows = _adaptive_claims(section)
    load_bearing_claim_ids: list[str] = []
    for item in claim_rows:
        if item.get("claim_id") or item.get("id"):
            load_bearing_claim_ids.extend(
                _adaptive_claim_ids(item.get("claim_id") or item.get("id"))
            )
    load_bearing_claim_ids = list(dict.fromkeys(load_bearing_claim_ids))
    claim_statements = {
        _adaptive_text(item.get("statement") or item.get("claim"), 240)
        for item in claim_rows
        if _adaptive_text(item.get("statement") or item.get("claim"), 240)
    }
    factual_rows: list[dict[str, Any]] = []
    usable_rows: list[dict[str, Any]] = []
    permission_failures: list[str] = []
    for row in rows:
        if not row.get("canonical_chunk_ids") and not row.get("chunk_id"):
            continue
        scope = normalize_scope_fit(row.get("scope_fit"))
        permission = _adaptive_source_permission(row)
        if scope in {"direct", "adjacent"}:
            usable_rows.append(row)
        if scope == "direct" and permission == "factual_support":
            factual_rows.append(row)
        elif scope == "direct" and permission in {"discovery_only", "background_and_candidate_only"}:
            identity = _adaptive_text(row.get("paper_id") or row.get("title"), 120)
            permission_failures.append(f"{identity}: {permission or 'missing_permission'}")

    unique_ids = {
        _adaptive_text(row.get("paper_id"), 160)
        for row in usable_rows
        if _adaptive_text(row.get("paper_id"), 160)
    }
    scoped_direct_ids = {
        _adaptive_text(row.get("paper_id"), 160)
        for row in usable_rows
        if normalize_scope_fit(row.get("scope_fit")) == "direct"
        and _adaptive_text(row.get("paper_id"), 160)
    }
    direct_ids = {
        _adaptive_text(row.get("paper_id"), 160)
        for row in factual_rows
        if _adaptive_text(row.get("paper_id"), 160)
    }
    venues = {
        _adaptive_text(row.get("venue"), 120).casefold()
        for row in usable_rows
        if _adaptive_text(row.get("venue"), 120)
    }
    covered_roles = {
        role
        for row in factual_rows
        for role in _adaptive_source_roles(row)
        if role in ADAPTIVE_ROLE_ORDER
    }
    covered_required = [role for role in contract.required_roles if role in covered_roles]
    missing_required = [role for role in contract.required_roles if role not in covered_roles]
    missing_optional = [role for role in contract.optional_roles if role not in covered_roles]
    source_claim_ids = {
        claim_id
        for row in factual_rows
        for claim_id in _adaptive_source_claim_ids(row)
    }
    if load_bearing_claim_ids:
        supported_claim_ids = source_claim_ids.intersection(load_bearing_claim_ids)
        unsupported_claims = [
            claim_id for claim_id in load_bearing_claim_ids
            if claim_id not in supported_claim_ids
        ]
    elif claim_statements:
        # Without stable IDs, only an explicit claim-support field can close a
        # load-bearing claim.  This avoids counting an unrelated chunk merely
        # because the section has a claim list.
        supported_claim_ids = set()
        unsupported_claims = sorted(claim_statements)
    else:
        supported_claim_ids = set()
        unsupported_claims = []
    visual_ready = True
    if contract.visual_asset_required:
        visual_ready = any(
            str(row.get("content_depth") or "").casefold() == "fulltext"
            and (
                row.get("visual_asset_ids")
                or row.get("visual_chunk_ids")
                or row.get("visual_assets")
                or row.get("visual_ingest_status") in {"accepted", "complete", "ready"}
            )
            for row in factual_rows
        )

    core_breadth_met = (
        len(unique_ids) >= contract.minimum_unique_sources
        and len(direct_ids) >= contract.minimum_direct_sources
        and len(unique_ids) >= contract.minimum_distinct_papers
        and len(venues) >= contract.minimum_distinct_venues
    )
    claims_met = not unsupported_claims
    roles_met = not missing_required
    visual_met = visual_ready
    section_role = str(contract.section_role or "").casefold().strip()
    single_source_roles = {
        "introduction", "intro", "outlook", "conclusion", "framing",
        "transition", "scope", "methods_note",
    }
    minimum_writable_sources = (
        1
        if section_role in single_source_roles
        else min(2, max(1, contract.minimum_unique_sources))
    )
    plural_synthesis_met = len(unique_ids) >= minimum_writable_sources
    limitations: list[str] = []
    reasons: list[str] = []
    if missing_optional:
        limitations.append("optional_roles_uncovered:" + ",".join(missing_optional))
    if legacy_targets:
        try:
            old_unique = int(legacy_targets.get("minimum_unique_sources") or 0)
            old_direct = int(legacy_targets.get("minimum_direct_sources") or 0)
        except (TypeError, ValueError):
            old_unique = old_direct = 0
        if old_unique and len(unique_ids) < old_unique:
            limitations.append(f"article_breadth_target_shortfall:{len(unique_ids)}/{old_unique}")
        if old_direct and len(direct_ids) < old_direct:
            limitations.append(f"article_direct_target_shortfall:{len(direct_ids)}/{old_direct}")
    if permission_failures:
        # Discovery/background rows are intentionally retained for traceability
        # and follow-up, but a weak row is not a section-wide blocker when the
        # factual direct pool independently closes the required obligations.
        limitations.append(
            "weak_permission_rows_excluded_from_factual_support:"
            + str(len(permission_failures))
        )
    if missing_required:
        reasons.append("load_bearing_roles_uncovered:" + ",".join(missing_required))
    if unsupported_claims:
        reasons.append("load_bearing_claims_unsupported")
    if not visual_met:
        reasons.append("required_visual_asset_not_available_from_fulltext")
    if not core_breadth_met and usable_rows:
        limitations.append(
            f"core_source_diversity_shortfall:{len(unique_ids)}/{contract.minimum_unique_sources}"
        )
    if not factual_rows and usable_rows:
        reasons.append("no_direct_factual_support_sources")

    if not rows or not usable_rows:
        outcome = "merge_required" if contract.merge_if_under_supported else "needs_more_literature"
        reasons.append("no_writable_section_material")
    elif not (roles_met and claims_met and visual_met):
        outcome = "needs_more_literature"
    elif not plural_synthesis_met:
        outcome = (
            "merge_required"
            if contract.merge_if_under_supported
            else "needs_more_literature"
        )
        reasons.append(
            "substantive_section_requires_plural_sources:"
            f"{len(unique_ids)}/{minimum_writable_sources}"
        )
    elif contract.merge_if_under_supported and not core_breadth_met:
        outcome = "merge_required"
        reasons.append("section_is_explicitly_mergeable_when_core_breadth_is_unfilled")
    elif core_breadth_met:
        outcome = "material_ready" if not limitations else "material_ready_with_limits"
    elif factual_rows:
        outcome = "material_ready_with_limits"
        reasons.append("core_claims_are_supported_but_optional_breadth_is_unfilled")
    else:
        outcome = "needs_more_literature"
    return AdaptiveCoverageReadiness(
        outcome=outcome,
        contract=contract,
        unique_sources=len(unique_ids),
        direct_sources=len(direct_ids),
        scoped_direct_sources=len(scoped_direct_ids),
        factual_direct_sources=len(direct_ids),
        distinct_papers=len(unique_ids),
        distinct_venues=len(venues),
        covered_required_roles=covered_required,
        missing_required_roles=missing_required,
        missing_optional_roles=missing_optional,
        supported_load_bearing_claims=len(supported_claim_ids),
        unsupported_load_bearing_claims=unsupported_claims,
        factual_permission_sources=len(direct_ids),
        permission_failures=list(dict.fromkeys(permission_failures)),
        visual_asset_ready=visual_ready,
        limitations=list(dict.fromkeys(limitations)),
        reasons=list(dict.fromkeys(reasons)),
    )


def normalize_scope_fit(value: Any, *, default: str = "unreviewed") -> str:
    """Normalize all legacy scope spellings to the canonical vocabulary."""

    if hasattr(value, "value"):
        value = value.value
    raw = str(value or "").strip().casefold().replace("-", "_")
    if raw.startswith("scopefit."):
        raw = raw.split(".", 1)[1]
    aliases = {
        "in_domain": "direct",
        "directly_relevant": "direct",
        "cross_domain_analogy": "adjacent",
        "near_domain": "adjacent",
        "off_domain": "out_of_scope",
        "out_of_domain": "out_of_scope",
        "unknown": "unreviewed",
        "not_run": "unreviewed",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in CANONICAL_SCOPE_FITS else default


def normalize_content_depth(value: Any, *, default: str = "metadata") -> str:
    if hasattr(value, "value"):
        value = value.value
    raw = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "full_text": "fulltext",
        "fulltext_with_visuals": "fulltext",
        "s2_body_snippet": "structured_snippet",
        "text_chunk": "structured_snippet",
        "abstract_only": "abstract",
        "materialized_abstract_claim": "abstract_claim",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in CANONICAL_CONTENT_DEPTHS else default


def normalize_use_permission(value: Any, *, default: str = "discovery_only") -> str:
    if hasattr(value, "value"):
        value = value.value
    raw = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "factual_direct": "factual_support",
        "factual_assertion": "factual_support",
        "background_only": "background_and_candidate_only",
        "synthesis_support": "contextual_or_qualified_support",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in CANONICAL_USE_PERMISSIONS else default


_SCOPE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_SCOPE_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "using",
        "into", "study", "based", "show", "shows", "result", "results",
        "method", "methods", "approach", "system", "systems", "paper",
    }
)


def _scope_tokens(text: Any) -> set[str]:
    return {
        token.casefold()
        for token in _SCOPE_TOKEN_RE.findall(str(text or ""))
        if token.casefold() not in _SCOPE_STOPWORDS
    }


def assess_structured_snippet(
    text: str,
    *,
    query: str = "",
    section_context: str = "",
    limitations: Iterable[str] = (),
) -> dict[str, Any]:
    """Assign scope and context completeness to one S2 body snippet.

    S2 has already performed document-level parsing, but a search snippet can
    still be a partial sentence or omit a referenced equation/figure.  This
    deterministic check is deliberately a downgrade-only gate: it never
    penalizes a complete, directly matching snippet merely because it is not a
    locally downloaded PDF.
    """

    body_tokens = _scope_tokens(text)
    query_tokens = _scope_tokens(" ".join((query, section_context)))
    overlap = len(body_tokens & query_tokens) / max(1, len(query_tokens))
    limitations_list = [
        str(item).strip()
        for item in limitations
        if str(item).strip()
    ]
    lower = str(text or "").strip().casefold()
    if lower.endswith(("...", "…")):
        limitations_list.append("possible_truncation")
    if re.search(r"\b(?:fig(?:ure)?|table|eq(?:uation)?)\.?\s*\d", lower):
        limitations_list.append("referenced_visual_or_equation_not_in_snippet")
    # The S2 structured-body policy admits 500-character body snippets.  A
    # snippet is peer text evidence when it is direct, complete, and free of
    # explicit truncation/omitted-asset markers; provider prestige is not a
    # substitute for these checks.
    complete = len(str(text or "").strip()) >= 500 and not limitations_list
    if overlap >= 0.18:
        scope = "direct"
    elif overlap >= 0.06:
        scope = "adjacent"
    else:
        scope = "contextual"
    return {
        "scope_fit": normalize_scope_fit(scope),
        "context_complete": bool(complete),
        "topic_overlap": round(overlap, 4),
        "context_limitations": list(dict.fromkeys(limitations_list)),
    }


REVIEW_MODE_DEFAULTS: dict[str, dict[str, Any]] = {
    "comprehensive_review": {
        "reference_target_range": [100, 180],
        "word_target_range": [16000, 28000],
        "section_target_range": [7, 12],
        "section_unique_range": [8, 24],
        "section_direct_range": [5, 16],
        "default_roles": [
            "foundation",
            "mechanism",
            "method",
            "frontier",
            "controversy",
            "application",
        ],
    },
    "critical_narrative_review": {
        "reference_target_range": [70, 130],
        "word_target_range": [10000, 22000],
        "section_target_range": [6, 10],
        "section_unique_range": [6, 18],
        "section_direct_range": [4, 12],
        "default_roles": [
            "foundation",
            "mechanism",
            "method",
            "frontier",
        ],
    },
    "focused_perspective": {
        "reference_target_range": [40, 90],
        "word_target_range": [6000, 14000],
        "section_target_range": [4, 8],
        "section_unique_range": [4, 12],
        "section_direct_range": [3, 8],
        "default_roles": ["mechanism", "frontier", "controversy"],
    },
    "research_program": {
        "reference_target_range": [60, 120],
        "word_target_range": [10000, 20000],
        "section_target_range": [5, 10],
        "section_unique_range": [6, 18],
        "section_direct_range": [4, 12],
        "default_roles": ["foundation", "mechanism", "frontier", "application"],
    },
}

_MODE_ALIASES = {
    "comprehensive": "comprehensive_review",
    "comprehensive_review": "comprehensive_review",
    "critical_narrative": "critical_narrative_review",
    "critical_narrative_review": "critical_narrative_review",
    "narrative_review": "critical_narrative_review",
    "perspective": "focused_perspective",
    "perspective_review": "focused_perspective",
    "focused_perspective": "focused_perspective",
    "research_program": "research_program",
    "program": "research_program",
}


def _as_range(value: Any, fallback: list[int]) -> list[int]:
    if isinstance(value, dict):
        value = [value.get("min"), value.get("max")]
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return list(fallback)
    try:
        low, high = int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return list(fallback)
    if low < 0 or high < low:
        return list(fallback)
    return [low, high]


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _nested_sources(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    charter = blueprint.get("review_charter")
    constraints = blueprint.get("constraints")
    quality_contract = blueprint.get("review_quality_contract")
    if isinstance(charter, dict):
        constraints = charter.get("constraints", constraints)
        charter_quality = charter.get("review_quality_contract")
    else:
        charter_quality = None
    return [
        quality_contract if isinstance(quality_contract, dict) else {},
        charter_quality if isinstance(charter_quality, dict) else {},
        blueprint,
        charter if isinstance(charter, dict) else {},
        constraints if isinstance(constraints, dict) else {},
    ]


def _normalize_mode(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _MODE_ALIASES.get(text, "")


@dataclass(slots=True)
class ReviewModeContract:
    """One authoritative target contract shared by planning and coverage."""

    mode: str = "critical_narrative_review"
    reference_target_range: list[int] = field(default_factory=lambda: [70, 130])
    word_target_range: list[int] = field(default_factory=lambda: [10000, 22000])
    section_target_range: list[int] = field(default_factory=lambda: [6, 10])
    section_unique_range: list[int] = field(default_factory=lambda: [6, 18])
    section_direct_range: list[int] = field(default_factory=lambda: [4, 12])
    default_roles: list[str] = field(default_factory=list)
    source_shortfall_is_reported: bool = True
    allow_abstract_background: bool = True
    allow_metadata_for_writing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def minimum_references(self) -> int:
        return int(self.reference_target_range[0])

    @property
    def maximum_references(self) -> int:
        return int(self.reference_target_range[1])

    def section_targets(
        self,
        *,
        section: dict[str, Any] | None = None,
        section_count: int | None = None,
    ) -> dict[str, int]:
        section = section if isinstance(section, dict) else {}
        explicit = section.get("literature_coverage_target")
        if isinstance(explicit, dict):
            try:
                unique = int(explicit.get("minimum_unique_sources"))
            except (TypeError, ValueError):
                unique = 0
            try:
                direct = int(explicit.get("minimum_direct_sources"))
            except (TypeError, ValueError):
                direct = 0
            if unique > 0 and direct > 0:
                return {
                    "minimum_unique_sources": max(1, unique),
                    "minimum_direct_sources": max(1, min(unique, direct)),
                }

        count = max(1, int(section_count or self.section_target_range[0]))
        # The minimum article corpus is distributed across sections, with a
        # modest overlap reserve because the same landmark can legitimately
        # serve neighbouring sections.
        unique = math.ceil(self.minimum_references / count * 1.15)
        direct = math.ceil(unique * 0.60)
        unique = max(self.section_unique_range[0], min(self.section_unique_range[1], unique))
        direct = max(self.section_direct_range[0], min(unique, direct))
        return {
            "minimum_unique_sources": unique,
            "minimum_direct_sources": direct,
        }


def resolve_review_contract(
    blueprint: dict[str, Any] | None = None,
    *,
    section: dict[str, Any] | None = None,
) -> ReviewModeContract:
    """Resolve a contract without letting a legacy field silently win.

    Explicit ``review_mode``/``review_scale`` wins.  If a legacy blueprint has
    only ``methodology_identity=critical_narrative_review``, compatibility is
    preserved.  A section may carry a serialized contract from the orchestrator
    and that contract is accepted as the most specific input.
    """

    blueprint = blueprint if isinstance(blueprint, dict) else {}
    section = section if isinstance(section, dict) else {}
    embedded = section.get("review_quality_contract") or blueprint.get(
        "review_quality_contract"
    )
    if not embedded and isinstance(blueprint.get("review_charter"), dict):
        embedded = blueprint["review_charter"].get("review_quality_contract")
    if isinstance(embedded, dict) and embedded.get("mode") in REVIEW_MODE_DEFAULTS:
        payload = dict(embedded)
        mode = str(payload["mode"])
    else:
        mode = ""
        for source in _nested_sources(blueprint):
            for key in ("review_mode", "review_scale", "target_article_type"):
                mode = _normalize_mode(source.get(key))
                if mode:
                    break
            if mode:
                break
        if not mode:
            methodology = str(blueprint.get("methodology_identity") or "")
            mode = _normalize_mode(methodology) or "critical_narrative_review"
        payload = {}

    defaults = REVIEW_MODE_DEFAULTS[mode]
    sources = _nested_sources(blueprint)
    for key in ("review_mode", "review_scale", "target_article_type"):
        for source in sources:
            if _normalize_mode(source.get(key)) == mode:
                payload.setdefault("source_field", key)
                break

    def pick_range(name: str, fallback_name: str) -> list[int]:
        if name in payload:
            return _as_range(payload.get(name), defaults[fallback_name])
        for source in sources:
            for key in (name, fallback_name):
                if key in source:
                    return _as_range(source.get(key), defaults[fallback_name])
        return list(defaults[fallback_name])

    return ReviewModeContract(
        mode=mode,
        reference_target_range=pick_range("reference_target_range", "reference_target_range"),
        word_target_range=pick_range("word_target_range", "word_target_range"),
        section_target_range=pick_range("section_target_range", "section_target_range"),
        section_unique_range=pick_range("section_unique_range", "section_unique_range"),
        section_direct_range=pick_range("section_direct_range", "section_direct_range"),
        default_roles=list(defaults["default_roles"]),
        source_shortfall_is_reported=True,
        allow_abstract_background=True,
        allow_metadata_for_writing=False,
    )


def route_for_content_depth(content_depth: str, *, source_kind: str = "") -> str:
    value = str(content_depth or source_kind or "metadata").strip().casefold()
    if value in {"fulltext", "full_text", "fulltext_with_visuals"}:
        return "fulltext_local_or_publisher"
    if value in {"structured_snippet", "s2_body_snippet", "text_chunk"}:
        return "semantic_scholar_structured_snippet"
    if value in {"abstract_claim", "materialized_abstract_claim"}:
        return "semantic_scholar_abstract_claim"
    if value in {"abstract", "tldr", "abstract_only"}:
        return "semantic_scholar_abstract_or_tldr"
    return "metadata_discovery"


def permission_for_content(
    content_depth: str,
    *,
    scope_fit: str = "unreviewed",
    context_complete: bool = True,
) -> dict[str, Any]:
    """Return explicit downstream permissions for a resource.

    This is intentionally permissive for synthesis/background use while
    refusing to let metadata or abstract-only records become direct factual
    support.  Scope is independent: an adjacent paper can be useful, but it
    cannot silently support an in-domain measurement.
    """

    depth = normalize_content_depth(content_depth)
    scope = normalize_scope_fit(scope_fit)
    if depth in {"fulltext", "structured_snippet"}:
        permission = (
            "factual_support"
            if context_complete and scope == "direct"
            else "contextual_or_qualified_support"
        )
        allowed = [
            "background",
            "mechanism",
            "method",
            "measurement",
            "comparison",
            "application",
            "trend",
            "author_synthesis",
        ]
    elif depth == "partial_fulltext":
        permission = "contextual_or_qualified_support"
        allowed = [
            "background",
            "mechanism",
            "method",
            "trend",
            "candidate_lead",
            "author_synthesis",
        ]
    elif depth == "abstract_claim":
        # This is not a generic metadata abstract.  It is materialized only
        # after exact S2-body and public-OA routes fail for an identified
        # paper.  It may support what the authors explicitly report in the
        # abstract, but never unstated measurements, mechanisms, or causality.
        permission = "contextual_or_qualified_support"
        allowed = [
            "paper_reported_claim",
            "background",
            "trend",
            "candidate_lead",
            "author_synthesis",
        ]
    elif depth in {"abstract", "abstract_only", "tldr"}:
        permission = "background_and_candidate_only"
        allowed = ["background", "trend", "candidate_lead", "author_synthesis"]
    else:
        permission = "discovery_only"
        allowed = ["discovery", "candidate_lead"]
    if scope in {"out_of_scope", "unreviewed"}:
        if depth == "metadata":
            permission = "discovery_only"
            allowed = ["discovery", "candidate_lead"]
        elif depth == "abstract_claim":
            permission = "contextual_or_qualified_support"
            allowed = [
                "paper_reported_claim",
                "background",
                "trend",
                "candidate_lead",
                "author_synthesis",
            ]
        elif depth in {"abstract", "abstract_only", "tldr"}:
            # An abstract with an unconfirmed scope remains a background
            # candidate; it must not gain the broader contextual permission
            # merely because a generic indexed row exists.
            permission = "background_and_candidate_only"
            allowed = ["background", "trend", "candidate_lead", "author_synthesis"]
        else:
            permission = "contextual_or_qualified_support"
            allowed = ["discovery", "candidate_lead", "background"]
    return {
        "content_depth": depth,
        "use_permission": permission,
        "allowed_claim_kinds": allowed,
        "factual_support_allowed": permission == "factual_support",
        "scope_fit": scope,
        "context_complete": bool(context_complete),
    }


def source_route_record(
    *,
    discovery_route: str,
    materialization_route: str = "",
    content_depth: str = "metadata",
    scope_fit: str = "unreviewed",
    context_complete: bool = True,
    events: Iterable[dict[str, Any]] = (),
    metadata_conflicts: Iterable[str] = (),
) -> dict[str, Any]:
    permission = permission_for_content(
        content_depth,
        scope_fit=scope_fit,
        context_complete=context_complete,
    )
    return {
        "discovery_route": discovery_route or "unknown",
        "materialization_route": materialization_route or "not_materialized",
        "content_depth": permission["content_depth"],
        "use_permission": permission["use_permission"],
        "allowed_claim_kinds": permission["allowed_claim_kinds"],
        "factual_support_allowed": permission["factual_support_allowed"],
        "scope_fit": permission["scope_fit"],
        "context_complete": permission["context_complete"],
        "route_events": [dict(item) for item in events if isinstance(item, dict)],
        "metadata_conflicts": list(dict.fromkeys(str(item) for item in metadata_conflicts if str(item).strip())),
    }


def evaluate_discovery_stop(
    *,
    unique_papers: int,
    minimum_papers: int,
    covered_roles: Iterable[str],
    required_roles: Iterable[str],
    new_information_gain: float,
    no_gain_rounds: int,
    max_rounds: int,
    current_wave_index: int = 0,
    max_wave_index: int = 0,
    covered_dimensions: Iterable[str] = (),
    required_dimensions: Iterable[str] = (),
    observed_relation_count: int = 0,
    new_papers: int = 0,
    new_roles: int = 0,
    new_dimensions: int = 0,
    new_relations: int = 0,
    required_relation_tasks: Iterable[str] = (),
    satisfied_relation_tasks: Iterable[str] = (),
) -> dict[str, Any]:
    """One shared, explainable stop decision for S2 waves and section search.

    no_gain_rounds is only one input. A marginal-gain stop is legal only
    after the executor reports the current wave and all four incremental
    signals, so an uninitialized counter cannot silently terminate discovery.
    """

    missing_roles = sorted(set(required_roles) - set(covered_roles))
    missing_dimensions = sorted(set(required_dimensions) - set(covered_dimensions))
    required_relations = list(
        dict.fromkeys(str(item) for item in required_relation_tasks if str(item))
    )
    # Observed citation/recommendation edges never satisfy a semantic task.
    satisfied_relations = set(
        str(item) for item in satisfied_relation_tasks if str(item)
    )
    relation_tasks_satisfied = len(
        set(required_relations).intersection(satisfied_relations)
    )
    missing_relation_tasks = [
        item for item in required_relations if item not in satisfied_relations
    ]
    target_reached = (
        unique_papers >= minimum_papers
        and not missing_roles
        and not missing_dimensions
        and (
            not missing_relation_tasks
        )
    )
    metrics = {
        "current_wave_index": int(current_wave_index),
        "max_wave_index": int(max_wave_index or max_rounds),
        "unique_papers": int(unique_papers),
        "new_papers": int(new_papers),
        "new_roles": int(new_roles),
        "new_dimensions": int(new_dimensions),
        "observed_relation_count": int(observed_relation_count),
        "new_relations": int(new_relations),
        "new_information_gain": round(float(new_information_gain or 0.0), 4),
        "no_gain_rounds": int(no_gain_rounds),
        "missing_roles": missing_roles,
        "missing_dimensions": missing_dimensions,
        "missing_relation_tasks": missing_relation_tasks,
        "satisfied_relation_tasks": sorted(satisfied_relations),
        "relation_tasks_satisfied": relation_tasks_satisfied,
    }
    if target_reached:
        return {"stop": True, "reason": "targets_met", "metrics": metrics, **metrics}
    no_gain_stop = (
        current_wave_index >= 2
        and no_gain_rounds >= 2
        and new_papers == 0
        and new_roles == 0
        and new_dimensions == 0
        and new_relations == 0
        and float(new_information_gain or 0.0) <= 0.0
    )
    if no_gain_stop:
        return {
            "stop": True,
            "reason": "marginal_gain_exhausted",
            "metrics": metrics,
            **metrics,
        }
    if max_wave_index > 0 and current_wave_index >= max_wave_index:
        return {
            "stop": True,
            "reason": "wave_budget_reached_with_reported_shortfall",
            "metrics": metrics,
            **metrics,
        }
    return {
        "stop": False,
        "reason": "continue_targeted_wave",
        "metrics": metrics,
        **metrics,
    }
