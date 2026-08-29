"""Deterministic, multi-channel ranking for S2 literature candidates."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from optomind_research.s2_schemas import S2PaperRecord


_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}", re.IGNORECASE)
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "using",
    "based",
    "study",
    "paper",
    "review",
    "toward",
    "towards",
    "via",
    "under",
    "over",
    "their",
    "between",
    "through",
}

_GENERIC_DOMAIN_TERMS = {
    "optical",
    "optic",
    "photonics",
    "photonic",
    "thin",
    "film",
    "films",
    "multilayer",
    "multilayers",
    "structure",
    "structures",
    "system",
    "systems",
    "design",
    "device",
    "devices",
}

_ROLE_TERMS: dict[str, set[str]] = {
    "foundation": {"first", "origin", "principle", "theory", "fundamental"},
    "mechanism": {
        "mechanism",
        "physics",
        "model",
        "theory",
        "resonance",
        "scattering",
        "interference",
        "coupling",
    },
    "method": {
        "method",
        "fabrication",
        "design",
        "optimization",
        "measurement",
        "characterization",
        "simulation",
    },
    "comparison": {
        "comparison",
        "benchmark",
        "versus",
        "performance",
        "tradeoff",
        "tradeoffs",
    },
    "frontier": {
        "recent",
        "emerging",
        "advanced",
        "programmable",
        "inverse",
        "active",
        "dynamic",
    },
    "controversy": {
        "challenge",
        "limitation",
        "debate",
        "uncertainty",
        "tradeoff",
        "failure",
    },
    "application": {
        "application",
        "device",
        "sensor",
        "imaging",
        "communication",
        "energy",
        "industrial",
    },
    "review": {"review", "perspective", "roadmap", "overview", "survey"},
}


def tokenize(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(text or "")
        if token.casefold() not in _STOPWORDS
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = math.sqrt(sum(a * a for a in left))
    norm_r = math.sqrt(sum(b * b for b in right))
    if norm_l <= 0 or norm_r <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_l * norm_r)))


@dataclass(slots=True)
class CandidateQualityVector:
    direct_relevance: float = 0.0
    section_role_fit: float = 0.0
    semantic_seed_similarity: float = 0.0
    citation_landmark_strength: float = 0.0
    frontier_recency: float = 0.0
    review_value: float = 0.0
    oa_route_quality: float = 0.0
    abstract_completeness: float = 0.0
    tldr_available: bool = False
    text_availability: str = ""
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class S2Candidate:
    paper: S2PaperRecord
    facet_id: str
    query_texts: list[str] = field(default_factory=list)
    requested_roles: list[str] = field(default_factory=list)
    discovery_channels: list[str] = field(default_factory=list)
    quality_vector: CandidateQualityVector = field(
        default_factory=CandidateQualityVector
    )
    assigned_pools: list[str] = field(default_factory=list)
    decision: str = "retain"
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "paper": self.paper.to_dict(),
            "facet_id": self.facet_id,
            "query_texts": self.query_texts,
            "requested_roles": self.requested_roles,
            "discovery_channels": self.discovery_channels,
            "quality_vector": self.quality_vector.to_dict(),
            "assigned_pools": self.assigned_pools,
            "decision": self.decision,
            "decision_reason": self.decision_reason,
        }


class S2CandidateRanker:
    """Build an explainable quality matrix and retain role-diverse candidates."""

    def __init__(self, *, current_year: int | None = None) -> None:
        self.current_year = current_year or datetime.now(timezone.utc).year

    def score(
        self,
        paper: S2PaperRecord,
        *,
        queries: Iterable[str],
        requested_roles: Iterable[str],
        seed_vector: list[float] | None = None,
    ) -> CandidateQualityVector:
        query_tokens = tokenize(" ".join(queries))
        paper_tokens = tokenize(
            " ".join([paper.title, paper.abstract, paper.tldr, paper.venue])
        )
        direct = jaccard(query_tokens, paper_tokens)
        # Title matches are especially informative for literature discovery.
        title_overlap = jaccard(query_tokens, tokenize(paper.title))
        direct = min(1.0, direct * 0.55 + title_overlap * 0.75)
        anchor_terms = query_tokens - _GENERIC_DOMAIN_TERMS
        anchor_overlap = anchor_terms & paper_tokens
        if anchor_terms and not anchor_overlap:
            direct = min(direct, 0.05)

        role_terms = set()
        normalized_roles = [str(role).casefold() for role in requested_roles]
        for role in normalized_roles:
            role_terms.update(_ROLE_TERMS.get(role, set()))
        role_fit = jaccard(role_terms, paper_tokens) if role_terms else 0.0
        publication_types = {item.casefold() for item in paper.publication_types}
        if "review" in normalized_roles and any(
            marker in " ".join(publication_types)
            for marker in ("review", "meta-analysis")
        ):
            role_fit = max(role_fit, 0.95)

        citation_strength = min(
            1.0, math.log1p(max(0, paper.citation_count)) / math.log1p(2000)
        )
        recency = 0.0
        if paper.year:
            age = max(0, self.current_year - paper.year)
            recency = max(0.0, 1.0 - age / 12.0)
        review_value = 1.0 if any(
            "review" in item or "meta-analysis" in item
            for item in publication_types
        ) else (0.75 if any(term in paper.title.casefold() for term in ("review", "perspective", "roadmap")) else 0.0)
        oa_quality = 1.0 if paper.is_oa and paper.s2_open_access_candidate_url else (
            0.65 if paper.is_oa else 0.0
        )
        abstract_completeness = min(1.0, len(paper.abstract) / 1200.0)
        semantic = cosine(seed_vector or [], paper.specter2_vector)
        if semantic < 0:
            semantic = 0.0

        risks: list[str] = []
        if not paper.title:
            risks.append("missing_title")
        if not paper.abstract and not paper.tldr:
            risks.append("no_abstract_or_tldr")
        if paper.is_oa and not paper.s2_open_access_candidate_url:
            risks.append("oa_without_route")
        if direct < 0.06 and semantic < 0.25:
            risks.append("weak_direct_match")

        return CandidateQualityVector(
            direct_relevance=round(direct, 4),
            section_role_fit=round(role_fit, 4),
            semantic_seed_similarity=round(semantic, 4),
            citation_landmark_strength=round(citation_strength, 4),
            frontier_recency=round(recency, 4),
            review_value=round(review_value, 4),
            oa_route_quality=round(oa_quality, 4),
            abstract_completeness=round(abstract_completeness, 4),
            tldr_available=bool(paper.tldr),
            text_availability=paper.text_availability,
            risk_flags=risks,
        )

    @staticmethod
    def assign_pools(
        vector: CandidateQualityVector,
        paper: S2PaperRecord,
        requested_roles: Iterable[str],
    ) -> list[str]:
        pools: list[str] = []
        roles = {str(role).casefold() for role in requested_roles}
        topical_fit = max(
            vector.direct_relevance, vector.semantic_seed_similarity
        )
        title_lower = paper.title.casefold()
        foundation_title = any(
            marker in title_lower
            for marker in ("fundamental", "theory", "principle", "origin", "seminal")
        )
        if vector.direct_relevance >= 0.08:
            pools.append("direct_relevance_pool")
        if vector.citation_landmark_strength >= 0.55 and (
            topical_fit >= 0.06 or ("foundation" in roles and foundation_title)
        ):
            pools.append("citation_landmark_pool")
        if vector.review_value >= 0.7 and topical_fit >= 0.08:
            pools.append("review_perspective_pool")
        if vector.frontier_recency >= 0.65 and topical_fit >= 0.06:
            pools.append("recent_frontier_pool")
        if vector.semantic_seed_similarity >= 0.55:
            pools.append("semantic_recommendation_pool")
        if "mechanism" in roles and vector.section_role_fit >= 0.08:
            pools.append("mechanism_neighbor_pool")
        if paper.is_oa and paper.s2_open_access_candidate_url:
            pools.append("oa_fulltext_candidate_pool")
        if not pools:
            pools.append("background_candidate_pool")
        return pools

    def build_candidate(
        self,
        paper: S2PaperRecord,
        *,
        facet_id: str,
        queries: list[str],
        requested_roles: list[str],
        discovery_channel: str,
        seed_vector: list[float] | None = None,
    ) -> S2Candidate:
        vector = self.score(
            paper,
            queries=queries,
            requested_roles=requested_roles,
            seed_vector=seed_vector,
        )
        pools = self.assign_pools(vector, paper, requested_roles)
        decision = "retain"
        reason = "retained_by_multichannel_matrix"
        if "missing_title" in vector.risk_flags:
            decision = "reject"
            reason = "missing_identity_title"
        return S2Candidate(
            paper=paper,
            facet_id=facet_id,
            query_texts=list(dict.fromkeys(queries)),
            requested_roles=list(dict.fromkeys(requested_roles)),
            discovery_channels=[discovery_channel],
            quality_vector=vector,
            assigned_pools=pools,
            decision=decision,
            decision_reason=reason,
        )

    @staticmethod
    def merge_candidates(candidates: Iterable[S2Candidate]) -> list[S2Candidate]:
        merged: dict[str, S2Candidate] = {}
        for candidate in candidates:
            key = (
                candidate.paper.paper_id
                or candidate.paper.doi.casefold()
                or candidate.paper.title.casefold()
            )
            if not key:
                continue
            current = merged.get(key)
            if current is None:
                merged[key] = candidate
                continue
            current.query_texts = list(
                dict.fromkeys(current.query_texts + candidate.query_texts)
            )
            current.requested_roles = list(
                dict.fromkeys(current.requested_roles + candidate.requested_roles)
            )
            current.discovery_channels = list(
                dict.fromkeys(
                    current.discovery_channels + candidate.discovery_channels
                )
            )
            current.assigned_pools = list(
                dict.fromkeys(current.assigned_pools + candidate.assigned_pools)
            )
            for field_name in (
                "direct_relevance",
                "section_role_fit",
                "semantic_seed_similarity",
                "citation_landmark_strength",
                "frontier_recency",
                "review_value",
                "oa_route_quality",
                "abstract_completeness",
            ):
                setattr(
                    current.quality_vector,
                    field_name,
                    max(
                        getattr(current.quality_vector, field_name),
                        getattr(candidate.quality_vector, field_name),
                    ),
                )
            current.quality_vector.risk_flags = list(
                dict.fromkeys(
                    current.quality_vector.risk_flags
                    + candidate.quality_vector.risk_flags
                )
            )
        return list(merged.values())

    @staticmethod
    def portfolio_sort(candidates: Iterable[S2Candidate]) -> list[S2Candidate]:
        """Sort without collapsing role-specific strengths into a hard gate."""

        def key(item: S2Candidate) -> tuple[float, ...]:
            q = item.quality_vector
            topical_fit = max(q.direct_relevance, q.semantic_seed_similarity)
            return (
                1.0 if item.decision != "reject" else 0.0,
                1.0 if topical_fit >= 0.08 else 0.0,
                topical_fit,
                q.direct_relevance,
                q.section_role_fit,
                max(
                    q.review_value,
                    q.citation_landmark_strength,
                    q.frontier_recency,
                ),
                q.oa_route_quality,
            )

        return sorted(candidates, key=key, reverse=True)
