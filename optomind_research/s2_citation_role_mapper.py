"""Deterministic citation-role assignment for S2 text and paper candidates."""

from __future__ import annotations

import re
from typing import Iterable

from optomind_research.s2_schemas import CitationRole, S2PaperRecord


_ROLE_PATTERNS: list[tuple[CitationRole, set[str]]] = [
    (
        "historical_origin",
        {"first", "initial", "origin", "seminal", "pioneer", "introduced"},
    ),
    (
        "mechanism_neighbor",
        {
            "mechanism",
            "physics",
            "theory",
            "model",
            "resonance",
            "scattering",
            "interference",
            "coupling",
        },
    ),
    (
        "method_example",
        {
            "method",
            "fabrication",
            "optimization",
            "algorithm",
            "measurement",
            "characterization",
            "simulation",
        },
    ),
    (
        "comparative_example",
        {"compare", "comparison", "versus", "benchmark", "outperform", "tradeoff"},
    ),
    (
        "controversy_or_boundary",
        {
            "however",
            "limitation",
            "limitations",
            "challenge",
            "challenges",
            "uncertain",
            "controversy",
            "disagreement",
            "fails",
        },
    ),
    (
        "application_example",
        {
            "device",
            "deployment",
            "sensor",
            "imaging",
            "communication",
            "energy",
        },
    ),
]

_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {item.casefold() for item in _TOKEN_RE.findall(text or "")}


def map_citation_roles(
    *,
    query_or_claim: str,
    text: str,
    section: str = "",
    paper: S2PaperRecord | None = None,
    requested_roles: Iterable[str] = (),
    direct_score: float = 0.0,
    current_year: int = 2026,
) -> list[CitationRole]:
    """Assign multiple valid uses instead of forcing direct-evidence semantics."""

    query_tokens = _tokens(query_or_claim)
    text_tokens = _tokens(" ".join([text, section, paper.title if paper else ""]))
    overlap = len(query_tokens & text_tokens) / max(1, len(query_tokens))
    roles: list[CitationRole] = []

    if direct_score >= 0.25 or overlap >= 0.45:
        roles.append("direct_support")
    elif direct_score >= 0.08 or overlap >= 0.18:
        roles.append("partial_support")
    else:
        roles.append("background_context")

    requested = {str(role).casefold() for role in requested_roles}
    for role, terms in _ROLE_PATTERNS:
        if role == "historical_origin" and "foundation" not in requested:
            continue
        if text_tokens & terms:
            roles.append(role)
    if "application" in requested and text_tokens & {"application", "applications"}:
        roles.append("application_example")
    if "foundation" in requested and paper and paper.citation_count >= 50:
        roles.append("historical_origin")
    if "frontier" in requested and paper and paper.year:
        if current_year - paper.year <= 3:
            roles.append("frontier_progress")
    if "review" in requested and paper:
        publication_types = " ".join(paper.publication_types).casefold()
        if "review" in publication_types or any(
            token in paper.title.casefold()
            for token in ("review", "perspective", "roadmap")
        ):
            roles.append("review_pointer")

    return list(dict.fromkeys(roles))
