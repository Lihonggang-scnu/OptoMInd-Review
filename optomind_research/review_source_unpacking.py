"""Domain-generic review-source unpacking core.

A verified claim may be bound to a review/perspective/roadmap rather than to
the original study behind a fact.  This sidecar stage keeps the review as
secondary evidence and emits deterministic trace tasks that point back at the
original cited studies named by inline citation markers.

The core is deliberately network-free.  Local code owns every fixed output
field (identities, quotes, excerpts, markers, task ids, outcomes).  An LLM, if
injected as a ranker, returns only high-information selections/reasons keyed
by task_id and candidate_id.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from optomind_research.runtime.material_unit_store import (
    material_unit_from_text_chunk,
)

__all__ = [
    "REVIEW_SOURCE_KINDS",
    "OUTCOMES",
    "NumberedBibliography",
    "review_source_signal",
    "is_review_source",
    "detect_review_bound_claims",
    "extract_citation_markers",
    "expand_citation_marker",
    "build_surrounding_excerpt",
    "associate_citation_markers",
    "build_review_trace_task",
    "build_review_trace_tasks",
    "EXPANSION_QUOTA_CLASS",
    "original_source_material_unit",
    "find_existing_original_unit",
    "default_materializer",
    "parse_numbered_bibliography",
    "build_review_bibliography_skeleton",
    "build_review_source_ranker_prompt",
    "unpack_review_sources",
]

REVIEW_SOURCE_KINDS = frozenset({
    "review",
    "perspective",
    "roadmap",
    "road map",
    "survey",
    "opinion",
})

EXPANSION_QUOTA_CLASS = "dominant_source_unbundling_non_quota"

OUTCOMES = frozenset({
    "original_source_materialized",
    "original_source_found_metadata_only",
    "unresolved_review_reference",
    "no_inline_reference",
})

_TITLE_PATTERNS = (
    r"\breview\b",
    r"\bperspective\b",
    r"\bopinion\b",
    r"\broad ?map\b",
    r"\bsurvey\b",
)

# Bracket citation markers: [12], [12-14], [12, 15].
_BRACKET_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"\[([0-9][0-9,\s\u2013-]*)\]"
)
# Parenthesized numeric citation groups require a comma or range separator so
# ordinary parenthetical prose like "(12)" is not treated as a citation.
_PAREN_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"\(([0-9][0-9,\s\u2013-]*[,,\s\u2013-][0-9\s\u2013-]*)\)"
)
_TOKEN_SPLIT_RE = re.compile(r"[,\s]+")
_RANGE_RE = re.compile(r"^([0-9]+)\s*[\-\u2013]\s*([0-9]+)$")
_SINGLE_RE = re.compile(r"^[0-9]+$")

_BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:\d{1,3}[.)]?[ \t]*)?"
    r"(REFERENCES|BIBLIOGRAPHY|WORKS CITED|LITERATURE CITED)"
    r"[ \t]*:?[ \t]*$"
)
_RELAXED_BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:\d{1,4}[.)]?[ \t]*)?"
    r"(REFERENCES|BIBLIOGRAPHY|WORKS CITED|LITERATURE CITED)"
    r"[ \t]*:?[ \t]*(?P<rest>.*)$"
)
_ENTRY_START_RE = re.compile(r"(?m)^[ \t]*\[(\d{1,4})\][ \t]*(.*)$")
_PAGE_ONLY_LINE_RE = re.compile(
    r"^\s*(?:[-–—]?\s*)?(?:page\s+)?\d{1,4}\s*"
    r"(?:of\s+\d{1,4})?\s*(?:[-–—]?\s*)?$",
    re.IGNORECASE,
)


class NumberedBibliography(dict):
    """``dict[int, entry]`` carrying the parse audit on ``.audit``."""

    def __init__(
        self,
        *args: Any,
        audit: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.audit = dict(audit or {})


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _clean_id(value: Any) -> str:
    return " ".join(str(value or "").split())


def _is_year(number: int) -> bool:
    return 1000 <= number <= 2099


def _expand_number_token(token: str) -> Optional[list[int]]:
    """Expand one citation token (single number or inclusive range)."""
    token = token.strip()
    match = _RANGE_RE.match(token)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end and not _is_year(start) and not _is_year(end):
            return list(range(start, end + 1))
        return None
    if _SINGLE_RE.match(token):
        number = int(token)
        if number >= 1 and not _is_year(number):
            return [number]
    return None


def expand_citation_marker(raw: str) -> list[int]:
    """Expand a citation marker string into unique sorted reference numbers.

    ``[12-14]`` -> ``[12, 13, 14]``; ``[12, 15]`` -> ``[12, 15]``.
    Markers containing zero, fractional tokens, or four-digit years are not
    valid citations and expand to ``[]``.
    """
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    elif inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    numbers: list[int] = []
    for token in _TOKEN_SPLIT_RE.split(inner):
        if not token:
            continue
        expanded = _expand_number_token(token)
        if expanded is None:
            return []
        numbers.extend(expanded)
    return sorted(set(numbers))


def extract_citation_markers(text: str) -> list[dict[str, Any]]:
    """Extract inline citation markers from review text.

    Returns marker records with ``raw`` (the original marker text), the
    expanded ``numbers``, and character offsets.  Guards reject measurement
    intervals such as ``[0,1]``, array indexing such as ``arr[12]``, function
    calls such as ``f(12,15)``, and ordinary four-digit years.
    """
    text = str(text or "")
    markers: list[dict[str, Any]] = []
    for pattern, form in (
        (_BRACKET_MARKER_RE, "bracket"),
        (_PAREN_MARKER_RE, "parenthesized"),
    ):
        for match in pattern.finditer(text):
            numbers = expand_citation_marker(match.group(0))
            if not numbers:
                continue
            markers.append({
                "raw": match.group(0),
                "numbers": numbers,
                "start": match.start(),
                "end": match.end(),
                "form": form,
            })
    markers.sort(key=lambda row: (row["start"], row["end"]))
    return markers


def review_source_signal(unit: Mapping[str, Any]) -> Optional[str]:
    """Return an explicit/heuristic signal when a unit is review-like."""
    identity = unit.get("identity") or {}
    card = unit.get("durable_content_card") or {}
    quality = card.get("content_quality") or {}

    def tokens(value: Any) -> Iterable[str]:
        if isinstance(value, str):
            for token in re.split(r"[,\s;]+", value):
                token = token.strip().casefold()
                if token:
                    yield token
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                yield from tokens(item)

    for source in (unit, unit.get("raw_metadata") or {}, identity):
        if not isinstance(source, Mapping):
            continue
        for key in ("publicationTypes", "publication_types"):
            for token in tokens(source.get(key)):
                if token in REVIEW_SOURCE_KINDS:
                    return f"publication_types:{token}"
    candidates = [
        _norm_text(unit.get("work_type")),
        _norm_text(unit.get("publication_type")),
        _norm_text(identity.get("work_type")),
        _norm_text(identity.get("publication_type")),
        _norm_text(quality.get("source_kind")),
        _norm_text(identity.get("source_kind")),
        _norm_text((identity.get("locator") or {}).get("source_kind")),
        _norm_text(identity.get("title")),
    ]
    for value in candidates:
        if value in REVIEW_SOURCE_KINDS:
            return f"source_kind:{value}"
    title = _norm_text(identity.get("title"))
    for pattern in _TITLE_PATTERNS:
        if re.search(pattern, title):
            return f"title:{pattern}"
    return None


def is_review_source(unit: Mapping[str, Any]) -> bool:
    return review_source_signal(unit) is not None


def _claim_bound_evidence(
    claim: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Generic bound chunk/quote extraction for both probe and runtime claims."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(chunk_id: Any, quote: Any, unit_id: Any = None) -> None:
        chunk_id = _clean_id(chunk_id)
        unit_id = _clean_id(unit_id)
        if not chunk_id and not unit_id:
            return
        key = chunk_id or f"unit:{unit_id}"
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "chunk_id": chunk_id,
            "unit_id": unit_id,
            "quote": str(quote or "").strip(),
        })

    add(claim.get("bound_chunk_id"), "")
    add("", "", claim.get("bound_unit_id"))
    add(claim.get("supporting_text_chunk_id"), "")
    for value in claim.get("supporting_text_chunk_ids") or []:
        add(value, "")
    for container_key in ("component_verification", "claim_components"):
        for component in claim.get(container_key) or []:
            if not isinstance(component, Mapping):
                continue
            for binding in component.get("bindings") or []:
                if not isinstance(binding, Mapping):
                    continue
                quote_exact = binding.get("quote_exact")
                if quote_exact is False:
                    continue
                add(
                    binding.get("chunk_id"),
                    binding.get("verbatim_quote") or binding.get("quote"),
                    binding.get("unit_id"),
                )
    return rows


def detect_review_bound_claims(
    claims: Iterable[Mapping[str, Any]],
    material_units: Mapping[str, Mapping[str, Any]],
    *,
    review_paper_ids: Optional[Iterable[Any]] = None,
    review_unit_ids: Optional[Iterable[Any]] = None,
) -> list[dict[str, Any]]:
    """Identify claims whose bound source is review/perspective/roadmap-like.

    The review unit is preserved as secondary evidence in ``review_fallback``;
    the claim itself is never mutated or downgraded.
    """
    units_by_chunk: dict[str, Mapping[str, Any]] = {}
    units_by_unit: dict[str, Mapping[str, Any]] = {}
    for unit in material_units.values():
        identity = unit.get("identity") or {}
        chunk_id = _clean_id(identity.get("chunk_id"))
        if chunk_id:
            units_by_chunk[chunk_id] = unit
        unit_id = _clean_id(unit.get("unit_id"))
        if unit_id:
            units_by_unit[unit_id] = unit
    explicit_paper_ids = {
        _clean_id(value) for value in review_paper_ids or () if _clean_id(value)
    }
    explicit_unit_ids = {
        _clean_id(value) for value in review_unit_ids or () if _clean_id(value)
    }
    detected: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = _clean_id(claim.get("claim_id"))
        if not claim_id:
            continue
        for bound in _claim_bound_evidence(claim):
            unit = (
                units_by_chunk.get(bound["chunk_id"])
                or units_by_unit.get(bound["unit_id"])
                or units_by_unit.get(bound["chunk_id"])
            )
            if unit is None:
                continue
            signal = review_source_signal(unit)
            identity = unit.get("identity") or {}
            unit_id = _clean_id(unit.get("unit_id")) or bound["unit_id"]
            if (
                signal is None
                and _clean_id(identity.get("paper_id")) in explicit_paper_ids
            ):
                signal = "explicit_review_paper_id"
            if signal is None and unit_id in explicit_unit_ids:
                signal = "explicit_review_unit_id"
            if signal is None:
                continue
            chunk_id = _clean_id(identity.get("chunk_id")) or bound["chunk_id"]
            key = f"{claim_id}\0{chunk_id}"
            if key in seen_tasks:
                continue
            seen_tasks.add(key)
            detected.append({
                "claim_id": claim_id,
                "claim_statement": str(
                    claim.get("statement")
                    or claim.get("claim_statement")
                    or claim.get("text")
                    or ""
                ).strip(),
                "claim_role": str(claim.get("role") or "supporting"),
                "review_chunk_id": chunk_id,
                "review_unit_id": unit_id,
                "review_paper_id": _clean_id(identity.get("paper_id")),
                "review_title": _clean_id(identity.get("title")),
                "exact_quote": bound["quote"],
                "review_source_signal": signal,
                "review_fallback": {
                    "unit_id": unit_id,
                    "chunk_id": chunk_id,
                    "paper_id": _clean_id(identity.get("paper_id")),
                    "title": _clean_id(identity.get("title")),
                    "role": "review_secondary",
                },
            })
    return detected


def _find_quote_offsets(unit_text: str, quote: str) -> tuple[int, int]:
    """Locate the verified quote in the unit text (exact then normalized)."""
    if not quote:
        return -1, -1
    index = unit_text.find(quote)
    if index >= 0:
        return index, index + len(quote)
    normalized_unit = re.sub(r"\s+", " ", unit_text).casefold()
    normalized_quote = re.sub(r"\s+", " ", quote).casefold()
    index = normalized_unit.find(normalized_quote)
    if index < 0:
        return -1, -1
    start = index
    while start > 0 and unit_text[start - 1] != " ":
        start -= 1
    end = min(len(unit_text), index + len(normalized_quote))
    while end < len(unit_text) and unit_text[end] != " ":
        end += 1
    return start, end


_CITATION_MARKER_TOKEN = (
    r"(?:\[[0-9][0-9,\s\u2013-]*\]"
    r"|\([0-9][0-9,\s\u2013-]*[,,\s\u2013-][0-9\s\u2013-]*\))"
)
# A terminal period followed by one or more citation markers owns those
# markers in the PRECEDING sentence (e.g. ``ends.[14] Next``), so a marker
# after the previous sentence's terminal punctuation is not attributed to the
# sentence that follows.  The cluster must be followed by a plausible new
# sentence start (uppercase/quote/bracket or end) to avoid splitting on
# mid-sentence abbreviations like ``e.g. [12] and ...``.
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?:"
    r"[.!?](?:\s+)?"
    + _CITATION_MARKER_TOKEN
    + r"(?:(?:\s*[,,\s\u2013-]\s*)" + _CITATION_MARKER_TOKEN + r")*"
    + r"\s*(?=[A-Z\"\u201c\u201d'(\[]|$)"
    + r"|[.!?]\s+(?=[A-Z\"\u201c\u201d'(\[])"
    + r"|[.!?]\s*$"
    + r"|\n\s*\n"
    + r")"
)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Conservative sentence spans: merge on abbreviations/lowercase continuations."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def associate_citation_markers(
    unit_text: str,
    quote: str,
    *,
    window_chars: int = 200,
) -> dict[str, Any]:
    """Associate citation markers with the verified quote by relation/strength.

    Relations:
    - ``exact_quote`` (strong): the marker text occurs inside the verified quote;
    - ``same_sentence`` (medium): the marker is outside the quote but in the
      sentence containing the quote (including markers immediately after the
      quoted span before terminal punctuation);
    - ``nearby_context`` (weak): the marker only appears elsewhere in the
      surrounding excerpt.

    If the quote is not found in the unit text, only markers literally inside
    the exact quote count as direct; excerpt markers are nearby.
    """
    quote = str(quote or "").strip()
    unit_text = str(unit_text or "")
    excerpt_info = build_surrounding_excerpt(
        unit_text, quote, window_chars=window_chars
    )
    excerpt = str(excerpt_info.get("excerpt") or "")
    quote_found = bool(excerpt_info.get("quote_found"))
    excerpt_start = int(excerpt_info.get("excerpt_start") or 0)
    associations: list[dict[str, Any]] = []
    if quote_found:
        quote_start_value = excerpt_info.get("quote_start")
        quote_start = (
            int(quote_start_value)
            if quote_start_value is not None
            else -1
        )
        quote_end_value = excerpt_info.get("quote_end")
        quote_end = (
            int(quote_end_value)
            if quote_end_value is not None
            else -1
        )
        sentence_spans = [
            (start + excerpt_start, end + excerpt_start)
            for start, end in _sentence_spans(excerpt)
        ]
        quote_sentence_spans = [
            (start, end)
            for start, end in sentence_spans
            if start <= quote_start and quote_end <= end
        ]
        for marker in extract_citation_markers(excerpt):
            marker_start = marker["start"] + excerpt_start
            marker_end = marker["end"] + excerpt_start
            if marker_start >= quote_start and marker_end <= quote_end:
                relation = "exact_quote"
            elif any(
                start <= marker_start and marker_end <= end
                for start, end in quote_sentence_spans
            ):
                relation = "same_sentence"
            else:
                relation = "nearby_context"
            associations.append({
                **marker,
                "start": marker_start,
                "end": marker_end,
                "relation": relation,
                "strength": {
                    "exact_quote": "strong",
                    "same_sentence": "medium",
                    "nearby_context": "weak",
                }[relation],
                "source": "excerpt",
            })
    else:
        for marker in extract_citation_markers(quote):
            associations.append({
                **marker,
                "relation": "exact_quote",
                "strength": "strong",
                "source": "quote",
            })
        for marker in extract_citation_markers(excerpt):
            associations.append({
                **marker,
                "start": marker["start"] + excerpt_start,
                "end": marker["end"] + excerpt_start,
                "relation": "nearby_context",
                "strength": "weak",
                "source": "excerpt",
            })
    return {
        "excerpt": excerpt,
        "quote_found": quote_found,
        "associations": associations,
    }


def build_surrounding_excerpt(
    unit_text: str,
    quote: str,
    *,
    window_chars: int = 200,
) -> dict[str, Any]:
    """Deterministic excerpt around the verified quote in the review text."""
    unit_text = str(unit_text or "")
    quote = str(quote or "").strip()
    window_chars = max(0, int(window_chars))
    if not quote:
        return {
            "excerpt": unit_text[:window_chars],
            "quote_found": False,
            "quote_start": -1,
            "quote_end": -1,
            "excerpt_start": 0,
        }
    quote_start, quote_end = _find_quote_offsets(unit_text, quote)
    if quote_start < 0:
        return {
            "excerpt": unit_text[:window_chars],
            "quote_found": False,
            "quote_start": -1,
            "quote_end": -1,
            "excerpt_start": 0,
        }
    half = max(1, window_chars // 2)
    start = max(0, quote_start - half)
    end = min(len(unit_text), quote_end + half)
    return {
        "excerpt": unit_text[start:end],
        "quote_found": True,
        "quote_start": quote_start,
        "quote_end": quote_end,
        "excerpt_start": start,
    }


def _review_unit_text(unit: Mapping[str, Any]) -> str:
    durable = unit.get("durable_content") or {}
    return str(
        durable.get("raw_text")
        or durable.get("normalized_text")
        or (unit.get("durable_content_card") or {}).get("observable_content")
        or ""
    )


def build_review_trace_task(
    review_bound_claim: Mapping[str, Any],
    unit_text: str = "",
    *,
    window_chars: int = 200,
) -> dict[str, Any]:
    """Build one deterministic trace task from a review-bound claim."""
    quote = str(review_bound_claim.get("exact_quote") or "")
    association = associate_citation_markers(
        unit_text, quote, window_chars=window_chars
    )
    excerpt = association.get("excerpt") or ""
    associations = list(association.get("associations") or [])
    direct_associations = [
        marker for marker in associations
        if marker.get("relation") in {"exact_quote", "same_sentence"}
    ]
    nearby_associations = [
        marker for marker in associations
        if marker.get("relation") == "nearby_context"
    ]
    citation_numbers = sorted({
        number
        for marker in direct_associations
        for number in marker.get("numbers") or []
    })
    nearby_citation_numbers = sorted({
        number
        for marker in nearby_associations
        for number in marker.get("numbers") or []
    })

    def unique_raw(rows: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            raw = str(row.get("raw") or "")
            if raw and raw not in seen:
                seen.add(raw)
                out.append(raw)
        return out

    raw_markers = unique_raw(direct_associations)
    nearby_raw_markers = unique_raw(nearby_associations)
    task_key = json.dumps([
        review_bound_claim.get("claim_id", ""),
        review_bound_claim.get("review_chunk_id", ""),
        citation_numbers,
    ], ensure_ascii=False, sort_keys=True)
    task_id = (
        "review_trace:"
        + hashlib.sha1(task_key.encode("utf-8")).hexdigest()[:20]
    )
    if not citation_numbers:
        outcome = "no_inline_reference"
        if nearby_citation_numbers:
            why = (
                "No directly associated inline citation marker was found in "
                "the verified quote or its containing sentence; nearby "
                "citation clues are preserved but were not used for automatic "
                "resolution."
            )
        else:
            why = (
                "No inline numeric citation markers were found in the verified "
                "quote or surrounding window; the review remains secondary "
                "evidence."
            )
    else:
        outcome = "unresolved_review_reference"
        why = (
            "Directly associated inline citation markers were found but no "
            "original source has been resolved yet."
        )
    return {
        "task_id": task_id,
        "claim_id": review_bound_claim.get("claim_id", ""),
        "claim_statement": review_bound_claim.get("claim_statement", ""),
        "review_chunk_id": review_bound_claim.get("review_chunk_id", ""),
        "review_unit_id": review_bound_claim.get("review_unit_id", ""),
        "review_paper_id": review_bound_claim.get("review_paper_id", ""),
        "review_title": review_bound_claim.get("review_title", ""),
        "exact_quote": quote,
        "surrounding_excerpt": excerpt,
        "quote_found_in_unit": bool(association.get("quote_found")),
        "citation_marker_associations": associations,
        "citation_markers": raw_markers,
        "citation_numbers": citation_numbers,
        "nearby_citation_markers": nearby_raw_markers,
        "nearby_citation_numbers": nearby_citation_numbers,
        "outcome": outcome,
        "why": why,
        "candidate_original_sources": [],
        "selected_original_sources": [],
        "materialized_unit_ids": [],
        "review_fallback": review_bound_claim.get("review_fallback") or {},
    }


def build_review_trace_tasks(
    review_bound_claims: Iterable[Mapping[str, Any]],
    material_units: Mapping[str, Mapping[str, Any]],
    *,
    window_chars: int = 200,
) -> list[dict[str, Any]]:
    """Emit stable, deduplicated trace tasks across review-bound claims."""
    units_by_chunk = {
        _clean_id((unit.get("identity") or {}).get("chunk_id")): unit
        for unit in material_units.values()
    }
    units_by_unit = {
        _clean_id(unit.get("unit_id")): unit
        for unit in material_units.values()
    }
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for review_claim in review_bound_claims:
        unit = (
            units_by_chunk.get(review_claim.get("review_chunk_id", ""))
            or units_by_unit.get(review_claim.get("review_unit_id", ""))
        )
        task = build_review_trace_task(
            review_claim,
            _review_unit_text(unit) if unit is not None else "",
            window_chars=window_chars,
        )
        if task["task_id"] in seen:
            continue
        seen.add(task["task_id"])
        tasks.append(task)
    return tasks


def original_source_material_unit(
    candidate: Mapping[str, Any],
    *,
    task: Optional[Mapping[str, Any]] = None,
    text: Optional[str] = None,
) -> dict[str, Any]:
    """Build an incremental MaterialUnit-compatible text record.

    Trust semantics are inherited from ``material_unit_from_text_chunk``:
    ``use_permission``, ``source_kind``, ``content_depth``, and allowed claim
    kinds are passed through unchanged.  No stricter context rule is imposed.
    """
    source_text = str(
        text
        or candidate.get("text")
        or candidate.get("raw_text")
        or ""
    ).strip()
    if not source_text:
        raise ValueError(
            "original_source_material_unit requires non-empty original text"
        )
    task = task or {}
    review_fallback = task.get("review_fallback") or {}
    review_chunk_id = (
        task.get("review_chunk_id")
        or review_fallback.get("chunk_id")
        or ""
    )
    chunk_id = _clean_id(
        candidate.get("chunk_id")
        or (
            f"unpack:{_clean_id(review_chunk_id)}:"
            f"{','.join(str(number) for number in task.get('citation_numbers') or [])}"
        )
    )
    paper_id = _clean_id(candidate.get("paper_id"))
    doi = _clean_id(candidate.get("doi"))
    if not paper_id and not doi:
        raise ValueError(
            "original_source_material_unit requires paper_id or doi"
        )
    use_permission = _clean_id(
        candidate.get("use_permission")
        or (review_fallback.get("use_permission"))
    )
    source_kind = _clean_id(candidate.get("source_kind"))
    content_depth = _clean_id(
        candidate.get("content_depth")
        or (
            "fulltext"
            if source_kind in {"s2_body", "oa_fulltext", "fulltext", "snippet"}
            else "abstract"
        )
    )
    chunk = {
        "text": source_text,
        "chunk_id": chunk_id,
        "paper_id": paper_id,
        "doi": doi,
        "title": _clean_id(candidate.get("title")),
        "use_permission": use_permission,
        "source_kind": source_kind,
        "content_depth": content_depth,
        "context_complete": bool(candidate.get("context_complete", True)),
        "allowed_claim_kinds": list(
            candidate.get("allowed_claim_kinds") or []
        ),
        "provenance": {
            "unpacked_from_review_chunk_id": _clean_id(review_chunk_id),
            "unpacked_from_review_unit_id": _clean_id(
                task.get("review_unit_id")
                or review_fallback.get("unit_id")
            ),
            "citation_markers": list(
                task.get("citation_markers") or []
            ),
            "citation_numbers": list(
                task.get("citation_numbers") or []
            ),
        },
    }
    paper = {
        "paper_id": paper_id,
        "doi": doi,
        "title": _clean_id(candidate.get("title")),
        "content_depth": content_depth,
    }
    return material_unit_from_text_chunk(chunk, paper)


def find_existing_original_unit(
    candidate: Mapping[str, Any],
    material_units: Mapping[str, Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """Locate an already-materialized original source unit, if any."""
    paper_id = _clean_id(candidate.get("paper_id"))
    doi = _clean_id(candidate.get("doi")).casefold().replace(
        "https://doi.org/", ""
    )
    chunk_id = _clean_id(candidate.get("chunk_id"))
    for unit in material_units.values():
        identity = unit.get("identity") or {}
        if chunk_id and _clean_id(identity.get("chunk_id")) == chunk_id:
            return unit
        if paper_id and _clean_id(identity.get("paper_id")) == paper_id:
            return unit
        unit_doi = _clean_id(identity.get("doi")).casefold().replace(
            "https://doi.org/", ""
        )
        if doi and unit_doi == doi:
            return unit
    return None


def default_materializer(
    candidate: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    material_units: Optional[Mapping[str, Mapping[str, Any]]] = None,
    create: bool = True,
) -> Optional[Mapping[str, Any]]:
    """Default materializer: reuse an existing unit or build an incremental one."""
    existing = find_existing_original_unit(
        candidate, material_units or {}
    )
    if existing is not None:
        return {
            "unit_id": _clean_id(existing.get("unit_id")),
            "unit": dict(existing),
            "created": False,
        }
    if not create:
        return None
    if not (candidate.get("text") or candidate.get("raw_text")):
        return None
    unit = original_source_material_unit(candidate, task=task)
    return {
        "unit_id": _clean_id(unit.get("unit_id")),
        "unit": unit,
        "created": True,
    }


def _lines_with_offsets(text: str) -> list[tuple[str, int]]:
    lines: list[tuple[str, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        lines.append((line, offset))
        offset += len(line)
    return lines


def _parse_numbered_bibliography_tail(
    tail_text: str,
    *,
    mode: str,
    heading_found: bool,
    heading_offset: int,
    audit: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    lines = _lines_with_offsets(tail_text)
    starts: list[tuple[int, int, int]] = []
    for line_index, (line, line_offset) in enumerate(lines):
        match = _ENTRY_START_RE.match(line)
        if match is None:
            continue
        starts.append((
            int(match.group(1)),
            line_index,
            line_offset + match.start(1) - 1,
        ))
    audit["entry_markers_found"] = len(starts)
    entries: dict[int, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    removed_page_lines: list[dict[str, Any]] = []
    for index, (number, line_index, entry_offset) in enumerate(starts):
        next_line_index = (
            starts[index + 1][1] if index + 1 < len(starts) else len(lines)
        )
        next_offset = (
            starts[index + 1][2] if index + 1 < len(starts) else len(tail_text)
        )
        raw_text = tail_text[entry_offset:next_offset]
        if number in entries:
            duplicates.append({
                "reference_number": number,
                "occurrence_document_offset": entry_offset + heading_offset,
                "raw_preview": raw_text[:200],
                "resolution": "first_wins_dropped",
            })
            continue
        cleaned_lines: list[str] = []
        removed_here: list[dict[str, Any]] = []
        for line_index_scan in range(line_index, next_line_index):
            line_text = lines[line_index_scan][0].rstrip("\r\n")
            if _PAGE_ONLY_LINE_RE.match(line_text):
                removed_here.append({
                    "line_index": line_index_scan,
                    "text": line_text,
                })
                continue
            cleaned_lines.append(line_text)
        removed_page_lines.extend(removed_here)
        cleaned_text = "\n".join(cleaned_lines)
        candidate_text = " ".join(cleaned_text.split())
        if len(candidate_text) > 1000:
            candidate_text = candidate_text[:1000].rstrip() + " ..."
        first_line_match = _ENTRY_START_RE.match(lines[line_index][0])
        entries[number] = {
            "reference_number": number,
            "raw_text": raw_text,
            "cleaned_text": cleaned_text,
            "candidate_text": candidate_text,
            "query_text": candidate_text[:300],
            "first_line": (
                str(first_line_match.group(2) or "").strip()
                if first_line_match is not None
                else ""
            ),
            "entry_start": entry_offset + heading_offset,
            "entry_end": next_offset + heading_offset,
            "line_count": next_line_index - line_index,
        }
    audit.update({
        "mode": mode,
        "heading_found": heading_found,
        "heading_offset": heading_offset,
        "entry_count": len(entries),
        "duplicate_numbers": duplicates,
        "removed_page_number_lines": removed_page_lines,
        "entries_by_number": sorted(entries),
    })
    return entries


def _flattened_entry_records(
    rest: str,
) -> Optional[dict[int, dict[str, Any]]]:
    """Parse flattened ``[n] ... [n+1] ...`` entries from one text region.

    Requires at least three markers, a sequence starting at ``[1]``, strictly
    increasing numbers, and a minimum substantive segment between markers so
    ordinary inline body citations (``[1], [2], and [3]``) are rejected.
    """
    markers = list(re.finditer(r"\[(\d{1,4})\]", rest))
    if len(markers) < 3:
        return None
    numbers = [int(match.group(1)) for match in markers]
    if numbers[0] != 1 or any(
        later <= earlier
        for earlier, later in zip(numbers, numbers[1:])
    ):
        return None
    entries: dict[int, dict[str, Any]] = {}
    for index, (marker, number) in enumerate(zip(markers, numbers)):
        segment_start = marker.end()
        segment_end = (
            markers[index + 1].start()
            if index + 1 < len(markers)
            else len(rest)
        )
        segment = rest[segment_start:segment_end].strip()
        if len(segment) < 8:
            return None
        raw_text = rest[marker.start():segment_end].rstrip()
        candidate_text = " ".join(segment.split())
        entries[number] = {
            "reference_number": number,
            "raw_text": raw_text,
            "cleaned_text": segment,
            "candidate_text": candidate_text[:1000],
            "query_text": candidate_text[:300],
            "first_line": segment.splitlines()[0].strip() if segment else "",
            "entry_start": marker.start(),
            "entry_end": segment_end,
            "line_count": 1,
        }
    return entries


def parse_numbered_bibliography(
    text: str,
    *,
    mode: str = "bibliography_only",
) -> NumberedBibliography:
    """Parse IEEE-like numbered bibliography entries into ``dict[int, record]``.

    ``mode="bibliography_only"`` treats the whole input as bibliography pages
    (the caller may pass already-isolated PDF/OA tail text).  In
    ``mode="whole_document"`` the parser finds the last References/Bibliography
    heading and parses only the tail; without a heading it returns an auditable
    empty result rather than guessing.

    Entries start with ``[n]`` at the beginning of a line and continue through
    wrapped lines until the next numbered entry.  Standalone page-number
    header/footer lines are removed from the cleaned/candidate text while the
    verbatim ``raw_text`` is preserved.  Duplicate reference numbers are
    resolved deterministically as first-wins and reported in ``audit``.
    """
    text = str(text or "")
    audit: dict[str, Any] = {
        "mode": mode,
        "heading_found": False,
        "heading_offset": None,
        "entry_count": 0,
        "duplicate_numbers": [],
        "removed_page_number_lines": [],
        "entries_by_number": [],
    }
    if mode == "whole_document":
        matches = list(_BIBLIOGRAPHY_HEADING_RE.finditer(text))
        if not matches:
            # PDF flattening may put the heading and the first entry on one
            # line (e.g. "71 References [1] ... [2] ...").  Try the relaxed
            # same-line form before failing; inline body citations are
            # rejected by the flattened sequence/entry checks.
            relaxed_matches = list(
                _RELAXED_BIBLIOGRAPHY_HEADING_RE.finditer(text)
            )
            if relaxed_matches:
                heading = relaxed_matches[-1]
                flattened_tail = text[heading.start("rest"):]
                records = _flattened_entry_records(flattened_tail)
                if records is not None:
                    shift = heading.start("rest")
                    for entry in records.values():
                        entry["entry_start"] += shift
                        entry["entry_end"] += shift
                    audit["heading_found"] = True
                    audit["heading_text"] = " ".join(
                        heading.group(0).split()
                    )
                    audit["heading_offset"] = heading.start()
                    audit["flattened_single_line"] = True
                    audit["flattened_entry_count"] = len(records)
                    audit["flattened_sequence_checks"] = {
                        "starts_at_1": True,
                        "strictly_increasing": True,
                        "minimum_entry_count": 3,
                        "minimum_segment_chars": 8,
                    }
                    audit["entry_count"] = len(records)
                    audit["entries_by_number"] = sorted(records)
                    return NumberedBibliography(records, audit=audit)
            audit.update({
                "heading_found": False,
                "reason": "no_references_heading_found",
                "flattened_single_line": False,
            })
            return NumberedBibliography({}, audit=audit)
        heading = matches[-1]
        audit["heading_found"] = True
        audit["heading_text"] = " ".join(heading.group(0).split())
        audit["heading_offset"] = heading.start()
        tail = text[heading.end():]
        heading_offset = heading.end()
    elif mode == "bibliography_only":
        tail = text
        heading_offset = 0
    else:
        raise ValueError(
            "parse_numbered_bibliography mode must be "
            "'bibliography_only' or 'whole_document'"
        )
    entries = _parse_numbered_bibliography_tail(
        tail,
        mode=mode,
        heading_found=bool(audit.get("heading_found")),
        heading_offset=heading_offset,
        audit=audit,
    )
    flattened_entries: Optional[dict[int, dict[str, Any]]] = None
    if len(entries) < 3:
        if mode == "whole_document":
            records = _flattened_entry_records(tail)
            if records is not None:
                for entry in records.values():
                    entry["entry_start"] += heading_offset
                    entry["entry_end"] += heading_offset
                flattened_entries = records
            else:
                relaxed_matches = list(
                    _RELAXED_BIBLIOGRAPHY_HEADING_RE.finditer(text)
                )
                if relaxed_matches:
                    heading = relaxed_matches[-1]
                    flattened_tail = text[heading.start("rest"):]
                    records = _flattened_entry_records(flattened_tail)
                    if records is not None:
                        shift = heading.start("rest")
                        for entry in records.values():
                            entry["entry_start"] += shift
                            entry["entry_end"] += shift
                        flattened_entries = records
                        audit["heading_found"] = True
                        audit["heading_text"] = " ".join(
                            heading.group(0).split()
                        )
                        audit["heading_offset"] = heading.start()
        else:
            records = _flattened_entry_records(text)
            if records is not None:
                flattened_entries = records
    if flattened_entries is not None:
        audit["flattened_single_line"] = True
        audit["flattened_entry_count"] = len(flattened_entries)
        audit["flattened_sequence_checks"] = {
            "starts_at_1": True,
            "strictly_increasing": True,
            "minimum_entry_count": 3,
            "minimum_segment_chars": 8,
        }
        audit["entry_count"] = len(flattened_entries)
        audit["entries_by_number"] = sorted(flattened_entries)
        return NumberedBibliography(flattened_entries, audit=audit)
    audit["flattened_single_line"] = False
    if mode == "whole_document" and not audit.get("heading_found"):
        audit["reason"] = audit.get("reason") or "no_references_heading_found"
    return NumberedBibliography(entries, audit=audit)


def build_review_bibliography_skeleton(
    entries: Mapping[int, Mapping[str, Any]],
    *,
    review_paper_id: str = "",
    review_unit_id: str = "",
) -> dict[str, Mapping[int, Mapping[str, Any]]]:
    """Build a review-scoped bibliography index skeleton for the resolver.

    The returned mapping is keyed by review paper id (or unit id) and then by
    reference number, matching ``unpack_review_sources``' per-review
    ``bibliography_index``/``review_bibliography`` shape.  Each candidate
    preserves ``review_paper_id`` and ``reference_number`` plus a stable
    candidate/query text; callers may enrich it with S2/OpenAlex identity and
    material before resolution.
    """
    review_paper_id = _clean_id(review_paper_id)
    review_unit_id = _clean_id(review_unit_id)
    review_key = review_paper_id or review_unit_id or "unscoped_review"
    index: dict[int, Mapping[str, Any]] = {}
    for number in sorted(entries):
        record = entries[number] or {}
        index[int(number)] = {
            "reference_number": int(number),
            "candidate_id": f"bib:{review_key}:{int(number)}",
            "review_paper_id": review_paper_id,
            "review_unit_id": review_unit_id,
            "raw_text": str(record.get("raw_text") or ""),
            "candidate_text": str(
                record.get("candidate_text")
                or record.get("cleaned_text")
                or record.get("raw_text")
                or ""
            ),
            "query_text": str(
                record.get("query_text")
                or record.get("candidate_text")
                or ""
            ),
            "source_kind": "bibliography_entry",
            "content_depth": "metadata",
        }
    return {review_key: index}


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    paper_id = _clean_id(candidate.get("paper_id"))
    doi = _clean_id(candidate.get("doi")).casefold().replace(
        "https://doi.org/", ""
    )
    if paper_id:
        return paper_id
    if doi:
        return f"doi:{doi}"
    chunk_id = _clean_id(candidate.get("chunk_id"))
    if chunk_id:
        return f"chunk:{chunk_id}"
    title = _clean_id(candidate.get("title"))
    if title:
        return f"title:{title}"
    return (
        "candidate:"
        + hashlib.sha1(
            json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()[:12]
    )


def _resolve_task_sources(
    task: Mapping[str, Any],
    *,
    bibliography_index: Mapping[int, Mapping[str, Any]],
    candidate_pool: Sequence[Mapping[str, Any]],
    ranker: Optional[
        Callable[
            [Mapping[str, Any], Sequence[Mapping[str, Any]]],
            Sequence[Mapping[str, Any]],
        ]
    ],
) -> tuple[list[dict[str, Any]], str]:
    citation_numbers = list(task.get("citation_numbers") or [])
    if not citation_numbers:
        return [], "no_inline_reference"
    exact: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number in citation_numbers:
        candidate = bibliography_index.get(int(number))
        if candidate is None:
            continue
        key = _candidate_key(candidate)
        if key and key not in seen:
            seen.add(key)
            exact.append(dict(candidate))
    if exact:
        return exact, "exact_bibliography_number_match"
    if ranker is not None and candidate_pool:
        selected = ranker(task, list(candidate_pool)) or []
        pool_by_key = {
            _candidate_key(candidate): candidate
            for candidate in candidate_pool
        }
        resolved: list[dict[str, Any]] = []
        for row in selected:
            if not isinstance(row, Mapping):
                continue
            candidate_id = _clean_id(row.get("candidate_id"))
            candidate = pool_by_key.get(candidate_id)
            if candidate is None:
                continue
            candidate = dict(candidate)
            candidate["selection_reason"] = str(
                row.get("reason") or ""
            ).strip()
            key = _candidate_key(candidate)
            if key and key not in seen:
                seen.add(key)
                resolved.append(candidate)
        if resolved:
            return resolved, "semantic_ranker_selection"
    return [], "no_resolution"


def _review_identity_key(task: Mapping[str, Any]) -> str:
    return (
        _clean_id(task.get("review_paper_id"))
        or _clean_id(task.get("review_unit_id"))
        or ""
    )


def _is_int_keyed_mapping(mapping: Mapping[Any, Any]) -> bool:
    if not mapping:
        return False
    for key in mapping:
        try:
            int(key)
        except (TypeError, ValueError):
            return False
    return True


def _lookup_review_bibliography(
    task: Mapping[str, Any],
    *,
    bibliography_index: Optional[Mapping[Any, Any]],
    review_bibliography: Optional[Mapping[str, Mapping[int, Mapping[str, Any]]]],
    reference_provider: Optional[
        Callable[
            [Mapping[str, Any]],
            Optional[Mapping[int, Mapping[str, Any]]],
        ]
    ],
    allow_global: bool,
) -> dict[int, Mapping[str, Any]]:
    """Resolve the bibliography index scoped to the current review task."""
    if reference_provider is not None:
        provided = reference_provider(task)
        if isinstance(provided, Mapping):
            return dict(provided)
        return {}
    key = _review_identity_key(task)
    for source in (review_bibliography, bibliography_index):
        if not isinstance(source, Mapping) or not source:
            continue
        if _is_int_keyed_mapping(source):
            if allow_global:
                return dict(source)
            continue
        for candidate_key in (
            _clean_id(task.get("review_paper_id")),
            _clean_id(task.get("review_unit_id")),
            key,
        ):
            if not candidate_key or candidate_key not in source:
                continue
            found = source[candidate_key]
            if isinstance(found, Mapping):
                return dict(found)
    return {}


def _lookup_review_candidate_pool(
    task: Mapping[str, Any],
    *,
    candidate_pool: Any,
    allow_global: bool,
) -> list[Mapping[str, Any]]:
    """Resolve the candidate pool scoped to the current review task."""
    if candidate_pool is None:
        return []
    if isinstance(candidate_pool, Mapping):
        key = _review_identity_key(task)
        for candidate_key in (
            _clean_id(task.get("review_paper_id")),
            _clean_id(task.get("review_unit_id")),
            key,
        ):
            if not candidate_key or candidate_key not in candidate_pool:
                continue
            found = candidate_pool[candidate_key]
            return list(found) if found else []
        return []
    pool = list(candidate_pool)
    return pool if allow_global else []


def unpack_review_sources(
    claims: Iterable[Mapping[str, Any]],
    material_units: Mapping[str, Mapping[str, Any]],
    *,
    bibliography_index: Optional[Mapping[Any, Any]] = None,
    review_bibliography: Optional[
        Mapping[str, Mapping[int, Mapping[str, Any]]]
    ] = None,
    reference_provider: Optional[
        Callable[
            [Mapping[str, Any]],
            Optional[Mapping[int, Mapping[str, Any]]],
        ]
    ] = None,
    candidate_pool: Any = (),
    ranker: Optional[
        Callable[
            [Mapping[str, Any], Sequence[Mapping[str, Any]]],
            Sequence[Mapping[str, Any]],
        ]
    ] = None,
    review_paper_ids: Optional[Iterable[Any]] = None,
    review_unit_ids: Optional[Iterable[Any]] = None,
    materializer: Optional[
        Callable[[Mapping[str, Any], Mapping[str, Any]], Optional[Mapping[str, Any]]]
    ] = None,
    window_chars: int = 200,
) -> Mapping[str, Any]:
    """Unpack review-bound claims into resolved original-source trace tasks."""
    if materializer is None:
        def _default_materializer(
            candidate: Mapping[str, Any],
            task: Mapping[str, Any],
        ) -> Optional[Mapping[str, Any]]:
            return default_materializer(
                candidate, task, material_units=material_units
            )

        materializer = _default_materializer
    review_bound = detect_review_bound_claims(
        claims,
        material_units,
        review_paper_ids=review_paper_ids,
        review_unit_ids=review_unit_ids,
    )
    tasks = build_review_trace_tasks(
        review_bound, material_units, window_chars=window_chars
    )
    review_identities = {
        _review_identity_key(task)
        for task in tasks
        if _review_identity_key(task)
    }
    allow_global = len(review_identities) <= 1
    resolved_tasks: list[dict[str, Any]] = []
    for task in tasks:
        task = dict(task)
        bibliography = _lookup_review_bibliography(
            task,
            bibliography_index=bibliography_index,
            review_bibliography=review_bibliography,
            reference_provider=reference_provider,
            allow_global=allow_global,
        )
        pool = _lookup_review_candidate_pool(
            task,
            candidate_pool=candidate_pool,
            allow_global=allow_global,
        )
        candidates, resolution_mode = _resolve_task_sources(
            task,
            bibliography_index=bibliography,
            candidate_pool=pool,
            ranker=ranker,
        )
        task["bibliography_scope"] = (
            _review_identity_key(task) or "single_review"
        )
        if not bibliography and not pool and task.get("citation_numbers"):
            task["bibliography_scope_ambiguous"] = not allow_global
            task["why"] = (
                "No review-scoped bibliography index or candidate pool was "
                "available for this review."
            )
        task["candidate_original_sources"] = list(
            task.get("candidate_original_sources") or []
        ) + [dict(candidate) for candidate in candidates]
        materialized_ids: list[str] = []
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            materialized = materializer(candidate, task)
            candidate_record = dict(candidate)
            if materialized is not None:
                unit_id = _clean_id(materialized.get("unit_id"))
                if unit_id:
                    materialized_ids.append(unit_id)
                    candidate_record["materialized_unit_id"] = unit_id
                else:
                    candidate_record["metadata_only"] = True
            selected.append(candidate_record)
        task["selected_original_sources"] = selected
        task["materialized_unit_ids"] = materialized_ids
        if not task.get("citation_numbers"):
            task["outcome"] = "no_inline_reference"
            if task.get("nearby_citation_numbers"):
                task["why"] = (
                    "No directly associated inline citation marker was found "
                    "in the verified quote or its containing sentence; nearby "
                    "citation clues are preserved but were not used for "
                    "automatic resolution."
                )
            else:
                task["why"] = (
                    "No inline numeric citation markers were found in the "
                    "verified quote or surrounding window; the review remains "
                    "secondary evidence."
                )
        elif not selected:
            task["outcome"] = "unresolved_review_reference"
            task["why"] = (
                "Inline citation markers "
                f"({', '.join(task.get('citation_markers') or [])}) were found "
                "but no original source was resolved by the exact bibliography "
                "index or the injected ranker."
            )
            if task.get("bibliography_scope_ambiguous"):
                task["why"] += (
                    " A global index/pool was not reused across multiple "
                    "review identities."
                )
        elif materialized_ids:
            task["outcome"] = "original_source_materialized"
            task["why"] = (
                f"Resolved {len(selected)} original source(s) via "
                f"{resolution_mode}; materialized "
                f"{len(materialized_ids)} unit(s)."
            )
        else:
            task["outcome"] = "original_source_found_metadata_only"
            task["why"] = (
                f"Resolved {len(selected)} original source(s) via "
                f"{resolution_mode} but no materialized text unit is available; "
                "metadata-only outcome preserves the review fallback."
            )
        resolved_tasks.append(task)
    outcome_counts = {
        outcome: sum(
            1 for task in resolved_tasks if task["outcome"] == outcome
        )
        for outcome in sorted(OUTCOMES)
    }
    return {
        "tasks": resolved_tasks,
        "review_bound_claim_count": len(review_bound),
        "task_count": len(resolved_tasks),
        "outcome_counts": outcome_counts,
    }


def build_review_source_ranker_prompt(
    task: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Build the LLM fill-in contract for semantic candidate selection.

    The model returns only selections/reasons keyed by task_id and
    candidate_id; every fixed field in the trace task is locally owned.
    """
    prompt_path = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "Review Source Unpacker.txt"
    )
    system = prompt_path.read_text(encoding="utf-8")
    payload = {
        "task": {
            "task_id": task.get("task_id", ""),
            "claim_id": task.get("claim_id", ""),
            "claim_statement": task.get("claim_statement", ""),
            "review_paper_id": task.get("review_paper_id", ""),
            "review_unit_id": task.get("review_unit_id", ""),
            "review_title": task.get("review_title", ""),
            "exact_quote": task.get("exact_quote", ""),
            "surrounding_excerpt": task.get("surrounding_excerpt", ""),
            "citation_markers": task.get("citation_markers") or [],
            "citation_numbers": task.get("citation_numbers") or [],
            "citation_marker_associations": (
                task.get("citation_marker_associations") or []
            ),
            "nearby_citation_markers": (
                task.get("nearby_citation_markers") or []
            ),
            "nearby_citation_numbers": (
                task.get("nearby_citation_numbers") or []
            ),
        },
        "candidates": [
            {
                "candidate_id": _candidate_key(candidate)
                or str(index),
                "paper_id": candidate.get("paper_id", ""),
                "title": candidate.get("title", ""),
                "doi": candidate.get("doi", ""),
                "year": candidate.get("year", ""),
                "source_kind": candidate.get("source_kind", ""),
                "content_depth": candidate.get("content_depth", ""),
            }
            for index, candidate in enumerate(candidates)
        ],
        "required_output": {
            "selections": [{
                "task_id": "exact task id",
                "candidate_id": "exact candidate id",
                "reason": (
                    "concise evidence-based reason this candidate is the "
                    "original cited source"
                ),
            }],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
