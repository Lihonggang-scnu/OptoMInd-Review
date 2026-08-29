"""Dominant-review full-reference expansion stage.

A review that dominates many verified claims is treated as an entry map: its
complete numbered bibliography is parsed and screened, and retained original
studies are turned into acquisition requests for the existing
S2-body -> public-OA-fulltext -> true-abstract materialization path.

This module is network-free and performs no retrieval.  Local code owns every
fixed schema/audit field; a screening model, when injected, returns only
high-information fill-in decisions keyed by reference number.
"""

from __future__ import annotations

import hashlib
import json
import re
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from optomind_research.review_source_unpacking import (
    EXPANSION_QUOTA_CLASS,
    REVIEW_SOURCE_KINDS,
    expand_citation_marker,
    extract_citation_markers,
    review_source_signal,
)

__all__ = [
    "SCREENING_CONTRACT_VERSION",
    "ACQUISITION_PRIORITY_ORDER",
    "SCREENING_BATCH_SIZE_DEFAULT",
    "DominantReviewExpansionInput",
    "build_dominant_review_input",
    "extract_reference_identity",
    "collect_mention_contexts",
    "build_reference_cards",
    "merge_enriched_metadata",
    "build_screening_batches",
    "screening_coverage_audit",
    "build_screening_batch_prompt",
    "merge_screening_decisions",
    "evidence_precedence_contract",
    "build_acquisition_requests",
    "deduplicate_reference_records",
    "run_dominant_review_expansion",
    "SOURCE_EXPANSION_TYPES",
    "SOURCE_EXPANSION_TRIGGER_THRESHOLD",
    "classify_source_expansion_type",
    "source_expansion_policy",
    "plan_source_expansion_triggers",
    "source_expansion_to_expansion_input",
    "DOMINANT_REVIEW_TRIGGER_MANIFEST_VERSION",
    "extract_section_paper_claim_index",
    "build_dominant_review_trigger_manifest",
    "reconstruct_cached_review_document",
    "build_cached_review_document_map",
    "confirm_dominant_review_source",
]

SCREENING_CONTRACT_VERSION = "dominant_review_reference_screening.v1"
DOMINANT_REVIEW_TRIGGER_MANIFEST_VERSION = (
    "dominant_review_trigger_manifest.v1"
)
ACQUISITION_PRIORITY_ORDER = (
    "s2_structured_body",
    "public_oa_fulltext",
    "abstract_claim",
)
SCREENING_BATCH_SIZE_DEFAULT = 16
ACQUISITION_PRIORITY_LEVELS = frozenset({"high", "medium", "low"})
EVIDENCE_ROLES = frozenset({
    "central_fact",
    "method_or_measurement",
    "comparison",
    "boundary_or_limitation",
    "background",
})
SCREEN_STATUSES = frozenset({"kept", "skipped", "pending_review"})
SOURCE_EXPANSION_TYPES = frozenset({
    "review_unbundling",
    "empirical_antecedent_expansion",
    "unknown_source_expansion",
})
SOURCE_EXPANSION_TRIGGER_THRESHOLD = 0.10


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _clean_id(value: Any) -> str:
    return _clean_text(value)


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


@dataclass
class DominantReviewExpansionInput:
    """Complete input contract for one dominant-review expansion run."""

    user_question: str
    dynamic_axes: list[str] = field(default_factory=list)
    section_workplan: Any = field(default_factory=dict)
    current_section_tasks: list[Any] = field(default_factory=list)
    review_identity: Mapping[str, Any] = field(default_factory=dict)
    review_body: str = ""
    bibliography: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    claim_local_marker_associations: Mapping[int, list[Mapping[str, Any]]] = (
        field(default_factory=dict)
    )
    enriched_metadata: Mapping[str, Any] = field(default_factory=dict)
    quota_class: str = EXPANSION_QUOTA_CLASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_question": self.user_question,
            "dynamic_axes": list(self.dynamic_axes),
            "section_workplan": copy.deepcopy(self.section_workplan),
            "current_section_tasks": copy.deepcopy(self.current_section_tasks),
            "review_identity": dict(self.review_identity),
            "review_body": self.review_body,
            "bibliography": {
                int(number): dict(entry)
                for number, entry in self.bibliography.items()
            },
            "claim_local_marker_associations": {
                int(number): [dict(row) for row in rows]
                for number, rows in self.claim_local_marker_associations.items()
            },
            "enriched_metadata": dict(self.enriched_metadata),
            "quota_class": self.quota_class,
        }


def build_dominant_review_input(
    *,
    user_question: str,
    dynamic_axes: Optional[Iterable[str]] = None,
    section_workplan: Any = None,
    current_section_tasks: Optional[Iterable[Any]] = None,
    review_identity: Optional[Mapping[str, Any]] = None,
    review_body: str = "",
    bibliography: Optional[Mapping[int, Mapping[str, Any]]] = None,
    claim_local_marker_associations: Optional[
        Mapping[int, Sequence[Mapping[str, Any]]]
    ] = None,
    enriched_metadata: Optional[Mapping[str, Any]] = None,
    quota_class: str = EXPANSION_QUOTA_CLASS,
) -> DominantReviewExpansionInput:
    """Build the validated expansion input contract."""
    return DominantReviewExpansionInput(
        user_question=_clean_text(user_question),
        dynamic_axes=[_clean_text(value) for value in dynamic_axes or ()],
        section_workplan=copy.deepcopy(
            section_workplan if section_workplan is not None else {}
        ),
        current_section_tasks=[
            copy.deepcopy(value) for value in current_section_tasks or ()
        ],
        review_identity=dict(review_identity or {}),
        review_body=str(review_body or ""),
        bibliography={
            int(number): dict(entry)
            for number, entry in (bibliography or {}).items()
        },
        claim_local_marker_associations={
            int(number): [dict(row) for row in rows]
            for number, rows in (
                claim_local_marker_associations or {}
            ).items()
        },
        enriched_metadata=dict(enriched_metadata or {}),
        quota_class=_clean_id(quota_class) or EXPANSION_QUOTA_CLASS,
    )


_DOI_RE = re.compile(r"(?i)\b10\.\d{4,9}/[^\s,;]+")
_ARXIV_RE = re.compile(r"(?i)\b(?:arxiv:|arxiv\.org/abs/)(\d{4}\.\d{4,5})")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_QUOTED_TITLE_RE = re.compile(r"[\"']([^\"']{8,200})[\"']")


def _trim_trailing_punctuation(value: str) -> str:
    return value.rstrip(".,;:)]} ")


def extract_reference_identity(
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract stable local identity from one bibliography entry."""
    raw_text = str(entry.get("raw_text") or "")
    candidate_text = str(
        entry.get("candidate_text")
        or entry.get("cleaned_text")
        or entry.get("raw_text")
        or ""
    )
    first_line = str(entry.get("first_line") or "")
    doi_match = _DOI_RE.search(raw_text)
    doi = (
        _trim_trailing_punctuation(doi_match.group(0))
        if doi_match
        else ""
    )
    arxiv_match = _ARXIV_RE.search(raw_text)
    arxiv_id = arxiv_match.group(1) if arxiv_match else ""
    title_match = _QUOTED_TITLE_RE.search(raw_text)
    title = (
        title_match.group(1).rstrip(".,;:")
        if title_match
        else ""
    )
    if not title:
        title = candidate_text[:180]
    year_match = _YEAR_RE.search(raw_text)
    year = year_match.group(1) if year_match else ""
    author_source = re.sub(r"^\[[0-9]+\][ \t]*", "", first_line).strip()
    author_source = re.split(r",[\s]*[\"']", author_source, maxsplit=1)[0]
    authors = _trim_trailing_punctuation(author_source)[:200]
    batch_lookup_ids: list[str] = []
    if doi:
        batch_lookup_ids.append(f"DOI:{doi}")
    if arxiv_id:
        batch_lookup_ids.append(f"ARXIV:{arxiv_id}")
    return {
        "doi": doi,
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": year,
        "batch_lookup_ids": batch_lookup_ids,
        "lookup_ids_empty": not batch_lookup_ids,
        "candidate_text": candidate_text,
        "first_line": first_line,
    }


def deduplicate_reference_records(
    records: Mapping[int, Mapping[str, Any]],
    *,
    dedupe_keys: Sequence[str] = ("doi", "arxiv_id", "title"),
) -> tuple[dict[int, Mapping[str, Any]], dict[str, Any]]:
    """Deduplicate numbered bibliography records with auditable reasons.

    First occurrence wins; later records sharing a DOI, arXiv id, or
    normalized title are excluded and audited with ``duplicate_identity``.
    Unique records are all kept — no admission/quota cap applies.
    """
    seen: dict[str, int] = {}
    kept: dict[int, Mapping[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    for number in sorted(records):
        number = int(number)
        record = records[number] or {}
        identity = (
            record.get("identity")
            if isinstance(record.get("identity"), Mapping)
            else extract_reference_identity(record)
        )
        keys: list[str] = []
        for key in dedupe_keys:
            if key == "title":
                value = _clean_text(identity.get("title") or record.get("title"))
                value = value.casefold()
            else:
                value = _clean_id(identity.get(key) or record.get(key))
            if value:
                keys.append(f"{key}:{value}")
        if not keys:
            kept[number] = dict(record)
            continue
        matching_key = ""
        duplicate_of: Optional[int] = None
        for candidate in keys:
            if candidate in seen:
                matching_key = candidate
                duplicate_of = seen[candidate]
                break
        if duplicate_of is not None:
            excluded.append({
                "reference_number": number,
                "duplicate_of": duplicate_of,
                "reason": "duplicate_identity",
                "matching_key": matching_key,
            })
            continue
        for candidate in keys:
            seen.setdefault(candidate, number)
        kept[number] = dict(record)
    return kept, {
        "total_record_count": len(records),
        "kept_record_count": len(kept),
        "excluded_record_count": len(excluded),
        "excluded_reference_numbers": [
            row["reference_number"] for row in excluded
        ],
        "excluded_records": excluded,
        "admission_cap": None,
        "non_quota": True,
    }


_BIBLIOGRAPHY_HEADING_LINE_RE = re.compile(
    r"^\s*(?:references|bibliography|works cited|literature cited)\s*:?\s*$",
    re.IGNORECASE,
)
_HEADING_LINE_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?[ \t]+|\b(?:section|chapter)\b[ \t]+\d+[.:]?[ \t]+)?"
    r"[A-Z][A-Za-z0-9 ,&()/\-]{2,80}[ \t]*$"
)
_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?:[.!?]\s+(?=[A-Z\"\u201c\u201d'(\[])|[.!?]\s*$|\n\s*\n)"
)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _strip_bibliography_tail(review_body: str) -> str:
    lines = review_body.splitlines(keepends=True)
    cut = len(review_body)
    offset = 0
    for line in lines:
        if _BIBLIOGRAPHY_HEADING_LINE_RE.match(line):
            cut = offset
        offset += len(line)
    return review_body[:cut]


def collect_mention_contexts(
    review_body: str,
    *,
    max_contexts_per_number: int = 5,
    context_chars: int = 300,
) -> Mapping[int, list[Mapping[str, Any]]]:
    """Collect all in-review mention contexts for each reference number.

    The review body is truncated at the first References/Bibliography heading
    so body sentences before the bibliography are used.  Each mention carries
    the nearby section heading/path, a bounded surrounding sentence, and the
    marker text/offset.
    """
    body = _strip_bibliography_tail(review_body)
    lines = body.splitlines(keepends=True)
    heading_paths: list[tuple[int, str]] = []
    offset = 0
    for line in lines:
        stripped = line.strip()
        if (
            stripped
            and not stripped.endswith((".", "!", "?"))
            and _HEADING_LINE_RE.match(line)
            and not _BIBLIOGRAPHY_HEADING_LINE_RE.match(line)
        ):
            heading_paths.append((offset, _clean_text(stripped)))
        offset += len(line)
    sentence_spans = _sentence_spans(body)
    mentions_by_number: dict[int, list[Mapping[str, Any]]] = {}
    for marker in extract_citation_markers(body):
        for number in marker.get("numbers") or []:
            mentions = mentions_by_number.setdefault(int(number), [])
            if len(mentions) >= max_contexts_per_number:
                continue
            sentence = ""
            for start, end in sentence_spans:
                if start <= marker["start"] <= end:
                    sentence = _clean_text(body[start:end])[:context_chars]
                    break
            heading = ""
            for heading_offset, heading_text in heading_paths:
                if heading_offset <= marker["start"]:
                    heading = heading_text
            mentions.append({
                "marker": marker["raw"],
                "char_offset": marker["start"],
                "section_heading": heading,
                "section_path": heading,
                "sentence": sentence,
            })
    return mentions_by_number


def build_reference_cards(
    bibliography: Mapping[int, Mapping[str, Any]],
    *,
    review_identity: Mapping[str, Any],
    review_body: str,
    claim_local_marker_associations: Optional[
        Mapping[int, Sequence[Mapping[str, Any]]]
    ] = None,
    mention_contexts: Optional[Mapping[int, Sequence[Mapping[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """Build one reference card per bibliography entry (complete coverage)."""
    mentions = (
        dict(mention_contexts or {})
        if mention_contexts is not None
        else collect_mention_contexts(review_body)
    )
    claim_local = {
        int(number): [dict(row) for row in rows]
        for number, rows in (
            claim_local_marker_associations or {}
        ).items()
    }
    cards: list[dict[str, Any]] = []
    for number in sorted(bibliography):
        entry = dict(bibliography[int(number)] or {})
        contexts = [
            dict(row) for row in mentions.get(int(number), [])
        ]
        cards.append({
            "reference_number": int(number),
            "raw_text": str(entry.get("raw_text") or ""),
            "candidate_text": str(
                entry.get("candidate_text")
                or entry.get("cleaned_text")
                or entry.get("raw_text")
                or ""
            ),
            "identity": extract_reference_identity(entry),
            "mention_contexts": contexts,
            "mention_context_total": len(contexts),
            "claim_local_priority_signals": list(
                claim_local.get(int(number), [])
            ),
            "enriched": {},
            "screen": {
                "status": "pending_review",
                "decision": None,
            },
            "conflict_status": "pending_primary_check",
            "evidence_precedence": "original_primary",
            "review_identity": dict(review_identity),
        })
    return cards


def merge_enriched_metadata(
    cards: Sequence[Mapping[str, Any]],
    enriched_by_identifier: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Merge enriched S2/OpenAlex metadata into reference cards.

    Keys may be reference numbers (int/str), ``DOI:<id>``/``ARXIV:<id>`` batch
    lookup ids, raw DOI/arXiv strings, or S2/OpenAlex ids.  Cards without
    metadata are preserved unchanged.
    """
    merged_cards: list[dict[str, Any]] = []
    normalized: dict[str, Any] = {}
    for key, value in (enriched_by_identifier or {}).items():
        normalized[str(key).strip().casefold()] = value
        if _is_int(key):
            normalized[str(int(key))] = value
    for card in cards:
        card = dict(card)
        identity = dict(card.get("identity") or {})
        candidates = [
            str(card.get("reference_number") or ""),
            *(str(value) for value in identity.get("batch_lookup_ids") or []),
            str(identity.get("doi") or ""),
            str(identity.get("arxiv_id") or ""),
            str(identity.get("s2_paper_id") or ""),
            str(identity.get("openalex_id") or ""),
        ]
        enriched: dict[str, Any] = {}
        for key in candidates:
            key = key.strip().casefold()
            if not key or key not in normalized:
                continue
            value = normalized[key]
            if isinstance(value, Mapping):
                enriched.update(value)
            else:
                enriched["metadata"] = value
            break
        if enriched.get("title"):
            identity["title"] = str(enriched["title"])
        if enriched.get("s2_paper_id"):
            identity["s2_paper_id"] = str(enriched["s2_paper_id"])
        if enriched.get("openalex_id"):
            identity["openalex_id"] = str(enriched["openalex_id"])
        card["identity"] = identity
        card["enriched"] = enriched
        merged_cards.append(card)
    return merged_cards


def build_screening_batches(
    cards: Sequence[Mapping[str, Any]],
    *,
    batch_size: int = SCREENING_BATCH_SIZE_DEFAULT,
) -> list[dict[str, Any]]:
    """Partition the complete bibliography into bounded screening batches."""
    batch_size = max(1, int(batch_size))
    batches: list[dict[str, Any]] = []
    for index in range(0, len(cards), batch_size):
        chunk = list(cards[index:index + batch_size])
        batches.append({
            "batch_index": len(batches) + 1,
            "reference_numbers": [
                int(card.get("reference_number")) for card in chunk
            ],
            "cards": chunk,
        })
    return batches


def screening_coverage_audit(
    batches: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove every reference card appears exactly once across batches."""
    expected = {
        int(card.get("reference_number")) for card in cards
    }
    seen: list[int] = []
    for batch in batches:
        seen.extend(
            int(number) for number in batch.get("reference_numbers") or []
        )
    duplicates = sorted({
        number for number in seen if seen.count(number) > 1
    })
    covered = set(seen)
    missing = sorted(expected - covered)
    return {
        "expected_count": len(expected),
        "covered_count": len(covered),
        "batch_count": len(batches),
        "batch_sizes": [len(batch.get("reference_numbers") or []) for batch in batches],
        "missing_reference_numbers": missing,
        "duplicate_reference_numbers": duplicates,
        "complete": not missing and not duplicates,
        "quota_class": EXPANSION_QUOTA_CLASS,
        "non_quota": True,
        "no_admission_cap": True,
    }


def expansion_quota_accounting(
    *,
    total_reference_count: int,
    deduplicated_reference_count: int,
    screened_in_count: int,
    pending_count: int = 0,
    ordinary_quota_decrement: int = 0,
) -> dict[str, Any]:
    """Explicit non-quota accounting for the expansion side channel."""
    return {
        "quota_class": EXPANSION_QUOTA_CLASS,
        "non_quota": True,
        "ordinary_quota_decrement": ordinary_quota_decrement,
        "admission_cap": None,
        "scientific_cap": None,
        "total_reference_count": total_reference_count,
        "deduplicated_reference_count": deduplicated_reference_count,
        "screened_in_count": screened_in_count,
        "pending_review_count": pending_count,
        "eligible_for_materialization_count": screened_in_count,
        "note": (
            "Dominant-source reference unpacking is a separate non-quota "
            "side channel.  It does not replace or shrink normal first-pass "
            "retrieval and does not consume ordinary chapter candidate, "
            "supplementary-retrieval, or per-route admission slots.  "
            "Operational batching/rate limiting is allowed and must process "
            "all screened-in references eventually; it is never reported as "
            "a scientific/admission cap."
        ),
    }


def _screening_card_payload(card: Mapping[str, Any]) -> dict[str, Any]:
    identity = card.get("identity") or {}
    enriched = card.get("enriched") or {}
    return {
        "reference_number": int(card.get("reference_number")),
        "candidate_text": str(card.get("candidate_text") or ""),
        "identity": {
            "doi": identity.get("doi", ""),
            "arxiv_id": identity.get("arxiv_id", ""),
            "title": identity.get("title", ""),
            "authors": identity.get("authors", ""),
            "year": identity.get("year", ""),
            "batch_lookup_ids": identity.get("batch_lookup_ids") or [],
        },
        "mention_contexts": list(card.get("mention_contexts") or []),
        "claim_local_priority_signals": list(
            card.get("claim_local_priority_signals") or []
        ),
        "enriched": {
            "title": enriched.get("title", ""),
            "abstract": enriched.get("abstract", ""),
            "s2_paper_id": enriched.get("s2_paper_id", ""),
            "openalex_id": enriched.get("openalex_id", ""),
        },
    }


def _normalize_workplan_sections(section_workplan: Any) -> list[Any]:
    """Extract a section list from Mapping or Sequence inputs without loss."""
    if isinstance(section_workplan, Mapping):
        nested = section_workplan.get("sections")
        if isinstance(nested, (list, tuple)):
            return list(nested)
        if isinstance(nested, Mapping):
            return [nested]
        return [section_workplan]
    if isinstance(section_workplan, (list, tuple)):
        return list(section_workplan)
    return []


def _bounded_section(section: Any, *, field_limit: int = 400) -> dict[str, Any]:
    """One bounded section payload carrying the fixed division-of-labor keys."""
    if isinstance(section, str):
        return {"title": section[:field_limit]}
    if not isinstance(section, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "section_id",
        "id",
        "title",
        "argument_role",
        "must_cover",
        "must_not_cover",
        "key_questions",
    ):
        value = section.get(key)
        if isinstance(value, list):
            out[key] = [
                str(item)[:field_limit]
                for item in value
                if str(item).strip()
            ][:40]
        elif value is not None:
            out[key] = str(value)[:field_limit]
        else:
            out[key] = ""
    return out


def _section_workplan_payload(section_workplan: Any) -> dict[str, Any]:
    sections = _normalize_workplan_sections(section_workplan)
    return {
        "sections": [
            _bounded_section(section) for section in sections
        ],
        "section_count": len(sections),
    }


def _section_tasks_payload(current_section_tasks: Any) -> list[Any]:
    tasks: list[Any] = []
    for task in current_section_tasks or []:
        if isinstance(task, Mapping):
            tasks.append({
                key: value
                for key, value in task.items()
                if key in {
                    "task_id",
                    "section_id",
                    "task",
                    "objective",
                    "status",
                }
            })
        elif isinstance(task, str):
            tasks.append(task[:400])
    return tasks


def build_screening_batch_prompt(
    batch: Mapping[str, Any],
    *,
    user_question: str,
    dynamic_axes: Sequence[str],
    section_workplan: Mapping[str, Any],
    current_section_tasks: Sequence[str],
    review_identity: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Build one screening payload; the model fills only decision fields."""
    prompt_path = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "Dominant Review Reference Screener.txt"
    )
    system = prompt_path.read_text(encoding="utf-8")
    payload = {
        "screening_contract_version": SCREENING_CONTRACT_VERSION,
        "user_question": user_question,
        "dynamic_axes": list(dynamic_axes),
        "section_workplan": _section_workplan_payload(section_workplan),
        "current_section_tasks": _section_tasks_payload(current_section_tasks),
        "review_identity": dict(review_identity),
        "reference_cards": [
            _screening_card_payload(card)
            for card in batch.get("cards") or []
        ],
        "policy": {
            "permissive": True,
            "no_top_n_quota": True,
            "quota_class": EXPANSION_QUOTA_CLASS,
            "non_quota": True,
            "no_admission_cap": True,
            "acquisition_priority_order": list(ACQUISITION_PRIORITY_ORDER),
            "evidence_precedence": "original_primary",
            "conflict_status": "pending_primary_check",
        },
        "required_output": {
            "decisions": [{
                "reference_number": "exact reference number",
                "relevance_score": "0-100",
                "keep": "true|false",
                "useful_axes": ["axis"],
                "useful_sections": ["section"],
                "likely_evidence_roles": [
                    "central_fact|method_or_measurement|comparison|"
                    "boundary_or_limitation|background"
                ],
                "acquisition_priority": "high|medium|low",
                "reason": "concise evidence-based reason",
            }],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _normalize_keep(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "keep", "yes", "1"}:
            return True
        if lowered in {"false", "skip", "no", "0"}:
            return False
    return None


def merge_screening_decisions(
    cards: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge model decisions; malformed/missing rows stay pending_review."""
    card_by_number = {
        int(card.get("reference_number")): card for card in cards
    }
    pending: list[int] = []
    duplicate_numbers: list[int] = []
    seen: set[int] = set()
    for row in decisions:
        if not isinstance(row, Mapping):
            continue
        number_value = row.get("reference_number")
        if not _is_int(number_value):
            continue
        number = int(number_value)
        if number in seen:
            duplicate_numbers.append(number)
            continue
        seen.add(number)
        card = card_by_number.get(number)
        if card is None:
            continue
        score = row.get("relevance_score")
        keep = _normalize_keep(row.get("keep"))
        priority = _clean_id(row.get("acquisition_priority")).casefold()
        reason = str(row.get("reason") or "").strip()
        roles = row.get("likely_evidence_roles")
        valid = (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and 0 <= float(score) <= 100
            and keep is not None
            and priority in ACQUISITION_PRIORITY_LEVELS
            and isinstance(roles, list)
            and bool(reason)
        )
        if not valid:
            pending.append(number)
            continue
        card["screen"] = {
            "status": "kept" if keep else "skipped",
            "decision": {
                "reference_number": number,
                "relevance_score": float(score),
                "keep": keep,
                "useful_axes": [
                    _clean_text(value)
                    for value in (row.get("useful_axes") or [])
                    if _clean_text(value)
                ],
                "useful_sections": [
                    _clean_text(value)
                    for value in (row.get("useful_sections") or [])
                    if _clean_text(value)
                ],
                "likely_evidence_roles": [
                    _clean_id(value)
                    for value in roles
                    if _clean_id(value) in EVIDENCE_ROLES
                ],
                "acquisition_priority": priority,
                "reason": reason,
            },
        }
    for number, card in card_by_number.items():
        if card.get("screen", {}).get("status") == "pending_review":
            pending.append(number)
    pending = sorted(set(pending))
    counts = {
        status: sum(
            1
            for card in card_by_number.values()
            if (card.get("screen") or {}).get("status") == status
        )
        for status in SCREEN_STATUSES
    }
    return {
        "cards": [dict(card_by_number[number]) for number in sorted(card_by_number)],
        "audit": {
            "all_reference_numbers": sorted(card_by_number),
            "preserved_record_count": len(card_by_number),
            "status_counts": counts,
            "pending_reference_numbers": pending,
            "duplicate_decision_numbers": sorted(set(duplicate_numbers)),
            "decision_row_count": len(decisions),
        },
    }


def evidence_precedence_contract() -> dict[str, Any]:
    """Original-over-review evidence precedence contract."""
    return {
        "evidence_precedence": "original_primary",
        "original_paper_controls": [
            "factual claims",
            "method claims",
            "measurement claims",
        ],
        "review_secondary_roles": [
            "synthesis",
            "history",
            "context",
        ],
        "never_overwrite": True,
        "conflict_status": "pending_primary_check",
        "conflict_handling": (
            "If the review synthesis conflicts with the cited original "
            "paper's verified text, the original paper controls factual, "
            "method, and measurement claims.  The review remains secondary "
            "evidence for synthesis/history only.  Neither source is silently "
            "overwritten; downstream comparison marks conflict_status."
        ),
    }


def _card_open_access_url(card: Mapping[str, Any]) -> str:
    enriched = card.get("enriched") if isinstance(card.get("enriched"), Mapping) else {}
    identity = card.get("identity") if isinstance(card.get("identity"), Mapping) else {}
    for source in (card, enriched, identity):
        for key in (
            "open_access_url",
            "openAccessPdf",
            "pdf_url",
            "oa_url",
            "s2_open_access_candidate_url",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def build_acquisition_requests(
    cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Emit acquisition requests for kept records only."""
    requests: list[dict[str, Any]] = []
    for card in cards:
        if (card.get("screen") or {}).get("status") != "kept":
            continue
        identity = dict(card.get("identity") or {})
        enriched = dict(card.get("enriched") or {})
        decision = (card.get("screen") or {}).get("decision") or {}
        open_access_url = _card_open_access_url(card)
        verified_identifiers = {
            "doi": identity.get("doi") or enriched.get("doi", ""),
            "arxiv_id": (
                identity.get("arxiv_id") or enriched.get("arxiv_id", "")
            ),
            "s2_paper_id": (
                identity.get("s2_paper_id")
                or enriched.get("s2_paper_id", "")
            ),
            "openalex_id": (
                identity.get("openalex_id")
                or enriched.get("openalex_id", "")
            ),
        }
        requests.append({
            "reference_number": int(card.get("reference_number")),
            "identity": {
                "doi": identity.get("doi", ""),
                "arxiv_id": identity.get("arxiv_id", ""),
                "title": identity.get("title", ""),
                "authors": identity.get("authors", ""),
                "year": identity.get("year", ""),
                "batch_lookup_ids": list(
                    identity.get("batch_lookup_ids") or []
                ),
                "s2_paper_id": identity.get("s2_paper_id", ""),
                "openalex_id": identity.get("openalex_id", ""),
            },
            "verified_identifiers": verified_identifiers,
            "enriched": {
                "title": enriched.get("title", ""),
                "abstract": enriched.get("abstract", ""),
                "s2_paper_id": enriched.get("s2_paper_id", ""),
                "openalex_id": enriched.get("openalex_id", ""),
                "doi": identity.get("doi", ""),
                "arxiv_id": identity.get("arxiv_id", ""),
                "year": enriched.get("year", identity.get("year", "")),
                "authors": enriched.get("authors", identity.get("authors", "")),
                "venue": enriched.get("venue", ""),
                "external_ids": dict(enriched.get("external_ids") or {}),
                "open_access_url": open_access_url,
            },
            "open_access_url": open_access_url,
            "query_text": str(card.get("candidate_text") or ""),
            "acquisition_priority": decision.get("acquisition_priority", "low"),
            "acquisition_priority_order": list(ACQUISITION_PRIORITY_ORDER),
            "useful_axes": list(decision.get("useful_axes") or []),
            "useful_sections": list(decision.get("useful_sections") or []),
            "likely_evidence_roles": list(
                decision.get("likely_evidence_roles") or []
            ),
            "reason": str(decision.get("reason") or ""),
            "relevance_score": float(decision.get("relevance_score") or 0.0),
            "conflict_status": str(card.get("conflict_status") or "pending_primary_check"),
            "evidence_precedence": str(
                card.get("evidence_precedence") or "original_primary"
            ),
            "review_secondary": dict(card.get("review_identity") or {}),
        })
    return {
        "requests": requests,
        "audit": {
            "kept_request_count": len(requests),
            "priority_order": list(ACQUISITION_PRIORITY_ORDER),
            "quota_class": EXPANSION_QUOTA_CLASS,
            "non_quota": True,
            "ordinary_quota_decrement": 0,
            "admission_cap": None,
            "eligible_for_materialization_count": len(requests),
        },
        "quota_accounting": expansion_quota_accounting(
            total_reference_count=len(cards),
            deduplicated_reference_count=len(cards),
            screened_in_count=len(requests),
            pending_count=sum(
                1
                for card in cards
                if (card.get("screen") or {}).get("status")
                == "pending_review"
            ),
        ),
    }


def run_dominant_review_expansion(
    expansion_input: DominantReviewExpansionInput,
    *,
    screen_decisions_call: Optional[
        Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]
    ] = None,
    batch_size: int = SCREENING_BATCH_SIZE_DEFAULT,
) -> dict[str, Any]:
    """Run the full dominant-review expansion orchestration (network-free core).

    ``screen_decisions_call`` is the only model-facing injection point; it
    receives one screening batch payload and returns fill-in decision rows.
    When omitted, every record stays ``pending_review`` audibly.
    """
    cards = build_reference_cards(
        expansion_input.bibliography,
        review_identity=expansion_input.review_identity,
        review_body=expansion_input.review_body,
        claim_local_marker_associations=(
            expansion_input.claim_local_marker_associations
        ),
    )
    cards = merge_enriched_metadata(
        cards, expansion_input.enriched_metadata
    )
    batches = build_screening_batches(cards, batch_size=batch_size)
    coverage = screening_coverage_audit(batches, cards)
    decisions: list[dict[str, Any]] = []
    batch_records: list[dict[str, Any]] = []
    if screen_decisions_call is not None:
        for batch in batches:
            rows = screen_decisions_call(batch) or []
            decisions.extend(
                dict(row) for row in rows if isinstance(row, Mapping)
            )
            batch_records.append({
                "batch_index": batch.get("batch_index"),
                "reference_numbers": batch.get("reference_numbers"),
                "decision_row_count": len(rows),
            })
    merged = merge_screening_decisions(cards, decisions)
    acquisition = build_acquisition_requests(merged["cards"])
    quota_accounting = expansion_quota_accounting(
        total_reference_count=len(cards),
        deduplicated_reference_count=len(cards),
        screened_in_count=(
            merged["audit"]["status_counts"].get("kept") or 0
        ),
        pending_count=(
            merged["audit"]["status_counts"].get("pending_review") or 0
        ),
    )
    return {
        "contract_version": SCREENING_CONTRACT_VERSION,
        "input": expansion_input.to_dict(),
        "coverage_audit": coverage,
        "batch_records": batch_records,
        "cards": merged["cards"],
        "screening_audit": merged["audit"],
        "acquisition": acquisition,
        "quota_accounting": quota_accounting,
        "evidence_precedence": evidence_precedence_contract(),
    }


def classify_source_expansion_type(
    source_id: Any,
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Classify a source's expansion type from review-like or empirical signals.

    Reviews/roadmaps/perspectives unpack full bibliographies
    (``review_unbundling``).  Explicit empirical/original-research sources get
    ``empirical_antecedent_expansion`` (their own findings stay primary and
    their references are antecedents/comparators/background).  Anything
    without a clear signal is ``unknown_source_expansion``.
    """
    metadata = metadata or {}
    unit = {
        "identity": {
            "title": metadata.get("title") or "",
        },
        "work_type": (
            metadata.get("source_type") or metadata.get("work_type")
        ),
        "publication_type": metadata.get("publication_type"),
        "publicationTypes": metadata.get("publicationTypes"),
        "publication_types": metadata.get("publication_types"),
        "raw_metadata": metadata.get("raw_metadata") or {},
        "durable_content_card": {
            "content_quality": {
                "source_kind": metadata.get("source_kind") or "",
            }
        },
    }
    if review_source_signal(unit) is not None:
        return "review_unbundling"
    type_text = " ".join([
        str(metadata.get("source_type") or ""),
        str(metadata.get("work_type") or ""),
        str(metadata.get("publication_type") or ""),
    ]).casefold()
    if any(
        token in type_text
        for token in (
            "empirical",
            "original research",
            "primary study",
            "research article",
        )
    ):
        return "empirical_antecedent_expansion"
    return "unknown_source_expansion"


def source_expansion_policy(source_type: str) -> dict[str, Any]:
    """Per-type evidence/reference policy for a source expansion task."""
    base = {
        "never_overwrite": True,
        "conflict_status": "pending_primary_check",
        "original_primary_on_conflict": True,
    }
    if source_type == "review_unbundling":
        return {
            **base,
            "role": "review_secondary_for_synthesis_history",
            "reference_role": "original_source_recovery",
        }
    if source_type == "empirical_antecedent_expansion":
        return {
            **base,
            "role": "empirical_primary_for_own_findings",
            "reference_role": (
                "antecedents_comparators_background_only; references must "
                "never overwrite the empirical paper's own findings"
            ),
        }
    return {
        **base,
        "role": "unknown_source_conservative",
        "reference_role": (
            "antecedents_background_only; no source overwrite"
        ),
    }


def _normalize_claim_id(value: Any) -> str:
    if isinstance(value, Mapping):
        return _clean_id(value.get("claim_id") or value.get("id"))
    return _clean_id(value)


def _is_final_verified_claim(value: Any) -> bool:
    """Bare ids are already-filtered callers; mappings must be verified/ready."""
    if not isinstance(value, Mapping):
        return True
    if "ready_for_write" in value:
        return bool(value.get("ready_for_write"))
    if "verified" in value and isinstance(value.get("verified"), bool):
        return bool(value.get("verified"))
    status = _clean_id(value.get("status")).casefold()
    if status:
        return status in {"verified", "pass", "accepted", "ready"}
    return False


def _normalize_source_ids(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {
            _clean_id(item) for item in value if _clean_id(item)
        }
    source_id = _clean_id(value)
    return {source_id} if source_id else set()


def _source_bibliography_status(
    source_id: str,
    metadata: Mapping[str, Any],
    bibliography_by_source: Mapping[str, Any],
) -> tuple[Optional[Mapping[int, Mapping[str, Any]]], str, Optional[str]]:
    value = bibliography_by_source.get(source_id)
    if value is None:
        value = metadata.get("bibliography")
    if value is None:
        return None, "missing", "missing_bibliography"
    if not isinstance(value, Mapping):
        return None, "unparseable", "bibliography_not_a_mapping"
    if "error" in value and not any(
        _is_int(key) for key in value
    ):
        return None, "unparseable", str(
            value.get("error") or "unparseable_bibliography"
        )
    entries = {
        int(key): dict(row)
        for key, row in value.items()
        if _is_int(key) and isinstance(row, Mapping)
    }
    if not entries:
        return None, "missing", "empty_bibliography"
    return entries, "obtainable", None


def plan_source_expansion_triggers(
    section_claims: Iterable[Any],
    claim_source_index: Mapping[Any, Any],
    *,
    source_metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    bibliography_by_source: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Plan one independent reference-unpacking task per qualifying source.

    A source triggers when it supports strictly more than 10% of the section's
    distinct final verified claim ids.  Missing/unparseable bibliographies are
    recorded as nonblocking skips.  Triggered tasks carry the parsed complete
    bibliography and can feed the existing full-reference screener without a
    top-N quota.
    """
    denominator = sorted({
        claim_id
        for value in section_claims
        for claim_id in [_normalize_claim_id(value)]
        if claim_id and _is_final_verified_claim(value)
    })
    normalized_index: dict[str, set[str]] = {}
    for raw_claim, raw_sources in (claim_source_index or {}).items():
        claim_id = _normalize_claim_id(raw_claim)
        sources = _normalize_source_ids(raw_sources)
        if not claim_id or not sources:
            continue
        normalized_index.setdefault(claim_id, set()).update(sources)
    all_sources = sorted({
        source_id
        for sources in normalized_index.values()
        for source_id in sources
    })
    source_metadata = source_metadata or {}
    bibliography_by_source = bibliography_by_source or {}
    denominator_count = len(denominator)
    source_rows: list[dict[str, Any]] = []
    triggered_tasks: list[dict[str, Any]] = []
    for source_id in all_sources:
        claim_ids = sorted({
            claim_id
            for claim_id, sources in normalized_index.items()
            if source_id in sources
        } & set(denominator))
        claim_count = len(claim_ids)
        raw_share = (
            claim_count / denominator_count
            if denominator_count
            else 0.0
        )
        triggered = (
            denominator_count > 0
            and raw_share > SOURCE_EXPANSION_TRIGGER_THRESHOLD
        )
        claim_share = round(raw_share, 4)
        metadata = dict(source_metadata.get(source_id) or {})
        source_type = classify_source_expansion_type(source_id, metadata)
        bibliography, ref_status, ref_skip_reason = (
            _source_bibliography_status(
                source_id, metadata, bibliography_by_source
            )
        )
        skip_reason: Optional[str] = None
        if not triggered:
            skip_reason = "below_threshold"
        elif ref_status != "obtainable":
            skip_reason = ref_skip_reason
        row = {
            "source_id": source_id,
            "source_type": source_type,
            "source_title": str(metadata.get("title") or source_id),
            "claim_count": claim_count,
            "section_claim_count": denominator_count,
            "claim_share": claim_share,
            "trigger_rule": "strictly_more_than_10_percent",
            "threshold": SOURCE_EXPANSION_TRIGGER_THRESHOLD,
            "triggered": triggered,
            "reference_status": ref_status,
            "skip_reason": skip_reason,
        }
        source_rows.append(row)
        if triggered and ref_status == "obtainable":
            task_id = (
                "source_expansion:"
                + hashlib.sha1(source_id.encode("utf-8")).hexdigest()[:20]
            )
            deduped_bibliography, dedup_audit = (
                deduplicate_reference_records(bibliography)
            )
            triggered_tasks.append({
                "task_id": task_id,
                "source_id": source_id,
                "source_type": source_type,
                "source_title": str(metadata.get("title") or source_id),
                "source_identity": {
                    "paper_id": source_id,
                    "title": str(metadata.get("title") or source_id),
                },
                "review_body": str(
                    metadata.get("body_text")
                    or metadata.get("review_body")
                    or ""
                ),
                "reference_bibliography": deduped_bibliography,
                "deduplication_audit": dedup_audit,
                "claim_ids": claim_ids,
                "claim_count": claim_count,
                "section_claim_count": denominator_count,
                "claim_share": claim_share,
                "can_feed_full_reference_screener": True,
                "quota_class": EXPANSION_QUOTA_CLASS,
                "non_quota": True,
                "no_admission_cap": True,
                "acquisition_contract": {
                    "priority_order": list(ACQUISITION_PRIORITY_ORDER),
                    "no_top_n_quota": True,
                    "quota_class": EXPANSION_QUOTA_CLASS,
                },
                "source_policy": source_expansion_policy(source_type),
            })
    return {
        "denominator": denominator,
        "denominator_count": denominator_count,
        "threshold": SOURCE_EXPANSION_TRIGGER_THRESHOLD,
        "trigger_rule": "strictly_more_than_10_percent",
        "source_rows": source_rows,
        "triggered_tasks": triggered_tasks,
        "skipped_sources": [
            row for row in source_rows if row["skip_reason"] is not None
        ],
        "audit": {
            "sources_considered": len(source_rows),
            "triggered_sources": sum(
                1 for row in source_rows if row["triggered"]
            ),
            "tasks_created": len(triggered_tasks),
            "skipped_sources": len(
                [row for row in source_rows if row["skip_reason"] is not None]
            ),
            "skipped_reasons": {
                reason: sum(
                    1
                    for row in source_rows
                    if row["skip_reason"] == reason
                )
                for reason in sorted({
                    row["skip_reason"]
                    for row in source_rows
                    if row["skip_reason"] is not None
                })
            },
        },
    }


def source_expansion_to_expansion_input(
    task: Mapping[str, Any],
    *,
    user_question: str = "",
    dynamic_axes: Optional[Iterable[str]] = None,
    section_workplan: Any = None,
    current_section_tasks: Optional[Iterable[Any]] = None,
) -> DominantReviewExpansionInput:
    """Turn one triggered source task into the full-reference screener input."""
    source_identity = dict(
        task.get("source_identity")
        or {"paper_id": task.get("source_id", "")}
    )
    return build_dominant_review_input(
        user_question=user_question,
        dynamic_axes=dynamic_axes or [],
        section_workplan=section_workplan,
        current_section_tasks=current_section_tasks or [],
        review_identity=source_identity,
        review_body=str(task.get("review_body") or ""),
        bibliography=dict(task.get("reference_bibliography") or {}),
    )


def extract_section_paper_claim_index(
    section: Mapping[str, Any],
) -> dict[str, Any]:
    """Index candidate claims/chunks to papers from a blueprint section.

    Reads ``candidate_claim_pool.claims`` (stable claim/proposal ids and their
    supporting chunk ids) and ``candidate_evidence_digest.chunk_index`` (or
    ``candidate_text_chunks``) to derive the chunk->paper map.  Claims without
    any mapped chunk are excluded from the denominator and audited.
    """
    digest = section.get("candidate_evidence_digest") or {}
    digest = digest if isinstance(digest, Mapping) else {}
    chunk_by_id: dict[str, dict[str, str]] = {}
    for row in digest.get("chunk_index") or []:
        if not isinstance(row, Mapping) or not row.get("chunk_id"):
            continue
        chunk_by_id.setdefault(str(row.get("chunk_id") or ""), {
            "paper_id": str(row.get("paper_id") or ""),
            "title": str(row.get("title") or ""),
        })
    for row in section.get("candidate_text_chunks") or []:
        if not isinstance(row, Mapping) or not row.get("chunk_id"):
            continue
        chunk_by_id.setdefault(str(row.get("chunk_id") or ""), {
            "paper_id": str(row.get("paper_id") or ""),
            "title": str(row.get("title") or ""),
        })
    pool = section.get("candidate_claim_pool") or {}
    pool = pool if isinstance(pool, Mapping) else {}
    claim_entries: list[dict[str, Any]] = []
    claims_without_paper_chunks: list[str] = []
    for entry in pool.get("claims") or []:
        if not isinstance(entry, Mapping):
            continue
        claim_id = _clean_id(
            entry.get("claim_id") or entry.get("claim_proposal_id")
        )
        if not claim_id:
            continue
        chunk_ids = [
            str(value)
            for value in (entry.get("supporting_text_chunk_ids") or [])
            if str(value).strip()
        ]
        claim_entries.append({
            "claim_id": claim_id,
            "chunk_ids": chunk_ids,
            "statement": _clean_text(entry.get("statement"))[:420],
        })
        if not any(chunk_by_id.get(chunk_id, {}).get("paper_id") for chunk_id in chunk_ids):
            claims_without_paper_chunks.append(claim_id)
    claim_source_index: dict[str, list[str]] = {}
    chunk_paper_index: dict[str, str] = {}
    for claim in claim_entries:
        papers: list[str] = []
        for chunk_id in claim["chunk_ids"]:
            paper_id = (chunk_by_id.get(chunk_id) or {}).get("paper_id") or ""
            if paper_id:
                chunk_paper_index[chunk_id] = paper_id
                papers.append(paper_id)
        claim_source_index[claim["claim_id"]] = sorted(set(papers))
    return {
        "claim_entries": claim_entries,
        "chunk_paper_index": chunk_paper_index,
        "claim_source_index": claim_source_index,
        "chunk_count": len(chunk_by_id),
        "claim_count": len(claim_entries),
        "claims_without_paper_chunks": claims_without_paper_chunks,
        "audit": {
            "chunk_index_rows": len(digest.get("chunk_index") or []),
            "candidate_claim_count": len(claim_entries),
            "claims_without_paper_chunks": claims_without_paper_chunks,
        },
    }


def _reference_list_acquisition_plan(
    reference_acquisition_plan: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    plan = dict(reference_acquisition_plan or {})
    defaults: dict[str, Any] = {
        "source": "trigger_review_bibliography",
        "bibliography_parser": "parse_numbered_bibliography(mode='whole_document')",
        "screening": {
            "batch_size": SCREENING_BATCH_SIZE_DEFAULT,
            "complete_coverage_required": True,
            "obviously_irrelevant_may_be_skipped_with_reason": True,
        },
        "deduplication": "first occurrence wins by DOI/arXiv/normalized title",
        "quota_class": EXPANSION_QUOTA_CLASS,
        "non_quota": True,
        "no_count_cap": True,
    }
    defaults.update(plan)
    return defaults


def _s2_enrichment_plan(reference: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(reference.get("identity") or {})
    lookup_ids = [
        str(value)
        for value in (identity.get("batch_lookup_ids") or [])
        if str(value).strip()
    ]
    return {
        "exact_match": {
            "channel": "s2_batch_papers",
            "lookup_ids": lookup_ids,
            "lookup_ids_empty": not lookup_ids,
            "no_title_guess_when_ids_unavailable": True,
        },
        "batch": {
            "endpoint": "existing_s2_batch_endpoint",
            "operational_batching": True,
            "admission_cap": None,
        },
        "oa_fulltext": {
            "route": "public_oa_fulltext",
            "fallback": "abstract_claim",
            "priority_order": list(ACQUISITION_PRIORITY_ORDER),
        },
        "failures": (
            "Lookup failures and missing identifiers are audited; no "
            "reference id is fabricated."
        ),
        "quota_class": EXPANSION_QUOTA_CLASS,
        "non_quota": True,
        "ordinary_quota_decrement": 0,
    }


def _material_cache_contract(reference: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(reference.get("identity") or {})
    return {
        "schema_version": "material_units.v1",
        "source": "dominant_review_reference_unpacking",
        "required_fields": [
            "unit_id",
            "identity.chunk_id",
            "identity.paper_id",
            "durable_content.raw_text",
            "durable_content_card.content_quality.evidence_ceiling",
            "audit.source_provenance",
        ],
        "provenance": {
            "origin": "cited reference recovered from trigger review",
            "review_secondary": True,
            "original_primary_on_conflict": True,
            "source_identity": {
                "paper_id": identity.get("paper_id") or "",
                "doi": identity.get("doi") or "",
                "arxiv_id": identity.get("arxiv_id") or "",
                "title": identity.get("title") or "",
                "batch_lookup_ids": list(
                    identity.get("batch_lookup_ids") or []
                ),
            },
        },
        "availability": {
            "bibliography_entry_present": bool(reference.get("raw_text")),
            "enriched_metadata_present": bool(reference.get("enriched")),
            "acquisition_planned": True,
        },
        "permission_ceiling": "set_by_existing_materialization_pipeline",
        "quota_class": EXPANSION_QUOTA_CLASS,
        "non_quota": True,
    }


def build_dominant_review_trigger_manifest(
    blueprint: Mapping[str, Any],
    *,
    section_index: int = 0,
    source_metadata: Optional[Mapping[str, Mapping[str, Any]]] = None,
    bibliography_by_source: Optional[Mapping[str, Any]] = None,
    reference_acquisition_plan: Optional[Mapping[str, Any]] = None,
    blueprint_path: Optional[Path] = None,
    confirmed_review_paper_ids: Optional[Iterable[str]] = None,
    document_reconstructions: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a reusable dominant-review trigger/manifest for one section.

    Every paper whose candidate-claim share is strictly above the 10% trigger
    is considered independently.  Only review/information-rich sources
    confirmed by ``classify_source_expansion_type`` proceed to unpacking;
    other triggers are audited as skips.  The manifest carries the
    reference-list acquisition plan, S2 exact/batch/OA enrichment plan, and
    output material-cache contract/provenance, all under the independent
    non-quota channel.  No reference id is fabricated when unavailable.
    """
    sections = blueprint.get("sections") or []
    if not isinstance(sections, list) or not sections:
        raise ValueError("blueprint has no sections")
    if section_index < 0 or section_index >= len(sections):
        raise IndexError(
            f"section_index {section_index} out of range 0..{len(sections)-1}"
        )
    section = sections[section_index]
    if not isinstance(section, Mapping):
        raise ValueError("section must be a mapping")
    index = extract_section_paper_claim_index(section)
    source_metadata = source_metadata or {}
    bibliography_by_source = bibliography_by_source or {}
    confirmed_review_ids = {
        str(paper_id) for paper_id in (confirmed_review_paper_ids or ())
    }
    plan = plan_source_expansion_triggers(
        section_claims=[
            claim["claim_id"] for claim in index["claim_entries"]
        ],
        claim_source_index=index["claim_source_index"],
        source_metadata=source_metadata,
        bibliography_by_source=bibliography_by_source,
    )
    unpack_tasks: list[dict[str, Any]] = []
    non_review_skips: list[dict[str, Any]] = []
    for task in plan["triggered_tasks"]:
        source_id = str(task.get("source_id") or "")
        metadata = dict(source_metadata.get(source_id) or {})
        source_type = str(task.get("source_type") or "unknown_source_expansion")
        confirmed = source_id in confirmed_review_ids
        if source_type != "review_unbundling" and not confirmed:
            non_review_skips.append({
                "source_id": source_id,
                "source_title": str(
                    task.get("source_title") or metadata.get("title") or source_id
                ),
                "claim_count": int(task.get("claim_count") or 0),
                "claim_share": float(task.get("claim_share") or 0.0),
                "source_type": source_type,
                "triggered": True,
                "skip_reason": "not_review_source",
            })
            continue
        confirmation = confirm_dominant_review_source(
            source_id,
            metadata,
            confirmed_review_paper_ids=confirmed_review_ids,
        )
        claim_ids = list(task.get("claim_ids") or [])
        claim_id_set = set(claim_ids)
        chunk_ids = sorted({
            chunk_id
            for claim in index["claim_entries"]
            if claim["claim_id"] in claim_id_set
            for chunk_id in claim["chunk_ids"]
        })
        reference_bibliography = dict(task.get("reference_bibliography") or {})
        references = [
            {
                "reference_number": int(number),
                "raw_text": str(entry.get("raw_text") or ""),
                "candidate_text": str(
                    entry.get("candidate_text")
                    or entry.get("cleaned_text")
                    or entry.get("raw_text")
                    or ""
                ),
                "identity": extract_reference_identity(entry),
                "enriched": {},
            }
            for number, entry in reference_bibliography.items()
        ]
        unpack_tasks.append({
            "task_id": str(task.get("task_id") or ""),
            "source_id": source_id,
            "source_title": str(
                task.get("source_title") or metadata.get("title") or source_id
            ),
            "source_identity": dict(task.get("source_identity") or {}),
            "source_type": (
                "review_unbundling"
                if source_type == "review_unbundling"
                else "review_unbundling_caller_confirmed"
            ),
            "review_confirmation": confirmation,
            "claim_ids": claim_ids,
            "claim_count": int(task.get("claim_count") or 0),
            "chunk_ids": chunk_ids,
            "chunk_count": len(chunk_ids),
            "section_claim_count": int(task.get("section_claim_count") or 0),
            "claim_share": float(task.get("claim_share") or 0.0),
            "reference_bibliography": reference_bibliography,
            "references": references,
            "reference_status": str(task.get("reference_status") or "obtainable"),
            "deduplication_audit": dict(
                task.get("deduplication_audit") or {}
            ),
            "reference_list_acquisition_plan": (
                _reference_list_acquisition_plan(reference_acquisition_plan)
            ),
            "s2_enrichment_plan": [
                _s2_enrichment_plan(reference) for reference in references
            ],
            "material_cache_contract": [
                _material_cache_contract(reference)
                for reference in references
            ],
            "acquisition_contract": dict(
                task.get("acquisition_contract") or {}
            ),
            "source_policy": dict(task.get("source_policy") or {}),
            "quota_class": EXPANSION_QUOTA_CLASS,
            "non_quota": True,
            "no_admission_cap": True,
            "ordinary_quota_decrement": 0,
        })
    skipped_sources: list[dict[str, Any]] = []
    for row in plan.get("skipped_sources") or []:
        normalized_row = dict(row)
        source_id = str(normalized_row.get("source_id") or "")
        # Review-type confirmation is the gate: a triggered source that is
        # not review-like is skipped as such even when its bibliography is
        # also unavailable; the bibliography failure stays audited.
        if (
            normalized_row.get("triggered")
            and source_id not in confirmed_review_ids
            and str(normalized_row.get("source_type") or "")
            != "review_unbundling"
        ):
            normalized_row["bibliography_reason"] = normalized_row.get(
                "skip_reason"
            )
            normalized_row["skip_reason"] = "not_review_source"
        skipped_sources.append(normalized_row)
    skipped_sources.extend(non_review_skips)
    manifest_audit = {
        "sources_considered": int(plan["audit"]["sources_considered"]),
        "triggered_sources": int(plan["audit"]["triggered_sources"]),
        "review_confirmed_unpack_tasks": len(unpack_tasks),
        "skipped_sources": len(skipped_sources),
        "skipped_reasons": {
            reason: sum(
                1
                for row in skipped_sources
                if row.get("skip_reason") == reason
            )
            for reason in sorted({
                str(row.get("skip_reason") or "")
                for row in skipped_sources
                if row.get("skip_reason")
            })
        },
        "candidate_claims_indexed": index["claim_count"],
        "claims_without_paper_chunks": index[
            "claims_without_paper_chunks"
        ],
    }
    return {
        "schema_version": DOMINANT_REVIEW_TRIGGER_MANIFEST_VERSION,
        "blueprint_path": str(blueprint_path or ""),
        "section_index": section_index,
        "section_id": str(section.get("section_id") or ""),
        "trigger_rule": (
            "strictly_more_than_10_percent_candidate_claim_share"
        ),
        "threshold": SOURCE_EXPANSION_TRIGGER_THRESHOLD,
        "denominator_count": int(plan["denominator_count"]),
        "denominator_claim_ids": list(plan["denominator"]),
        "source_rows": list(plan["source_rows"]),
        "confirmed_review_paper_ids": sorted(confirmed_review_ids),
        "cached_document_reconstruction": {
            str(paper_id): dict(reconstruction)
            for paper_id, reconstruction in (
                document_reconstructions or {}
            ).items()
        },
        "review_confirmed_unpack_tasks": unpack_tasks,
        "skipped_sources": skipped_sources,
        "non_quota_policy": {
            "quota_class": EXPANSION_QUOTA_CLASS,
            "non_quota": True,
            "no_count_cap": True,
            "admission_cap": None,
            "ordinary_quota_decrement": 0,
            "s2_oa_section_supplementary_quota_consumed": False,
            "deduplication": (
                "first occurrence wins by DOI/arXiv/normalized title"
            ),
            "irrelevant_reference_screening": (
                "bounded screening batches; obviously irrelevant references "
                "may be skipped with an auditable reason"
            ),
        },
        "evidence_precedence": evidence_precedence_contract(),
        "audit": manifest_audit,
    }


def _unit_ordinal(unit: Mapping[str, Any]) -> Optional[int]:
    durable = unit.get("durable_content") or {}
    durable = durable if isinstance(durable, Mapping) else {}
    identity = unit.get("identity") or {}
    identity = identity if isinstance(identity, Mapping) else {}
    for candidate in (
        durable.get("ordinal"),
        identity.get("ordinal"),
        unit.get("ordinal"),
    ):
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _unit_raw_text(unit: Mapping[str, Any]) -> str:
    durable = unit.get("durable_content") or {}
    durable = durable if isinstance(durable, Mapping) else {}
    return str(
        durable.get("raw_text")
        or durable.get("normalized_text")
        or ""
    )


def reconstruct_cached_review_document(
    units: Iterable[Mapping[str, Any]],
    paper_id: str,
) -> dict[str, Any]:
    """Reconstruct one source document from fulltext material units.

    Only units whose identity.paper_id equals ``paper_id`` are used; foreign
    paper units are never concatenated and are audited.  Units are ordered by
    durable ordinal (numeric), with missing-ordinal units appended last in
    raw-text order.  Identical chunks (same content_hash or normalized raw
    text) are deduplicated first-wins.  Title and provenance come from the
    first kept unit; nothing is fabricated when the paper is missing.
    """
    paper_id = str(paper_id or "")
    paper_units: list[Mapping[str, Any]] = []
    foreign_unit_ids: list[str] = []
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        identity = unit.get("identity") or {}
        identity = identity if isinstance(identity, Mapping) else {}
        unit_paper_id = str(
            identity.get("paper_id") or unit.get("paper_id") or ""
        )
        unit_id = str(
            unit.get("unit_id")
            or identity.get("unit_id")
            or identity.get("chunk_id")
            or ""
        )
        if unit_paper_id and unit_paper_id != paper_id:
            if unit_id:
                foreign_unit_ids.append(unit_id)
            continue
        paper_units.append(unit)
    if not paper_units:
        return {
            "paper_id": paper_id,
            "found": False,
            "reason": "paper_id_not_in_cache",
            "body": "",
            "title": "",
            "unit_count": 0,
            "deduplicated_count": 0,
            "ordinal_missing_count": 0,
            "ordinal_order_complete": False,
            "body_chars": 0,
            "foreign_unit_count": len(foreign_unit_ids),
            "foreign_unit_ids": foreign_unit_ids,
            "section_paths": [],
            "provenance": {"paper_id": paper_id},
        }
    seen_content: set[str] = set()
    ordered: list[Mapping[str, Any]] = []
    ordinal_missing_count = 0
    for unit in sorted(
        paper_units,
        key=lambda item: (
            _unit_ordinal(item) is None,
            _unit_ordinal(item) if _unit_ordinal(item) is not None else 0,
            _unit_raw_text(item),
        ),
    ):
        if _unit_ordinal(unit) is None:
            ordinal_missing_count += 1
        raw_text = _unit_raw_text(unit)
        durable = unit.get("durable_content") or {}
        durable = durable if isinstance(durable, Mapping) else {}
        content_hash = str(durable.get("content_hash") or "")
        content_key = (
            f"hash:{content_hash}"
            if content_hash
            else f"text:{_clean_text(raw_text).casefold()}"
        )
        if content_key in seen_content:
            continue
        seen_content.add(content_key)
        ordered.append(unit)
    body = "\n".join(
        _unit_raw_text(unit) for unit in ordered
    ).strip()
    first = ordered[0]
    identity = first.get("identity") or {}
    identity = identity if isinstance(identity, Mapping) else {}
    title = str(identity.get("title") or "")
    section_paths = sorted({
        str((unit.get("durable_content") or {}).get("section_path") or "")
        for unit in ordered
        if str((unit.get("durable_content") or {}).get("section_path") or "")
    })
    return {
        "paper_id": paper_id,
        "found": True,
        "reason": None,
        "body": body,
        "title": title,
        "unit_count": len(paper_units),
        "deduplicated_count": len(paper_units) - len(ordered),
        "ordinal_missing_count": ordinal_missing_count,
        "ordinal_order_complete": ordinal_missing_count == 0,
        "body_chars": len(body),
        "foreign_unit_count": len(foreign_unit_ids),
        "foreign_unit_ids": foreign_unit_ids,
        "section_paths": section_paths,
        "provenance": {
            "paper_id": paper_id,
            "title": title,
            "first_unit_id": str(
                first.get("unit_id")
                or identity.get("unit_id")
                or identity.get("chunk_id")
                or ""
            ),
            "source": "durable_material_units_cache",
        },
    }


def build_cached_review_document_map(
    material_units_payload: Mapping[str, Any],
    paper_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Reconstruct one document per requested paper id from the cache payload."""
    units = material_units_payload.get("units") or []
    return {
        str(paper_id): reconstruct_cached_review_document(units, paper_id)
        for paper_id in paper_ids
    }


def confirm_dominant_review_source(
    source_id: str,
    metadata: Mapping[str, Any],
    *,
    confirmed_review_paper_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Explicit, auditable review-source confirmation.

    A caller-supplied approved review id always confirms.  Otherwise only
    trustworthy cached/S2 type metadata (publication types / source kind /
    review-like title signal) confirms; a long reference list alone never
    confirms a review.
    """
    source_id = str(source_id or "")
    confirmed_ids = {
        str(paper_id) for paper_id in (confirmed_review_paper_ids or ())
    }
    if source_id in confirmed_ids:
        return {
            "confirmed": True,
            "source": "caller_confirmed",
            "audit_note": (
                "User-approved review source supplied by the runner; no "
                "classification guess."
            ),
        }
    metadata = metadata or {}
    unit = {
        "identity": {"title": metadata.get("title") or ""},
        "work_type": (
            metadata.get("source_type") or metadata.get("work_type")
        ),
        "publication_type": metadata.get("publication_type"),
        "publicationTypes": metadata.get("publicationTypes"),
        "publication_types": metadata.get("publication_types"),
        "raw_metadata": metadata.get("raw_metadata") or {},
        "durable_content_card": {
            "content_quality": {
                "source_kind": metadata.get("source_kind") or "",
            }
        },
    }
    signal = review_source_signal(unit)
    if signal is not None:
        return {
            "confirmed": True,
            "source": "cached_or_s2_type_metadata",
            "signal": signal,
            "audit_note": (
                "Trustworthy type metadata indicates a review/information-rich "
                "source; reference-count alone was not used."
            ),
        }
    return {
        "confirmed": False,
        "source": "not_confirmed",
        "audit_note": (
            "Not confirmed as a review; a long bibliography alone is not a "
            "review signal."
        ),
    }
