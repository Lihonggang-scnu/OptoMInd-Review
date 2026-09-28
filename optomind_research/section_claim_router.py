"""Deterministic paragraph-local claim routing for compact evidence handles.

The router assigns every ready claim exactly one primary paragraph using local
text relevance only (character n-gram cosine plus strong argument-sequence
hints). No model call and no domain-specific vocabulary are used.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_EXPLICIT_SOURCE = "paragraph_functions_explicit"
_HINT_SOURCE = "argument_sequence_strong_hint"
_RELEVANCE_SOURCE = "relevance_routing"


def _normalize_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _char_ngrams(text: str, n: int = 3) -> Counter[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return Counter()
    if len(normalized) < n:
        return Counter([normalized])
    return Counter(
        normalized[index:index + n]
        for index in range(len(normalized) - n + 1)
    )


def text_similarity(left: str, right: str) -> float:
    """Deterministic char n-gram cosine; 0.0 when either side is empty."""
    left_grams = _char_ngrams(left)
    right_grams = _char_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    dot = sum(count * right_grams[gram] for gram, count in left_grams.items())
    left_norm = math.sqrt(sum(count * count for count in left_grams.values()))
    right_norm = math.sqrt(sum(count * count for count in right_grams.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _explicit_ids(entry: Any) -> list[str]:
    if not isinstance(entry, dict):
        return []
    raw = entry.get("claim_ids") or []
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(value) for value in raw if str(value)]


@dataclass
class ClaimRoutingResult:
    """Primary and secondary claim assignments plus diagnostics."""

    primary_by_paragraph: list[list[dict[str, Any]]]
    secondary_by_paragraph: list[list[dict[str, Any]]]
    primary_sources: list[list[str]]
    unassigned_claim_ids: list[str]
    unsupported_claim_ids: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "deterministic_local_routing",
            "primary_claim_ids_by_paragraph": [
                [str(claim.get("claim_id") or "") for claim in claims]
                for claims in self.primary_by_paragraph
            ],
            "secondary_claim_ids_by_paragraph": [
                [str(claim.get("claim_id") or "") for claim in claims]
                for claims in self.secondary_by_paragraph
            ],
            "primary_sources": self.primary_sources,
            "unassigned_claim_ids": self.unassigned_claim_ids,
            "unsupported_claim_ids": self.unsupported_claim_ids,
            "diagnostics": self.diagnostics,
        }


def _paragraph_query(
    paragraph_index: int,
    paragraph_functions: list[Any],
    argument_sequence: list[Any],
    section_key_questions: list[str] | None,
) -> str:
    function = (
        paragraph_functions[paragraph_index]
        if paragraph_index < len(paragraph_functions)
        else {}
    )
    step = (
        argument_sequence[paragraph_index]
        if paragraph_index < len(argument_sequence)
        else {}
    )
    parts = [
        function.get("title") if isinstance(function, dict) else "",
        function.get("purpose") if isinstance(function, dict) else "",
        function.get("function") if isinstance(function, dict) else "",
        step.get("step") if isinstance(step, dict) else "",
    ]
    parts.extend(section_key_questions or [])
    return " ".join(str(part) for part in parts if str(part))


def _claim_document(claim: dict[str, Any], evidence_text: str) -> str:
    components = list(claim.get("supported_components") or [])
    caveats = list(claim.get("caveats") or [])
    parts = [
        claim.get("statement_for_writing"),
        claim.get("statement"),
        claim.get("role"),
        " ".join(str(part) for part in components if str(part)),
        " ".join(str(part) for part in caveats if str(part)),
        evidence_text,
    ]
    return " ".join(str(part) for part in parts if str(part))


def route_section_claims(
    *,
    paragraph_functions: list[Any],
    argument_sequence: list[Any],
    claims: list[dict[str, Any]],
    ready_claim_ids: set[str],
    evidence_text_by_claim: dict[str, str] | None = None,
    expected_paragraphs: int,
    section_key_questions: list[str] | None = None,
    argument_sequence_boost: float = 0.35,
) -> ClaimRoutingResult:
    """Route every ready claim to exactly one primary paragraph.

    Explicit ``paragraph_functions`` claim_ids own their paragraph first.
    ``argument_sequence`` claim_ids act as strong hints (secondary candidates
    plus a relevance boost). Remaining ready claims are assigned greedily by
    deterministic char n-gram relevance with a balanced per-paragraph residual
    capacity. Claims with no evidence text are still assigned primary ownership
    and recorded as unsupported.
    """
    evidence_text_by_claim = evidence_text_by_claim or {}
    paragraph_count = max(1, expected_paragraphs)
    claims_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in claims
        if claim.get("claim_id")
    }
    ready_ids = [
        claim_id
        for claim_id in (ready_claim_ids or set())
        if claim_id in claims_by_id
    ]
    primary: list[list[dict[str, Any]]] = [
        [] for _ in range(paragraph_count)
    ]
    primary_sources: list[list[str]] = [
        [] for _ in range(paragraph_count)
    ]
    primary_ids: set[str] = set()
    explicit_count = 0
    duplicate_explicit: list[str] = []
    unready_explicit: list[str] = []

    for paragraph_index in range(paragraph_count):
        function = (
            paragraph_functions[paragraph_index]
            if paragraph_index < len(paragraph_functions)
            else None
        )
        for claim_id in _explicit_ids(function):
            if claim_id not in claims_by_id or claim_id not in ready_claim_ids:
                unready_explicit.append(claim_id)
                continue
            if claim_id in primary_ids:
                duplicate_explicit.append(claim_id)
                continue
            primary[paragraph_index].append(claims_by_id[claim_id])
            primary_sources[paragraph_index].append(_EXPLICIT_SOURCE)
            primary_ids.add(claim_id)
            explicit_count += 1

    hint_ids_by_paragraph: list[list[str]] = [
        [] for _ in range(paragraph_count)
    ]
    for paragraph_index in range(paragraph_count):
        step = (
            argument_sequence[paragraph_index]
            if paragraph_index < len(argument_sequence)
            else None
        )
        seen: set[str] = set()
        for claim_id in _explicit_ids(step):
            if (
                claim_id in claims_by_id
                and claim_id in ready_claim_ids
                and claim_id not in seen
            ):
                hint_ids_by_paragraph[paragraph_index].append(claim_id)
                seen.add(claim_id)

    secondary: list[list[dict[str, Any]]] = [
        [] for _ in range(paragraph_count)
    ]
    for paragraph_index in range(paragraph_count):
        primary_in_paragraph = {
            str(claim.get("claim_id") or "")
            for claim in primary[paragraph_index]
        }
        for claim_id in hint_ids_by_paragraph[paragraph_index]:
            if claim_id not in primary_in_paragraph:
                secondary[paragraph_index].append(claims_by_id[claim_id])

    residual_ids = [
        claim_id for claim_id in ready_ids if claim_id not in primary_ids
    ]
    residual_capacity: list[int] = []
    residual_counts = [0] * paragraph_count
    capacity_overflow: list[str] = []
    if residual_ids:
        queries = [
            _paragraph_query(
                paragraph_index,
                paragraph_functions,
                argument_sequence,
                section_key_questions,
            )
            for paragraph_index in range(paragraph_count)
        ]
        documents = {
            claim_id: _claim_document(
                claims_by_id[claim_id],
                evidence_text_by_claim.get(claim_id, ""),
            )
            for claim_id in residual_ids
        }
        scores = {
            claim_id: [
                text_similarity(queries[paragraph_index], documents[claim_id])
                + (
                    argument_sequence_boost
                    if claim_id in hint_ids_by_paragraph[paragraph_index]
                    else 0.0
                )
                for paragraph_index in range(paragraph_count)
            ]
            for claim_id in residual_ids
        }
        base_capacity = max(1, math.ceil(len(residual_ids) / paragraph_count))
        residual_capacity = [base_capacity + 2] * paragraph_count
        order = sorted(
            residual_ids,
            key=lambda claim_id: (
                -max(scores[claim_id]),
                claim_id,
            ),
        )
        for claim_id in order:
            ranked = sorted(
                range(paragraph_count),
                key=lambda paragraph_index: (
                    -scores[claim_id][paragraph_index],
                    paragraph_index,
                ),
            )
            chosen = next(
                (
                    paragraph_index
                    for paragraph_index in ranked
                    if residual_counts[paragraph_index] < residual_capacity[paragraph_index]
                ),
                ranked[0],
            )
            if residual_counts[chosen] >= residual_capacity[chosen]:
                capacity_overflow.append(claim_id)
            residual_counts[chosen] += 1
            primary[chosen].append(claims_by_id[claim_id])
            source = (
                _HINT_SOURCE
                if claim_id in hint_ids_by_paragraph[chosen]
                else _RELEVANCE_SOURCE
            )
            primary_sources[chosen].append(source)
            primary_ids.add(claim_id)

    unsupported_ids = sorted(
        claim_id
        for claim_id in ready_ids
        if not evidence_text_by_claim.get(claim_id, "").strip()
    )
    unassigned_ids = sorted(
        claim_id for claim_id in ready_ids if claim_id not in primary_ids
    )
    diagnostics = {
        "total_ready_claims": len(ready_ids),
        "explicit_primary_count": explicit_count,
        "residual_primary_count": len(residual_ids),
        "duplicate_explicit_claim_ids": duplicate_explicit,
        "unready_or_unknown_explicit_ids": unready_explicit,
        "per_paragraph_primary_counts": [
            len(claims) for claims in primary
        ],
        "per_paragraph_residual_counts": residual_counts,
        "per_paragraph_residual_capacity": residual_capacity,
        "capacity_overflow_claim_ids": capacity_overflow
        if residual_ids else [],
        "secondary_hint_counts": [
            len(claims) for claims in secondary
        ],
    }
    return ClaimRoutingResult(
        primary_by_paragraph=primary,
        secondary_by_paragraph=secondary,
        primary_sources=primary_sources,
        unassigned_claim_ids=unassigned_ids,
        unsupported_claim_ids=unsupported_ids,
        diagnostics=diagnostics,
    )
